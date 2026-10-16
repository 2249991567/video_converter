"""
视频信息面板组件
================

左侧展示自动提取的视频中间帧缩略图（同时作为拖拽导入的放置区），
右侧以中文表单形式展示 ffprobe 解析出的全部元数据。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config.settings import SUPPORTED_EXTENSIONS, VIDEO_EXTENSIONS
from core.ffprobe_parser import VideoInfo
from utils.logger import get_logger

logger = get_logger("ui.video_info")

# 缩略图预览区尺寸
THUMB_WIDTH = 300
THUMB_HEIGHT = 169

# 表单中需要换行显示的字段（内容较长）
_WRAP_KEYS = ("文件名", "文件路径")

# 空状态提示文案
_PLACEHOLDER_TEXT = "拖拽视频到此处\n或点击「选择视频」"


class VideoInfoPanel(QGroupBox):
    """视频信息 + 缩略图预览面板。"""

    filesDropped = Signal(list)      # 拖入的文件路径列表
    browseRequested = Signal()       # 请求打开文件选择对话框
    thumbnailRefreshRequested = Signal()  # 请求重新生成缩略图

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("① 视频信息", parent)
        self._value_labels: Dict[str, QLabel] = {}
        self._thumbnail_path: Optional[str] = None
        self._has_video = False

        self.setAcceptDrops(True)
        self._build_ui()
        self.clear()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 14, 12, 12)
        root.setSpacing(10)

        content = QHBoxLayout()
        content.setSpacing(14)

        # ---- 左：缩略图预览（同时也是拖拽区） ----
        left = QVBoxLayout()
        left.setSpacing(8)

        self.thumbnail_label = QLabel(self)
        self.thumbnail_label.setFixedSize(THUMB_WIDTH, THUMB_HEIGHT)
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setWordWrap(True)
        self.thumbnail_label.setStyleSheet(
            "border: 1px dashed #9aa4b2; border-radius: 6px;"
            "background-color: #f7f9fc; color: #7a8699;"
        )
        left.addWidget(self.thumbnail_label)

        button_row = QHBoxLayout()
        button_row.setSpacing(6)
        self.browse_button = QPushButton("选择视频…", self)
        self.browse_button.clicked.connect(self.browseRequested.emit)
        self.refresh_button = QPushButton("刷新缩略图", self)
        self.refresh_button.clicked.connect(self.thumbnailRefreshRequested.emit)
        self.refresh_button.setEnabled(False)
        button_row.addWidget(self.browse_button)
        button_row.addWidget(self.refresh_button)
        button_row.addStretch(1)
        left.addLayout(button_row)

        hint = QLabel("提示：可直接把视频文件拖拽到左侧预览区", self)
        hint.setStyleSheet("color: #8a94a6; font-size: 11px;")
        hint.setWordWrap(True)
        left.addWidget(hint)
        left.addStretch(1)

        content.addLayout(left, 0)

        # ---- 右：元数据表单 ----
        form_container = QWidget(self)
        self.info_form = QFormLayout(form_container)
        self.info_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.info_form.setFormAlignment(Qt.AlignTop)
        self.info_form.setHorizontalSpacing(12)
        self.info_form.setVerticalSpacing(6)
        self.info_form.setContentsMargins(0, 0, 0, 0)

        # 表单字段固定为 VideoInfo 的标准字段集合，保证顺序稳定不跳动
        for key in VideoInfo().summary_dict():
            value_label = QLabel("—", form_container)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if key in _WRAP_KEYS:
                value_label.setWordWrap(True)
                value_label.setMaximumWidth(430)
            else:
                value_label.setWordWrap(False)
            self._value_labels[key] = value_label
            caption = QLabel(f"{key}：", form_container)
            caption.setStyleSheet("color: #5b6472;")
            self.info_form.addRow(caption, value_label)

        content.addWidget(form_container, 1)
        root.addLayout(content)

        self.status_label = QLabel("尚未加载视频", self)
        self.status_label.setStyleSheet("color: #8a94a6; font-size: 11px;")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def set_info(self, info: VideoInfo) -> None:
        """填充视频信息与缩略图占位。"""
        self._has_video = True
        self.refresh_button.setEnabled(True)
        for key, value_label in self._value_labels.items():
            value_label.setText(info.summary_dict().get(key, "—"))
        self.status_label.setText(
            f"已加载：{info.filename}｜{info.resolution}｜{info.duration_str}"
        )
        if self._thumbnail_path is None:
            self._show_placeholder("正在生成缩略图…")

    def clear(self) -> None:
        """清空面板，回到空状态。"""
        self._has_video = False
        self._thumbnail_path = None
        self.refresh_button.setEnabled(False)
        for value_label in self._value_labels.values():
            value_label.setText("—")
        self.status_label.setText("尚未加载视频")
        self._show_placeholder(_PLACEHOLDER_TEXT)

    def set_loading(self, loading: bool, text: str = "正在解析视频信息…") -> None:
        """加载状态提示。"""
        if loading:
            self.status_label.setText(text)
            self.browse_button.setEnabled(False)
        else:
            self.browse_button.setEnabled(True)

    def set_status(self, text: str, is_error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            "color: #c0392b; font-size: 11px;" if is_error else "color: #8a94a6; font-size: 11px;"
        )

    def set_thumbnail(self, image_path: Optional[str]) -> None:
        """设置缩略图；失败时降级为占位图。"""
        if not image_path or not Path(image_path).exists():
            self._thumbnail_path = None
            self._show_placeholder("缩略图不可用" if self._has_video else _PLACEHOLDER_TEXT)
            return
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            self._thumbnail_path = None
            self._show_placeholder("缩略图不可用")
            return
        self._thumbnail_path = image_path
        scaled = pixmap.scaled(
            THUMB_WIDTH, THUMB_HEIGHT, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        self.thumbnail_label.setPixmap(scaled)
        self.thumbnail_label.setStyleSheet(
            "border: 1px solid #d6dbe3; border-radius: 6px; background-color: #111;"
        )

    def thumbnail_path(self) -> Optional[str]:
        return self._thumbnail_path

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _show_placeholder(self, text: str) -> None:
        """绘制带提示文字的占位图，避免空标签撑不起拖拽区域。"""
        pixmap = QPixmap(THUMB_WIDTH, THUMB_HEIGHT)
        pixmap.fill(QColor("#f2f5f9"))
        painter = QPainter(pixmap)
        painter.setPen(QColor("#8a94a6"))
        font = QFont()
        font.setPointSize(10)
        painter.setFont(font)
        painter.drawText(pixmap.rect(), Qt.AlignCenter | Qt.TextWordWrap, text)
        painter.end()
        self.thumbnail_label.setPixmap(pixmap)
        self.thumbnail_label.setStyleSheet(
            "border: 1px dashed #9aa4b2; border-radius: 6px; background-color: #f7f9fc;"
        )

    # ------------------------------------------------------------------
    # 拖拽事件
    # ------------------------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802  (Qt 命名规范)
        if self._extract_video_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._extract_video_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self._extract_video_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.filesDropped.emit(paths)

    @staticmethod
    def _extract_video_paths(mime_data) -> List[str]:
        """从拖拽数据中筛出受支持的媒体文件。"""
        if not mime_data or not mime_data.hasUrls():
            return []
        paths: List[str] = []
        for url in mime_data.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
                paths.append(str(path))
        return paths

    # ------------------------------------------------------------------
    # 供主窗口调用：打开文件选择对话框
    # ------------------------------------------------------------------
    def pick_file_dialog(self, parent: QWidget, last_dir: str = "") -> List[str]:
        """打开文件选择对话框，返回用户选择的文件列表。"""
        video_filter = "视频文件 (" + " ".join(f"*{ext}" for ext in VIDEO_EXTENSIONS) + ")"
        all_filter = "支持的媒体文件 (" + " ".join(f"*{ext}" for ext in SUPPORTED_EXTENSIONS) + ")"
        files, _ = QFileDialog.getOpenFileNames(
            parent, "选择视频文件", last_dir, f"{video_filter};;{all_filter};;所有文件 (*.*)"
        )
        return files
