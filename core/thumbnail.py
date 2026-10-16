"""
缩略图生成模块
==============

职责：从视频中间位置抽取一帧，生成界面预览用的缩略图。

实现策略（两级容错）：
1. 主方案：用 ffmpeg 命令行 `-ss` 快速定位到视频中间时间点截取一帧（jpg），
   再用 OpenCV 读取、等比缩放、输出为标准 PNG 缓存到本地。
2. 兜底方案：若 ffmpeg 截图失败（例如损坏文件、不支持的编码），
   改用 OpenCV 的 VideoCapture 直接定位并抓取中间帧。

缓存策略：以「文件绝对路径 + 修改时间」的 MD5 作为文件名，
缓存命中时直接复用，避免重复截图拖慢界面。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import cv2

from config.settings import CREATE_NO_WINDOW, THUMBNAIL_DIR, get_ffmpeg_path
from utils.logger import format_command, get_logger

logger = get_logger("core.thumbnail")

# 默认缩略图宽度（像素）与截图超时（秒）
DEFAULT_WIDTH = 480
THUMBNAIL_TIMEOUT = 25


def default_thumbnail_path(video_path: str) -> str:
    """根据「视频绝对路径 + 修改时间」计算缓存文件名（PNG）。"""
    path = Path(video_path)
    try:
        signature = f"{path.resolve()}|{int(path.stat().st_mtime)}|{path.stat().st_size}"
    except OSError:
        signature = str(path)
    digest = hashlib.md5(signature.encode("utf-8")).hexdigest()
    return str(THUMBNAIL_DIR / f"{digest}.png")


def extract_frame(video_path: str, at_second: float, output_path: str) -> bool:
    """
    用 ffmpeg 命令行在指定时间点截取一帧图片。

    :return: 是否成功产出图片文件
    """
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        logger.warning("未找到 ffmpeg，无法抽取缩略图帧")
        return False

    command = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-ss", f"{max(0.0, at_second):.3f}",   # 放在 -i 之前，使用快速定位
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "3",
        output_path,
    ]
    logger.debug("抽取缩略图帧: %s", format_command(command))
    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=THUMBNAIL_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffmpeg 抽取帧失败：%s", exc)
        return False

    frame_file = Path(output_path)
    return frame_file.exists() and frame_file.stat().st_size > 0


def _grab_frame_with_cv2(video_path: str, at_second: float):
    """兜底方案：用 OpenCV 读取指定时间点的帧（失败时回退到第一帧）。"""
    capture = cv2.VideoCapture(video_path)
    if not capture.isOpened():
        logger.warning("OpenCV 无法打开视频：%s", video_path)
        return None
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, at_second) * 1000.0)
        ok, frame = capture.read()
        if not ok:
            capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = capture.read()
        return frame if ok else None
    finally:
        capture.release()


def _resize_keep_ratio(image, target_width: int):
    """等比缩放到目标宽度（宽度足够小则保持原样，高度取偶数以适配常见编码器）。"""
    height, width = image.shape[:2]
    if width <= 0 or height <= 0:
        return image
    if width <= target_width:
        return image
    ratio = target_width / float(width)
    new_height = max(2, int(round(height * ratio)))
    if new_height % 2 != 0:
        new_height += 1
    return cv2.resize(image, (target_width, new_height), interpolation=cv2.INTER_AREA)


def generate_thumbnail(
    video_path: str,
    duration: float = 0.0,
    width: int = DEFAULT_WIDTH,
    output_path: Optional[str] = None,
    force: bool = False,
) -> Optional[str]:
    """
    生成视频缩略图。

    :param video_path: 源视频路径
    :param duration: 视频时长（秒），用于定位中间帧；为 0 时截取第 1 秒
    :param width: 缩略图目标宽度
    :param output_path: 自定义输出路径，默认走缓存目录
    :param force: 强制重新生成（忽略缓存）
    :return: 缩略图路径；失败返回 None（UI 应降级为占位图，不得崩溃）
    """
    source = Path(video_path)
    if not source.exists():
        logger.warning("缩略图跳过：文件不存在 %s", video_path)
        return None

    target = Path(output_path) if output_path else Path(default_thumbnail_path(str(source)))

    # 缓存命中判断：缓存文件存在且不早于视频文件的修改时间
    if not force and target.exists():
        try:
            if target.stat().st_mtime >= source.stat().st_mtime and target.stat().st_size > 0:
                logger.debug("命中缩略图缓存：%s", target)
                return str(target)
        except OSError:
            pass

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.warning("缩略图目录不可写：%s", target.parent)
        return None

    # 定位到视频中间位置（无法获知时长时取第 1 秒，比第 0 帧更有代表性）
    at_second = duration * 0.5 if duration > 0 else 1.0

    temp_fd, temp_name = tempfile.mkstemp(suffix=".jpg", dir=str(THUMBNAIL_DIR))
    os.close(temp_fd)
    temp_frame = Path(temp_name)

    image = None
    try:
        # 一级方案：ffmpeg 命令行截帧
        if extract_frame(str(source), at_second, str(temp_frame)):
            image = cv2.imread(str(temp_frame))
        # 二级方案：OpenCV 直接抓帧
        if image is None:
            logger.info("改用 OpenCV 抽取缩略图帧：%s", source.name)
            image = _grab_frame_with_cv2(str(source), at_second)
        if image is None:
            logger.error("缩略图生成失败（两种方案均未取到帧）：%s", video_path)
            return None

        normalized = _resize_keep_ratio(image, width)
        if not cv2.imwrite(str(target), normalized):
            logger.error("缩略图写入失败：%s", target)
            return None
        logger.info("缩略图已生成：%s", target)
        return str(target)
    except Exception as exc:  # noqa: BLE001  缩略图失败绝不能影响主流程
        logger.exception("缩略图生成异常：%s", exc)
        return None
    finally:
        try:
            if temp_frame.exists():
                temp_frame.unlink()
        except OSError:
            pass


def clear_thumbnail_cache() -> int:
    """清空缩略图缓存，返回删除的文件数量。"""
    removed = 0
    try:
        for item in THUMBNAIL_DIR.glob("*"):
            if item.is_file():
                try:
                    item.unlink()
                    removed += 1
                except OSError:
                    continue
    except OSError as exc:
        logger.warning("清理缩略图缓存失败：%s", exc)
    logger.info("已清理 %d 个缩略图缓存文件", removed)
    return removed
