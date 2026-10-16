"""
日志工具模块
============

职责：为整个项目提供统一的日志能力。
- 同时输出到「控制台」与「滚动日志文件」，日志文件位于 ~/.video_converter_ai/logs/app.log
- 全链路记录：用户操作、ffprobe/ffmpeg 命令行、执行耗时、错误堆栈
- 使用 RotatingFileHandler 限制单文件 5MB、最多保留 5 份，避免日志无限膨胀
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from config.settings import LOG_FILE, settings

_LOGGER_NAME = "VideoConverterAI"
_initialized = False

_CONSOLE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
_FILE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(filename)s:%(lineno)d | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: Optional[str] = None, log_file: Path = LOG_FILE) -> logging.Logger:
    """
    初始化根日志器（幂等：重复调用不会重复添加 Handler）。

    :param level: 日志级别字符串，如 "DEBUG" / "INFO"，默认读取配置
    :param log_file: 日志文件路径
    """
    global _initialized
    logger = logging.getLogger(_LOGGER_NAME)
    if _initialized:
        return logger

    level_name = (level or str(settings.get("log_level", "INFO"))).upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False

    formatter_console = logging.Formatter(_CONSOLE_FORMAT, datefmt=_DATE_FORMAT)
    formatter_file = logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT)

    # 1) 控制台输出
    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(formatter_console)
    console_handler.setLevel(logging.INFO)
    logger.addHandler(console_handler)

    # 2) 滚动文件输出
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            filename=str(log_file),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter_file)
        file_handler.setLevel(logging.DEBUG)
        logger.addHandler(file_handler)
    except OSError:
        # 日志文件不可写时仅使用控制台输出
        logger.warning("日志文件无法写入，仅输出到控制台：%s", log_file)

    _initialized = True
    logger.debug("日志系统初始化完成，日志文件：%s", log_file)
    return logger


def get_logger(name: str = "") -> logging.Logger:
    """获取子日志器，例如 get_logger(__name__)、get_logger("core.ffmpeg")。"""
    setup_logging()
    return logging.getLogger(f"{_LOGGER_NAME}.{name}" if name else _LOGGER_NAME)


def format_command(command: Sequence[str]) -> str:
    """
    把命令参数列表还原成可读的命令行字符串（用于日志留痕）。
    含空格或特殊字符的参数会用双引号包裹，便于用户直接复制到终端复现问题。
    """
    parts: List[str] = []
    for item in command:
        text = str(item)
        if text == "" or any(ch in text for ch in ' \t"'):
            parts.append(f'"{text}"')
        else:
            parts.append(text)
    return " ".join(parts)


def log_command(command: Sequence[str], logger: Optional[logging.Logger] = None) -> None:
    """记录一条即将执行的命令行。"""
    (logger or get_logger("subprocess")).info("执行命令: %s", format_command(command))
