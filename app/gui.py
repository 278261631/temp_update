#!/usr/bin/env python3
"""PySide6 主界面：天区收集 + 升级模板。"""

import os
from datetime import datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox, QDateEdit, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .config import AppConfig, BASE_DIR
from .gui_config import GuiConfig
from .region_index import INDEX_PATH, load_gy1_index
from .template_ops import UpdateSettings
from .workers import RegionCollectWorker, TemplateWorker

DEFAULT_WORKSPACE = os.path.join(BASE_DIR, "templates_update")


class MainWindow(QMainWindow):
    def __init__(self, config: AppConfig, gui_config: GuiConfig = None):
        super().__init__()
        self.config = config
        self.gui_config = gui_config or GuiConfig()
        self.worker = None

        self.setWindowTitle("KATS 天区收集 / 升级模板工具")
        self.resize(980, 720)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        root.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_region_tab(), "天区收集")
        self.tabs.addTab(self._build_update_tab(), "升级模板")
        self.tabs.addTab(self._build_log_tab(), "日志")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self.tabs)

        self._apply_gui_config()
        self._refresh_index_info()
        self._log("程序已启动。")
        self._log(f"配置来源: {self.config.config_path}")
        self._log(f"状态配置: {self.gui_config.path}")

    # ---------------- header ----------------
    def _build_header(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("天区收集 / 升级模板")
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)
        note = QLabel("数据源地址与天区坐标表请放在 config/url_config.json（已被 .gitignore 忽略，切勿提交）。")
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)
        return box

    # ---------------- region tab ----------------
    def _build_region_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        date_box = QGroupBox("日期范围（YYYYMMDD）")
        grid = QGridLayout(date_box)
        today = QDate.currentDate()

        grid.addWidget(QLabel("起始日期:"), 0, 0)
        self.start_date = QDateEdit(today)
        self.start_date.setCalendarPopup(True)
        self.start_date.setDisplayFormat("yyyyMMdd")
        grid.addWidget(self.start_date, 0, 1)

        grid.addWidget(QLabel("终止日期:"), 0, 2)
        self.end_date = QDateEdit(today)
        self.end_date.setCalendarPopup(True)
        self.end_date.setDisplayFormat("yyyyMMdd")
        grid.addWidget(self.end_date, 0, 3)

        self.collect_btn = QPushButton("收集GY1天区信息")
        self.collect_btn.clicked.connect(self._start_collect)
        grid.addWidget(self.collect_btn, 1, 0, 1, 2)

        self.region_status = QLabel("就绪")
        grid.addWidget(self.region_status, 1, 2, 1, 2)

        grid.addWidget(QLabel("URL 模板(收集):"), 2, 0)
        self.collect_template_combo = self._build_template_combo("collect")
        grid.addWidget(self.collect_template_combo, 2, 1, 1, 3)
        layout.addWidget(date_box)

        index_box = QGroupBox("GY1 索引信息")
        index_layout = QVBoxLayout(index_box)
        self.index_path_label = QLabel(INDEX_PATH)
        self.index_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.index_info_label = QLabel("")
        refresh_btn = QPushButton("刷新索引信息")
        refresh_btn.clicked.connect(self._refresh_index_info)
        index_row = QHBoxLayout()
        index_row.addWidget(self.index_info_label, 1)
        index_row.addWidget(refresh_btn)
        index_layout.addWidget(self.index_path_label)
        index_layout.addLayout(index_row)
        layout.addWidget(index_box)

        tip = QLabel("说明：仅针对 GY1。优先从本地缓存加载，当某天无缓存时才扫描远端。")
        tip.setStyleSheet("color: #1a56db;")
        layout.addWidget(tip)
        layout.addStretch(1)
        return page

    # ---------------- update tab ----------------
    def _build_update_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        param_box = QGroupBox("参数")
        grid = QGridLayout(param_box)

        grid.addWidget(QLabel("每个天区最近日期数:"), 0, 0)
        self.recent_days = QSpinBox()
        self.recent_days.setRange(1, 30)
        self.recent_days.setValue(3)
        grid.addWidget(self.recent_days, 0, 1)

        grid.addWidget(QLabel("生成模板最少参与图像数:"), 1, 0)
        self.min_images = QSpinBox()
        self.min_images.setRange(1, 999)
        self.min_images.setValue(30)
        grid.addWidget(self.min_images, 1, 1)

        grid.addWidget(QLabel("SWarp 线程数(0=自动):"), 2, 0)
        self.swarp_threads = QSpinBox()
        self.swarp_threads.setRange(0, 64)
        self.swarp_threads.setValue(0)
        grid.addWidget(self.swarp_threads, 2, 1)

        grid.addWidget(QLabel("预处理线程数:"), 3, 0)
        self.preprocess_threads = QSpinBox()
        self.preprocess_threads.setRange(1, 64)
        self.preprocess_threads.setValue(20)
        grid.addWidget(self.preprocess_threads, 3, 1)
        layout.addWidget(param_box)

        target_box = QGroupBox("目标与工作目录")
        form = QFormLayout(target_box)
        self.targets_edit = QPlainTextEdit()
        self.targets_edit.setPlaceholderText("天区列表（含系统名），例如 GY6_K002-8,GY6_K002-9")
        self.targets_edit.setFixedHeight(90)
        form.addRow("天区列表:", self.targets_edit)

        self.download_template_combo = self._build_template_combo("download")
        form.addRow("URL 模板(下载):", self.download_template_combo)

        self.workspace_edit = QLineEdit(DEFAULT_WORKSPACE)
        ws_btn = QPushButton("浏览...")
        ws_btn.clicked.connect(self._pick_workspace)
        ws_row = QHBoxLayout()
        ws_row.addWidget(self.workspace_edit, 1)
        ws_row.addWidget(ws_btn)
        ws_widget = QWidget()
        ws_widget.setLayout(ws_row)
        form.addRow("工作目录:", ws_widget)
        layout.addWidget(target_box)

        action_box = QGroupBox("操作")
        action_layout = QVBoxLayout(action_box)
        btn_row = QHBoxLayout()
        self.btn_download = QPushButton("下载目标天区观测文件")
        self.btn_download.clicked.connect(lambda: self._start_template("download"))
        self.btn_preprocess = QPushButton("预处理图像")
        self.btn_preprocess.clicked.connect(lambda: self._start_template("preprocess"))
        self.btn_make = QPushButton("生成新模板")
        self.btn_make.clicked.connect(lambda: self._start_template("make"))
        for b in (self.btn_download, self.btn_preprocess, self.btn_make):
            btn_row.addWidget(b)
        action_layout.addLayout(btn_row)

        self.update_status = QLabel("就绪")
        action_layout.addWidget(self.update_status)

        tip = QLabel("说明：填写天区列表（含系统名，如 GY6_K002-8），按 gy1_region_index.json 查找最近日期并下载相应观测文件。")
        tip.setStyleSheet("color: gray;")
        action_layout.addWidget(tip)
        layout.addWidget(action_box)
        layout.addStretch(1)
        return page

    # ---------------- log tab ----------------
    def _build_log_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)

        row = QHBoxLayout()
        clear_btn = QPushButton("清空日志")
        clear_btn.clicked.connect(self.log_view.clear)
        save_btn = QPushButton("保存日志")
        save_btn.clicked.connect(self._save_log)
        row.addStretch(1)
        row.addWidget(clear_btn)
        row.addWidget(save_btn)
        layout.addLayout(row)
        return page

    # ---------------- helpers ----------------
    def _log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {message}")

    def _save_log(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "保存日志", "region_template.log", "Log (*.log);;Text (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_view.toPlainText())

    def _refresh_index_info(self) -> None:
        index = load_gy1_index()
        dates = len(index)
        total_regions = sum(len(v) for v in index.values() if isinstance(v, list))
        self.index_info_label.setText(f"已缓存 {dates} 天，累计 {total_regions} 条天区记录。")

    def _pick_workspace(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择工作目录", self.workspace_edit.text() or BASE_DIR)
        if path:
            self.workspace_edit.setText(path)

    def _build_template_combo(self, scope: str) -> QComboBox:
        combo = QComboBox()
        for key, label in self.config.get_url_template_options().items():
            combo.addItem(label, key)
        idx = combo.findData(self.config.get_template_type(scope))
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(lambda _i, s=scope: self._on_template_type_changed(s))
        return combo

    def _on_template_type_changed(self, scope: str) -> None:
        combo = self.collect_template_combo if scope == "collect" else self.download_template_combo
        key = combo.currentData()
        if not key:
            return
        self.config.set_template_type(key, scope)
        self._log(f"[URL模板-{scope}] 已切换为 {key}: {self.config.get_url_template(scope)}")

    # ---------------- gui config ----------------
    def _apply_gui_config(self) -> None:
        gc = self.gui_config
        self.targets_edit.setPlainText(str(gc.get("targets") or ""))
        for edit, key in ((self.start_date, "start_date"), (self.end_date, "end_date")):
            text = str(gc.get(key) or "")
            date = QDate.fromString(text, "yyyyMMdd")
            if date.isValid():
                edit.setDate(date)
        self.recent_days.setValue(int(gc.get("recent_days") or 3))
        self.min_images.setValue(int(gc.get("min_images") or 30))
        self.preprocess_threads.setValue(int(gc.get("preprocess_threads") or 20))
        self.swarp_threads.setValue(int(gc.get("swarp_threads") or 0))
        self.workspace_edit.setText(str(gc.get("workspace") or DEFAULT_WORKSPACE))
        last_tab = int(gc.get("last_tab") or 0)
        if 0 <= last_tab < self.tabs.count():
            self.tabs.setCurrentIndex(last_tab)

    def _save_gui_config(self) -> None:
        self.gui_config.update(
            last_tab=self.tabs.currentIndex(),
            targets=self.targets_edit.toPlainText().strip(),
            start_date=self.start_date.date().toString("yyyyMMdd"),
            end_date=self.end_date.date().toString("yyyyMMdd"),
            recent_days=self.recent_days.value(),
            min_images=self.min_images.value(),
            preprocess_threads=self.preprocess_threads.value(),
            swarp_threads=self.swarp_threads.value(),
            workspace=self.workspace_edit.text().strip(),
        )

    def _on_tab_changed(self, _index: int) -> None:
        self.gui_config.update(last_tab=self.tabs.currentIndex())

    def _set_region_status(self, text: str, color: str) -> None:
        self.region_status.setText(text)
        self.region_status.setStyleSheet(f"color: {color};")

    def _set_update_status(self, text: str, color: str) -> None:
        self.update_status.setText(text)
        self.update_status.setStyleSheet(f"color: {color};")

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    # ---------------- region collect ----------------
    def _start_collect(self) -> None:
        if self._busy():
            QMessageBox.warning(self, "提示", "已有任务正在运行，请等待完成。")
            return
        start = self.start_date.date().toString("yyyyMMdd")
        end = self.end_date.date().toString("yyyyMMdd")
        if start > end:
            QMessageBox.showerror("错误", "起始日期不能大于终止日期")
            return
        self._log(f"[天区收集] 开始：{start} ~ {end}")
        self.collect_btn.setEnabled(False)
        worker = RegionCollectWorker(self.config, start, end, self)
        worker.status.connect(self._set_region_status)
        worker.log_line.connect(self._log)
        worker.finished.connect(self._on_region_finished)
        self.worker = worker
        worker.start()

    def _on_region_finished(self) -> None:
        self.collect_btn.setEnabled(True)
        self._refresh_index_info()
        self._log("[天区收集] 任务结束。")

    # ---------------- template actions ----------------
    def _build_settings(self) -> UpdateSettings:
        return UpdateSettings(
            recent_days=self.recent_days.value(),
            min_images=self.min_images.value(),
            preprocess_threads=self.preprocess_threads.value(),
            swarp_threads=self.swarp_threads.value(),
        )

    def _start_template(self, action: str) -> None:
        if self._busy():
            QMessageBox.warning(self, "提示", "已有任务正在运行，请等待完成。")
            return
        targets_text = self.targets_edit.toPlainText().strip()
        workspace_root = self.workspace_edit.text().strip() or DEFAULT_WORKSPACE
        if not targets_text:
            QMessageBox.showerror("错误", "请输入天区列表（例如 GY6_K002-8,GY6_K002-9）。")
            return
        os.makedirs(workspace_root, exist_ok=True)
        self._save_gui_config()

        self._log(f"[升级模板] 操作: {action}，天区: {targets_text}")
        self._set_buttons_enabled(False)
        worker = TemplateWorker(action, self.config, self._build_settings(),
                                targets_text, workspace_root, self)
        worker.status.connect(self._set_update_status)
        worker.log_line.connect(self._log)
        worker.finished.connect(self._on_template_finished)
        self.worker = worker
        worker.start()

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for b in (self.btn_download, self.btn_preprocess, self.btn_make):
            b.setEnabled(enabled)

    def _on_template_finished(self) -> None:
        self._set_buttons_enabled(True)
        self._log("[升级模板] 任务结束。")

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._busy():
            ret = QMessageBox.question(self, "退出", "任务仍在运行，确定退出？",
                                       QMessageBox.Yes | QMessageBox.No)
            if ret != QMessageBox.Yes:
                event.ignore()
                return
        self._save_gui_config()
        event.accept()
