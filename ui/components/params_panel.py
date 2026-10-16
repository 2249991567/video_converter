"""
输出参数配置面板
================

负责收集用户设置的转码参数，并生成 core 层的 `OutputParams` 对象。

界面包含：预设选择、输出格式、视频编码/分辨率/帧率/码率、音频编码/码率、
硬件加速开关、附加 ffmpeg 参数、输出目录（带记忆）。

所有修改都会通过 `paramsChanged` 信号广播出去（内部程序化赋值时用 `_loading`
标志抑制信号，避免 AI 填充参数时触发无意义的连锁刷新）。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config.settings import (
    AUDIO_BITRATE_OPTIONS,
    AUDIO_CODECS,
    FPS_OPTIONS,
    OUTPUT_FORMATS,
    OUTPUT_FORMAT_KEYS,
    PRESETS,
    PRESET_NAMES,
    RESOLUTIONS,
    VIDEO_BITRATE_OPTIONS,
    VIDEO_CODECS,
    settings,
)
from core.ffmpeg_engine import HardwareInfo, OutputParams
from utils.logger import get_logger

logger = get_logger("ui.params")


class ParamsPanel(QGroupBox):
    """转码参数配置面板。"""

    paramsChanged = Signal(object)        # OutputParams
    presetChanged = Signal(str)           # 预设名称
    outputDirChanged = Signal(str)        # 输出目录
    hardwareToggled = Signal(bool)        # 硬件加速开关

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("② 输出参数", parent)
        self._loading = False                 # 程序化赋值期间抑制信号
        self._hw_info: Optional[HardwareInfo] = None

        self._build_ui()
        self.set_output_dir(settings.output_dir)
        self.apply_preset(settings.get("last_preset", "通用 1080P"), emit=False)

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 14, 12, 12)
        root.setSpacing(10)

        # ---- 预设行 ----
        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        preset_row.addWidget(QLabel("常用预设：", self))
        self.preset_combo = QComboBox(self)
        self.preset_combo.addItems(PRESET_NAMES)
        self.preset_combo.setMinimumWidth(150)
        self.preset_combo.currentTextChanged.connect(self._on_preset_changed)
        preset_row.addWidget(self.preset_combo)
        self.preset_desc = QLabel("", self)
        self.preset_desc.setStyleSheet("color: #7a8699; font-size: 11px;")
        self.preset_desc.setWordWrap(True)
        preset_row.addWidget(self.preset_desc, 1)
        root.addLayout(preset_row)

        # ---- 参数表格（两列布局，节省纵向空间） ----
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        self.format_combo = QComboBox(self)
        self.format_combo.addItems(OUTPUT_FORMAT_KEYS)
        self.format_combo.currentTextChanged.connect(self._on_format_changed)

        self.vcodec_combo = QComboBox(self)
        self.vcodec_combo.addItems(VIDEO_CODECS)

        self.resolution_combo = QComboBox(self)
        self.resolution_combo.setEditable(True)
        self.resolution_combo.addItems(RESOLUTIONS)
        self.resolution_combo.setInsertPolicy(QComboBox.NoInsert)

        self.fps_combo = QComboBox(self)
        self.fps_combo.setEditable(True)
        self.fps_combo.addItems(FPS_OPTIONS)
        self.fps_combo.setInsertPolicy(QComboBox.NoInsert)

        self.vbitrate_combo = QComboBox(self)
        self.vbitrate_combo.setEditable(True)
        self.vbitrate_combo.addItems(VIDEO_BITRATE_OPTIONS)
        self.vbitrate_combo.setInsertPolicy(QComboBox.NoInsert)

        self.acodec_combo = QComboBox(self)
        self.acodec_combo.addItems(AUDIO_CODECS)

        self.abitrate_combo = QComboBox(self)
        self.abitrate_combo.setEditable(True)
        self.abitrate_combo.addItems(AUDIO_BITRATE_OPTIONS)
        self.abitrate_combo.setInsertPolicy(QComboBox.NoInsert)

        self.extra_edit = QLineEdit(self)
        self.extra_edit.setPlaceholderText("附加 ffmpeg 参数，如 -crf 18 -t 10（可留空）")

        grid.addWidget(QLabel("输出格式："), 0, 0, Qt.AlignRight)
        grid.addWidget(self.format_combo, 0, 1)
        grid.addWidget(QLabel("视频编码："), 0, 2, Qt.AlignRight)
        grid.addWidget(self.vcodec_combo, 0, 3)
        grid.addWidget(QLabel("分辨率："), 1, 0, Qt.AlignRight)
        grid.addWidget(self.resolution_combo, 1, 1)
        grid.addWidget(QLabel("帧率："), 1, 2, Qt.AlignRight)
        grid.addWidget(self.fps_combo, 1, 3)
        grid.addWidget(QLabel("视频码率："), 2, 0, Qt.AlignRight)
        grid.addWidget(self.vbitrate_combo, 2, 1)
        grid.addWidget(QLabel("音频编码："), 2, 2, Qt.AlignRight)
        grid.addWidget(self.acodec_combo, 2, 3)
        grid.addWidget(QLabel("音频码率："), 3, 0, Qt.AlignRight)
        grid.addWidget(self.abitrate_combo, 3, 1)
        grid.addWidget(QLabel("附加参数："), 3, 2, Qt.AlignRight)
        grid.addWidget(self.extra_edit, 3, 3)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        root.addLayout(grid)

        # ---- 硬件加速 ----
        hw_row = QHBoxLayout()
        hw_row.setSpacing(8)
        self.hw_check = QCheckBox("启用硬件加速（自动选用本机显卡编码）", self)
        self.hw_check.toggled.connect(self._on_hardware_toggled)
        hw_row.addWidget(self.hw_check)
        self.hw_label = QLabel("正在检测显卡…", self)
        self.hw_label.setStyleSheet("color: #7a8699; font-size: 11px;")
        self.hw_label.setWordWrap(True)
        hw_row.addWidget(self.hw_label, 1)
        root.addLayout(hw_row)

        # ---- 输出目录 ----
        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        dir_row.addWidget(QLabel("输出目录：", self))
        self.output_edit = QLineEdit(self)
        self.output_edit.setReadOnly(True)
        self.output_edit.setPlaceholderText("选择视频输出保存位置")
        dir_row.addWidget(self.output_edit, 1)
        self.browse_dir_button = QPushButton("浏览…", self)
        self.browse_dir_button.clicked.connect(self._choose_output_dir)
        dir_row.addWidget(self.browse_dir_button)
        self.open_dir_button = QPushButton("打开目录", self)
        self.open_dir_button.clicked.connect(
            lambda: self._open_dir_clicked(self.output_edit.text().strip())
        )
        dir_row.addWidget(self.open_dir_button)
        root.addLayout(dir_row)

        # ---- 统一绑定变更信号（放在最后，避免初始化时提前触发） ----
        for combo in (
            self.vcodec_combo, self.resolution_combo, self.fps_combo,
            self.vbitrate_combo, self.acodec_combo, self.abitrate_combo,
        ):
            combo.currentTextChanged.connect(self._on_any_changed)
        self.extra_edit.textChanged.connect(self._on_any_changed)

        self._open_dir_requested = None  # 由主窗口注入回调（见 set_open_dir_callback）

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def set_open_dir_callback(self, callback) -> None:
        """由主窗口注入「打开目录」回调，避免组件直接依赖主窗口。"""
        self._open_dir_requested = callback

    def current_params(self) -> OutputParams:
        """读取界面上的全部参数。"""
        return OutputParams(
            format=self.format_combo.currentText().strip() or "MP4",
            vcodec=self.vcodec_combo.currentText().strip() or "libx264",
            resolution=self.resolution_combo.currentText().strip() or "原始分辨率",
            fps=self.fps_combo.currentText().strip() or "原始帧率",
            v_bitrate=self.vbitrate_combo.currentText().strip() or "自动",
            acodec=self.acodec_combo.currentText().strip() or "aac",
            a_bitrate=self.abitrate_combo.currentText().strip() or "192k",
            hw_accel=self.hw_check.isChecked(),
            extra_args=self.extra_edit.text().strip(),
        )

    def apply_params(self, params: OutputParams, preset_name: Optional[str] = None,
                     emit: bool = True) -> None:
        """把参数对象写回界面控件。"""
        self._loading = True
        try:
            self._set_combo_text(self.format_combo, params.format, "MP4")
            self._set_combo_text(self.vcodec_combo, params.vcodec, "libx264")
            self._set_combo_text(self.resolution_combo, params.resolution, "原始分辨率")
            self._set_combo_text(self.fps_combo, params.fps, "原始帧率")
            self._set_combo_text(self.vbitrate_combo, params.v_bitrate, "自动")
            self._set_combo_text(self.acodec_combo, params.acodec, "aac")
            self._set_combo_text(self.abitrate_combo, params.a_bitrate, "192k")
            self.extra_edit.setText(params.extra_args or "")
            self.hw_check.setChecked(bool(params.hw_accel) and self._hardware_available())
            if preset_name and preset_name in PRESETS:
                self.preset_combo.setCurrentText(preset_name)
            self._update_widget_state()
        finally:
            self._loading = False
        if emit:
            self.paramsChanged.emit(self.current_params())

    def apply_preset(self, name: str, emit: bool = True) -> None:
        """套用内置预设。"""
        preset = PRESETS.get(name)
        if not preset:
            logger.warning("未知预设：%s", name)
            return
        self.apply_params(OutputParams.from_dict(preset.get("params")), preset_name=name, emit=emit)
        settings.set("last_preset", name)

    def set_output_dir(self, path: str) -> None:
        """设置输出目录（会自动记忆到本地配置）。"""
        if not path:
            return
        self.output_edit.setText(path)
        settings.output_dir = path
        if not self._loading:
            self.outputDirChanged.emit(path)

    def output_dir(self) -> str:
        return self.output_edit.text().strip() or settings.output_dir

    def set_hw_info(self, info: HardwareInfo) -> None:
        """
        展示硬件加速探测结果，并据此启用/禁用开关。
        未检测到可用硬件编码器时必须禁用开关，避免用户选了一个跑不通的编码器。
        """
        self._hw_info = info
        available = bool(info and info.available)
        self._loading = True
        try:
            self.hw_check.setEnabled(available)
            if not available:
                self.hw_check.setChecked(False)
            elif not self.hw_check.isChecked():
                # 恢复上次保存的开关状态（首次使用默认关闭）
                self.hw_check.setChecked(bool(settings.get("hw_accel_enabled", False)))
        finally:
            self._loading = False

        if available:
            self.hw_label.setText(f"已检测到 {info.label}；{info.detail}")
            self.hw_label.setStyleSheet("color: #2e7d32; font-size: 11px;")
        else:
            detail = info.detail if info else ""
            self.hw_label.setText(f"未检测到可用硬件编码器，已禁用硬件加速。{detail}")
            self.hw_label.setStyleSheet("color: #8a94a6; font-size: 11px;")

    def set_hw_checkbox(self, checked: bool) -> None:
        """由主窗口在 AI 推荐后同步勾选状态。"""
        self._loading = True
        try:
            self.hw_check.setChecked(bool(checked) and self._hardware_available())
        finally:
            self._loading = False

    # ------------------------------------------------------------------
    # 内部逻辑
    # ------------------------------------------------------------------
    def _hardware_available(self) -> bool:
        return bool(self._hw_info and self._hw_info.available)

    @staticmethod
    def _set_combo_text(combo: QComboBox, value: str, fallback: str) -> None:
        """把值写入下拉框：精确匹配优先，可编辑下拉框允许自定义文本。"""
        text = str(value or "").strip()
        index = combo.findText(text)
        if index >= 0:
            combo.setCurrentIndex(index)
            return
        if combo.isEditable():
            combo.setCurrentText(text or fallback)
            return
        fallback_index = combo.findText(fallback)
        combo.setCurrentIndex(fallback_index if fallback_index >= 0 else 0)

    def _on_any_changed(self, *_args) -> None:
        if self._loading:
            return
        self.paramsChanged.emit(self.current_params())

    def _on_preset_changed(self, name: str) -> None:
        preset = PRESETS.get(name)
        if preset:
            self.preset_desc.setText(preset.get("description", ""))
        if self._loading:
            return
        self.presetChanged.emit(name)
        self.apply_preset(name)

    def _on_format_changed(self, format_key: str) -> None:
        """切换输出格式时，自动套用该容器的默认编码组合。"""
        profile = OUTPUT_FORMATS.get(format_key)
        if profile is None:
            return
        self._update_widget_state()
        if self._loading:
            return
        self._loading = True
        try:
            if profile.get("audio_only"):
                self.vcodec_combo.setCurrentText("copy")
                self.resolution_combo.setCurrentText("原始分辨率")
                self.fps_combo.setCurrentText("原始帧率")
                self.vbitrate_combo.setCurrentText("自动")
                self.acodec_combo.setCurrentText(profile.get("acodec") or "libmp3lame")
                if self.abitrate_combo.currentText().strip() in ("", "自动"):
                    self.abitrate_combo.setCurrentText("192k")
            elif profile.get("gif"):
                self.fps_combo.setCurrentText("12")
                self.resolution_combo.setCurrentText("480x-1")
                self.extra_edit.setText("-loop 0")
            else:
                self.vcodec_combo.setCurrentText(profile.get("vcodec") or "libx264")
                self.acodec_combo.setCurrentText(profile.get("acodec") or "aac")
        finally:
            self._loading = False
        self.paramsChanged.emit(self.current_params())

    def _update_widget_state(self) -> None:
        """按目标格式禁用无意义的参数控件，降低用户误操作概率。"""
        profile = OUTPUT_FORMATS.get(self.format_combo.currentText().strip(), OUTPUT_FORMATS["MP4"])
        audio_only = bool(profile.get("audio_only"))
        is_gif = bool(profile.get("gif"))

        for widget in (self.vcodec_combo,):
            widget.setEnabled(not audio_only and not is_gif)
        for widget in (self.resolution_combo, self.fps_combo):
            widget.setEnabled(not audio_only)
        for widget in (self.vbitrate_combo,):
            widget.setEnabled(not audio_only and not is_gif)
        self.acodec_combo.setEnabled(not audio_only and not is_gif)

    def _on_hardware_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        if checked and not self._hardware_available():
            self._loading = True
            self.hw_check.setChecked(False)
            self._loading = False
            return
        settings.set("hw_accel_enabled", checked)
        self.hardwareToggled.emit(checked)
        self.paramsChanged.emit(self.current_params())

    def _choose_output_dir(self) -> None:
        current = self.output_edit.text().strip() or settings.output_dir
        chosen = QFileDialog.getExistingDirectory(self, "选择输出目录", current)
        if chosen:
            self.set_output_dir(chosen)
            logger.info("输出目录已更新：%s", chosen)

    def _open_dir_clicked(self, path: str) -> None:
        if self._open_dir_requested is not None:
            self._open_dir_requested(path)
