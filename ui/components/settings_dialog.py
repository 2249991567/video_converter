"""
设置对话框
==========

集中管理「大模型接入配置」与「运行偏好」：
- AI 设置：厂商、API Key、Base URL、模型 ID，支持一键测试连接
- 高级设置：ffmpeg 路径查看 / 手动指定、队列并发数、
            完成后是否自动打开输出目录、是否发送系统通知、日志与缓存管理

保存后写入本地 settings.json，实现配置持久化（下次启动自动生效）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ai.llm_client import LLMClient, LLMConfig
from config.settings import (
    LOG_FILE,
    LLM_PROVIDERS,
    PROVIDER_KEYS,
    get_ffmpeg_path,
    get_ffprobe_path,
    reset_binary_cache,
    settings,
)
from core.thumbnail import clear_thumbnail_cache
from utils.logger import get_logger

logger = get_logger("ui.settings")


class SettingsDialog(QDialog):
    """应用设置对话框。"""

    settingsChanged = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)
        self._build_ui()
        self._load_settings()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(12)

        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_ai_tab(), "AI 设置")
        self.tabs.addTab(self._build_advanced_tab(), "高级设置")
        root.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel, parent=self
        )
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ---------------- AI 设置页 ----------------
    def _build_ai_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 14, 10, 10)
        layout.setSpacing(10)

        tip = QLabel(
            "本程序通过 OpenAI 兼容协议调用大模型，可对接豆包（火山方舟）、DeepSeek、"
            "通义千问。API Key 仅保存在本机配置文件与内存中，不会上传到任何第三方服务。",
            page,
        )
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #7a8699; font-size: 11px;")
        layout.addWidget(tip)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.provider_combo = QComboBox(page)
        for key in PROVIDER_KEYS:
            self.provider_combo.addItem(LLM_PROVIDERS[key]["label"], key)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("服务厂商：", self.provider_combo)

        key_row = QWidget(page)
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        self.api_key_edit = QLineEdit(key_row)
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("粘贴你的 API Key")
        key_layout.addWidget(self.api_key_edit, 1)
        self.toggle_key_button = QPushButton("显示", key_row)
        self.toggle_key_button.setCheckable(True)
        self.toggle_key_button.setFixedWidth(56)
        self.toggle_key_button.toggled.connect(self._on_toggle_key)
        key_layout.addWidget(self.toggle_key_button)
        form.addRow("API Key：", key_row)

        self.base_url_edit = QLineEdit(page)
        form.addRow("接口地址：", self.base_url_edit)

        self.model_edit = QLineEdit(page)
        self.model_edit.setPlaceholderText("例如 deepseek-chat；豆包需填写 ep- 开头的推理接入点 ID")
        form.addRow("模型 ID：", self.model_edit)

        layout.addLayout(form)

        test_row = QHBoxLayout()
        self.test_button = QPushButton("测试连接", page)
        self.test_button.clicked.connect(self._on_test_connection)
        test_row.addWidget(self.test_button)
        self.test_result = QLabel("", page)
        self.test_result.setWordWrap(True)
        test_row.addWidget(self.test_result, 1)
        layout.addLayout(test_row)

        env_hint = QLabel(
            "也可以把密钥写在项目根目录的 .env 文件中（参考 .env.example），"
            "程序启动时会自动读取，此处留空即可。",
            page,
        )
        env_hint.setWordWrap(True)
        env_hint.setStyleSheet("color: #8a94a6; font-size: 11px;")
        layout.addWidget(env_hint)
        layout.addStretch(1)
        return page

    # ---------------- 高级设置页 ----------------
    def _build_advanced_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 14, 10, 10)
        layout.setSpacing(10)

        env_group = QGroupBox("运行环境", page)
        env_layout = QVBoxLayout(env_group)
        env_layout.setSpacing(6)

        self.ffmpeg_label = QLabel("", env_group)
        self.ffmpeg_label.setWordWrap(True)
        self.ffmpeg_label.setStyleSheet("font-size: 11px; color: #5b6472;")
        env_layout.addWidget(self.ffmpeg_label)

        env_button_row = QHBoxLayout()
        self.locate_ffmpeg_button = QPushButton("手动指定 ffmpeg 路径…", env_group)
        self.locate_ffmpeg_button.clicked.connect(self._on_locate_ffmpeg)
        env_button_row.addWidget(self.locate_ffmpeg_button)
        self.recheck_env_button = QPushButton("重新检测环境", env_group)
        self.recheck_env_button.clicked.connect(self._on_recheck_env)
        env_button_row.addWidget(self.recheck_env_button)
        env_button_row.addStretch(1)
        env_layout.addLayout(env_button_row)
        layout.addWidget(env_group)

        pref_group = QGroupBox("行为偏好", page)
        pref_form = QFormLayout(pref_group)
        pref_form.setHorizontalSpacing(12)
        pref_form.setVerticalSpacing(8)

        self.concurrent_spin = QSpinBox(pref_group)
        self.concurrent_spin.setRange(1, 3)
        self.concurrent_spin.setToolTip("同时执行的转换任务数量，默认 1；设置过高会拖慢单任务速度")
        pref_form.addRow("同时转换任务数：", self.concurrent_spin)

        self.notify_check = QCheckBox("转换完成后发送系统通知", pref_group)
        pref_form.addRow("", self.notify_check)

        self.open_dir_check = QCheckBox("转换完成后自动打开输出目录", pref_group)
        pref_form.addRow("", self.open_dir_check)

        self.output_dir_label = QLabel("", pref_group)
        self.output_dir_label.setWordWrap(True)
        self.output_dir_label.setStyleSheet("font-size: 11px; color: #5b6472;")
        pref_form.addRow("当前输出目录：", self.output_dir_label)
        layout.addWidget(pref_group)

        maintenance_group = QGroupBox("维护", page)
        maintenance_layout = QHBoxLayout(maintenance_group)
        maintenance_layout.setSpacing(8)
        self.open_log_button = QPushButton("查看日志文件", maintenance_group)
        self.open_log_button.clicked.connect(self._on_open_log)
        maintenance_layout.addWidget(self.open_log_button)
        self.clear_thumb_button = QPushButton("清理缩略图缓存", maintenance_group)
        self.clear_thumb_button.clicked.connect(self._on_clear_thumbnails)
        maintenance_layout.addWidget(self.clear_thumb_button)
        maintenance_layout.addStretch(1)
        layout.addWidget(maintenance_group)

        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # 读写配置
    # ------------------------------------------------------------------
    def _load_settings(self) -> None:
        provider = str(settings.get("llm_provider") or "deepseek")
        index = self.provider_combo.findData(provider)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.api_key_edit.setText(str(settings.get("api_key") or ""))
        self.base_url_edit.setText(str(settings.get("api_base_url") or ""))
        self.model_edit.setText(str(settings.get("api_model") or ""))
        self._apply_provider_defaults(fill_empty_only=True)

        self.concurrent_spin.setValue(int(settings.get("max_concurrent", 1) or 1))
        self.notify_check.setChecked(bool(settings.get("notification_enabled", True)))
        self.open_dir_check.setChecked(bool(settings.get("open_dir_after_finish", False)))
        self.output_dir_label.setText(settings.output_dir)
        self._refresh_env_label()

    def _on_save(self) -> None:
        settings.update({
            "llm_provider": self.provider_combo.currentData(),
            "api_key": self.api_key_edit.text().strip(),
            "api_base_url": self.base_url_edit.text().strip(),
            "api_model": self.model_edit.text().strip(),
            "max_concurrent": self.concurrent_spin.value(),
            "notification_enabled": self.notify_check.isChecked(),
            "open_dir_after_finish": self.open_dir_check.isChecked(),
        })
        logger.info("设置已保存：provider=%s 并发=%d", self.provider_combo.currentData(), self.concurrent_spin.value())
        self.settingsChanged.emit()
        self.accept()

    # ------------------------------------------------------------------
    # 交互逻辑
    # ------------------------------------------------------------------
    def _on_provider_changed(self, _index: int) -> None:
        self._apply_provider_defaults(fill_empty_only=False)

    def _apply_provider_defaults(self, fill_empty_only: bool) -> None:
        """切换厂商时回填该厂商的默认接口地址与模型 ID。"""
        provider = self.provider_combo.currentData()
        profile = LLM_PROVIDERS.get(provider) or {}
        if not fill_empty_only or not self.base_url_edit.text().strip():
            self.base_url_edit.setText(str(profile.get("base_url") or ""))
        if not fill_empty_only or not self.model_edit.text().strip():
            self.model_edit.setText(str(profile.get("model") or ""))

    def _on_toggle_key(self, checked: bool) -> None:
        self.api_key_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        self.toggle_key_button.setText("隐藏" if checked else "显示")

    def _on_test_connection(self) -> None:
        config = LLMConfig(
            provider=self.provider_combo.currentData(),
            api_key=self.api_key_edit.text().strip(),
            base_url=self.base_url_edit.text().strip(),
            model=self.model_edit.text().strip(),
            timeout=15,       # 测试连接用较短超时，避免界面长时间无响应
            max_retry=1,
        )
        if not config.configured:
            self.test_result.setText("请先填写 API Key 与模型 ID")
            self.test_result.setStyleSheet("color: #c0392b; font-size: 11px;")
            return

        self.test_button.setEnabled(False)
        self.test_result.setText("正在测试连接，请稍候（最长 15 秒）…")
        self.test_result.setStyleSheet("color: #1a73e8; font-size: 11px;")
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            ok, message = LLMClient(config).test_connection()
        finally:
            QGuiApplication.restoreOverrideCursor()
            self.test_button.setEnabled(True)

        self.test_result.setText(message)
        self.test_result.setStyleSheet(
            "color: #2e7d32; font-size: 11px;" if ok else "color: #c0392b; font-size: 11px;"
        )

    def _refresh_env_label(self) -> None:
        ffmpeg = get_ffmpeg_path()
        ffprobe = get_ffprobe_path()
        if ffmpeg and ffprobe:
            self.ffmpeg_label.setText(f"ffmpeg：{ffmpeg}\nffprobe：{ffprobe}")
            self.ffmpeg_label.setStyleSheet("font-size: 11px; color: #2e7d32;")
        else:
            missing = "、".join(name for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not path)
            self.ffmpeg_label.setText(f"未检测到：{missing}，请安装或手动指定路径。")
            self.ffmpeg_label.setStyleSheet("font-size: 11px; color: #c0392b;")

    def _on_locate_ffmpeg(self) -> None:
        file_filter = (
            "ffmpeg 可执行文件 (ffmpeg.exe ffmpeg);;所有文件 (*.*)"
            if os.name == "nt" else "所有文件 (*.*)"
        )
        chosen, _ = QFileDialog.getOpenFileName(self, "请选择 ffmpeg 可执行文件", "", file_filter)
        if not chosen:
            return
        ffmpeg_path = Path(chosen)
        probe_candidate = ffmpeg_path.with_name("ffprobe" + ffmpeg_path.suffix)
        if not probe_candidate.exists() and os.name == "nt":
            probe_candidate = ffmpeg_path.with_name("ffprobe.exe")
        if not probe_candidate.exists():
            QMessageBox.warning(self, "缺少 ffprobe", "同目录下未找到 ffprobe，请选择完整 ffmpeg 发行包中的可执行文件。")
            return
        os.environ["FFMPEG_PATH"] = str(ffmpeg_path)
        os.environ["FFPROBE_PATH"] = str(probe_candidate)
        reset_binary_cache()
        self._refresh_env_label()
        self.settingsChanged.emit()

    def _on_recheck_env(self) -> None:
        reset_binary_cache()
        self._refresh_env_label()
        self.settingsChanged.emit()

    def _on_open_log(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        if not Path(LOG_FILE).exists():
            QMessageBox.information(self, "暂无日志", "日志文件尚未生成。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_FILE)))

    def _on_clear_thumbnails(self) -> None:
        removed = clear_thumbnail_cache()
        QMessageBox.information(self, "操作完成", f"已清理 {removed} 个缩略图缓存文件。")
