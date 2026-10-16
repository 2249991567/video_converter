"""
AI 交互面板
===========

用户在这里用大白话描述需求（例如「转成 1080P 的 MP4，文件控制在 500M 以内」），
点击解析后由主窗口调用大模型把需求翻译成 ffmpeg 参数并回填到参数面板。

面板本身不包含任何网络逻辑，只负责收集输入与展示结果，保持 UI 与业务解耦。
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ai.param_interpreter import EXAMPLE_PROMPTS


class AIPanel(QGroupBox):
    """自然语言描述 + AI 解析的交互面板。"""

    interpretRequested = Signal(str)     # 请求解析描述文本
    recommendRequested = Signal()        # 请求智能参数推荐
    settingsRequested = Signal()         # 请求打开 AI 设置

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("③ AI 智能助手", parent)
        self._build_ui()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 14, 12, 12)
        root.setSpacing(8)

        tip = QLabel(
            "用日常语言描述你的需求，AI 会自动转换成转码参数并填入下方参数栏。"
            "例如：「转成 1080P 的 MP4，文件控制在 500M 以内，适合发朋友圈」",
            self,
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #7a8699; font-size: 11px;")
        root.addWidget(tip)

        # ---- 示例快速填充 ----
        example_row = QHBoxLayout()
        example_row.setSpacing(6)
        example_row.addWidget(QLabel("示例：", self))
        self.example_combo = QComboBox(self)
        self.example_combo.addItem("点击选择一条示例需求…")
        self.example_combo.addItems(EXAMPLE_PROMPTS)
        self.example_combo.currentIndexChanged.connect(self._on_example_selected)
        example_row.addWidget(self.example_combo, 1)
        root.addLayout(example_row)

        # ---- 输入框 ----
        self.input_edit = QPlainTextEdit(self)
        self.input_edit.setPlaceholderText(EXAMPLE_PROMPTS[0])
        self.input_edit.setFixedHeight(72)
        root.addWidget(self.input_edit)

        # ---- 按钮行 ----
        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.interpret_button = QPushButton("AI 解析并填充参数", self)
        self.interpret_button.setDefault(True)
        self.interpret_button.clicked.connect(self._on_interpret_clicked)
        button_row.addWidget(self.interpret_button, 1)

        self.recommend_button = QPushButton("智能推荐参数", self)
        self.recommend_button.clicked.connect(self.recommendRequested.emit)
        self.recommend_button.setToolTip("根据原视频属性与目标格式自动推荐编码与码率组合")
        button_row.addWidget(self.recommend_button, 1)

        self.settings_button = QPushButton("AI 设置", self)
        self.settings_button.clicked.connect(self.settingsRequested.emit)
        button_row.addWidget(self.settings_button)
        root.addLayout(button_row)

        # ---- 结果展示 ----
        self.llm_state_label = QLabel("", self)
        self.llm_state_label.setWordWrap(True)
        self.llm_state_label.setStyleSheet("color: #8a94a6; font-size: 11px;")
        root.addWidget(self.llm_state_label)

        self.result_label = QLabel("", self)
        self.result_label.setWordWrap(True)
        self.result_label.setStyleSheet("color: #7a8699; font-size: 11px;")
        root.addWidget(self.result_label)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def text(self) -> str:
        return self.input_edit.toPlainText().strip()

    def set_text(self, text: str) -> None:
        self.input_edit.setPlainText(text)

    def set_busy(self, busy: bool, message: str = "正在请求 AI 解析，请稍候…") -> None:
        """请求进行中：禁用按钮并给出提示，避免重复点击。"""
        self.interpret_button.setEnabled(not busy)
        self.recommend_button.setEnabled(not busy)
        self.example_combo.setEnabled(not busy)
        if busy:
            self.result_label.setText(message)
            self.result_label.setStyleSheet("color: #1a73e8; font-size: 11px;")

    def set_llm_state(self, configured: bool, provider_label: str = "", model: str = "") -> None:
        """展示当前大模型配置状态。"""
        if configured:
            self.llm_state_label.setText(f"AI 已就绪：{provider_label} / {model}（未配置时自动使用本地规则解析）")
            self.llm_state_label.setStyleSheet("color: #2e7d32; font-size: 11px;")
        else:
            self.llm_state_label.setText(
                "尚未配置大模型 API Key，AI 解析将自动降级为本地规则解析（点击右侧「AI 设置」可配置）"
            )
            self.llm_state_label.setStyleSheet("color: #e08e0b; font-size: 11px;")

    def set_result(self, explanation: str, ok: bool = True, warnings: Optional[List[str]] = None) -> None:
        """展示解析结果说明；降级时给出醒目的橙色提示。"""
        text = explanation or ""
        if warnings:
            text = (text + "\n" if text else "") + "；".join(str(item) for item in warnings)
        self.result_label.setText(text)
        if not ok:
            self.result_label.setStyleSheet("color: #e08e0b; font-size: 11px;")
        elif warnings:
            self.result_label.setStyleSheet("color: #1a73e8; font-size: 11px;")
        else:
            self.result_label.setStyleSheet("color: #2e7d32; font-size: 11px;")

    def clear_result(self) -> None:
        self.result_label.setText("")

    # ------------------------------------------------------------------
    # 内部槽函数
    # ------------------------------------------------------------------
    def _on_interpret_clicked(self) -> None:
        self.interpretRequested.emit(self.text())

    def _on_example_selected(self, index: int) -> None:
        if index <= 0:
            return
        self.input_edit.setPlainText(self.example_combo.currentText())
        self.example_combo.blockSignals(True)
        self.example_combo.setCurrentIndex(0)
        self.example_combo.blockSignals(False)
