"""
任务队列面板
============

以表格形式展示批量转码任务：文件名、目标格式、状态、进度条、速度、剩余时间，
并支持逐条取消、逐条移除、清空已完成、一键开始 / 暂停、打开输出目录。

面板只负责「展示 + 发出用户意图信号」，真正的队列调度由 core 层的
`ConversionManager` 完成，做到 UI 与业务逻辑解耦。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.ffmpeg_engine import ConversionTask, TaskStatus

COLUMN_TITLES = ["文件名", "目标格式", "状态", "进度", "速度", "剩余时间", "操作"]
COL_INDEX = {title: index for index, title in enumerate(COLUMN_TITLES)}


class TaskQueuePanel(QGroupBox):
    """转换任务队列表格。"""

    startRequested = Signal()
    pauseRequested = Signal()
    cancelTaskRequested = Signal(str)
    removeTaskRequested = Signal(str)
    clearFinishedRequested = Signal()
    openOutputDirRequested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("④ 转换任务队列", parent)
        self._order: List[str] = []              # 任务 ID 按表格行顺序保存
        self._row_by_id: Dict[str, int] = {}
        self._tasks: Dict[str, ConversionTask] = {}
        self._progress_bars: Dict[str, QProgressBar] = {}
        self._cancel_buttons: Dict[str, QPushButton] = {}

        self._build_ui()
        self._refresh_summary()

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 14, 12, 12)
        root.setSpacing(8)

        self.table = QTableWidget(0, len(COLUMN_TITLES), self)
        self.table.setHorizontalHeaderLabels(COLUMN_TITLES)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_INDEX["文件名"], QHeaderView.Stretch)
        for title in ("目标格式", "状态", "速度", "剩余时间"):
            header.setSectionResizeMode(COL_INDEX[title], QHeaderView.ResizeToContents)
        header.setSectionResizeMode(COL_INDEX["进度"], QHeaderView.Fixed)
        self.table.setColumnWidth(COL_INDEX["进度"], 150)
        header.setSectionResizeMode(COL_INDEX["操作"], QHeaderView.ResizeToContents)
        root.addWidget(self.table, 1)

        # ---- 底部按钮行 ----
        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.start_button = QPushButton("开始转换", self)
        self.start_button.clicked.connect(self.startRequested.emit)
        button_row.addWidget(self.start_button)

        self.pause_button = QPushButton("暂停排队", self)
        self.pause_button.clicked.connect(self.pauseRequested.emit)
        self.pause_button.setEnabled(False)
        button_row.addWidget(self.pause_button)

        self.cancel_selected_button = QPushButton("取消选中", self)
        self.cancel_selected_button.clicked.connect(self._on_cancel_selected)
        button_row.addWidget(self.cancel_selected_button)

        self.remove_selected_button = QPushButton("移除选中", self)
        self.remove_selected_button.clicked.connect(self._on_remove_selected)
        button_row.addWidget(self.remove_selected_button)

        self.clear_button = QPushButton("清空已完成", self)
        self.clear_button.clicked.connect(self.clearFinishedRequested.emit)
        button_row.addWidget(self.clear_button)

        button_row.addStretch(1)
        self.summary_label = QLabel("", self)
        self.summary_label.setStyleSheet("color: #5b6472;")
        button_row.addWidget(self.summary_label)
        root.addLayout(button_row)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def add_task(self, task: ConversionTask) -> None:
        """新增一行任务。"""
        if task.task_id in self._row_by_id:
            self.update_task(task)
            return

        row = self.table.rowCount()
        self.table.insertRow(row)
        self._order.append(task.task_id)
        self._row_by_id[task.task_id] = row
        self._tasks[task.task_id] = task

        name_item = QTableWidgetItem(task.filename)
        name_item.setToolTip(f"输入：{task.input_path}\n输出：{task.output_path}\n参数：{task.params.describe()}")
        self.table.setItem(row, COL_INDEX["文件名"], name_item)
        self.table.setItem(row, COL_INDEX["目标格式"], QTableWidgetItem(task.params.format))

        progress_bar = QProgressBar(self.table)
        progress_bar.setRange(0, 100)
        progress_bar.setValue(0)
        progress_bar.setFormat("%p%")
        self.table.setCellWidget(row, COL_INDEX["进度"], progress_bar)
        self._progress_bars[task.task_id] = progress_bar

        action_widget = QWidget(self.table)
        action_layout = QHBoxLayout(action_widget)
        action_layout.setContentsMargins(2, 0, 2, 0)
        action_layout.setSpacing(4)

        cancel_button = QPushButton("取消", action_widget)
        cancel_button.setFixedWidth(52)
        cancel_button.clicked.connect(
            lambda _checked=False, task_id=task.task_id: self.cancelTaskRequested.emit(task_id)
        )
        action_layout.addWidget(cancel_button)
        self._cancel_buttons[task.task_id] = cancel_button

        remove_button = QPushButton("移除", action_widget)
        remove_button.setFixedWidth(52)
        remove_button.clicked.connect(
            lambda _checked=False, task_id=task.task_id: self.removeTaskRequested.emit(task_id)
        )
        action_layout.addWidget(remove_button)
        self.table.setCellWidget(row, COL_INDEX["操作"], action_widget)

        self.update_task(task)

    def update_task(self, task: ConversionTask) -> None:
        """刷新某一行的状态、进度、速度与剩余时间。"""
        row = self._row_by_id.get(task.task_id)
        if row is None or row >= self.table.rowCount():
            return

        status_item = QTableWidgetItem(task.status_label)
        status_item.setToolTip(task.message or "")
        if task.status == TaskStatus.SUCCESS:
            status_item.setForeground(Qt.darkGreen)
        elif task.status == TaskStatus.FAILED:
            status_item.setForeground(Qt.red)
        elif task.status == TaskStatus.CANCELLED:
            status_item.setForeground(Qt.gray)
        self.table.setItem(row, COL_INDEX["状态"], status_item)

        progress_bar = self._progress_bars.get(task.task_id)
        if progress_bar is not None:
            progress_bar.setValue(int(max(0.0, min(100.0, task.progress))))
            if task.status == TaskStatus.FAILED:
                progress_bar.setStyleSheet("QProgressBar::chunk { background-color: #e74c3c; }")
            elif task.status == TaskStatus.CANCELLED:
                progress_bar.setStyleSheet("QProgressBar::chunk { background-color: #95a5a6; }")
            elif task.status == TaskStatus.SUCCESS:
                progress_bar.setStyleSheet("QProgressBar::chunk { background-color: #27ae60; }")
            else:
                progress_bar.setStyleSheet("")

        self.table.setItem(row, COL_INDEX["速度"], QTableWidgetItem(task.speed_str))
        self.table.setItem(row, COL_INDEX["剩余时间"], QTableWidgetItem(task.eta_str))

        cancel_button = self._cancel_buttons.get(task.task_id)
        if cancel_button is not None:
            cancel_button.setEnabled(task.status in (TaskStatus.PENDING, TaskStatus.RUNNING))

        self._refresh_summary()

    def remove_task(self, task_id: str) -> None:
        """移除一行任务并重建行号索引。"""
        row = self._row_by_id.get(task_id)
        if row is None:
            return
        self.table.removeRow(row)
        self._progress_bars.pop(task_id, None)
        self._cancel_buttons.pop(task_id, None)
        self._tasks.pop(task_id, None)
        self._order = [item for item in self._order if item != task_id]
        self._row_by_id = {item: index for index, item in enumerate(self._order)}
        self._refresh_summary()

    def selected_task_id(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._order):
            return None
        return self._order[row]

    def set_running(self, running: bool) -> None:
        """队列运行状态变化时切换按钮可用性。"""
        self.start_button.setEnabled(not running)
        self.pause_button.setEnabled(running)

    def task_count(self) -> int:
        return len(self._order)

    def select_task(self, task_id: str) -> None:
        row = self._row_by_id.get(task_id)
        if row is not None:
            self.table.selectRow(row)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _on_cancel_selected(self) -> None:
        task_id = self.selected_task_id()
        if task_id:
            self.cancelTaskRequested.emit(task_id)

    def _on_remove_selected(self) -> None:
        task_id = self.selected_task_id()
        if task_id:
            self.removeTaskRequested.emit(task_id)

    def _refresh_summary(self) -> None:
        """底部汇总：总数 + 各状态数量。"""
        total = self.table.rowCount()
        if not total:
            self.summary_label.setText("队列为空")
            return
        running = sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)
        pending = sum(1 for t in self._tasks.values() if t.status == TaskStatus.PENDING)
        done = sum(1 for t in self._tasks.values() if t.status == TaskStatus.SUCCESS)
        failed = sum(1 for t in self._tasks.values() if t.status == TaskStatus.FAILED)
        self.summary_label.setText(
            f"共 {total} 个任务｜等待 {pending}｜进行中 {running}｜完成 {done}｜失败 {failed}"
        )
