"""
core 层：视频信息解析、ffmpeg 转码执行、缩略图生成。

本层只依赖 config 与 utils，以及 PySide6 的 QtCore（用于异步线程与信号），
不依赖任何 UI 控件，保证核心逻辑可独立测试与复用。
"""

from core.ffprobe_parser import (  # noqa: F401
    FFProbeError,
    VideoInfo,
    humanize_probe_error,
    parse_video,
    probe_raw,
)
# ffmpeg_engine 依赖 PySide6.QtCore（异步线程与信号），
# 这里用 try/except 兜底：在没有安装 PySide6 的环境下，
# 仍然可以单独导入 ffprobe_parser / thumbnail（纯逻辑模块）做离线测试，
# 兑现「核心逻辑可独立测试」的设计承诺。
try:
    from core.ffmpeg_engine import (  # noqa: F401
        ConversionManager,
        ConversionTask,
        FFmpegWorker,
        HardwareInfo,
        OutputParams,
        TaskStatus,
        apply_hardware_acceleration,
        build_ffmpeg_command,
        build_output_path,
        detect_hardware_acceleration,
        humanize_command,
        humanize_ffmpeg_error,
    )
except ImportError as _exc:  # pragma: no cover - 仅用于无 Qt 环境的降级
    import warnings as _warnings

    _warnings.warn(
        f"core.ffmpeg_engine 未能导入（{_exc}），"
        "转码引擎相关 API 不可用，仅 ffprobe_parser / thumbnail 可用。"
    )
from core.thumbnail import (  # noqa: F401
    clear_thumbnail_cache,
    default_thumbnail_path,
    extract_frame,
    generate_thumbnail,
)
