"""
通用后台线程组件
================

把「耗时的核心逻辑」包装成 QThread，避免阻塞 Qt 主线程导致界面卡死。

- ProbeWorker      ：调用 ffprobe 解析视频信息
- ThumbnailWorker  ：生成视频缩略图
- InterpretWorker  ：调用大模型 / 本地规则解析自然语言需求

三个线程都遵循同一约定：成功发 `succeeded`，失败发 `failed`，
且**绝不允许未捕获异常逃逸**（QThread 内抛异常会直接终止整个进程）。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal

from ai.param_interpreter import InterpretResult, ParamInterpreter
from core.ffprobe_parser import FFProbeError, VideoInfo, parse_video
from core.thumbnail import generate_thumbnail
from utils.logger import get_logger

logger = get_logger("ui.workers")


class ProbeWorker(QThread):
    """异步解析视频元数据。"""

    succeeded = Signal(object)   # VideoInfo
    failed = Signal(str)         # 中文错误提示

    def __init__(self, video_path: str, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._video_path = video_path

    def run(self) -> None:
        try:
            info = parse_video(self._video_path)
        except FFProbeError as exc:
            logger.warning("视频解析失败：%s", exc)
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("视频解析出现未知异常")
            self.failed.emit(f"【解析失败】读取视频信息时出现异常：{exc}")
        else:
            self.succeeded.emit(info)


class ThumbnailWorker(QThread):
    """异步生成缩略图。"""

    succeeded = Signal(str)      # 缩略图路径
    failed = Signal(str)

    def __init__(self, video_path: str, duration: float = 0.0, force: bool = False,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._video_path = video_path
        self._duration = duration
        self._force = force

    def run(self) -> None:
        try:
            path = generate_thumbnail(
                self._video_path, duration=self._duration, force=self._force
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("缩略图生成出现未知异常")
            self.failed.emit(f"缩略图生成失败：{exc}")
        else:
            if path:
                self.succeeded.emit(path)
            else:
                self.failed.emit("未能从视频中提取到可用画面")


class InterpretWorker(QThread):
    """
    异步执行 AI 参数解析或智能推荐。

    :param mode: "interpret"（自然语言解析）或 "recommend"（智能参数推荐）
    """

    succeeded = Signal(object)   # InterpretResult
    failed = Signal(str)

    def __init__(
        self,
        interpreter: ParamInterpreter,
        mode: str = "interpret",
        text: str = "",
        video_info: Optional[VideoInfo] = None,
        target_format: Optional[str] = None,
        use_llm: bool = False,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._interpreter = interpreter
        self._mode = mode
        self._text = text
        self._video_info = video_info
        self._target_format = target_format
        self._use_llm = use_llm

    def run(self) -> None:
        try:
            if self._mode == "recommend":
                if self._video_info is None:
                    self.failed.emit("请先加载视频后再使用智能参数推荐")
                    return
                result: InterpretResult = self._interpreter.recommend(
                    self._video_info, target_format=self._target_format, use_llm=self._use_llm
                )
            else:
                result = self._interpreter.interpret(self._text, self._video_info)
        except Exception as exc:  # noqa: BLE001
            logger.exception("AI 解析线程出现未知异常")
            self.failed.emit(f"AI 解析失败：{exc}")
        else:
            self.succeeded.emit(result)
