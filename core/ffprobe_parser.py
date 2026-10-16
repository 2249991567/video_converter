"""
视频元数据解析模块（基于 ffprobe 原生命令行）
=============================================

职责：调用系统 `ffprobe` 命令，把视频/音频文件的完整元数据解析成结构化对象。

设计说明：
- 严格遵守「禁止使用任何 ffmpeg 封装库」的约束，全部通过 subprocess 调用命令行完成；
- ffprobe 输出的任何字段缺失都不能导致异常，必须做安全兜底（真实世界里的视频容器
  千奇百怪，缺 size / 缺 bit_rate / 缺 channel_layout 都很常见）；
- 对外只暴露 `parse_video()`，UI 层拿到 `VideoInfo` 后可直接调用 `summary_dict()`
  渲染表单，无需关心底层字段名。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Optional

from config.settings import CREATE_NO_WINDOW, get_ffprobe_path
from utils.logger import format_command, get_logger

logger = get_logger("core.ffprobe")

# ffprobe 单次探测超时（秒）：超长视频或网络挂载盘可能需要更久，30 秒足够
PROBE_TIMEOUT = 30


class FFProbeError(Exception):
    """ffprobe 解析失败（文件不存在、损坏、非媒体文件等）。"""


# ----------------------------------------------------------------------
# 安全类型转换工具
# ----------------------------------------------------------------------

def _to_int(value: Any, default: int = 0) -> int:
    """把 ffprobe 返回的字符串安全转成 int。"""
    if value is None:
        return default
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    """把 ffprobe 返回的字符串安全转成 float。"""
    if value is None:
        return default
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_fraction(value: Any) -> float:
    """
    解析 ffprobe 的分数型帧率（如 "30000/1001" → 29.97）。
    对 "0/0"、"N/A"、None 等异常值统一返回 0.0。
    """
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text or text in ("0/0", "N/A", "n/a"):
        return 0.0
    try:
        return float(Fraction(text))
    except (ValueError, ZeroDivisionError, TypeError):
        try:
            return float(text)
        except (TypeError, ValueError):
            return 0.0


def _format_filesize(num_bytes: int) -> str:
    """字节数转人类可读字符串：1.23 GB / 456.78 MB / 12.3 KB。"""
    if num_bytes <= 0:
        return "未知"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.2f} TB"


def _format_duration(seconds: float) -> str:
    """秒数转时:分:秒（不足 1 小时为 分:秒 的 00:00 形式）。"""
    if seconds <= 0:
        return "未知"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _format_bitrate(bps: int) -> str:
    """码率转人类可读字符串。"""
    if bps <= 0:
        return "未知"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    return f"{bps / 1000:.0f} kbps"


def _format_sample_rate(hz: int) -> str:
    """采样率转 kHz 字符串。"""
    if hz <= 0:
        return "未知"
    return f"{hz / 1000:.1f} kHz".replace(".0 kHz", " kHz")


def _channel_label(channels: int, layout: str = "") -> str:
    """声道数转中文说明。"""
    if channels <= 0:
        return "无音频流"
    known = {1: "单声道", 2: "立体声", 6: "5.1 环绕", 8: "7.1 环绕"}
    name = known.get(channels, "")
    suffix = f" ({name})" if name else ""
    if layout and not name:
        suffix = f" ({layout})"
    return f"{channels} 声道{suffix}"


def _resolution_tier(width: int, height: int) -> str:
    """按短边判定分辨率档位（竖屏视频同样适用）。"""
    short_side = min(width, height) if width and height else 0
    if short_side >= 2160:
        return "4K"
    if short_side >= 1440:
        return "2K"
    if short_side >= 1080:
        return "1080P"
    if short_side >= 720:
        return "720P"
    if short_side >= 480:
        return "480P"
    if short_side >= 360:
        return "360P"
    return "低分辨率" if short_side else ""


# ----------------------------------------------------------------------
# 数据模型
# ----------------------------------------------------------------------

@dataclass
class VideoInfo:
    """视频（或音频）文件的完整元数据。"""

    path: str = ""
    filename: str = ""
    filesize: int = 0                 # 字节
    duration: float = 0.0             # 秒
    width: int = 0
    height: int = 0
    video_codec: str = ""
    video_codec_long: str = ""
    pix_fmt: str = ""
    fps: float = 0.0
    profile: str = ""
    bitrate: int = 0                  # 总码率 bps
    audio_codec: str = ""
    audio_channels: int = 0
    audio_channel_layout: str = ""
    sample_rate: int = 0
    container: str = ""
    has_video: bool = False
    has_audio: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    # ---------------- 展示用只读属性 ----------------
    @property
    def resolution(self) -> str:
        if not self.width or not self.height:
            return "无视频流"
        return f"{self.width}x{self.height}"

    @property
    def resolution_label(self) -> str:
        if not self.width or not self.height:
            return "无视频流"
        tier = _resolution_tier(self.width, self.height)
        base = f"{self.width}x{self.height}"
        return f"{base} ({tier})" if tier else base

    @property
    def filesize_str(self) -> str:
        return _format_filesize(self.filesize)

    @property
    def duration_str(self) -> str:
        return _format_duration(self.duration)

    @property
    def fps_str(self) -> str:
        if not self.has_video:
            return "无视频流"
        if self.fps <= 0:
            return "静态图 / 未知"
        return f"{self.fps:.2f} fps".replace(".00 fps", " fps")

    @property
    def bitrate_str(self) -> str:
        return _format_bitrate(self.bitrate)

    @property
    def sample_rate_str(self) -> str:
        return _format_sample_rate(self.sample_rate)

    @property
    def channels_str(self) -> str:
        return _channel_label(self.audio_channels, self.audio_channel_layout)

    @property
    def video_codec_display(self) -> str:
        if not self.has_video:
            return "无视频流"
        codec = self.video_codec or "未知"
        return f"{codec} ({self.video_codec_long})" if self.video_codec_long and self.video_codec_long != codec else codec

    @property
    def audio_codec_display(self) -> str:
        if not self.has_audio:
            return "无音频流"
        return self.audio_codec or "未知"

    # ---------------- 序列化 ----------------
    def summary_dict(self) -> Dict[str, str]:
        """中文标签 → 展示值，UI 可直接遍历渲染（保持插入顺序）。"""
        return {
            "文件名": self.filename or "未知",
            "文件路径": self.path or "未知",
            "文件大小": self.filesize_str,
            "时长": self.duration_str,
            "分辨率": self.resolution_label,
            "视频编码": self.video_codec_display,
            "帧率": self.fps_str,
            "总码率": self.bitrate_str,
            "像素格式": self.pix_fmt or ("无视频流" if not self.has_video else "未知"),
            "音频编码": self.audio_codec_display,
            "采样率": self.sample_rate_str if self.has_audio else "无音频流",
            "声道数": self.channels_str,
            "封装格式": self.container or "未知",
        }

    def to_dict(self) -> Dict[str, Any]:
        """原始数值型字段（供 AI 层推理与日志记录使用）。"""
        return {
            "path": self.path,
            "filename": self.filename,
            "filesize": self.filesize,
            "duration": self.duration,
            "width": self.width,
            "height": self.height,
            "video_codec": self.video_codec,
            "pix_fmt": self.pix_fmt,
            "fps": self.fps,
            "profile": self.profile,
            "bitrate": self.bitrate,
            "audio_codec": self.audio_codec,
            "audio_channels": self.audio_channels,
            "audio_channel_layout": self.audio_channel_layout,
            "sample_rate": self.sample_rate,
            "container": self.container,
            "has_video": self.has_video,
            "has_audio": self.has_audio,
        }

    def probe_prompt(self) -> str:
        """生成给大模型看的“原视频属性”描述（自然语言转参数时使用）。"""
        parts = [
            f"文件名：{self.filename}",
            f"时长：{self.duration_str}（{self.duration:.2f} 秒）",
            f"文件大小：{self.filesize_str}",
        ]
        if self.has_video:
            parts.extend([
                f"分辨率：{self.resolution_label}",
                f"视频编码：{self.video_codec or '未知'}",
                f"帧率：{self.fps_str}",
                f"总码率：{self.bitrate_str}",
            ])
        else:
            parts.append("该文件不包含视频流")
        if self.has_audio:
            parts.extend([f"音频编码：{self.audio_codec or '未知'}", f"声道：{self.channels_str}"])
        else:
            parts.append("该文件不包含音频流")
        return "；".join(parts)


# ----------------------------------------------------------------------
# ffprobe 调用
# ----------------------------------------------------------------------

def humanize_probe_error(stderr: str) -> str:
    """把 ffprobe 的技术报错翻译成用户能看懂的中文提示。"""
    text = (stderr or "").strip()
    lowered = text.lower()

    if "no such file or directory" in lowered:
        return "【文件不存在】找不到该文件，可能已被移动或删除，请重新选择视频。"
    if "permission denied" in lowered:
        return "【权限不足】无法读取该文件，请检查文件是否被其他程序占用，或更换保存位置。"
    if "moov atom not found" in lowered or "invalid data found when processing input" in lowered:
        return "【文件损坏】这不是有效的视频文件，或文件已损坏/未下载完整，请更换文件重试。"
    if "unsupported codec" in lowered:
        return "【编码不支持】当前 ffmpeg 版本不支持该文件使用的编码格式，建议升级 ffmpeg。"
    if "timed out" in lowered or "timeout" in lowered:
        return "【解析超时】读取视频信息超时，文件可能存放在响应很慢的网络磁盘上。"
    tail = text.splitlines()[-3:] if text else []
    detail = " / ".join(line.strip() for line in tail if line.strip())
    return f"【解析失败】无法读取视频信息。{detail}" if detail else "【解析失败】无法读取视频信息，请确认文件是否完整。"


def probe_raw(path: str | Path) -> Dict[str, Any]:
    """
    执行 ffprobe 并返回原始 JSON 字典。

    :raises FFProbeError: ffprobe 不存在 / 文件不存在 / 命令执行失败
    """
    file_path = Path(path)
    ffprobe = get_ffprobe_path()
    if not ffprobe:
        raise FFProbeError("未检测到 ffprobe 命令，请先安装 ffmpeg 并加入系统 PATH。")
    if not file_path.exists():
        raise FFProbeError(f"文件不存在：{file_path}")
    if file_path.is_dir():
        raise FFProbeError(f"该路径是文件夹，不是视频文件：{file_path}")

    command = [
        ffprobe,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(file_path),
    ]
    logger.debug("执行命令: %s", format_command(command))

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("ffprobe 解析超时：%s", file_path)
        raise FFProbeError("【解析超时】读取视频信息超时，请确认文件是否完整或尝试更换存放位置。") from exc
    except OSError as exc:
        logger.error("ffprobe 执行失败：%s", exc)
        raise FFProbeError(f"【环境异常】ffprobe 无法执行：{exc}") from exc

    stdout = (completed.stdout or "").strip()
    if not stdout:
        message = humanize_probe_error(completed.stderr or "")
        logger.error("ffprobe 无输出：%s | %s", file_path, completed.stderr)
        raise FFProbeError(message)

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.error("ffprobe 输出解析失败：%s", stdout[:500])
        raise FFProbeError("【解析失败】ffprobe 返回内容格式异常，无法解析视频信息。") from exc

    if not isinstance(payload, dict):
        raise FFProbeError("【解析失败】ffprobe 返回内容格式异常，无法解析视频信息。")
    return payload


def parse_video(path: str | Path) -> VideoInfo:
    """
    解析视频文件，返回结构化元数据。

    :raises FFProbeError: 解析失败（错误信息已人性化，可直接展示给用户）
    """
    file_path = Path(path)
    payload = probe_raw(file_path)

    streams = payload.get("streams") or []
    fmt = payload.get("format") or {}

    video_stream: Optional[Dict[str, Any]] = None
    audio_stream: Optional[Dict[str, Any]] = None
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = stream.get("codec_type")
        if codec_type == "video" and video_stream is None:
            video_stream = stream
        elif codec_type == "audio" and audio_stream is None:
            audio_stream = stream

    info = VideoInfo()
    info.path = str(file_path.resolve())
    info.filename = file_path.name
    info.raw = payload

    # 文件大小：优先容器信息，缺失时回退文件系统
    info.filesize = _to_int(fmt.get("size"), 0)
    if info.filesize <= 0:
        try:
            info.filesize = file_path.stat().st_size
        except OSError:
            info.filesize = 0

    info.duration = _to_float(fmt.get("duration"), 0.0)
    info.container = str(fmt.get("format_name") or "").split(",")[0]

    # ---------------- 视频流 ----------------
    if video_stream:
        info.has_video = True
        info.width = _to_int(video_stream.get("width"), 0)
        info.height = _to_int(video_stream.get("height"), 0)
        info.video_codec = str(video_stream.get("codec_name") or "")
        info.video_codec_long = str(video_stream.get("codec_long_name") or "")
        info.pix_fmt = str(video_stream.get("pix_fmt") or "")
        info.profile = str(video_stream.get("profile") or "")
        # 帧率优先 r_frame_rate（真实帧率），其次 avg_frame_rate
        info.fps = _parse_fraction(video_stream.get("r_frame_rate")) or _parse_fraction(
            video_stream.get("avg_frame_rate")
        )
        if info.duration <= 0:
            info.duration = _to_float(video_stream.get("duration"), 0.0)

    # ---------------- 音频流 ----------------
    if audio_stream:
        info.has_audio = True
        info.audio_codec = str(audio_stream.get("codec_name") or "")
        info.audio_channels = _to_int(audio_stream.get("channels"), 0)
        info.audio_channel_layout = str(audio_stream.get("channel_layout") or "")
        info.sample_rate = _to_int(audio_stream.get("sample_rate"), 0)
        if not info.audio_channel_layout and info.audio_channels:
            info.audio_channel_layout = {1: "mono", 2: "stereo"}.get(info.audio_channels, "")

    # ---------------- 总码率 ----------------
    info.bitrate = _to_int(fmt.get("bit_rate"), 0)
    if info.bitrate <= 0:
        # 容器未提供时，用各流码率求和兜底
        total = 0
        for stream in streams:
            if isinstance(stream, dict):
                total += _to_int(stream.get("bit_rate"), 0)
        info.bitrate = total

    logger.info(
        "解析完成：%s | %s | %s | %s",
        info.filename, info.resolution, info.duration_str, info.video_codec or "无视频流",
    )
    return info
