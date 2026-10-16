"""
环境检测对话框
==============

启动时若未检测到 ffmpeg / ffprobe，弹出本对话框给出友好提示与分平台的安装指引，
并提供「手动指定 ffmpeg 路径」与「重新检测」，绝不让程序因为缺少依赖而崩溃退出。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from config.settings import (
    INSTALL_GUIDE,
    EnvironmentStatus,
    check_environment,
    reset_binary_cache,
)
from utils.logger import get_logger

logger = get_logger("ui.env_dialog")


class EnvCheckDialog(QDialog):
    """ffmpeg 环境缺失时的提示与安装指引对话框。"""

    def __init__(self, status: EnvironmentStatus, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("运行环境检测")
        self.setMinimumWidth(620)
        self._status = status
        self.continue_anyway = False

        self._build_ui()
        self._render_status()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        self.headline = QLabel(self)
        self.headline.setWordWrap(True)
        self.headline.setStyleSheet("font-size: 14px; font-weight: 600; color: #c0392b;")
        root.addWidget(self.headline)

        self.detail = QLabel(self)
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet("color: #5b6472;")
        root.addWidget(self.detail)

        self.guide = QTextEdit(self)
        self.guide.setReadOnly(True)
        self.guide.setPlainText(INSTALL_GUIDE)
        self.guide.setStyleSheet("font-family: Consolas, 'Microsoft YaHei', monospace;")
        root.addWidget(self.guide, 1)

        hint = QLabel(
            "说明：本程序的所有视频处理都通过调用系统 ffmpeg 命令行完成，"
            "因此必须先安装 ffmpeg 才能使用转换功能。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #8a94a6; font-size: 11px;")
        root.addWidget(hint)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)

        self.recheck_button = QPushButton("重新检测", self)
        self.recheck_button.clicked.connect(self._on_recheck)
        button_row.addWidget(self.recheck_button)

        self.locate_button = QPushButton("手动指定 ffmpeg 路径…", self)
        self.locate_button.clicked.connect(self._on_locate)
        button_row.addWidget(self.locate_button)

        button_row.addStretch(1)

        self.continue_button = QPushButton("暂时跳过（转换功能不可用）", self)
        self.continue_button.clicked.connect(self._on_continue)
        button_row.addWidget(self.continue_button)

        close_button = QPushButton("退出程序", self)
        close_button.clicked.connect(self._on_quit)
        close_button.setDefault(True)
        button_row.addWidget(close_button)

        root.addLayout(button_row)

    # ------------------------------------------------------------------
    def _render_status(self) -> None:
        """按当前检测结果刷新文案。"""
        status = self._status
        if status.available:
            self.headline.setText("运行环境检测通过")
            self.headline.setStyleSheet("font-size: 14px; font-weight: 600; color: #2e7d32;")
            self.detail.setText(status.message)
            self.guide.setPlainText(
                f"ffmpeg 路径：{status.ffmpeg_path}\n"
                f"ffprobe 路径：{status.ffprobe_path}\n"
                f"版本信息：{status.ffmpeg_version or '未知'}"
            )
            self.recheck_button.setEnabled(False)
            self.locate_button.setEnabled(False)
            self.continue_button.setEnabled(False)
            return

        self.headline.setText("未检测到 ffmpeg / ffprobe")
        self.headline.setStyleSheet("font-size: 14px; font-weight: 600; color: #c0392b;")
        problems = "、".join(status.problems) if status.problems else "ffmpeg 与 ffprobe 均不可用"
        self.detail.setText(
            f"检测结果：{problems}。请按下方指引安装 ffmpeg，安装完成后点击「重新检测」。"
        )

    # ------------------------------------------------------------------
    def _on_recheck(self) -> None:
        reset_binary_cache()
        self._status = check_environment()
        self._render_status()
        if self._status.available:
            QMessageBox.information(self, "检测成功", "已成功检测到 ffmpeg，可以开始使用转换功能了。")

    def _on_locate(self) -> None:
        """手动指定 ffmpeg 可执行文件路径（免去配置系统 PATH 的麻烦）。"""
        file_filter = "ffmpeg 可执行文件 (ffmpeg.exe ffmpeg);;所有文件 (*.*)" if os.name == "nt" else "所有文件 (*.*)"
        chosen, _ = QFileDialog.getOpenFileName(self, "请选择 ffmpeg 可执行文件", "", file_filter)
        if not chosen:
            return

        ffmpeg_path = Path(chosen)
        probe_candidate = ffmpeg_path.with_name("ffprobe" + ffmpeg_path.suffix)
        if not probe_candidate.exists() and os.name == "nt":
            probe_candidate = ffmpeg_path.with_name("ffprobe.exe")
        if not probe_candidate.exists():
            QMessageBox.warning(
                self,
                "缺少 ffprobe",
                "在同目录下没有找到 ffprobe，ffprobe 是解析视频信息所必需的，"
                "请确认选择的是完整 ffmpeg 发行包中的 ffmpeg 可执行文件。",
            )
            return

        # 通过环境变量覆盖路径，并清空缓存后重新检测
        os.environ["FFMPEG_PATH"] = str(ffmpeg_path)
        os.environ["FFPROBE_PATH"] = str(probe_candidate)
        logger.info("用户手动指定 ffmpeg 路径：%s", ffmpeg_path)
        self._on_recheck()

    def _on_continue(self) -> None:
        self.continue_anyway = True
        self.accept()

    def _on_quit(self) -> None:
        self.continue_anyway = False
        self.reject()


def ensure_environment(parent: Optional[QWidget] = None) -> bool:
    """
    启动时的环境守卫：
    - 环境正常 → 直接返回 True，不打扰用户；
    - 环境缺失 → 弹出安装指引对话框，由用户决定是否继续（返回 True 允许继续）。
    """
    status = check_environment()
    if status.available:
        logger.info("环境检测通过：%s", status.ffmpeg_version)
        return True

    logger.warning("环境检测未通过：%s", status.problems)
    dialog = EnvCheckDialog(status, parent)
    dialog.exec()
    return dialog.continue_anyway
