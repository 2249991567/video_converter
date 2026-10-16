"""
全局配置管理模块
================

职责：
1. 定义项目所有常量表（输出格式档案、编码器列表、分辨率/帧率/码率选项、内置预设）。
2. 提供配置持久化能力（AppSettings），把用户偏好与 API 密钥写入本地 JSON。
3. 提供运行环境检测（ffmpeg / ffprobe 是否可用）。

设计说明：
- 本模块**不导入任何 UI 框架**，保证 config 层可被 core / ai / ui 任意层安全引用。
- 配置与密钥的优先级：`.env` 环境变量 > 本地 settings.json > 代码内默认值。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ----------------------------------------------------------------------
# 一、路径常量
# ----------------------------------------------------------------------

APP_NAME = "VideoConverterAI"
APP_VERSION = "1.0.0"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 运行时数据目录（配置 / 日志 / 缩略图缓存），放在用户目录下，避免污染代码仓库
DATA_DIR = Path(os.getenv("VIDEO_CONVERTER_DATA_DIR") or (Path.home() / ".video_converter_ai"))
CONFIG_FILE = DATA_DIR / "settings.json"
LOG_DIR = DATA_DIR / "logs"
THUMBNAIL_DIR = DATA_DIR / "thumbnails"
TEMP_DIR = DATA_DIR / "temp"

for _d in (DATA_DIR, LOG_DIR, THUMBNAIL_DIR, TEMP_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:  # 极端情况下目录不可写也不应导致程序启动失败
        pass

# Windows 下隐藏 subprocess 的控制台黑窗
CREATE_NO_WINDOW: int = 0x08000000 if os.name == "nt" else 0

LOG_FILE = LOG_DIR / "app.log"


# ----------------------------------------------------------------------
# 二、输出格式档案
# ----------------------------------------------------------------------

# 视频/音频格式扩展名（供文件对话框使用）
VIDEO_EXTENSIONS = [
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v",
    ".mpg", ".mpeg", ".ts", ".3gp", ".rmvb", ".vob", ".mts",
]
AUDIO_EXTENSIONS = [".mp3", ".aac", ".wav", ".flac", ".m4a", ".ogg", ".wma"]
SUPPORTED_EXTENSIONS = VIDEO_EXTENSIONS + AUDIO_EXTENSIONS

# 输出容器档案：format_key -> 档案
# audio_only=True 表示该格式只保留音频；gif=True 表示走 GIF 专用滤镜链
OUTPUT_FORMATS: Dict[str, Dict[str, Any]] = {
    "MP4":  {"ext": ".mp4",  "container": "mp4",      "vcodec": "libx264",    "acodec": "aac",         "audio_only": False, "gif": False},
    "MKV":  {"ext": ".mkv",  "container": "matroska", "vcodec": "libx264",    "acodec": "aac",         "audio_only": False, "gif": False},
    "AVI":  {"ext": ".avi",  "container": "avi",      "vcodec": "mpeg4",      "acodec": "libmp3lame",  "audio_only": False, "gif": False},
    "MOV":  {"ext": ".mov",  "container": "mov",      "vcodec": "libx264",    "acodec": "aac",         "audio_only": False, "gif": False},
    "WebM": {"ext": ".webm", "container": "webm",     "vcodec": "libvpx-vp9", "acodec": "libopus",     "audio_only": False, "gif": False},
    "MP3":  {"ext": ".mp3",  "container": "mp3",      "vcodec": None,         "acodec": "libmp3lame",  "audio_only": True,  "gif": False},
    "GIF":  {"ext": ".gif",  "container": "gif",      "vcodec": None,         "acodec": None,          "audio_only": False, "gif": True},
}

OUTPUT_FORMAT_KEYS: List[str] = list(OUTPUT_FORMATS.keys())

# 下拉框可选值
VIDEO_CODECS: List[str] = [
    "copy",          # 直接复制原视频流（不重新编码，速度最快）
    "libx264",       # H.264 软编，兼容性最好
    "libx265",       # H.265 软编，同画质体积更小
    "libvpx-vp9",    # VP9，适合 WebM
    "mpeg4",         # 老设备兼容
    "h264_nvenc",    # NVIDIA 硬件编码
    "hevc_nvenc",
    "h264_qsv",      # Intel 核显硬件编码
    "h264_amf",      # AMD 硬件编码
    "prores",        # 剪辑中间格式
]

AUDIO_CODECS: List[str] = [
    "copy",
    "aac",
    "libmp3lame",
    "libopus",
    "libvorbis",
    "ac3",
    "pcm_s16le",
]

RESOLUTIONS: List[str] = [
    "原始分辨率",
    "3840x2160",   # 4K
    "2560x1440",   # 2K
    "1920x1080",   # 1080P
    "1280x720",    # 720P
    "854x480",     # 480P
    "640x360",     # 360P
    "1080x1920",   # 竖屏 1080P
    "720x1280",    # 竖屏 720P
    "1080x1080",   # 方形
]

FPS_OPTIONS: List[str] = ["原始帧率", "60", "50", "30", "25", "24", "15", "12", "10"]

VIDEO_BITRATE_OPTIONS: List[str] = [
    "自动",
    "30000k", "20000k", "12000k", "8000k", "6000k",
    "4000k", "2500k", "1500k", "1000k", "800k", "500k", "300k",
]

AUDIO_BITRATE_OPTIONS: List[str] = [
    "自动",
    "320k", "256k", "192k", "160k", "128k", "96k", "64k", "48k",
]

# 编码器 -> 所属硬件加速方案
HARDWARE_ENCODERS: Dict[str, str] = {
    "h264_nvenc": "nvenc",
    "hevc_nvenc": "nvenc",
    "h264_qsv": "qsv",
    "hevc_qsv": "qsv",
    "h264_amf": "amf",
    "hevc_amf": "amf",
}

# 软编编码器 -> 硬件加速方案对应的替换编码器
SOFT_TO_HARD_ENCODER: Dict[str, Dict[str, str]] = {
    "libx264": {"nvenc": "h264_nvenc", "qsv": "h264_qsv", "amf": "h264_amf"},
    "libx265": {"nvenc": "hevc_nvenc", "qsv": "hevc_qsv", "amf": "hevc_amf"},
}


# ----------------------------------------------------------------------
# 三、内置预设（题目要求的 6 种 + 2 种扩展）
# ----------------------------------------------------------------------

PRESETS: Dict[str, Dict[str, Any]] = {
    "通用 1080P": {
        "description": "兼容性最好的通用方案，H.264 + AAC，适合任意设备播放与分享",
        "params": {
            "format": "MP4", "vcodec": "libx264", "resolution": "1920x1080",
            "fps": "原始帧率", "v_bitrate": "自动", "acodec": "aac",
            "a_bitrate": "192k", "hw_accel": False, "extra_args": "",
        },
    },
    "抖音竖屏": {
        "description": "1080x1920 竖屏 + 30fps + 6Mbps，适配抖音/快手/视频号全屏播放",
        "params": {
            "format": "MP4", "vcodec": "libx264", "resolution": "1080x1920",
            "fps": "30", "v_bitrate": "6000k", "acodec": "aac",
            "a_bitrate": "128k", "hw_accel": False, "extra_args": "",
        },
    },
    "微信压缩": {
        "description": "480P + 800kbps，把几十兆视频压到微信可发送的体积",
        "params": {
            "format": "MP4", "vcodec": "libx264", "resolution": "854x480",
            "fps": "24", "v_bitrate": "800k", "acodec": "aac",
            "a_bitrate": "64k", "hw_accel": False, "extra_args": "",
        },
    },
    "无损转码": {
        "description": "仅重新封装容器（视频/音频流直接复制），画质零损失、速度极快",
        "params": {
            "format": "MKV", "vcodec": "copy", "resolution": "原始分辨率",
            "fps": "原始帧率", "v_bitrate": "自动", "acodec": "copy",
            "a_bitrate": "自动", "hw_accel": False, "extra_args": "",
        },
    },
    "纯音频提取": {
        "description": "只保留音轨并转成 MP3（192kbps），可用于提取背景音乐、人声",
        "params": {
            "format": "MP3", "vcodec": "copy", "resolution": "原始分辨率",
            "fps": "原始帧率", "v_bitrate": "自动", "acodec": "libmp3lame",
            "a_bitrate": "192k", "hw_accel": False, "extra_args": "",
        },
    },
    "GIF 动图": {
        "description": "12fps / 宽 480px 的高质量 GIF（自动调色板），适合聊天表情与文档演示",
        "params": {
            "format": "GIF", "vcodec": "copy", "resolution": "480x-1",
            "fps": "12", "v_bitrate": "自动", "acodec": "copy",
            "a_bitrate": "自动", "hw_accel": False, "extra_args": "-loop 0",
        },
    },
    "H.265 小体积": {
        "description": "H.265 编码，同画质下体积比 H.264 小约 30%～50%",
        "params": {
            "format": "MP4", "vcodec": "libx265", "resolution": "1920x1080",
            "fps": "原始帧率", "v_bitrate": "自动", "acodec": "aac",
            "a_bitrate": "128k", "hw_accel": False, "extra_args": "",
        },
    },
    "硬件加速极速转码": {
        "description": "自动选用本机显卡（NVIDIA / Intel / AMD）进行硬件编码，速度最快",
        "params": {
            "format": "MP4", "vcodec": "libx264", "resolution": "原始分辨率",
            "fps": "原始帧率", "v_bitrate": "6000k", "acodec": "aac",
            "a_bitrate": "128k", "hw_accel": True, "extra_args": "",
        },
    },
}

PRESET_NAMES: List[str] = list(PRESETS.keys())

# ----------------------------------------------------------------------
# 四、大模型厂商档案
# ----------------------------------------------------------------------

LLM_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "doubao": {
        "label": "豆包（火山方舟）",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "",
        "env_key": "DOUBAO_API_KEY",
        "env_base": "DOUBAO_BASE_URL",
        "env_model": "DOUBAO_MODEL",
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "env_key": "DEEPSEEK_API_KEY",
        "env_base": "DEEPSEEK_BASE_URL",
        "env_model": "DEEPSEEK_MODEL",
    },
    "qwen": {
        "label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "env_key": "QWEN_API_KEY",
        "env_base": "QWEN_BASE_URL",
        "env_model": "QWEN_MODEL",
    },
}

PROVIDER_KEYS: List[str] = list(LLM_PROVIDERS.keys())

# 大模型请求参数
LLM_TIMEOUT = 30          # 单次请求超时（秒）
LLM_MAX_RETRY = 2         # 失败重试次数
LLM_TEMPERATURE = 0.1     # 参数解析需要极高的确定性，温度取低值


# ----------------------------------------------------------------------
# 五、配置持久化
# ----------------------------------------------------------------------

DEFAULT_SETTINGS: Dict[str, Any] = {
    "output_dir": str(
        Path(os.getenv("DEFAULT_OUTPUT_DIR") or "").expanduser()
        if os.getenv("DEFAULT_OUTPUT_DIR")
        else (Path.home() / "Videos" / "VideoConverterAI_Output")
    ),
    "llm_provider": "",
    "api_key": "",
    "api_base_url": "",
    "api_model": "",
    "max_concurrent": 1,
    "hw_accel_enabled": False,
    "notification_enabled": True,
    "open_dir_after_finish": False,
    "last_preset": "通用 1080P",
    "last_input_dir": "",
    "last_format": "MP4",
    "window_size": [1280, 860],
    "log_level": "INFO",
}


class AppSettings:
    """轻量级配置管理类：读写本地 JSON，提供默认值兜底。"""

    def __init__(self, config_file: Path = CONFIG_FILE) -> None:
        self._config_file = config_file
        self._data: Dict[str, Any] = dict(DEFAULT_SETTINGS)
        self.load()

    # ---------------- 基础读写 ----------------
    def load(self) -> None:
        """从磁盘读取配置；文件损坏时静默回退到默认值，保证程序可启动。"""
        if not self._config_file.exists():
            return
        try:
            with open(self._config_file, "r", encoding="utf-8") as fp:
                loaded = json.load(fp)
            if isinstance(loaded, dict):
                self._data.update(loaded)
        except (json.JSONDecodeError, OSError):
            # 配置文件损坏不应阻塞程序启动
            pass

    def save(self) -> None:
        """把当前配置写回磁盘。"""
        try:
            self._config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._config_file, "w", encoding="utf-8") as fp:
                json.dump(self._data, fp, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._data:
            return self._data[key]
        return DEFAULT_SETTINGS.get(key, default)

    def set(self, key: str, value: Any, auto_save: bool = True) -> None:
        self._data[key] = value
        if auto_save:
            self.save()

    def update(self, values: Dict[str, Any], auto_save: bool = True) -> None:
        self._data.update(values)
        if auto_save:
            self.save()

    def reset(self) -> None:
        self._data = dict(DEFAULT_SETTINGS)
        self.save()

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    # ---------------- 便捷属性 ----------------
    @property
    def output_dir(self) -> str:
        """输出目录（自带记忆能力，保证目录真实存在）。"""
        path = Path(self.get("output_dir"))
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            fallback = Path(DEFAULT_SETTINGS["output_dir"])
            fallback.mkdir(parents=True, exist_ok=True)
            path = fallback
        return str(path)

    @output_dir.setter
    def output_dir(self, value: str) -> None:
        self.set("output_dir", value)

    def output_dir_for(self, value: str) -> None:
        """显式保存输出目录（供 UI 在用户选择目录后调用）。"""
        self.output_dir = value


# 全局单例：其他模块统一 `from config.settings import settings`
settings = AppSettings()


# ----------------------------------------------------------------------
# 六、环境与可执行文件探测
# ----------------------------------------------------------------------

_binary_cache: Dict[str, Optional[str]] = {}


def reset_binary_cache() -> None:
    """清空可执行文件路径缓存（用户在设置中手动指定 ffmpeg 路径后需调用）。"""
    _binary_cache.clear()


def find_executable(name: str) -> Optional[str]:
    """
    在系统中查找可执行文件路径，查找顺序：
    1. 显式环境变量（FFMPEG_PATH / FFPROBE_PATH）
    2. 系统 PATH（shutil.which）
    3. Windows 常见安装目录
    """
    if name in _binary_cache:
        return _binary_cache[name]

    env_value = os.getenv(f"{name.upper()}_PATH")
    if env_value and Path(env_value).is_file():
        _binary_cache[name] = env_value
        return env_value

    found = shutil.which(name)
    if found:
        _binary_cache[name] = found
        return found

    if os.name == "nt":
        candidates = [
            Path(r"C:\ffmpeg\bin") / f"{name}.exe",
            Path(r"C:\Program Files\ffmpeg\bin") / f"{name}.exe",
            Path(r"C:\Program Files (x86)\ffmpeg\bin") / f"{name}.exe",
            Path(os.getenv("LOCALAPPDATA", "") or ".") / "ffmpeg" / "bin" / f"{name}.exe",
            Path("D:/ffmpeg/bin") / f"{name}.exe",
            Path("E:/ffmpeg/bin") / f"{name}.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                _binary_cache[name] = str(candidate)
                return str(candidate)

    _binary_cache[name] = None
    return None


def get_ffmpeg_path() -> Optional[str]:
    return find_executable("ffmpeg")


def get_ffprobe_path() -> Optional[str]:
    return find_executable("ffprobe")


@dataclass
class EnvironmentStatus:
    """运行环境检测结果。"""

    ffmpeg_path: Optional[str] = None
    ffprobe_path: Optional[str] = None
    ffmpeg_version: str = ""
    available: bool = False
    message: str = ""
    problems: List[str] = field(default_factory=list)


INSTALL_GUIDE = """\
本程序依赖 ffmpeg / ffprobe 命令行工具完成视频处理，检测到本机尚未安装。

【Windows 安装方式】
方式一（推荐）：winget 一键安装
    winget install Gyan.FFmpeg
方式二：Scoop / Chocolatey
    scoop install ffmpeg
    choco install ffmpeg-full
方式三：手动安装
    1. 打开 https://www.gyan.dev/ffmpeg/builds/ 下载 ffmpeg-release-essentials.zip
    2. 解压到 C:\\ffmpeg
    3. 把 C:\\ffmpeg\\bin 添加到系统环境变量 Path 中
    4. 重新打开本程序（或重启终端使 PATH 生效）

【macOS 安装方式】
    brew install ffmpeg

【Linux 安装方式】
    sudo apt install ffmpeg        # Debian / Ubuntu
    sudo dnf install ffmpeg        # Fedora

【验证安装是否成功】
    ffmpeg -version
    ffprobe -version

【免安装方案】
    如果不想配置 PATH，也可以在程序“设置 - 高级”中直接指定
    ffmpeg.exe / ffprobe.exe 的绝对路径。
"""


def get_ffmpeg_version(ffmpeg_path: Optional[str] = None) -> str:
    """执行 `ffmpeg -version` 取首行版本信息。"""
    path = ffmpeg_path or get_ffmpeg_path()
    if not path:
        return ""
    try:
        completed = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        first_line = (completed.stdout or completed.stderr or "").strip().splitlines()
        return first_line[0] if first_line else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def check_environment() -> EnvironmentStatus:
    """启动时调用：检测 ffmpeg / ffprobe 是否可用。"""
    status = EnvironmentStatus(ffmpeg_path=get_ffmpeg_path(), ffprobe_path=get_ffprobe_path())

    if not status.ffmpeg_path:
        status.problems.append("未检测到 ffmpeg")
    if not status.ffprobe_path:
        status.problems.append("未检测到 ffprobe")

    if status.ffmpeg_path and status.ffprobe_path:
        status.ffmpeg_version = get_ffmpeg_version(status.ffmpeg_path)
        status.available = True
        status.message = f"环境检测通过：{status.ffmpeg_version or 'ffmpeg 已就绪'}"
    else:
        status.available = False
        status.message = "环境检测未通过：" + "、".join(status.problems)

    return status


def get_system_info() -> Dict[str, str]:
    """收集当前系统信息（用于日志与 SESSION_SUMMARY 生成）。"""
    return {
        "platform": sys.platform,
        "os_name": os.name,
        "python_version": sys.version.split()[0],
        "python_executable": sys.executable,
        "ffmpeg_path": get_ffmpeg_path() or "未找到",
        "ffprobe_path": get_ffprobe_path() or "未找到",
        "ffmpeg_version": get_ffmpeg_version() or "未知",
        "data_dir": str(DATA_DIR),
        "config_file": str(CONFIG_FILE),
    }
