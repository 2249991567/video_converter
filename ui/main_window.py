"""
主窗口
======

把「视频信息面板 / 参数面板 / AI 面板 / 任务队列面板」组装成一个完整的桌面应用，
并负责所有跨模块的编排：

- 加载视频（文件对话框 / 拖拽）→ ffprobe 解析 → 缩略图 → 智能参数推荐
- 自然语言 → AI 参数解析 → 回填参数面板
- 参数面板 → 生成任务 → ConversionManager 队列调度 → 进度实时刷新
- 任务完成 / 失败的系统通知、输出目录一键打开
- 全链路日志（运行日志面板 + 日志文件）

本文件只做「编排」，具体能力全部来自 core / ai 层，保证界面与业务解耦。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QPoint, QRectF, Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QIcon,
    QPainter,
    QPixmap,
    QPolygon,
)
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ai.llm_client import LLMConfig
from ai.param_interpreter import InterpretResult, ParamInterpreter
from config.settings import (
    APP_NAME,
    APP_VERSION,
    LOG_FILE,
    OUTPUT_FORMATS,
    SUPPORTED_EXTENSIONS,
    check_environment,
    reset_binary_cache,
    settings,
)
from core.ffmpeg_engine import (
    ConversionManager,
    ConversionTask,
    HardwareInfo,
    TaskStatus,
    apply_hardware_acceleration,
    build_output_path,
    detect_hardware_acceleration,
)
from core.ffprobe_parser import VideoInfo
from core.thumbnail import clear_thumbnail_cache
from ui.components.ai_panel import AIPanel
from ui.components.env_dialog import EnvCheckDialog
from ui.components.params_panel import ParamsPanel
from ui.components.settings_dialog import SettingsDialog
from ui.components.task_queue_panel import TaskQueuePanel
from ui.components.video_info_panel import VideoInfoPanel
from ui.components.workers import InterpretWorker, ProbeWorker, ThumbnailWorker
from utils.logger import get_logger

logger = get_logger("ui.main_window")

# 运行日志面板最多保留的行数
LOG_MAX_LINES = 800


def build_app_icon() -> QIcon:
    """
    用代码绘制应用图标（避免额外的图片资源文件）：
    深色圆角底 + 白色播放三角。
    """
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#1a73e8"))
    painter.drawRoundedRect(QRectF(2, 2, size - 4, size - 4), 14, 14)

    painter.setBrush(QColor("#ffffff"))
    triangle = QPolygon([
        QPoint(25, 18), QPoint(25, 46), QPoint(48, 32),
    ])
    painter.drawPolygon(triangle)
    painter.end()
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · AI 视频格式转换器 v{APP_VERSION}")
        self.setWindowIcon(build_app_icon())
        self.setAcceptDrops(True)

        # ---- 运行状态 ----
        self._video_info: Optional[VideoInfo] = None
        self._hardware: HardwareInfo = HardwareInfo()
        self._interpreter = ParamInterpreter()
        self._manager = ConversionManager(self)

        # 系统托盘图标（由 main.py 注入；未注入时通知降级为状态栏提示）
        self._tray: Optional[QSystemTrayIcon] = None

        # 后台线程引用（必须持有，否则会被 Python 垃圾回收导致线程异常）
        self._probe_worker: Optional[ProbeWorker] = None
        self._thumbnail_worker: Optional[ThumbnailWorker] = None
        self._ai_worker: Optional[InterpretWorker] = None

        self._build_ui()
        self._build_menu()
        self._build_status_bar()
        self._build_log_dock()
        self._connect_signals()
        self._bootstrap()

    # ==================================================================
    # 界面构建
    # ==================================================================
    def _build_ui(self) -> None:
        self.video_panel = VideoInfoPanel(self)
        self.params_panel = ParamsPanel(self)
        self.ai_panel = AIPanel(self)
        self.queue_panel = TaskQueuePanel(self)

        # ---- 左列：视频信息 + 参数 + 主操作按钮 ----
        left_container = QWidget(self)
        left_layout = QVBoxLayout(left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(12)
        left_layout.addWidget(self.video_panel)
        left_layout.addWidget(self.params_panel)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.add_task_button = QPushButton("添加到任务队列", left_container)
        self.add_task_button.setMinimumHeight(34)
        self.add_task_button.clicked.connect(self._on_add_to_queue)
        action_row.addWidget(self.add_task_button, 1)

        self.start_button = QPushButton("开始转换", left_container)
        self.start_button.setMinimumHeight(34)
        self.start_button.clicked.connect(self._on_start_conversion)
        action_row.addWidget(self.start_button, 1)

        self.open_output_button = QPushButton("打开输出目录", left_container)
        self.open_output_button.setMinimumHeight(34)
        self.open_output_button.clicked.connect(
            lambda: self._open_path(self.params_panel.output_dir())
        )
        action_row.addWidget(self.open_output_button)
        left_layout.addLayout(action_row)
        left_layout.addStretch(1)

        left_scroll = QScrollArea(self)
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QScrollArea.NoFrame)
        left_scroll.setWidget(left_container)

        # ---- 右列：AI 助手 + 任务队列 ----
        right_container = QWidget(self)
        right_layout = QVBoxLayout(right_container)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)
        right_layout.addWidget(self.ai_panel)
        right_layout.addWidget(self.queue_panel, 1)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.addWidget(left_scroll)
        splitter.addWidget(right_container)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        splitter.setSizes([560, 700])
        self.setCentralWidget(splitter)

        # 参数面板「打开目录」按钮的回调注入
        self.params_panel.set_open_dir_callback(self._open_path)

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("文件(&F)")
        open_action = QAction("打开视频…", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_browse_video)
        file_menu.addAction(open_action)

        add_action = QAction("把当前视频加入转换队列", self)
        add_action.setShortcut("Ctrl+Q")
        add_action.triggered.connect(self._on_add_to_queue)
        file_menu.addAction(add_action)

        file_menu.addSeparator()
        exit_action = QAction("退出", self)
        exit_action.setShortcut("Ctrl+W")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        tool_menu = menu_bar.addMenu("工具(&T)")
        settings_action = QAction("设置…", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._open_settings)
        tool_menu.addAction(settings_action)

        recheck_action = QAction("重新检测运行环境", self)
        recheck_action.triggered.connect(self._on_recheck_environment)
        tool_menu.addAction(recheck_action)

        redetect_hw_action = QAction("重新检测显卡硬件加速", self)
        redetect_hw_action.triggered.connect(self._on_redetect_hardware)
        tool_menu.addAction(redetect_hw_action)

        clear_cache_action = QAction("清理缩略图缓存", self)
        clear_cache_action.triggered.connect(self._on_clear_thumbnail_cache)
        tool_menu.addAction(clear_cache_action)

        tool_menu.addSeparator()
        log_action = QAction("显示 / 隐藏运行日志", self)
        log_action.setShortcut("Ctrl+L")
        log_action.triggered.connect(self._toggle_log_dock)
        tool_menu.addAction(log_action)

        help_menu = menu_bar.addMenu("帮助(&H)")
        about_action = QAction("关于本程序", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _build_status_bar(self) -> None:
        self.env_label = QLabel("运行环境：检测中…", self)
        self.task_label = QLabel("当前参数：-", self)
        status_bar = self.statusBar()
        status_bar.addWidget(self.env_label, 1)
        status_bar.addPermanentWidget(self.task_label)

    def _build_log_dock(self) -> None:
        self.log_view = QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(LOG_MAX_LINES)
        self.log_view.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")

        self.log_dock = QDockWidget("运行日志", self)
        self.log_dock.setWidget(self.log_view)
        self.log_dock.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)
        self.log_dock.hide()

    # ==================================================================
    # 信号连接
    # ==================================================================
    def _connect_signals(self) -> None:
        # ---- 视频信息面板 ----
        self.video_panel.browseRequested.connect(self._on_browse_video)
        self.video_panel.filesDropped.connect(self._on_files_dropped)
        self.video_panel.thumbnailRefreshRequested.connect(self._on_refresh_thumbnail)

        # ---- 参数面板 ----
        self.params_panel.paramsChanged.connect(self._on_params_changed)
        self.params_panel.outputDirChanged.connect(self._on_output_dir_changed)
        self.params_panel.presetChanged.connect(self._on_preset_changed)

        # ---- AI 面板 ----
        self.ai_panel.interpretRequested.connect(self._on_interpret_requested)
        self.ai_panel.recommendRequested.connect(self._on_recommend_requested)
        self.ai_panel.settingsRequested.connect(self._open_settings)

        # ---- 任务队列面板 ----
        self.queue_panel.startRequested.connect(self._on_start_conversion)
        self.queue_panel.pauseRequested.connect(self._on_pause_conversion)
        self.queue_panel.cancelTaskRequested.connect(self._on_cancel_task)
        self.queue_panel.removeTaskRequested.connect(self._on_remove_task)
        self.queue_panel.clearFinishedRequested.connect(self._on_clear_finished)
        self.queue_panel.openOutputDirRequested.connect(self._open_path)

        # ---- 转换管理器 ----
        self._manager.taskUpdated.connect(self.queue_panel.update_task)
        self._manager.taskFinished.connect(self._on_task_finished)
        self._manager.queueChanged.connect(self._update_window_title)
        self._manager.allFinished.connect(self._on_all_finished)
        self._manager.logMessage.connect(self._append_log)

    # ==================================================================
    # 启动初始化
    # ==================================================================
    def _bootstrap(self) -> None:
        # 1) 恢复窗口尺寸
        size = settings.get("window_size", [1280, 860])
        try:
            self.resize(int(size[0]), int(size[1]))
        except (TypeError, ValueError, IndexError):
            self.resize(1280, 860)

        # 2) 环境检测
        self._refresh_environment_label()

        # 3) 硬件加速探测（在后台阻塞很短，可直接同步执行；失败会自动降级）
        self._on_redetect_hardware(show_message=False)

        # 4) AI 状态
        self._refresh_ai_state()

        # 5) 输出目录
        self._append_log(f"输出目录：{settings.output_dir}")
        self._append_log(f"日志文件：{LOG_FILE}")

    def _refresh_ai_state(self) -> None:
        config = LLMConfig.from_settings()
        self.ai_panel.set_llm_state(config.configured, config.provider_label, config.model)

    def _refresh_environment_label(self) -> None:
        status = check_environment()
        if status.available:
            version = status.ffmpeg_version.split("Copyright")[0].strip() or "ffmpeg 已就绪"
            self.env_label.setText(f"运行环境：正常｜{version}")
            self.env_label.setStyleSheet("color: #2e7d32;")
        else:
            self.env_label.setText("运行环境：未检测到 ffmpeg，转换功能不可用（工具 → 重新检测运行环境）")
            self.env_label.setStyleSheet("color: #c0392b;")

    # ==================================================================
    # 视频加载流程
    # ==================================================================
    def _on_browse_video(self) -> None:
        files = self.video_panel.pick_file_dialog(self, str(settings.get("last_input_dir") or ""))
        if files:
            self._load_video(files[0])

    def _on_files_dropped(self, paths: List[str]) -> None:
        if paths:
            self._load_video(paths[0])

    def _load_video(self, path: str) -> None:
        """加载并解析视频（异步，避免大文件解析阻塞界面）。"""
        if not path:
            return
        if self._probe_worker is not None and self._probe_worker.isRunning():
            QMessageBox.information(self, "请稍候", "正在解析上一个视频，请稍后再试。")
            return

        self._append_log(f"开始解析视频：{path}")
        self.video_panel.set_loading(True)
        self.video_panel.set_status("正在解析视频信息…")
        self.ai_panel.clear_result()

        self._probe_worker = ProbeWorker(path, self)
        self._probe_worker.succeeded.connect(self._on_probe_succeeded)
        self._probe_worker.failed.connect(self._on_probe_failed)
        self._probe_worker.start()

    def _on_probe_succeeded(self, info: VideoInfo) -> None:
        self._video_info = info
        self.video_panel.set_loading(False)
        self.video_panel.set_info(info)
        settings.set("last_input_dir", str(Path(info.path).parent))
        self._append_log(f"解析成功：{info.filename}｜{info.resolution}｜{info.duration_str}｜{info.video_codec or '无视频流'}")

        # 异步生成缩略图
        self._start_thumbnail_worker(info.path, info.duration)

        # 自动智能推荐参数（纯本地规则，不依赖网络）
        self._run_recommend(use_llm=False, silent=True)

    def _on_probe_failed(self, message: str) -> None:
        self.video_panel.set_loading(False)
        self.video_panel.set_status(message, is_error=True)
        self._append_log(f"解析失败：{message}")
        QMessageBox.warning(self, "视频解析失败", message)

    def _start_thumbnail_worker(self, path: str, duration: float, force: bool = False) -> None:
        if self._thumbnail_worker is not None and self._thumbnail_worker.isRunning():
            self._thumbnail_worker.wait(1500)
        if force:
            self.video_panel.set_status("正在重新生成缩略图…")
        self._thumbnail_worker = ThumbnailWorker(path, duration, force, self)
        self._thumbnail_worker.succeeded.connect(self.video_panel.set_thumbnail)
        self._thumbnail_worker.failed.connect(
            lambda message: self._append_log(f"缩略图：{message}")
        )
        self._thumbnail_worker.start()

    def _on_refresh_thumbnail(self) -> None:
        if self._video_info is None:
            QMessageBox.information(self, "提示", "请先加载视频。")
            return
        self._start_thumbnail_worker(self._video_info.path, self._video_info.duration, force=True)

    # ==================================================================
    # AI 交互流程
    # ==================================================================
    def _on_interpret_requested(self, text: str) -> None:
        if not text:
            QMessageBox.information(self, "提示", "请先输入你的需求描述，例如：转成 1080P 的 MP4。")
            return
        self._start_ai_worker(mode="interpret", text=text)

    def _on_recommend_requested(self) -> None:
        if self._video_info is None:
            QMessageBox.information(self, "提示", "请先加载视频，智能推荐需要参考原视频属性。")
            return
        # 配置了大模型时，让模型在本地规则推荐结果基础上复核一次；未配置则纯本地推荐
        self._run_recommend(use_llm=LLMConfig.from_settings().configured, silent=False)

    def _run_recommend(self, use_llm: bool, silent: bool) -> None:
        self._start_ai_worker(
            mode="recommend",
            target_format=self.params_panel.current_params().format,
            use_llm=use_llm,
            silent=silent,
        )

    def _start_ai_worker(
        self,
        mode: str,
        text: str = "",
        target_format: Optional[str] = None,
        use_llm: bool = False,
        silent: bool = False,
    ) -> None:
        if self._ai_worker is not None and self._ai_worker.isRunning():
            QMessageBox.information(self, "请稍候", "上一个 AI 请求还在进行中，请稍后再试。")
            return

        self.ai_panel.set_busy(True, "正在分析你的需求…" if mode == "interpret" else "正在根据视频属性推荐参数…")
        self._ai_worker = InterpretWorker(
            interpreter=self._interpreter,
            mode=mode,
            text=text,
            video_info=self._video_info,
            target_format=target_format,
            use_llm=use_llm,
            parent=self,
        )
        self._ai_worker.succeeded.connect(
            lambda result: self._on_ai_succeeded(result, mode=mode, silent=silent)
        )
        self._ai_worker.failed.connect(self._on_ai_failed)
        self._ai_worker.start()

    def _on_ai_succeeded(self, result: InterpretResult, mode: str, silent: bool) -> None:
        self.ai_panel.set_busy(False)
        self.params_panel.apply_params(result.params)

        prefix = "已根据你的描述生成参数" if mode == "interpret" else "已根据原视频属性推荐参数"
        explanation = result.explanation or f"{prefix}：{result.params.describe()}"
        self.ai_panel.set_result(explanation, ok=result.ok, warnings=result.warnings)
        self._append_log(f"AI 结果（{result.source_label}）：{result.params.describe()}")

        if result.fallback and mode == "interpret":
            QMessageBox.warning(
                self,
                "AI 解析降级提示",
                explanation
                + "\n\n参数已自动填入右侧参数栏，请确认后再开始转换。",
            )
        elif not silent and mode == "recommend":
            QMessageBox.information(self, "智能推荐完成", explanation)

        self._on_params_changed(self.params_panel.current_params())

    def _on_ai_failed(self, message: str) -> None:
        self.ai_panel.set_busy(False)
        self.ai_panel.set_result(message, ok=False)
        self._append_log(f"AI 处理失败：{message}")
        QMessageBox.warning(self, "AI 处理失败", message)

    # ==================================================================
    # 任务队列流程
    # ==================================================================
    def _on_add_to_queue(self) -> None:
        if self._video_info is None:
            QMessageBox.information(self, "提示", "请先加载视频（支持拖拽导入或文件选择）。")
            return

        status = check_environment()
        if not status.available:
            QMessageBox.critical(
                self, "缺少 ffmpeg",
                "未检测到 ffmpeg，无法执行转换。请先安装 ffmpeg 后重试。",
            )
            self._on_recheck_environment()
            return

        params = self.params_panel.current_params()
        profile = OUTPUT_FORMATS.get(params.format, OUTPUT_FORMATS["MP4"])
        info = self._video_info

        if profile.get("audio_only") and not info.has_audio:
            QMessageBox.warning(self, "无法转换", "该文件不包含音频流，无法提取音频。")
            return
        if not profile.get("audio_only") and not info.has_video:
            QMessageBox.warning(self, "无法转换", "该文件不包含视频流，无法输出为视频格式。")
            return

        # 硬件加速：按探测结果替换编码器并关闭不可用开关
        if params.hw_accel:
            params = apply_hardware_acceleration(params, self._hardware)
            if not params.hw_accel:
                self._append_log("未检测到可用硬件编码器，本次任务已自动回退为 CPU 软件编码")

        output_dir = self.params_panel.output_dir()
        try:
            output_path = build_output_path(info.path, output_dir, params.format)
        except OSError as exc:
            QMessageBox.critical(self, "输出路径不可用", f"无法创建输出文件：{exc}")
            return

        task = ConversionTask.create(
            input_path=info.path,
            output_path=output_path,
            params=params,
            duration=info.duration,
            has_audio=info.has_audio,
            file_size=info.filesize,
        )
        self._manager.add_task(task)
        self.queue_panel.add_task(task)
        self.queue_panel.select_task(task.task_id)
        self.statusBar().showMessage(f"已加入队列：{task.filename} → {task.output_name}", 5000)
        self._append_log(f"已加入队列：{task.filename}｜{params.describe()} → {output_path}")

    def _on_start_conversion(self) -> None:
        if self.queue_panel.task_count() == 0:
            QMessageBox.information(self, "提示", "任务队列为空，请先把视频添加到队列。")
            return
        if not check_environment().available:
            QMessageBox.critical(self, "缺少 ffmpeg", "未检测到 ffmpeg，无法执行转换。")
            return

        self._manager.set_max_concurrent(int(settings.get("max_concurrent", 1) or 1))
        self._manager.start()
        self.queue_panel.set_running(True)
        self.statusBar().showMessage("已开始执行转换队列…", 4000)

    def _on_pause_conversion(self) -> None:
        self._manager.pause()
        self.queue_panel.set_running(False)
        self.statusBar().showMessage("已暂停派发新任务，正在执行的任务会继续完成。", 4000)

    def _on_cancel_task(self, task_id: str) -> None:
        task = self._manager.get_task(task_id)
        if task is None:
            return
        if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
            self._manager.cancel_task(task_id)
            self._append_log(f"用户取消任务：{task.filename}")

    def _on_remove_task(self, task_id: str) -> None:
        task = self._manager.get_task(task_id)
        if task is not None and task.status == TaskStatus.RUNNING:
            answer = QMessageBox.question(
                self, "任务正在执行",
                f"「{task.filename}」正在转换中，移除将同时取消该任务，确定继续吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self._manager.remove_task(task_id)
        self.queue_panel.remove_task(task_id)

    def _on_clear_finished(self) -> None:
        finished_status = (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED)
        ids = [task.task_id for task in self._manager.tasks if task.status in finished_status]
        removed = self._manager.clear_finished()
        for task_id in ids:
            self.queue_panel.remove_task(task_id)
        if removed:
            self.statusBar().showMessage(f"已清理 {removed} 个已完成任务。", 4000)

    def _on_task_finished(self, task: ConversionTask) -> None:
        if task.status == TaskStatus.SUCCESS:
            self._notify("转换完成", f"{task.filename} → {task.output_name}")
            self._append_log(f"[完成] {task.output_name}")
        elif task.status == TaskStatus.FAILED:
            self._notify("转换失败", f"{task.filename}：{task.message}")
            self._append_log(f"[失败] {task.filename}｜{task.message}")
        elif task.status == TaskStatus.CANCELLED:
            self._notify("任务已取消", task.filename)

    def _on_all_finished(self) -> None:
        self.queue_panel.set_running(False)
        tasks = self._manager.tasks
        success = sum(1 for task in tasks if task.status == TaskStatus.SUCCESS)
        failed = sum(1 for task in tasks if task.status == TaskStatus.FAILED)
        cancelled = sum(1 for task in tasks if task.status == TaskStatus.CANCELLED)
        summary = f"队列执行结束：成功 {success}｜失败 {failed}｜取消 {cancelled}"
        self.statusBar().showMessage(summary, 8000)
        self._append_log(summary)

        if settings.get("open_dir_after_finish", False) and success:
            self._open_path(self.params_panel.output_dir())

        if success and not failed:
            self._notify("全部任务完成", f"共成功转换 {success} 个文件")
        elif failed:
            QMessageBox.warning(
                self, "部分任务失败",
                f"{summary}\n\n可在任务列表中把鼠标移到「状态」列查看失败原因，"
                "也可以通过菜单「工具 → 显示运行日志」查看详细日志。",
            )

    # ==================================================================
    # 参数 / 设置 / 环境
    # ==================================================================
    def _on_params_changed(self, params) -> None:
        self.task_label.setText(f"当前参数：{params.describe()}")

    def _on_preset_changed(self, name: str) -> None:
        self._append_log(f"套用预设：{name}")

    def _on_output_dir_changed(self, path: str) -> None:
        self._append_log(f"输出目录已切换：{path}")
        self.statusBar().showMessage(f"输出目录：{path}", 4000)

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self)
        if dialog.exec():
            self._manager.set_max_concurrent(int(settings.get("max_concurrent", 1) or 1))
            self._refresh_ai_state()
            self._refresh_environment_label()
            self._on_redetect_hardware(show_message=False)
            self._append_log("设置已更新")

    def _on_recheck_environment(self) -> None:
        reset_binary_cache()
        status = check_environment()
        self._refresh_environment_label()
        if status.available:
            QMessageBox.information(self, "检测成功", f"已检测到 ffmpeg：\n{status.ffmpeg_version}")
        else:
            EnvCheckDialog(status, self).exec()

    def _on_redetect_hardware(self, show_message: bool = True) -> None:
        try:
            self._hardware = detect_hardware_acceleration(force=True)
        except Exception as exc:  # noqa: BLE001  探测失败不能影响启动
            logger.exception("硬件加速探测异常")
            self._hardware = HardwareInfo(detail=f"探测失败：{exc}")
        self.params_panel.set_hw_info(self._hardware)
        self._append_log(f"硬件加速探测：{self._hardware.label}")
        if show_message:
            QMessageBox.information(self, "硬件加速检测", f"{self._hardware.label}\n{self._hardware.detail}")

    def _on_clear_thumbnail_cache(self) -> None:
        removed = clear_thumbnail_cache()
        if self._video_info is not None:
            self._start_thumbnail_worker(self._video_info.path, self._video_info.duration, force=True)
        self.statusBar().showMessage(f"已清理 {removed} 个缩略图缓存文件。", 4000)

    def _toggle_log_dock(self) -> None:
        self.log_dock.setVisible(not self.log_dock.isVisible())

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于本程序",
            f"<h3>{APP_NAME} v{APP_VERSION}</h3>"
            "<p>桌面端 AI 视频格式转换器</p>"
            "<ul>"
            "<li>视频处理：全部通过 subprocess 调用系统 ffmpeg / ffprobe 原生命令行</li>"
            "<li>AI 能力：OpenAI 兼容协议对接豆包 / DeepSeek / 通义千问</li>"
            "<li>界面框架：PySide6（Qt6）</li>"
            "</ul>"
            f"<p style='color:#666'>日志文件：{LOG_FILE}</p>",
        )

    # ==================================================================
    # 工具方法
    # ==================================================================
    def _open_path(self, path: str) -> None:
        """用系统文件管理器打开目录或文件。"""
        if not path:
            QMessageBox.information(self, "提示", "路径为空，无法打开。")
            return
        target = Path(path)
        if not target.exists():
            QMessageBox.information(self, "路径不存在", f"路径不存在：{target}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        self._append_log(f"打开路径：{target}")

    def _notify(self, title: str, message: str) -> None:
        """发送系统通知（后台转换时用户无需盯着界面）。"""
        if not settings.get("notification_enabled", True):
            self.statusBar().showMessage(f"{title}：{message}", 6000)
            return
        if self._tray is not None and QSystemTrayIcon.isSystemTrayAvailable():
            self._tray.showMessage(title, message, QSystemTrayIcon.Information, 5000)
        else:
            self.statusBar().showMessage(f"{title}：{message}", 6000)

    def _append_log(self, message: str) -> None:
        """向运行日志面板追加一行（同时写入日志文件由 logger 负责）。"""
        if not hasattr(self, "log_view"):
            return
        self.log_view.appendPlainText(message)

    def _update_window_title(self) -> None:
        total = self.queue_panel.task_count()
        running = self._manager.running_count
        title = f"{APP_NAME} · AI 视频格式转换器 v{APP_VERSION}"
        if total:
            title += f" — 任务 {total} 个（进行中 {running}）"
        self.setWindowTitle(title)

    # ==================================================================
    # 拖拽支持（窗口级）
    # ==================================================================
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._extract_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._extract_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths = self._extract_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self._on_files_dropped(paths)
        else:
            event.ignore()

    @staticmethod
    def _extract_paths(mime_data) -> List[str]:
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

    # ==================================================================
    # 生命周期
    # ==================================================================
    def set_tray_icon(self, tray: Optional[QSystemTrayIcon]) -> None:
        """注入系统托盘图标（由 main.py 创建后调用），用于后台完成通知。"""
        self._tray = tray

    def closeEvent(self, event) -> None:  # noqa: N802
        """退出前保存配置、停止后台任务，避免留下僵尸进程。"""
        if self._manager.is_busy:
            answer = QMessageBox.question(
                self, "任务正在执行",
                "仍有转换任务在执行中，退出将中断所有任务，确定退出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return

        # 取消未完成任务并等待线程退出（QThread 运行中被销毁会导致进程异常）
        self._manager.shutdown()

        settings.set("window_size", [self.width(), self.height()])
        settings.set("last_preset", self.params_panel.preset_combo.currentText())
        settings.save()
        self._append_log("程序退出，配置已保存")
        logger.info("主窗口关闭，配置已保存")

        if self._tray is not None:
            self._tray.hide()
        event.accept()
