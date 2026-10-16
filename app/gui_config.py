#!/usr/bin/env python3
"""GUI 状态配置：记录上次选择的标签页、天区列表及各项参数。

保存位置：config/gui_config.json（已被 .gitignore 忽略）。
"""

import json
import os
from datetime import datetime
from typing import Any, Dict

from .config import BASE_DIR

GUI_CONFIG_PATH = os.path.join(BASE_DIR, "config", "gui_config.json")
DEFAULT_TARGETS = "GY6_K002-8,GY6_K002-9"


class GuiConfig:
    def __init__(self, path: str = None):
        today = datetime.now().strftime("%Y%m%d")
        self.defaults: Dict[str, Any] = {
            "last_tab": 0,
            "targets": DEFAULT_TARGETS,
            "start_date": today,
            "end_date": today,
            "recent_days": 3,
            "min_images": 30,
            "preprocess_threads": 20,
            "swarp_threads": 0,
            "workspace": os.path.join(BASE_DIR, "templates_update"),
        }
        self.path = path or GUI_CONFIG_PATH
        self.data: Dict[str, Any] = dict(self.defaults)
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update({k: v for k, v in loaded.items() if k in self.defaults})
        except Exception:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        if key in self.data:
            return self.data[key]
        if default is not None:
            return default
        return self.defaults.get(key)

    def update(self, **kwargs: Any) -> None:
        self.data.update(kwargs)
        self.save()

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
