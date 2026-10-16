"""
视频转换核心模块（subprocess 调用 ffmpeg + QThread 异步执行）
==========================================================

职责：
1. 把界面上的参数模型 `OutputParams` 翻译成一条完整的 ffmpeg 命令行（纯函数，可单元测试）；
2. 用 `FFmpegWorker`（QThread）异步执行转码，实时解析 `-progress` 协议输出，
   向前端推送「进度百分比 / 处理速度 / 剩余时间」；
3. 用 `ConversionManager` 管理任务队列，支持并发度控制、中途取消、批量执行；
4. 自动探测显卡硬件编码能力（NVIDIA NVENC / Intel QSV / AMD AMF）；
5. 把 ffmpeg 的技术报错翻译成普通用户看得懂的提示。

设计约束：
- 全程使用 subprocess 调用系统 ffmpeg 命令行，**未使用任何 ffmpeg 封装库**；
- Worker 线程内部捕获所有异常，绝不允许未捕获异常导致整个 GUI 进程退出；
- UI 与核心通过 Qt 信号槽通信，核心层不依赖任何界面控件。
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, QThread, Signal

from config.settings import (
    CREATE_NO_WINDOW,
    HARDWARE_ENCODERS,
    OUTPUT_FORMATS,
    SOFT_TO_HARD_ENCODER,
    get_ffmpeg_path,
    settings,
)
from utils.logger import format_command, get_logger, log_command

logger = get_logger("core.ffmpeg")

# 判定「疑似错误」输出行的关键字
_ERROR_PATTERN = re.compile(
    r"(error|invalid|failed|failure|permission denied|no such file|not found|"
    r"no space|unknown encoder|unsupported|cannot|unable|conversion failed|"
    r"moov atom|divisible by 2|broken pipe|resource temporarily unavailable)",
    re.IGNORECASE,
)


# ======================================================================
# 一、参数模型与命令构建（纯函数，便于测试）
# ======================================================================

@dataclass
class OutputParams:
    """一次转码任务的全部输出参数（与界面参数面板一一对应）。"""

    format: str = "MP4"                 # OUTPUT_FORMATS 的键
    vcodec: str = "libx264"             # 视频编码器
    resolution: str = "原始分辨率"        # "原始分辨率" / "1920x1080" / "480x-1"
    fps: str = "原始帧率"                # "原始帧率" / "30"
    v_bitrate: str = "自动"              # "自动" / "6000k"
    acodec: str = "aac"                 # 音频编码器
    a_bitrate: str = "192k"             # "自动" / "192k"
    hw_accel: bool = False              # 是否启用硬件加速
    extra_args: str = ""                # 附加 ffmpeg 参数

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": self.format,
            "vcodec": self.vcodec,
            "resolution": self.resolution,
            "fps": self.fps,
            "v_bitrate": self.v_bitrate,
            "acodec": self.acodec,
            "a_bitrate": self.a_bitrate,
            "hw_accel": self.hw_accel,
            "extra_args": self.extra_args,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "OutputParams":
        """从字典构建参数对象：未知键忽略、缺失键用默认值，并做类型兜底，保证永不抛异常。"""
        params = cls()
        if not isinstance(data, dict):
            return params
        for key in params.to_dict():
            if key not in data or data[key] is None:
                continue
            value = data[key]
            if key == "hw_accel":
                if isinstance(value, str):
                    value = value.strip().lower() in ("1", "true", "yes", "on", "是")
                else:
                    value = bool(value)
            else:
                value = str(value).strip()
                if not value:
                    continue
            setattr(params, key, value)
        return params

    def copy(self) -> "OutputParams":
        return OutputParams.from_dict(self.to_dict())

    def describe(self) -> str:
        """生成一句中文参数摘要，用于界面展示与日志留痕。"""
        profile = OUTPUT_FORMATS.get(self.format, OUTPUT_FORMATS["MP4"])
        if profile.get("gif"):
            return f"GIF 动图 / {self.fps} / 宽 {self.resolution} / {self.extra_args or '默认循环'}"
        if profile.get("audio_only"):
            return f"{self.format} 纯音频 / {self.acodec} / {self.a_bitrate}"
        video_part = f"{self.format} / {self.vcodec}"
        if self.resolution != "原始分辨率":
            video_part += f" / {self.resolution}"
        if self.fps != "原始帧率":
            video_part += f" / {self.fps}fps"
        bitrate_part = "" if self.v_bitrate == "自动" else f" / {self.v_bitrate}"
        audio_part = "" if not self.acodec else f" / {self.acodec} {self.a_bitrate}"
        hw_part = " / 硬件加速" if self.hw_accel else ""
        return video_part + bitrate_part + audio_part + hw_part


def _build_scale_filter(resolution: str) -> Optional[str]:
    """
    把界面上的分辨率字符串翻译成 ffmpeg 的 -vf scale 滤镜。

    - "原始分辨率"    → 不加滤镜
    - "480x-1"       → scale=480:-1（等比缩放到宽 480）
    - "1920x1080"    → scale + pad（保持原始宽高比、不足处补黑边，避免画面拉伸变形）
    - "1080"         → scale=-2:1080（按高度等比缩放）
    """
    value = (resolution or "").strip().lower()
    if not value or value == "原始分辨率":
        return None

    # 宽度固定、高度自适应的写法
    match_flexible = re.fullmatch(r"(\d+)\s*x\s*(-1)", value)
    if match_flexible:
        return f"scale={match_flexible.group(1)}:-1"

    match_fixed = re.fullmatch(r"(\d+)\s*x\s*(\d+)", value)
    if match_fixed:
        width, height = match_fixed.group(1), match_fixed.group(2)
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        )

    if value.isdigit():
        return f"scale=-2:{value}"

    return None


def _split_extra_args(extra_args: str) -> List[str]:
    """拆分用户附加参数（容错：引号不配对时退化为空格切分，绝不抛异常）。"""
    text = (extra_args or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        logger.warning("附加参数格式异常，已按空格切分：%s", text)
        return text.split()


def build_ffmpeg_command(
    params: OutputParams,
    input_path: str,
    output_path: str,
    has_audio: bool = True,
) -> List[str]:
    """
    构建完整的 ffmpeg 命令行参数列表。

    返回的列表最后一个元素一定是输出文件路径，便于直接交给 subprocess 执行。
    """
    profile = OUTPUT_FORMATS.get(params.format) or OUTPUT_FORMATS["MP4"]
    ffmpeg = get_ffmpeg_path() or "ffmpeg"

    command: List[str] = [ffmpeg, "-hide_banner", "-y"]

    # 硬件解码加速：仅在启用硬件加速且使用硬件编码器时追加
    if params.hw_accel and params.vcodec in HARDWARE_ENCODERS:
        command += ["-hwaccel", "auto"]

    command += ["-i", input_path]
    extra = _split_extra_args(params.extra_args)
    tail = ["-nostats", "-progress", "pipe:1"]

    # ---------------- 分支一：GIF 动图（两遍调色板算法，画质更好） ----------------
    if profile.get("gif"):
        fps = params.fps if params.fps != "原始帧率" else "12"
        width = str(params.resolution or "480")
        width = width.split("x")[0].strip()
        if not width.isdigit():
            width = "480"
        filter_complex = (
            f"fps={fps},scale={width}:-1:flags=lanczos,"
            f"split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        )
        command += ["-filter_complex", filter_complex, "-an"]
        command += extra + tail + [output_path]
        return command

    # ---------------- 分支二：纯音频格式（如 MP3） ----------------
    if profile.get("audio_only"):
        command += ["-vn", "-c:a", params.acodec or profile.get("acodec") or "libmp3lame"]
        if params.a_bitrate != "自动" and params.acodec != "copy":
            command += ["-b:a", params.a_bitrate]
        command += extra + tail + [output_path]
        return command

    # ---------------- 分支三：常规视频转码 ----------------
    video_copy = params.vcodec == "copy"
    if video_copy:
        command += ["-c:v", "copy"]
    else:
        command += ["-c:v", params.vcodec]
        if params.v_bitrate != "自动":
            command += ["-b:v", params.v_bitrate]
        elif params.vcodec in ("libx264", "libx265"):
            # 未指定码率时用恒定质量模式，兼顾体积与观感
            command += ["-crf", "23"]

    # 流复制模式下不能叠加 scale / 帧率 等重编码滤镜
    if not video_copy:
        scale_filter = _build_scale_filter(params.resolution)
        if scale_filter:
            command += ["-vf", scale_filter]
        if params.fps != "原始帧率":
            command += ["-r", params.fps]

    # 音频处理
    if has_audio:
        acodec = params.acodec or profile.get("acodec") or "aac"
        command += ["-c:a", acodec]
        if params.a_bitrate != "自动" and acodec != "copy":
            command += ["-b:a", params.a_bitrate]
    else:
        command += ["-an"]

    # MP4 / MOV 需要把索引前置，便于网页与社交平台秒开
    if profile.get("container") in ("mp4", "mov"):
        command += ["-movflags", "+faststart"]

    command += extra + tail + [output_path]
    return command


def build_output_path(
    input_path: str,
    output_dir: str,
    format_key: str,
    suffix: str = "",
) -> str:
    """按「原文件名 + 新扩展名」生成输出路径，同名文件自动追加序号避免覆盖。"""
    profile = OUTPUT_FORMATS.get(format_key) or OUTPUT_FORMATS["MP4"]
    stem = Path(input_path).stem or "output"
    directory = Path(output_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("输出目录创建失败：%s (%s)", directory, exc)

    extension = profile["ext"]
    candidate = directory / f"{stem}{suffix}{extension}"
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem}{suffix}({index}){extension}"
        index += 1
        if index > 9999:  # 极端保护，避免死循环
            break
    return str(candidate)


# ======================================================================
# 二、错误人性化翻译
# ======================================================================

_ERROR_TRANSLATIONS: List[tuple] = [
    (r"no such file or directory",
     "【文件不存在】找不到输入文件，可能已被移动或删除，请重新添加。"),
    (r"permission denied",
     "【文件被占用】没有权限读写文件。常见原因：视频正在播放器中播放，或输出目录被其他程序占用，"
     "请关闭相关程序后重试。"),
    (r"moov atom not found|invalid data found when processing input",
     "【文件损坏】这不是有效的媒体文件，或文件已损坏/下载不完整，请更换文件后重试。"),
    (r"unknown encoder|encoder .*not found|unknown .* codec",
     "【编码器不可用】当前 ffmpeg 不支持该编码器。若你勾选了硬件加速，请在设置中关闭硬件加速，"
     "或把视频编码改为 libx264 后重试。"),
    (r"cannot load nvcuda|cannot load .*nvidia|no capable devices found|no nvenc capable",
     "【显卡不可用】未找到支持 NVENC 的 NVIDIA 显卡，或显卡驱动版本过旧。"
     "请更新显卡驱动，或关闭硬件加速后使用 CPU 编码。"),
    (r"mfx session|qsv|libmfx|no device",
     "【核显不可用】Intel QSV 硬件加速不可用（常见于老平台或驱动缺失），请关闭硬件加速后重试。"),
    (r"no space left on device|not enough space",
     "【磁盘空间不足】目标磁盘空间不够，请清理空间或更换输出目录。"),
    (r"divisible by 2|width or height not divisible",
     "【分辨率不合法】视频宽高必须为偶数，请把分辨率调整为偶数（如 1280x720）后重试。"),
    (r"conversion failed|error while opening encoder|error initializing",
     "【转换失败】ffmpeg 在执行过程中失败，请检查参数是否被设备支持，或尝试用『通用 1080P』预设重试。"),
    (r"timed out|timeout",
     "【执行超时】ffmpeg 长时间没有响应，请确认文件存放位置是否可正常读取。"),
]


def humanize_ffmpeg_error(stderr: str) -> str:
    """把 ffmpeg 的技术报错翻译成普通用户能看懂的提示。"""
    text = (stderr or "").strip()
    if not text:
        return "【转换失败】ffmpeg 未输出详细信息，请打开日志文件查看完整记录。"

    lowered = text.lower()
    for pattern, message in _ERROR_TRANSLATIONS:
        if re.search(pattern, lowered):
            detail = _extract_error_detail(text)
            return f"{message}\n\n原始错误：{detail}" if detail else message

    detail = _extract_error_detail(text)
    return f"【转换失败】转换过程中出现错误，以下是原始日志供排查：\n{detail}"


def _extract_error_detail(text: str, max_lines: int = 4) -> str:
    """从错误输出中提取最后几行有效信息（过滤进度行）。"""
    lines = [
        line.strip() for line in text.splitlines()
        if line.strip() and "=" not in line.split(" ")[0]
    ]
    return " / ".join(lines[-max_lines:])


# ======================================================================
# 三、硬件加速探测
# ======================================================================

@dataclass
class HardwareInfo:
    """显卡硬件编码能力探测结果。"""

    available: bool = False
    vendor: str = ""                       # "nvenc" | "qsv" | "amf" | ""
    label: str = "未检测到可用硬件编码器"
    encoders: List[str] = field(default_factory=list)
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "vendor": self.vendor,
            "label": self.label,
            "encoders": list(self.encoders),
            "detail": self.detail,
        }


_VENDOR_LABELS = {
    "nvenc": "NVIDIA NVENC（N 卡硬件编码）",
    "qsv": "Intel QSV（核显硬件编码）",
    "amf": "AMD AMF（A 卡硬件编码）",
}

_hardware_cache: Optional[HardwareInfo] = None

# 试编码超时（秒）：合成 0.4 秒小画面，正常情况几百毫秒内完成
_ENCODER_PROBE_TIMEOUT = 15


def _probe_encoder(ffmpeg: str, encoder: str) -> bool:
    """
    试编码验证：用 lavfi 生成一小段合成画面，让目标编码器真正跑一遍。

    为什么必须实测：`ffmpeg -encoders` 只说明该编码器「被编译进了这份 ffmpeg」，
    并不代表本机硬件可用。缺少驱动、显卡不支持 NVENC、核显被禁用等情况会
    在打开编码器阶段抛 "Function not implemented" 之类的错误，
    仅凭列表匹配会误判为「支持硬件加速」，把用户引向必然失败的转换。
    """
    command = [
        ffmpeg, "-hide_banner", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=256x144:rate=5:duration=0.4",
        "-c:v", encoder, "-f", "null", "-",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=_ENCODER_PROBE_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("硬件编码器 %s 试编码异常：%s", encoder, exc)
        return False
    return completed.returncode == 0


def detect_hardware_acceleration(force: bool = False) -> HardwareInfo:
    """
    探测本机真正可用的硬件编码器：先看 `ffmpeg -encoders` 列表，
    再对候选编码器做一次小尺寸试编码，只有实测通过的才判定为可用。
    结果带缓存；任何异常都被吞掉并降级为“不可用”，绝不阻塞程序启动。
    """
    global _hardware_cache
    if _hardware_cache is not None and not force:
        return _hardware_cache

    info = HardwareInfo()
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        info.detail = "未找到 ffmpeg，无法检测硬件编码器"
        _hardware_cache = info
        return info

    output = ""
    try:
        completed = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8",
            errors="replace",
        )
        output = (completed.stdout or "") + (completed.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("硬件编码器探测失败：%s", exc)

    listed: List[str] = []
    for encoder in HARDWARE_ENCODERS:
        if re.search(rf"\b{re.escape(encoder)}\b", output):
            listed.append(encoder)

    # 按 nvenc > qsv > amf 的优先级逐个方案试编码，第一个通过的即为采用方案
    vendor = ""
    verified: List[str] = []
    failed_vendors: List[str] = []
    for candidate in ("nvenc", "qsv", "amf"):
        candidates = [item for item in listed if HARDWARE_ENCODERS[item] == candidate]
        if not candidates:
            continue
        # 优先用 H.264 编码器试编码，一次通过即认定该方案可用
        candidates.sort(key=lambda item: 0 if item.startswith("h264") else 1)
        working = [item for item in candidates if _probe_encoder(ffmpeg, item)]
        if working:
            vendor = candidate
            verified = working
            break
        failed_vendors.append(_VENDOR_LABELS.get(candidate, candidate))

    info.encoders = verified
    info.vendor = vendor
    info.available = bool(vendor and verified)
    info.label = _VENDOR_LABELS.get(vendor, "未检测到可用硬件编码器")

    details = [f"试编码验证通过的硬件编码器：{', '.join(verified) if verified else '无'}"]
    if verified and vendor == "nvenc":
        details.append(
            "已检测到 NVIDIA 显卡驱动 (nvidia-smi)"
            if shutil.which("nvidia-smi")
            else "未检测到 nvidia-smi"
        )
    if failed_vendors:
        details.append(
            f"{'、'.join(failed_vendors)} 在 ffmpeg 中已列出但试编码失败"
            f"（驱动缺失或硬件不支持），已判定为不可用；"
            f"备选编码器列表：{', '.join(listed) if listed else '无'}"
        )
    info.detail = "；".join(details)

    logger.info(
        "硬件加速探测：available=%s vendor=%s 实测可用=%s 列表已列出=%s",
        info.available, vendor, verified, listed,
    )
    _hardware_cache = info
    return info


def apply_hardware_acceleration(params: OutputParams, hw: HardwareInfo) -> OutputParams:
    """
    按探测结果把软编编码器替换为对应硬件编码器。
    硬件不可用时仅关闭 hw_accel 开关，不改动其它参数。
    """
    result = params.copy()
    if not hw or not hw.available:
        result.hw_accel = False
        return result

    result.hw_accel = True
    mapping = SOFT_TO_HARD_ENCODER.get(params.vcodec)
    if mapping and hw.vendor in mapping:
        result.vcodec = mapping[hw.vendor]
    return result


# ======================================================================
# 四、任务模型
# ======================================================================

class TaskStatus(str, Enum):
    """任务状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return {
            TaskStatus.PENDING: "等待中",
            TaskStatus.RUNNING: "转换中",
            TaskStatus.SUCCESS: "已完成",
            TaskStatus.FAILED: "失败",
            TaskStatus.CANCELLED: "已取消",
        }[self]


def _format_eta(seconds: float) -> str:
    """剩余秒数格式化成中文可读文本。

    负数或超过 24 小时视为「无法估算」；不足 1 秒视为「即将完成」，
    避免任务已到终点却仍显示「计算中」造成困惑。
    """
    if seconds < 0 or seconds > 24 * 3600:
        return "计算中"
    if seconds < 1:
        return "即将完成"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


@dataclass
class ConversionTask:
    """一条转码任务。"""

    task_id: str
    input_path: str
    output_path: str
    params: OutputParams
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    message: str = ""
    duration: float = 0.0          # 源视频时长（秒），用于计算百分比与剩余时间
    has_audio: bool = True
    file_size: int = 0
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def filename(self) -> str:
        return Path(self.input_path).name

    @property
    def output_name(self) -> str:
        return Path(self.output_path).name

    @property
    def status_label(self) -> str:
        return self.status.label

    @property
    def elapsed_str(self) -> str:
        """已耗时（秒 → 中文）；任务未开始时返回占位符。"""
        if not self.started_at:
            return "-"
        end = self.finished_at or time.time()
        return _format_eta(max(0.0, end - self.started_at))

    @property
    def speed_str(self) -> str:
        return self.speed or "-"

    @property
    def eta_str(self) -> str:
        return self.eta or "-"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "input_path": self.input_path,
            "output_path": self.output_path,
            "params": self.params.to_dict(),
            "status": self.status.value,
            "progress": round(self.progress, 2),
            "speed": self.speed,
            "eta": self.eta,
            "message": self.message,
            "duration": self.duration,
            "file_size": self.file_size,
        }

    @classmethod
    def create(
        cls,
        input_path: str,
        output_path: str,
        params: OutputParams,
        duration: float = 0.0,
        has_audio: bool = True,
        file_size: int = 0,
    ) -> "ConversionTask":
        """工厂方法：自动生成任务 ID。"""
        return cls(
            task_id=uuid.uuid4().hex[:12],
            input_path=input_path,
            output_path=output_path,
            params=params,
            duration=duration,
            has_audio=has_audio,
            file_size=file_size,
        )


# ======================================================================
# 五、异步执行线程
# ======================================================================

class FFmpegWorker(QThread):
    """
    单个转码任务的执行线程。

    信号：
    - progressChanged(task_id, 百分比, 速度, 剩余时间)
    - logLine(task_id, 原始输出行)
    - finishedTask(task_id, 是否成功, 人性化提示信息, 输出文件路径)
    """

    progressChanged = Signal(str, float, str, str)
    logLine = Signal(str, str)
    finishedTask = Signal(str, bool, str, str)

    def __init__(self, task: ConversionTask, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.task = task
        self._process: Optional[subprocess.Popen] = None
        self._cancelled = False
        self._error_lines: deque = deque(maxlen=60)
        self._tail_lines: deque = deque(maxlen=40)
        self._speed_value = 0.0
        self._last_seconds = 0.0

    # ---------------- 取消 ----------------
    def cancel(self) -> None:
        """请求取消：先 terminate，2 秒内未退出再强杀。"""
        self._cancelled = True
        process = self._process
        if process is None or process.poll() is not None:
            return
        logger.info("请求取消任务：%s", self.task.filename)
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass

    # ---------------- 主循环 ----------------
    def run(self) -> None:  # noqa: C901  (流程较长但线性清晰)
        task = self.task
        command = build_ffmpeg_command(task.params, task.input_path, task.output_path, task.has_audio)
        log_command(command, logger)

        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        task.progress = 0.0
        task.message = "正在转换…"
        duration = max(0.0, task.duration)

        returncode: Optional[int] = None
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            logger.error("ffmpeg 启动失败：%s", exc)
            task.status = TaskStatus.FAILED
            task.message = "【环境异常】无法启动 ffmpeg，请确认已正确安装并配置 PATH。"
            self.finishedTask.emit(task.task_id, False, task.message, "")
            return

        try:
            stdout = self._process.stdout
            if stdout is not None:
                for raw_line in stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    self.logLine.emit(task.task_id, line)
                    if self._cancelled:
                        break
                    self._handle_progress_line(line, duration)
            returncode = self._process.wait()
        except Exception as exc:  # noqa: BLE001  线程内必须兜底，避免崩溃整个 GUI
            logger.exception("转码线程异常：%s", exc)
            task.message = f"【内部错误】转码过程中出现异常：{exc}"
            returncode = -1
        finally:
            try:
                if self._process is not None and self._process.stdout is not None:
                    self._process.stdout.close()
            except OSError:
                pass

        self._finalize(returncode)

    # ---------------- 进度解析（-progress 协议） ----------------
    def _handle_progress_line(self, line: str, duration: float) -> None:
        """解析 ffmpeg 的 -progress pipe:1 输出，刷新进度、速度与剩余时间。"""
        if "=" not in line:
            if _ERROR_PATTERN.search(line):
                self._error_lines.append(line)
            self._tail_lines.append(line)
            return

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        if key in ("out_time_us", "out_time_ms"):
            # 注意：ffmpeg 历史原因，out_time_ms 实际输出的是「微秒」
            self._last_seconds = max(0.0, _safe_float(value) / 1_000_000.0)
            self._emit_progress(duration)
        elif key == "speed":
            if value in ("N/A", "", "0x"):
                self._speed_value = 0.0
            else:
                self._speed_value = max(0.0, _safe_float(value.replace("x", "")))
            self.task.speed = f"{self._speed_value:.2f}x" if self._speed_value > 0 else ""
            self._emit_progress(duration)
        elif key == "progress" and value == "end":
            self._last_seconds = duration
            self._emit_progress(duration, force=True)
        elif _ERROR_PATTERN.search(line):
            self._error_lines.append(line)

    def _emit_progress(self, duration: float, force: bool = False) -> None:
        """计算百分比与剩余时间并通过信号推送给界面。"""
        task = self.task
        if duration <= 0:
            # 无法得知时长时，只推送速度，不猜测百分比
            if force:
                task.progress = max(task.progress, 0.0)
                self.progressChanged.emit(task.task_id, task.progress, task.speed_str, task.eta_str)
            return

        percent = min(99.9, max(0.0, self._last_seconds / duration * 100.0))
        if not force and percent <= task.progress and percent < 99.9:
            return

        task.progress = percent
        remaining = max(0.0, duration - self._last_seconds)
        if self._speed_value > 0:
            task.eta = _format_eta(remaining / self._speed_value)
        else:
            task.eta = "计算中" if remaining > 1 else ""
        self.progressChanged.emit(task.task_id, percent, task.speed_str, task.eta_str)

    # ---------------- 收尾 ----------------
    def _finalize(self, returncode: Optional[int]) -> None:
        task = self.task
        task.finished_at = time.time()

        if self._cancelled:
            task.status = TaskStatus.CANCELLED
            task.message = "任务已取消"
            self._remove_partial_output()
            logger.info("任务已取消：%s", task.filename)
            self.finishedTask.emit(task.task_id, False, task.message, "")
            return

        output_file = Path(task.output_path)
        if returncode == 0 and output_file.exists() and output_file.stat().st_size > 0:
            task.status = TaskStatus.SUCCESS
            task.progress = 100.0
            task.eta = ""
            task.message = "转换完成"
            logger.info("任务完成：%s → %s（耗时 %s）", task.filename, output_file.name, task.elapsed_str)
            self.finishedTask.emit(task.task_id, True, task.message, str(output_file))
            return

        # 失败：优先用收集到的错误行，其次用输出尾部
        error_text = "\n".join(self._error_lines) if self._error_lines else "\n".join(self._tail_lines)
        task.status = TaskStatus.FAILED
        task.message = humanize_ffmpeg_error(error_text)
        logger.error("任务失败：%s | 退出码=%s | %s", task.filename, returncode, error_text)
        self._remove_partial_output()
        self.finishedTask.emit(task.task_id, False, task.message, "")

    def _remove_partial_output(self) -> None:
        """删除未完成的分片输出文件，避免用户误认为转换成功。"""
        try:
            output_file = Path(self.task.output_path)
            if output_file.exists():
                output_file.unlink()
                logger.debug("已清理未完成的输出文件：%s", output_file)
        except OSError as exc:
            logger.debug("清理临时文件失败：%s", exc)


def _safe_float(value: Any, default: float = 0.0) -> float:
    """安全转 float。"""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


# ======================================================================
# 六、队列管理器
# ======================================================================

class ConversionManager(QObject):
    """
    转码队列管理器（默认同时执行 1 个任务，可在设置中调整并发度）。

    信号：
    - taskUpdated(ConversionTask)  任务状态/进度变化，界面据此刷新表格
    - taskFinished(ConversionTask) 任务结束（成功、失败或取消）
    - queueChanged()               队列数量变化
    - allFinished()                队列全部结束
    - logMessage(str)              给「运行日志」面板推送一行文本
    """

    taskUpdated = Signal(object)
    taskFinished = Signal(object)
    queueChanged = Signal()
    allFinished = Signal()
    logMessage = Signal(str)

    def __init__(self, parent: Optional[QObject] = None, max_concurrent: Optional[int] = None) -> None:
        super().__init__(parent)
        self._tasks: List[ConversionTask] = []
        self._workers: Dict[str, FFmpegWorker] = {}
        configured = max_concurrent if max_concurrent is not None else settings.get("max_concurrent", 1)
        try:
            self._max_concurrent = max(1, int(configured))
        except (TypeError, ValueError):
            self._max_concurrent = 1
        self._running = False

    # ---------------- 队列属性 ----------------
    @property
    def tasks(self) -> List[ConversionTask]:
        return list(self._tasks)

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    def set_max_concurrent(self, value: int) -> None:
        """调整并发度；调大后会立即尝试派发排队中的任务。"""
        try:
            self._max_concurrent = max(1, int(value))
        except (TypeError, ValueError):
            self._max_concurrent = 1
        settings.set("max_concurrent", self._max_concurrent)
        if self._running:
            self._dispatch()

    def get_task(self, task_id: str) -> Optional[ConversionTask]:
        for task in self._tasks:
            if task.task_id == task_id:
                return task
        return None

    @property
    def pending_count(self) -> int:
        return sum(1 for task in self._tasks if task.status == TaskStatus.PENDING)

    @property
    def running_count(self) -> int:
        return sum(1 for task in self._tasks if task.status == TaskStatus.RUNNING)

    @property
    def finished_count(self) -> int:
        return sum(
            1 for task in self._tasks
            if task.status in (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED)
        )

    @property
    def is_busy(self) -> bool:
        return bool(self._workers) or self.pending_count > 0

    # ---------------- 队列操作 ----------------
    def add_task(self, task: ConversionTask) -> str:
        self._tasks.append(task)
        self.taskUpdated.emit(task)
        self.queueChanged.emit()
        logger.info("新增任务：%s → %s（%s）", task.filename, task.output_name, task.params.describe())
        # 队列已在运行中则立即触发派发
        if self._running:
            self._dispatch()
        return task.task_id

    def remove_task(self, task_id: str) -> bool:
        """移除任务；若正在执行会先取消。"""
        task = self.get_task(task_id)
        if task is None:
            return False
        if task.status == TaskStatus.RUNNING:
            self.cancel_task(task_id)
        self._tasks = [item for item in self._tasks if item.task_id != task_id]
        self.queueChanged.emit()
        return True

    def clear_finished(self) -> int:
        """清空所有已结束的任务，返回清除数量。"""
        active = (TaskStatus.PENDING, TaskStatus.RUNNING)
        remaining = [task for task in self._tasks if task.status in active]
        removed = len(self._tasks) - len(remaining)
        self._tasks = remaining
        if removed:
            self.queueChanged.emit()
        return removed

    def start(self) -> None:
        """开始执行队列（可重复调用，自动补齐空出的并发位）。"""
        self._running = True
        logger.info("开始执行队列：待处理 %d 个任务", self.pending_count)
        self._dispatch()

    def pause(self) -> None:
        """暂停派发新任务，已在执行的任务继续跑完。"""
        self._running = False
        logger.info("已暂停派发新任务")
        self.queueChanged.emit()

    def cancel_task(self, task_id: str) -> bool:
        """取消指定任务（等待中的直接标记取消，执行中的通知子进程终止）。"""
        task = self.get_task(task_id)
        if task is None:
            return False

        worker = self._workers.get(task_id)
        if worker is not None:
            worker.cancel()
            return True

        if task.status == TaskStatus.PENDING:
            task.status = TaskStatus.CANCELLED
            task.message = "已取消（未开始执行）"
            task.finished_at = time.time()
            self.taskUpdated.emit(task)
            self.taskFinished.emit(task)
            self.queueChanged.emit()
            self._check_all_finished()
            return True
        return False

    def cancel_all(self) -> None:
        """取消全部未结束的任务。"""
        for task in list(self._tasks):
            if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                self.cancel_task(task.task_id)

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """
        程序退出前的收尾：取消所有任务并等待线程真正退出。

        必要性：QThread 在仍处于运行状态时被销毁，Qt 会直接报错甚至终止进程，
        因此关闭窗口时必须显式 `wait()` 等待线程结束。
        """
        self._running = False
        for task in list(self._tasks):
            if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                self.cancel_task(task.task_id)

        deadline = time.time() + max(0, timeout_ms) / 1000.0
        for worker in list(self._workers.values()):
            remaining_ms = max(0, int((deadline - time.time()) * 1000))
            try:
                if not worker.wait(remaining_ms):
                    worker.terminate()
                    worker.wait(1500)
            except RuntimeError:
                # 线程对象已被 Qt 释放（deleteLater 已执行），无需再等待
                pass
        self._workers.clear()

    # ---------------- 内部派发 ----------------
    def _dispatch(self) -> None:
        """在并发度允许范围内把等待中的任务派发给 worker。"""
        if not self._running:
            return
        while len(self._workers) < self._max_concurrent:
            next_task = next(
                (task for task in self._tasks if task.status == TaskStatus.PENDING), None
            )
            if next_task is None:
                break
            self._start_task(next_task)
        self._check_all_finished()

    def _start_task(self, task: ConversionTask) -> None:
        worker = FFmpegWorker(task, parent=self)
        worker.progressChanged.connect(self._on_progress)
        worker.logLine.connect(self._on_log_line)
        worker.finishedTask.connect(self._on_task_finished)
        worker.finished.connect(worker.deleteLater)
        self._workers[task.task_id] = worker

        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        self.taskUpdated.emit(task)
        worker.start()

    def _on_progress(self, task_id: str, percent: float, speed: str, eta: str) -> None:
        task = self.get_task(task_id)
        if task is None:
            return
        task.progress = percent
        task.speed = speed
        task.eta = eta
        self.taskUpdated.emit(task)

    def _on_log_line(self, task_id: str, line: str) -> None:
        logger.debug("[ffmpeg:%s] %s", task_id, line)
        self.logMessage.emit(f"[{task_id}] {line}")

    def _on_task_finished(self, task_id: str, success: bool, message: str, output_path: str) -> None:
        self._workers.pop(task_id, None)
        task = self.get_task(task_id)
        if task is not None:
            if success and output_path:
                task.output_path = output_path
            if not task.message:
                task.message = message
            self.taskUpdated.emit(task)
            self.taskFinished.emit(task)

        if self._running:
            self._dispatch()
        else:
            self.queueChanged.emit()
            self._check_all_finished()

    def _check_all_finished(self) -> None:
        """队列已无待处理任务且无活跃线程时，发出 allFinished。"""
        if not self._tasks:
            return
        if self._workers or self.pending_count > 0:
            return
        self._running = False
        self.queueChanged.emit()
        logger.info("队列执行结束：成功 %d / 失败 %d / 取消 %d", *self._status_counts())
        self.allFinished.emit()

    def _status_counts(self) -> tuple:
        success = sum(1 for task in self._tasks if task.status == TaskStatus.SUCCESS)
        failed = sum(1 for task in self._tasks if task.status == TaskStatus.FAILED)
        cancelled = sum(1 for task in self._tasks if task.status == TaskStatus.CANCELLED)
        return success, failed, cancelled


def humanize_command(command: List[str]) -> str:
    """把命令列表转成可读字符串（供界面「查看命令」按钮展示）。"""
    return format_command(command)
