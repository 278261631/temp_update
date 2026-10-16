#!/usr/bin/env python3
"""基于 QThread 的后台任务：天区收集 与 模板更新各步骤。"""

from PySide6.QtCore import QThread, Signal

from .config import AppConfig
from .template_ops import UpdateSettings, TemplateEngine, collect_regions


class _BaseWorker(QThread):
    status = Signal(str, str)   # (text, color)
    log_line = Signal(str)      # 日志文本

    def __init__(self, parent=None):
        super().__init__(parent)
        self._failed = False


class RegionCollectWorker(_BaseWorker):
    def __init__(self, config: AppConfig, start: str, end: str, parent=None):
        super().__init__(parent)
        self.config = config
        self.start_date = start
        self.end_date = end

    def run(self):
        try:
            self.status.emit("处理中...", "blue")
            collect_regions(
                self.config, self.start_date, self.end_date,
                log=lambda m: self.log_line.emit(m),
                status=lambda t, c="black": self.status.emit(t, c),
            )
        except Exception as e:
            self._failed = True
            self.status.emit(f"失败：{e}", "red")
            self.log_line.emit(f"[天区收集] 出错: {e}")


class CheckTemplatesWorker(_BaseWorker):
    results = Signal(list)  # 每个模板的结果 dict 列表

    def __init__(self, template_dir: str, report_path: str,
                 frame_dir: str = None, diff_sample: int = 0, file_list=None, parent=None):
        super().__init__(parent)
        self.template_dir = template_dir
        self.report_path = report_path
        self.frame_dir = frame_dir
        self.diff_sample = diff_sample
        self.file_list = file_list

    def run(self):
        from .check_templates import check_and_report
        try:
            res = check_and_report(
                self.template_dir, self.report_path, self.frame_dir, self.diff_sample,
                log=lambda m: self.log_line.emit(m),
                status=lambda t, c="black": self.status.emit(t, c),
                file_list=self.file_list,
            )
            self.results.emit(res.get("files", []))
        except Exception as e:
            self._failed = True
            self.status.emit(f"检查出错: {e}", "red")
            self.log_line.emit(f"[检查] 出错: {e}")


class TemplateWorker(_BaseWorker):
    """action ∈ {download, preprocess, make}"""

    ACTIONS = {"download", "preprocess", "make"}

    def __init__(self, action: str, config: AppConfig, settings: UpdateSettings,
                 targets_text: str, workspace_root: str, parent=None):
        super().__init__(parent)
        if action not in self.ACTIONS:
            raise ValueError(f"未知操作: {action}")
        self.action = action
        self.config = config
        self.settings = settings
        self.targets_text = targets_text
        self.workspace_root = workspace_root

    def run(self):
        engine = TemplateEngine(
            self.config, self.settings, self.targets_text, self.workspace_root,
            log=lambda m: self.log_line.emit(m),
            status=lambda t, c="black": self.status.emit(t, c),
        )
        try:
            if self.action == "download":
                engine.download_problem_templates()
            elif self.action == "preprocess":
                engine.preprocess_images()
            else:  # make
                engine.make_templates()
        except ImportError as e:
            self._failed = True
            self.status.emit(f"缺少依赖库: {e}", "red")
            self.log_line.emit(f"[依赖] {e}")
        except Exception as e:
            self._failed = True
            self.status.emit(f"出错: {e}", "red")
            self.log_line.emit(f"[{self.action}] 出错: {e}")
