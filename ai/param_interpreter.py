"""
自然语言参数解析模块（AI 增强核心）
==================================

职责：
1. 把用户的大白话需求（"转成 1080P 的 MP4，文件控制在 500M 以内"）通过大模型
   解析成结构化的 ffmpeg 转码参数；
2. 对模型输出做**字段级强校验**，任何一项不合法都回退默认值，杜绝脏参数进到 ffmpeg；
3. 提供三级容错降级链：LLM 解析 → 本地规则解析 → 通用 1080P 默认参数，
   保证没有 API Key、断网、模型返回垃圾时功能依然可用；
4. 提供 `recommend()`，根据源视频属性 + 目标格式做智能参数推荐（含硬件加速）。

本模块是纯逻辑层，不导入任何 Qt 代码。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from config.settings import (
    AUDIO_CODECS,
    OUTPUT_FORMATS,
    PRESETS,
    VIDEO_CODECS,
)
from core.ffmpeg_engine import (
    HardwareInfo,
    OutputParams,
    apply_hardware_acceleration,
    detect_hardware_acceleration,
)
from core.ffprobe_parser import VideoInfo
from utils.logger import get_logger

from ai.llm_client import LLMClient, LLMError

logger = get_logger("ai.interpreter")

# 静态提示：告诉用户哪些字段可以被识别
EXAMPLE_PROMPTS = [
    "转成 1080P 的 MP4，文件控制在 500M 以内，适合发朋友圈",
    "帮我压小一点，转成适合发微信的 MP4，越小越好",
    "转成抖音竖屏视频，30 帧，要清晰一点",
    "提取音频，保存成 MP3",
    "无损转换，只换容器不要重新编码",
    "把这段视频做成 GIF 动图，要流畅一点",
]

# ----------------------------------------------------------------------
# 一、严格约束输出的 System Prompt
# ----------------------------------------------------------------------

SYSTEM_PROMPT = """\
你是视频转码参数专家，负责把用户口语化的视频处理需求翻译成结构化的转码参数。

【输出要求】
1. 只输出一个 JSON 对象，不要输出 Markdown 代码块、不要输出任何解释性文字、不要输出多余字段。
2. 所有字段都必须出现，用户没有提到的字段填默认值，不要留空、不要编造。
3. JSON 结构固定为：
{
  "format": "MP4",
  "vcodec": "libx264",
  "resolution": "1920x1080",
  "fps": "原始帧率",
  "v_bitrate": "自动",
  "acodec": "aac",
  "a_bitrate": "192k",
  "extra_args": "",
  "explanation": "用一到两句中文解释你的选择"
}

【字段取值范围（严格白名单，超出范围即为非法）】
- format: 只能取 "MP4" | "MKV" | "AVI" | "MOV" | "WebM" | "MP3" | "GIF"，默认 "MP4"
- vcodec: 只能取 "copy" | "libx264" | "libx265" | "libvpx-vp9" | "mpeg4" | "prores" | "h264_nvenc" | "hevc_nvenc" | "h264_qsv" | "h264_amf"，默认 "libx264"
- resolution: 只能取 "原始分辨率" 或 "宽x高" 形式的字符串，例如 "1920x1080"；竖屏写 "1080x1920"；
  需要按宽度等比缩放时写 "480x-1"；默认 "原始分辨率"
- fps: 只能取 "原始帧率" 或整数字符串，例如 "30"；默认 "原始帧率"
- v_bitrate: 只能取 "自动" 或形如 "6000k" 的字符串；默认 "自动"
- a_bitrate: 只能取 "自动" 或形如 "192k" 的字符串；默认 "192k"
- acodec: 只能取 "copy" | "aac" | "libmp3lame" | "libopus" | "libvorbis" | "ac3" | "pcm_s16le"，默认 "aac"
- extra_args: 字符串，可为空字符串；只允许追加合法 ffmpeg 参数，例如 "-crf 18"、"-loop 0"、"-t 10"；
  禁止包含 "-i"、输入输出文件名或任何 shell 元字符
- explanation: 中文字符串，1~2 句话

【决策规则】
- 用户给出体积上限和目标时长时：推荐视频码率 ≈ 目标体积(MB) × 8192 ÷ 时长(秒) × 0.9，
  例如「500M、5 分钟」→ 约 1200k
- 「发朋友圈 / 发微信 / 压缩」→ MP4 + libx264 + 854x480 或 1280x720 + 码率不高于 1500k + AAC 64k~128k
- 「抖音 / 快手 / 竖屏 / 短视频」→ MP4 + libx264 + 1080x1920 + 30fps + 6000k + AAC 128k
- 「高清 / 无损 / 不压缩画质 / 只换格式」→ vcodec 与 acodec 都用 "copy"，resolution 用 "原始分辨率"
- 「只要音频 / 提取音乐 / 背景音乐」→ format "MP3" + acodec "libmp3lame" + a_bitrate "192k"
- 「动图 / GIF」→ format "GIF" + fps "12" + resolution "480x-1" + extra_args "-loop 0"
- 「体积越小越好 / 越小越好」→ 优先 libx265，其次降低分辨率到 854x480
- 「4K/2K/2160p」→ resolution 分别取 "3840x2160" / "2560x1440"

【示例】
用户：转成 1080P 的 MP4，文件控制在 500M 以内，适合发朋友圈（原视频 5 分钟）
输出：{"format":"MP4","vcodec":"libx264","resolution":"1920x1080","fps":"原始帧率","v_bitrate":"12000k","acodec":"aac","a_bitrate":"128k","extra_args":"","explanation":"按 500MB 体积上限与 5 分钟时长推算码率约 12000k，1080P + AAC 128k 适合社交平台分享。"}

用户：转成抖音竖屏视频，要清晰一点
输出：{"format":"MP4","vcodec":"libx264","resolution":"1080x1920","fps":"30","v_bitrate":"6000k","acodec":"aac","a_bitrate":"128k","extra_args":"","explanation":"抖音竖屏标准为 1080x1920、30fps，6Mbps 码率可保证清晰度。"}

用户：把视频里的背景音乐提取出来
输出：{"format":"MP3","vcodec":"copy","resolution":"原始分辨率","fps":"原始帧率","v_bitrate":"自动","acodec":"libmp3lame","a_bitrate":"192k","extra_args":"","explanation":"仅保留音轨并转成 192kbps 的 MP3。"}
"""


# ----------------------------------------------------------------------
# 二、结果模型
# ----------------------------------------------------------------------

@dataclass
class InterpretResult:
    """一次自然语言解析的结果。"""

    ok: bool                                   # 是否成功获得 LLM 解析结果
    params: OutputParams                       # 永远可直接使用的参数对象
    explanation: str = ""                      # 面向用户的中文说明
    fallback: bool = False                     # 是否走了降级链
    source: str = "llm"                        # llm / rule / default
    raw_response: str = ""                     # 模型原始返回（排障用）
    warnings: List[str] = field(default_factory=list)

    @property
    def source_label(self) -> str:
        return {"llm": "AI 解析", "rule": "本地规则解析", "default": "默认参数"}.get(self.source, self.source)


# ----------------------------------------------------------------------
# 三、解析工具
# ----------------------------------------------------------------------

def extract_json(text: Any) -> Optional[Dict[str, Any]]:
    """
    从模型返回文本中稳健地提取 JSON 对象：
    兼容 Markdown 代码围栏、前后夹杂解释文字等常见“不听话”情况。
    """
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    if not candidate:
        return None

    # 去掉 ```json ... ``` 代码块围栏
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            parsed = json.loads(candidate[start:end + 1])
        except json.JSONDecodeError:
            return None

    return parsed if isinstance(parsed, dict) else None


_FORMAT_PATTERN = re.compile(r"^\d{2,5}x(\d{2,5}|-1)$")
_FPS_PATTERN = re.compile(r"^\d{1,3}$")
_BITRATE_PATTERN = re.compile(r"^\d{1,6}k$")


def coerce_params(data: Optional[Dict[str, Any]]) -> Tuple[OutputParams, List[str]]:
    """
    对模型返回的字段做**逐项强校验**，任何非法值都回退为默认值并记录原因。
    返回值中的 params 一定是完全合法的、可直接交给 ffmpeg 使用的参数。
    """
    params = OutputParams()
    warnings: List[str] = []
    if not isinstance(data, dict):
        warnings.append("模型返回内容不是 JSON 对象，已全部使用默认参数")
        return params, warnings

    def _invalid(key: str, value: Any, reason: str) -> None:
        warnings.append(f"{key} 取值非法（{value}，{reason}），已回退默认值 {getattr(params, key)}")

    # format
    value = str(data.get("format", "") or "").strip()
    if value:
        matched = next((key for key in OUTPUT_FORMATS if key.lower() == value.lower()), None)
        if matched:
            params.format = matched
        else:
            _invalid("format", value, "不在支持的格式列表内")

    # vcodec
    value = str(data.get("vcodec", "") or "").strip()
    if value:
        if value in VIDEO_CODECS:
            params.vcodec = value
        else:
            _invalid("vcodec", value, "不在支持的编码器列表内")

    # resolution
    value = str(data.get("resolution", "") or "").strip()
    if value:
        if value == "原始分辨率" or _FORMAT_PATTERN.match(value):
            params.resolution = value
        else:
            _invalid("resolution", value, "格式应为 原始分辨率 或 宽x高")

    # fps
    value = str(data.get("fps", "") or "").strip()
    if value:
        if value == "原始帧率":
            params.fps = value
        elif _FPS_PATTERN.match(value) and 1 <= int(value) <= 240:
            params.fps = value
        else:
            _invalid("fps", value, "应为 原始帧率 或 1~240 的整数")

    # 视频码率
    value = str(data.get("v_bitrate", "") or "").strip()
    if value:
        if value == "自动" or _BITRATE_PATTERN.match(value):
            params.v_bitrate = value
        else:
            _invalid("v_bitrate", value, "应为 自动 或形如 6000k")

    # 音频码率
    value = str(data.get("a_bitrate", "") or "").strip()
    if value:
        if value == "自动" or _BITRATE_PATTERN.match(value):
            params.a_bitrate = value
        else:
            _invalid("a_bitrate", value, "应为 自动 或形如 192k")

    # 音频编码
    value = str(data.get("acodec", "") or "").strip()
    if value:
        if value in AUDIO_CODECS:
            params.acodec = value
        else:
            _invalid("acodec", value, "不在支持的音频编码列表内")

    # 附加参数：做安全清洗，剔除危险内容
    value = str(data.get("extra_args", "") or "").strip()
    if value:
        cleaned = _sanitize_extra_args(value, warnings)
        params.extra_args = cleaned

    # 保持与目标格式的一致性（例如选了 MP3 却给了视频编码）
    profile = OUTPUT_FORMATS.get(params.format, OUTPUT_FORMATS["MP4"])
    if profile.get("audio_only"):
        params.vcodec = "copy"
        params.resolution = "原始分辨率"
        params.fps = "原始帧率"
        params.v_bitrate = "自动"
        # 模型没给音频编码器（或给了 copy）时，直接套用该容器的默认音频编码
        if not str(data.get("acodec", "") or "").strip() or params.acodec == "copy":
            params.acodec = profile.get("acodec") or "libmp3lame"
    elif profile.get("gif"):
        params.acodec = "copy"
        params.v_bitrate = "自动"
        if params.fps == "原始帧率":
            params.fps = "12"
        if params.resolution == "原始分辨率":
            params.resolution = "480x-1"
        if not params.extra_args:
            params.extra_args = "-loop 0"

    return params, warnings


_MEDIA_OR_IMAGE_EXT = re.compile(
    r"\.(mp4|mkv|avi|mov|webm|flv|wmv|m4v|ts|gif|mp3|aac|wav|flac|m4a|ogg|jpg|jpeg|png|bmp)$",
    re.IGNORECASE,
)


def _sanitize_extra_args(text: str, warnings: List[str]) -> str:
    """清洗附加参数：禁止出现 -i（输入）、输入输出文件名、路径与 shell 元字符。"""
    tokens = text.replace("|", " ").replace(";", " ").replace("&", " ").split()
    safe: List[str] = []
    blocked = False
    for token in tokens:
        lowered = token.lower()
        if lowered in ("-i", "-y", "-hide_banner", "-nostats") or lowered.startswith("-progress"):
            blocked = True
            continue
        if any(char in token for char in (";", "|", "&", ">", "<", "`", "$")):
            blocked = True
            continue
        # 形似文件名或路径的参数（如 out.mp4、D:/tmp/a.mkv）一律剔除
        if _MEDIA_OR_IMAGE_EXT.search(token) or "/" in token or "\\" in token:
            blocked = True
            continue
        safe.append(token)
    if blocked:
        warnings.append("附加参数中包含不安全内容，已自动剔除")
    return " ".join(safe)


def _explanation_from_params(params: OutputParams) -> str:
    """模型没给 explanation 时，自动生成一句中文说明。"""
    return f"已根据你的描述生成参数：{params.describe()}"


# ----------------------------------------------------------------------
# 四、本地规则解析（离线兜底能力）
# ----------------------------------------------------------------------

AUDIO_ONLY_KEYWORDS = ("只要音频", "纯音频", "提取音", "提取音乐", "背景音乐", "去掉画面", "只要声音", "提取声音", "音频提取")
LOSSLESS_KEYWORDS = ("无损", "不重新编码", "不压缩画质", "保持原画质", "流复制", "只换容器", "只换格式")
SMALL_KEYWORDS = ("越小越好", "尽量小", "压小", "体积小", "压缩")
HW_KEYWORDS = ("硬件加速", "显卡加速", "nvenc", "qsv", "显卡编码", "gpu加速", "硬编")

_RESOLUTION_KEYWORDS = [
    (re.compile(r"4320p|8k", re.IGNORECASE), "7680x4320"),
    (re.compile(r"2160p|4k", re.IGNORECASE), "3840x2160"),
    (re.compile(r"1440p|2k", re.IGNORECASE), "2560x1440"),
    (re.compile(r"1080p|全高清|fhd", re.IGNORECASE), "1920x1080"),
    (re.compile(r"720p|高清", re.IGNORECASE), "1280x720"),
    (re.compile(r"480p|标清", re.IGNORECASE), "854x480"),
    (re.compile(r"360p", re.IGNORECASE), "640x360"),
]

_SIZE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*(gb|g|mb|m|兆)(?![a-z0-9])", re.IGNORECASE)
_BITRATE_MBPS_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*mbps", re.IGNORECASE)
_BITRATE_K_PATTERN = re.compile(r"(\d+)\s*k(?:bps)?(?![a-z0-9])", re.IGNORECASE)
_FPS_PATTERN_TEXT = re.compile(r"(\d{1,3})\s*(?:fps|帧)", re.IGNORECASE)
_DURATION_PATTERNS = [
    (re.compile(r"(\d+)\s*小时\s*(\d+)\s*分"), lambda m: int(m.group(1)) * 3600 + int(m.group(2)) * 60),
    (re.compile(r"(\d+)\s*小时"), lambda m: int(m.group(1)) * 3600),
    (re.compile(r"(\d+)\s*分(?:钟)?\s*(\d+)\s*秒"), lambda m: int(m.group(1)) * 60 + int(m.group(2))),
    (re.compile(r"(\d+)\s*分(?:钟)?"), lambda m: int(m.group(1)) * 60),
    (re.compile(r"(\d+)\s*秒"), lambda m: int(m.group(1))),
    (re.compile(r"(\d+)\s*s(?![a-z0-9])", re.IGNORECASE), lambda m: int(m.group(1))),
]


def _detect_format(text: str, raw: str) -> str:
    """从文本中识别目标容器格式，默认 MP4。"""
    if re.search(r"gif|动图", raw, re.IGNORECASE):
        return "GIF"
    if re.search(r"mp3|只要音频|纯音频|提取音|背景音乐|音频提取", raw, re.IGNORECASE):
        return "MP3"
    if re.search(r"webm|vp9", text):
        return "WebM"
    if re.search(r"\bmkv\b", text):
        return "MKV"
    if re.search(r"\bavi\b", text):
        return "AVI"
    if re.search(r"\bmov\b", text):
        return "MOV"
    return "MP4"


def _detect_resolution(raw: str) -> Optional[str]:
    """识别分辨率：优先显式「宽x高」，其次档位关键词 / 竖屏 / 方形。"""
    explicit = re.search(r"(\d{3,4})\s*[xX*×]\s*(\d{3,4})", raw)
    if explicit:
        return f"{explicit.group(1)}x{explicit.group(2)}"

    if re.search(r"竖屏|竖版|抖音|快手|视频号|小红书", raw):
        return "1080x1920"
    if re.search(r"方形|正方形|方屏", raw):
        return "1080x1080"

    for pattern, resolution in _RESOLUTION_KEYWORDS:
        if pattern.search(raw):
            return resolution
    return None


def _detect_fps(raw: str) -> Optional[str]:
    match = _FPS_PATTERN_TEXT.search(raw)
    if not match:
        return None
    value = int(match.group(1))
    return str(value) if 1 <= value <= 240 else None


def _detect_size_mb(raw: str) -> Optional[float]:
    """识别体积上限（统一换算成 MB）。"""
    match = _SIZE_PATTERN.search(raw)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return value * 1024 if unit in ("gb", "g") else value


def _detect_duration_seconds(raw: str) -> Optional[float]:
    for pattern, extractor in _DURATION_PATTERNS:
        match = pattern.search(raw)
        if match:
            return float(extractor(match))
    return None


def _detect_video_bitrate(raw: str) -> Optional[str]:
    match = _BITRATE_MBPS_PATTERN.search(raw)
    if match:
        return f"{int(float(match.group(1)) * 1000)}k"
    match = _BITRATE_K_PATTERN.search(raw)
    if match:
        return f"{int(match.group(1))}k"
    return None


def _detect_audio_bitrate(raw: str) -> Optional[str]:
    """音频码率：只认作为音频上下文出现的 kbps 值。"""
    match = re.search(r"(?:音频|声音|音质)[^0-9]{0,6}(\d+)\s*k", raw, re.IGNORECASE)
    return f"{int(match.group(1))}k" if match else None


def _detect_video_codec(text: str) -> Optional[str]:
    """识别视频编码器。"""
    if re.search(r"h\.?\s?265|hevc", text):
        return "libx265"
    if re.search(r"h\.?\s?264|avc", text):
        return "libx264"
    if re.search(r"vp9", text):
        return "libvpx-vp9"
    return None


def rule_based_parse(text: str) -> Tuple[OutputParams, List[str]]:
    """
    本地正则规则解析：不依赖网络与 API Key，作为 AI 解析失败时的第一降级方案，
    同时保证在完全离线环境下「自然语言转参数」依然可用。
    """
    raw = text or ""
    text_lower = raw.lower()
    params = OutputParams()
    warnings: List[str] = []

    # 1) 目标格式与纯音频 / GIF 特例
    format_key = _detect_format(text_lower, raw)
    audio_only = format_key == "MP3" or any(key in raw for key in AUDIO_ONLY_KEYWORDS)
    is_gif = format_key == "GIF"
    params.format = "GIF" if is_gif else ("MP3" if audio_only else format_key)

    if audio_only:
        params.vcodec = "copy"
        params.acodec = "libmp3lame"
        params.a_bitrate = "192k"

    if is_gif:
        params.fps = "12"
        params.resolution = "480x-1"
        params.acodec = "copy"
        params.extra_args = "-loop 0"

    # 2) 分辨率与帧率
    if not audio_only:
        resolution = _detect_resolution(raw)
        if resolution:
            params.resolution = resolution
        if not is_gif:
            fps = _detect_fps(raw)
            if fps:
                params.fps = fps

    # 3) 体积上限 + 时长 → 推算码率
    if not audio_only and not is_gif:
        size_mb = _detect_size_mb(raw)
        duration = _detect_duration_seconds(raw)
        if size_mb and duration and duration > 0:
            kbps = size_mb * 8192 / duration * 0.9
            kbps = max(100, min(30000, int(round(kbps / 100.0) * 100)))
            params.v_bitrate = f"{kbps}k"
            warnings.append(
                f"按体积上限 {size_mb:g}MB / 时长 {duration:g} 秒推算视频码率约 {kbps}k"
            )
        # 4) 显式码率优先级更高
        explicit_bitrate = _detect_video_bitrate(raw)
        if explicit_bitrate:
            params.v_bitrate = explicit_bitrate

        # 5) 编码器
        codec = _detect_video_codec(text_lower)
        if codec:
            params.vcodec = codec

    # 6) 音频编码与码率
    if not audio_only:
        if re.search(r"mp3", text_lower):
            params.acodec = "libmp3lame"
        elif re.search(r"opus", text_lower):
            params.acodec = "libopus"
        audio_bitrate = _detect_audio_bitrate(raw)
        if audio_bitrate:
            params.a_bitrate = audio_bitrate

    # 7) 无损 / 只换容器
    if not audio_only and not is_gif and any(key in raw for key in LOSSLESS_KEYWORDS):
        params.vcodec = "copy"
        params.acodec = "copy"
        params.v_bitrate = "自动"
        params.resolution = "原始分辨率"
        params.fps = "原始帧率"
        warnings.append("识别到「无损 / 不重新编码」需求，改为直接复制原始音视频流")

    # 8) 越小越好
    if not audio_only and not is_gif and any(key in raw.lower() for key in SMALL_KEYWORDS):
        if params.vcodec not in ("copy",):
            params.vcodec = "libx265"
        params.resolution = params.resolution if params.resolution != "原始分辨率" else "854x480"
        params.v_bitrate = params.v_bitrate if params.v_bitrate != "自动" else "800k"
        warnings.append("识别到「体积优先」需求，已改用 H.265 + 低码率方案")

    # 9) 硬件加速
    if any(key in text_lower for key in HW_KEYWORDS):
        params.hw_accel = True

    return params, warnings


# ----------------------------------------------------------------------
# 五、解析器主体
# ----------------------------------------------------------------------

class ParamInterpreter:
    """自然语言 → ffmpeg 参数 的解释器。"""

    def __init__(self, client: Optional[LLMClient] = None) -> None:
        self._client = client

    @property
    def client(self) -> LLMClient:
        """延迟创建客户端，避免程序启动时就发起网络依赖。"""
        if self._client is None:
            self._client = LLMClient()
        return self._client

    @property
    def llm_ready(self) -> bool:
        return self.client.configured

    # ---------------- 自然语言解析 ----------------
    def interpret(self, text: str, video_info: Optional[VideoInfo] = None) -> InterpretResult:
        """
        解析用户自然语言需求，返回可直接使用的转码参数。

        降级链：LLM 解析 → 本地规则解析 → 通用 1080P 默认参数。
        本方法**不会抛出异常**。
        """
        description = (text or "").strip()
        if not description:
            fallback = self._default_result("输入内容为空，已使用通用 1080P 默认参数。")
            return fallback

        # ---------- 第一级：大模型解析 ----------
        try:
            raw_reply = self.client.chat(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": self._build_user_content(description, video_info)},
                ],
                temperature=0.1,
                max_tokens=800,
                response_format_json=True,
            )
        except LLMError as exc:
            logger.warning("大模型解析失败，进入降级链：%s", exc)
            return self._degrade(description, str(exc), "")
        except Exception as exc:  # noqa: BLE001  任何未知异常都不能影响主流程
            logger.exception("大模型解析出现未知异常")
            return self._degrade(description, f"未知错误：{exc}", "")

        parsed = extract_json(raw_reply)
        if parsed is None:
            logger.warning("模型返回内容无法解析为 JSON：%s", raw_reply[:300])
            return self._degrade(description, "大模型返回内容不是合法 JSON", raw_reply)

        params, warnings = coerce_params(parsed)
        explanation = str(parsed.get("explanation") or "").strip() or _explanation_from_params(params)
        logger.info("AI 解析成功：%s → %s", description[:40], params.describe())
        return InterpretResult(
            ok=True,
            params=params,
            explanation=explanation,
            fallback=bool(warnings),
            source="llm",
            raw_response=raw_reply,
            warnings=warnings,
        )

    @staticmethod
    def _build_user_content(description: str, video_info: Optional[VideoInfo]) -> str:
        """拼装用户消息：把源视频属性一并交给模型做决策依据。"""
        parts = [f"用户需求：{description}"]
        if video_info is not None:
            parts.append(f"源视频信息：{video_info.probe_prompt()}")
            parts.append("请结合源视频信息给出最合理的参数。")
        else:
            parts.append("当前没有加载源视频，请按用户需求直接给出参数。")
        return "\n".join(parts)

    # ---------------- 降级链 ----------------
    def _degrade(self, description: str, reason: str, raw_reply: str) -> InterpretResult:
        """LLM 失败后的降级处理：先规则，再默认。"""
        rule_params, rule_warnings = rule_based_parse(description)
        hit = self._rule_hit(rule_params)
        if hit:
            explanation = (
                f"AI 解析失败（{reason}），已根据你的描述用本地规则生成参数：{rule_params.describe()}。"
                "请确认无误后再开始转换。"
            )
            warnings = [f"降级原因：{reason}"] + rule_warnings
            return InterpretResult(
                ok=False, params=rule_params, explanation=explanation,
                fallback=True, source="rule", raw_response=raw_reply, warnings=warnings,
            )

        params = OutputParams.from_dict(PRESETS["通用 1080P"]["params"])
        explanation = (
            f"AI 解析失败（{reason}），描述中也未识别到可用的转码意图，"
            "已回退为「通用 1080P」默认参数，请手动调整后再开始转换。"
        )
        return InterpretResult(
            ok=False, params=params, explanation=explanation,
            fallback=True, source="default", raw_response=raw_reply,
            warnings=[f"降级原因：{reason}"],
        )

    @staticmethod
    def _rule_hit(params: OutputParams) -> bool:
        """
        判断规则解析是否真的命中了用户意图。
        规则结果与全默认参数完全一致时，说明描述里没有任何可识别信息，
        此时走「通用 1080P 默认参数」分支并给出更明确的提示。
        """
        return params.to_dict() != OutputParams().to_dict()

    def _default_result(self, message: str) -> InterpretResult:
        params = OutputParams.from_dict(PRESETS["通用 1080P"]["params"])
        return InterpretResult(
            ok=False, params=params, explanation=message,
            fallback=True, source="default",
        )

    # ---------------- 智能参数推荐 ----------------
    def recommend(
        self,
        video_info: VideoInfo,
        target_format: Optional[str] = None,
        use_llm: bool = False,
    ) -> InterpretResult:
        """
        根据源视频属性 + 目标格式推荐最优参数组合（纯本地规则，保证离线可用）。
        :param use_llm: 是否额外让大模型对推荐结果做一次复核（失败则直接用规则结果）
        """
        params = OutputParams()
        warnings: List[str] = []

        format_key = target_format if target_format in OUTPUT_FORMATS else "MP4"
        profile = OUTPUT_FORMATS[format_key]
        params.format = format_key

        # 音频专用格式：只关心音频参数
        if profile.get("audio_only"):
            params.vcodec = "copy"
            params.acodec = profile.get("acodec") or "libmp3lame"
            params.a_bitrate = "192k" if video_info.sample_rate >= 44100 else "128k"
            explanation = f"源文件为 {video_info.audio_codec or '未知音频'}，提取为 {params.a_bitrate} 的 MP3。"
            return InterpretResult(ok=True, params=params, explanation=explanation, source="rule", warnings=warnings)

        # GIF：固定为轻量参数
        if profile.get("gif"):
            params.fps = "12"
            params.resolution = "480x-1"
            params.acodec = "copy"
            params.extra_args = "-loop 0"
            explanation = "GIF 体积敏感，推荐 12fps + 宽 480px 并启用调色板优化。"
            return InterpretResult(ok=True, params=params, explanation=explanation, source="rule", warnings=warnings)

        # ---------- 分辨率 ----------
        short_side = min(video_info.width, video_info.height) if video_info.width and video_info.height else 0
        if short_side >= 2160:
            resolution, bitrate = "1920x1080", "8000k"
        elif short_side >= 1080:
            resolution, bitrate = "1920x1080", "6000k"
        elif short_side >= 720:
            resolution, bitrate = "1280x720", "3000k"
        elif short_side > 0 and short_side < 480:
            resolution, bitrate = "原始分辨率", "1200k"
        else:
            resolution, bitrate = "854x480", "1500k"

        # 源分辨率比目标更低时不放大，避免无意义体积膨胀
        if video_info.width and video_info.height and short_side and short_side < 720:
            resolution = "原始分辨率"

        params.resolution = resolution
        params.v_bitrate = bitrate

        # ---------- 帧率 ----------
        if video_info.fps > 45:
            params.fps = "30"
            warnings.append(f"源帧率 {video_info.fps:.0f}fps 较高，降为 30fps 以减小体积")
        else:
            params.fps = "原始帧率"

        # ---------- 音频 ----------
        params.acodec = profile.get("acodec") or "aac"
        params.a_bitrate = "192k" if (video_info.bitrate > 2_000_000 or not video_info.has_audio) else "128k"
        if video_info.has_audio and video_info.sample_rate and video_info.sample_rate < 44100:
            params.a_bitrate = "128k"

        # ---------- 编码器 ----------
        params.vcodec = profile.get("vcodec") or "libx264"

        # ---------- 硬件加速 ----------
        hardware: Optional[HardwareInfo] = None
        try:
            hardware = detect_hardware_acceleration()
        except Exception:  # noqa: BLE001  探测失败不影响推荐
            hardware = None
        hardware_note = ""
        if hardware is not None and hardware.available:
            params = apply_hardware_acceleration(params, hardware)
            hardware_note = f"，并自动启用 {hardware.label} 硬件加速"

        explanation = (
            f"源视频为 {video_info.resolution_label} / {video_info.fps_str}，"
            f"推荐 {params.format} + {params.vcodec} + {params.resolution} + "
            f"{params.fps if params.fps != '原始帧率' else '原始帧率'} + {params.v_bitrate}"
            f"{hardware_note}。"
        )

        result = InterpretResult(
            ok=True, params=params, explanation=explanation,
            source="rule", warnings=warnings,
        )
        if not use_llm:
            return result

        # 可选的二次复核：让大模型确认或微调
        try:
            review = self.interpret(
                f"源视频信息：{video_info.probe_prompt()}。请以 {params.describe()} 为基础给出最终参数。",
                video_info,
            )
            if review.ok:
                review.warnings = warnings + review.warnings
                review.explanation = f"AI 复核后推荐：{review.params.describe()}。{review.explanation}"
                return review
            result.warnings.append("AI 复核失败，已采用本地规则推荐结果")
            return result
        except Exception:  # noqa: BLE001
            result.warnings.append("AI 复核异常，已采用本地规则推荐结果")
            return result


# 便捷函数：给 UI 层直接调用
def interpret_text(text: str, video_info: Optional[VideoInfo] = None,
                   client: Optional[LLMClient] = None) -> InterpretResult:
    """一行调用自然语言解析。"""
    return ParamInterpreter(client).interpret(text, video_info)
