#!/usr/bin/env python3
"""PySide6 主界面：天区收集 + 升级模板。"""

import os
import re
import subprocess
from datetime import datetime

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QDialog,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGraphicsPixmapItem, QGraphicsScene,
    QGraphicsView, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .config import AppConfig, BASE_DIR
from .gui_config import GuiConfig
from .region_index import INDEX_PATH, load_gy1_index
from .template_ops import UpdateSettings
from .workers import CheckTemplatesWorker, RegionCollectWorker, TemplateWorker
from .check_templates import is_template_file

DEFAULT_WORKSPACE = os.path.join(BASE_DIR, "templates_update")


class _ZoomView(QGraphicsView):
    """支持鼠标拖拽平移、滚轮缩放的图像视图。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)

    def wheelEvent(self, event):  # noqa: N802
        factor = 1.25 if event.angleDelta().y() > 0 else 1.0 / 1.25
        self.scale(factor, factor)


class PreviewDialog(QDialog):
    def __init__(self, pixmap: QPixmap, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 820)
        layout = QVBoxLayout(self)

        self.view = _ZoomView(self)
        self.scene = QGraphicsScene(self)
        self.item = QGraphicsPixmapItem(pixmap)
        self.scene.addItem(self.item)
        self.view.setScene(self.scene)
        self.view.setBackgroundBrush(QColor("black"))
        layout.addWidget(self.view, 1)

        row = QHBoxLayout()
        hint = QLabel("拖拽平移 · 滚轮缩放")
        hint.setStyleSheet("color: gray;")
        fit_btn = QPushButton("适应窗口")
        fit_btn.clicked.connect(self._fit)
        one_btn = QPushButton("1:1")
        one_btn.clicked.connect(self._reset)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(hint)
        row.addStretch(1)
        row.addWidget(fit_btn)
        row.addWidget(one_btn)
        row.addWidget(close_btn)
        layout.addLayout(row)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._fit()

    def _fit(self):
        self.view.fitInView(self.item, Qt.KeepAspectRatio)

    def _reset(self):
        self.view.resetTransform()


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
        self.tabs.addTab(self._build_check_tab(), "检查模板")
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
        self.recent_days.setRange(1, 1000)
        self.recent_days.setValue(3)
        self.recent_days.setMinimumWidth(80)
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

        grid.addWidget(QLabel("合成方式:"), 3, 0)
        self.combine_combo = QComboBox()
        self.combine_combo.addItem("中值 MEDIAN", "MEDIAN")
        self.combine_combo.addItem("平均 AVERAGE", "AVERAGE")
        grid.addWidget(self.combine_combo, 3, 1)

        grid.addWidget(QLabel("预处理线程数:"), 4, 0)
        self.preprocess_threads = QSpinBox()
        self.preprocess_threads.setRange(1, 64)
        self.preprocess_threads.setValue(20)
        grid.addWidget(self.preprocess_threads, 4, 1)

        grid.addWidget(QLabel("检查抽样对照数(0=关):"), 5, 0)
        self.check_diff = QSpinBox()
        self.check_diff.setRange(0, 999)
        self.check_diff.setValue(0)
        grid.addWidget(self.check_diff, 5, 1)

        grid.addWidget(QLabel("模板加常数(k·σ, 0=关):"), 6, 0)
        self.add_const_sigma = QDoubleSpinBox()
        self.add_const_sigma.setRange(0.0, 100.0)
        self.add_const_sigma.setSingleStep(0.5)
        self.add_const_sigma.setDecimals(1)
        self.add_const_sigma.setValue(5.0)
        grid.addWidget(self.add_const_sigma, 6, 1)

        grid.addWidget(QLabel("无覆盖像素(NaN):"), 7, 0)
        self.nan_fill_combo = QComboBox()
        self.nan_fill_combo.addItem("保留 NaN", "keep")
        self.nan_fill_combo.addItem("填背景常数", "bg")
        self.nan_fill_combo.addItem("填 0", "zero")
        grid.addWidget(self.nan_fill_combo, 7, 1)

        grid.addWidget(QLabel("最小覆盖帧数(0=关):"), 8, 0)
        self.min_frames = QSpinBox()
        self.min_frames.setRange(0, 9999)
        self.min_frames.setValue(0)
        grid.addWidget(self.min_frames, 8, 1)

        self.overwrite_chk = QCheckBox("覆盖已存在模板")
        self.overwrite_chk.setChecked(False)
        grid.addWidget(self.overwrite_chk, 9, 0, 1, 2)
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
        try:
            self.add_const_sigma.setValue(float(gc.get("add_constant_sigma")))
        except Exception:
            self.add_const_sigma.setValue(5.0)
        idx_nf = self.nan_fill_combo.findData(str(gc.get("nan_fill") or "keep"))
        self.nan_fill_combo.setCurrentIndex(idx_nf if idx_nf >= 0 else 0)
        try:
            self.min_frames.setValue(int(gc.get("min_frames") or 0))
        except Exception:
            self.min_frames.setValue(0)
        idx_cb = self.combine_combo.findData(str(gc.get("swarp_combine") or "MEDIAN"))
        self.combine_combo.setCurrentIndex(idx_cb if idx_cb >= 0 else 0)
        self.overwrite_chk.setChecked(bool(gc.get("overwrite_templates")))
        self.workspace_edit.setText(str(gc.get("workspace") or DEFAULT_WORKSPACE))
        check_dir = str(gc.get("check_dir") or "")
        self.check_dir_edit.setText(check_dir)
        if check_dir and os.path.isdir(check_dir):
            self._list_dir_contents(check_dir)
        last_tab = int(gc.get("last_tab") or 0)
        if 0 <= last_tab < self.tabs.count():
            self.tabs.setCurrentIndex(last_tab)

        # 载入完成后再接自动保存，避免加载过程回写；任何改动即时落盘，不会因异常退出而丢配置
        for w in (self.recent_days, self.min_images, self.swarp_threads,
                  self.preprocess_threads, self.check_diff, self.add_const_sigma,
                  self.min_frames):
            w.valueChanged.connect(self._autosave)
        for w in (self.workspace_edit, self.check_dir_edit):
            w.editingFinished.connect(self._autosave)
        self.targets_edit.textChanged.connect(self._autosave)
        self.nan_fill_combo.currentIndexChanged.connect(self._autosave)
        self.combine_combo.currentIndexChanged.connect(self._autosave)
        self.overwrite_chk.toggled.connect(self._autosave)

    def _autosave(self, *args) -> None:
        self._save_gui_config()

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
            add_constant_sigma=self.add_const_sigma.value(),
            nan_fill=self.nan_fill_combo.currentData(),
            swarp_combine=self.combine_combo.currentData(),
            min_frames=self.min_frames.value(),
            overwrite_templates=self.overwrite_chk.isChecked(),
            workspace=self.workspace_edit.text().strip(),
            check_dir=self.check_dir_edit.text().strip(),
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
            add_constant_sigma=self.add_const_sigma.value(),
            nan_fill=self.nan_fill_combo.currentData() or "keep",
            swarp_combine=self.combine_combo.currentData() or "MEDIAN",
            min_frames=self.min_frames.value(),
            overwrite_templates=self.overwrite_chk.isChecked(),
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

    # ---------------- check tab ----------------
    def _build_check_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        top = QGroupBox("模板目录")
        row = QHBoxLayout(top)
        self.check_dir_edit = QLineEdit()
        self.check_dir_edit.setPlaceholderText("选择包含模板 *.fits 的目录（例如 templates_update/output/gy6）")
        browse = QPushButton("浏览...")
        browse.clicked.connect(self._pick_check_dir)
        self.check_btn = QPushButton("检查")
        self.check_btn.clicked.connect(self._start_check)
        self.reveal_btn = QPushButton("转到文件")
        self.reveal_btn.clicked.connect(self._reveal_current)
        self.preview_btn = QPushButton("查看")
        self.preview_btn.clicked.connect(self._preview_current)
        self.to_targets_btn = QPushButton("填入天区列表")
        self.to_targets_btn.clicked.connect(self._fill_targets_from_selection)
        row.addWidget(self.check_dir_edit, 1)
        row.addWidget(browse)
        row.addWidget(self.check_btn)
        row.addWidget(self.reveal_btn)
        row.addWidget(self.preview_btn)
        row.addWidget(self.to_targets_btn)
        layout.addWidget(top)

        self.check_status = QLabel("就绪")
        layout.addWidget(self.check_status)

        self._check_results = []
        self._dir_files = []
        self._results_by_name = {}
        self.check_table = QTableWidget(0, 11)
        self.check_table.setHorizontalHeaderLabels(
            ["文件", "状态", "尺寸", "NaN/Inf", "FOV宽′", "FOV高′", "FWHM", "椭圆度",
             "LM5SIG", "背景RMS", "问题"])
        self.check_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.check_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.check_table.setSortingEnabled(True)
        header = self.check_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(10, QHeaderView.Stretch)
        layout.addWidget(self.check_table, 1)

        note = QLabel("颜色：绿=正常，橙=警告，红=错误。选中若干行则只检查选中项，未选中则检查目录全部。"
                      "（几何离群判据需 ≥8 个样本，子集检查时通常不触发。）")
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)
        return page

    def _pick_check_dir(self) -> None:
        start = self.check_dir_edit.text() or self.workspace_edit.text() or BASE_DIR
        path = QFileDialog.getExistingDirectory(self, "选择模板目录", start)
        if path:
            self.check_dir_edit.setText(path)
            self._list_dir_contents(path)

    def _current_file(self) -> str:
        row = self.check_table.currentRow()
        if row < 0:
            sel = self.check_table.selectionModel().selectedRows()
            row = sel[0].row() if sel else -1
        if row < 0:
            return ""
        item = self.check_table.item(row, 0)
        if item is None:
            return ""
        return os.path.join(self.check_dir_edit.text().strip(), item.text())

    def _fill_targets_from_selection(self) -> None:
        rows = sorted({idx.row() for idx in self.check_table.selectionModel().selectedRows()})
        if not rows:
            cur = self.check_table.currentRow()
            if cur >= 0:
                rows = [cur]
        if not rows:
            QMessageBox.warning(self, "提示", "请先在表格中选中要处理的行。")
            return
        tel = os.path.basename(os.path.normpath(self.check_dir_edit.text().strip()))
        system = tel.upper() if re.match(r"^gy\d+$", tel, re.IGNORECASE) else ""
        items, skipped = [], []
        for r in rows:
            item = self.check_table.item(r, 0)
            if item is None:
                continue
            region = os.path.splitext(item.text())[0]
            if not re.match(r"^K\d{3}-\d+$", region, re.IGNORECASE):
                skipped.append(item.text())
                continue
            items.append(f"{system}_{region}" if system else region)
        if not items:
            QMessageBox.warning(self, "提示", "选中的行里没有可识别的天区名（形如 K017-1）。")
            return
        self.targets_edit.setPlainText(", ".join(items))
        self._save_gui_config()
        if not system:
            self._log("[检查模板] 未能从目录名识别系统名，天区未加系统前缀。")
        if skipped:
            self._log(f"[检查模板] 跳过无法识别的行: {', '.join(skipped)}")
        self._log(f"[检查模板] 已填入天区列表 {len(items)} 项。")
        self.tabs.setCurrentIndex(1)  # 切到“升级模板”

    def _reveal_current(self) -> None:
        path = self._current_file()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "提示", "请先在表格中选中一行模板。")
            return
        try:
            subprocess.Popen(["explorer", "/select," + os.path.normpath(path)])
        except Exception:
            os.startfile(os.path.dirname(path))

    def _preview_current(self) -> None:
        path = self._current_file()
        if not path or not os.path.exists(path):
            QMessageBox.warning(self, "提示", "请先在表格中选中一行模板。")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            from .preview import asinh_png
            png = asinh_png(path)
        except Exception as e:
            QApplication.restoreOverrideCursor()
            QMessageBox.showerror("错误", f"生成预览失败:\n{e}")
            return
        QApplication.restoreOverrideCursor()
        pixmap = QPixmap(png)
        if pixmap.isNull():
            QMessageBox.showerror("错误", "无法加载预览图。")
            return
        self._log(f"[检查模板] 预览: {png}")
        dlg = PreviewDialog(pixmap, f"asinh 预览 - {os.path.basename(path)}", self)
        dlg.exec()

    def _list_dir_contents(self, path: str) -> None:
        """列出目录内全部模板，并尝试载入上次保存的结果。"""
        try:
            self._dir_files = sorted(f for f in os.listdir(path)
                                     if os.path.isfile(os.path.join(path, f)) and is_template_file(f))
        except Exception as e:
            self._dir_files = []
            self.check_status.setText(f"无法读取目录: {e}")
            self.check_status.setStyleSheet("color: red;")
            return
        self._results_by_name = self._load_saved_results(path)
        self._refresh_check_table()
        n_re = len(self._results_by_name)
        self.check_status.setText(f"目录共 {len(self._dir_files)} 个模板；已载入上次结果 {n_re} 个。点击“检查”开始。")
        self.check_status.setStyleSheet("color: black;")

    def _check_store_dir(self, template_dir: str) -> str:
        """检查结果的存放目录（在当前项目目录下，而非模板目录）。"""
        norm = os.path.normpath(template_dir)
        name = os.path.basename(norm) or "root"
        parent = os.path.basename(os.path.dirname(norm))
        key = f"{parent}__{name}" if parent and parent not in (".", os.sep) else name
        store = os.path.join(BASE_DIR, "check_results", key)
        os.makedirs(store, exist_ok=True)
        return store

    def _results_json(self, path: str) -> str:
        return os.path.join(self._check_store_dir(path), "fits_check_results.json")

    def _report_path_for(self, template_dir: str) -> str:
        return os.path.join(self._check_store_dir(template_dir), "fits_check_report.txt")

    def _load_saved_results(self, path: str) -> dict:
        import json
        fp = self._results_json(path)
        if not os.path.isfile(fp):
            return {}
        try:
            with open(fp, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return {e["name"]: e for e in data if isinstance(e, dict) and e.get("name")}
        except Exception:
            return {}

    def _refresh_check_table(self) -> None:
        """按目录清单渲染：已检查的显示结果，其余显示“未检查”。"""
        self.check_table.setRowCount(0)
        for name in self._dir_files:
            e = self._results_by_name.get(name)
            if e is None:
                e = {"name": name, "_pending": True}
            self._add_check_row(e)
        self._check_results = list(self._results_by_name.values())

    def _persist_results(self, path: str) -> None:
        from .check_templates import save_results
        fp = self._results_json(path)
        entries = [self._results_by_name[n] for n in self._dir_files if n in self._results_by_name]
        save_results(entries, fp)

    def _add_check_row(self, e: dict) -> None:
        # 排序期间禁止刷新以免错行
        self.check_table.setSortingEnabled(False)
        r = self.check_table.rowCount()
        self.check_table.insertRow(r)
        errs, warns = e.get("errors", []), e.get("warnings", [])
        if e.get("_pending"):
            status, color = "未检查", QColor("gray")
        elif errs:
            status, color = "错误", QColor("red")
        elif warns:
            status, color = "警告", QColor("#c77700")
        else:
            status, color = "正常", QColor("green")

        def fmt(v, nd=2):
            try:
                return f"{float(v):.{nd}f}"
            except Exception:
                return "-"

        shape = e.get("shape")
        shape = f"{shape[0]}×{shape[1]}" if isinstance(shape, (tuple, list)) else "-"
        nf = e.get("nonfinite")
        nf = "-" if nf is None else str(nf)
        issue = "; ".join(errs + warns)
        vals = [e.get("name", ""), status, shape, nf,
                fmt(e.get("fov_w_arcmin")), fmt(e.get("fov_h_arcmin")),
                fmt(e.get("fwhm")), fmt(e.get("ellipticity"), 3), fmt(e.get("lm5sig")),
                fmt(e.get("bg_rms")), issue]
        for c, v in enumerate(vals):
            item = QTableWidgetItem(str(v))
            if c == 1:
                item.setForeground(color)
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            self.check_table.setItem(r, c, item)
        self.check_table.setSortingEnabled(True)

    def _start_check(self) -> None:
        if self._busy():
            QMessageBox.warning(self, "提示", "已有任务正在运行，请等待完成。")
            return
        template_dir = self.check_dir_edit.text().strip()
        if not os.path.isdir(template_dir):
            QMessageBox.showerror("错误", f"模板目录不存在:\n{template_dir}")
            return
        tel = os.path.basename(os.path.normpath(template_dir)).lower()
        frame_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.normpath(template_dir))), "update", tel)
        report_path = self._report_path_for(template_dir)

        # 有选中行 -> 只检查选中项；否则检查全部
        file_list = None
        rows = sorted({idx.row() for idx in self.check_table.selectionModel().selectedRows()})
        if rows:
            file_list = [self.check_table.item(r, 0).text() for r in rows
                         if self.check_table.item(r, 0) is not None]
        scope = f"选中 {len(file_list)} 个" if file_list else "全部"
        self._log(f"[检查模板] 目录: {template_dir}（检查范围: {scope}）")
        self._save_gui_config()
        self._set_buttons_enabled(False)
        worker = CheckTemplatesWorker(template_dir, report_path,
                                      frame_dir=frame_dir,
                                      diff_sample=self.check_diff.value(),
                                      file_list=file_list, parent=self)
        worker.status.connect(self._set_check_status)
        worker.log_line.connect(self._log)
        worker.results.connect(self._populate_check_table)
        worker.finished.connect(self._on_check_finished)
        self.worker = worker
        worker.start()

    def _set_check_status(self, text: str, color: str) -> None:
        self.check_status.setText(text)
        self.check_status.setStyleSheet(f"color: {color};")

    def _populate_check_table(self, entries: list) -> None:
        # 合并到累计结果（按文件名），未参与本次检查的保持原状态
        for e in entries:
            name = e.get("name")
            if name:
                self._results_by_name[name] = e
        template_dir = self.check_dir_edit.text().strip()
        if template_dir and os.path.isdir(template_dir):
            self._persist_results(template_dir)
        self._refresh_check_table()

    def _on_check_finished(self) -> None:
        self._set_buttons_enabled(True)

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for b in (self.btn_download, self.btn_preprocess, self.btn_make, self.check_btn):
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
