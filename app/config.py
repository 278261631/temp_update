#!/usr/bin/env python3
"""应用配置加载（URL 模板 / 天区坐标 / ASTAP 命令）。

真实配置保存在 config/url_config.json，该文件已被 .gitignore 忽略，
用于避免把数据源地址、天区坐标表、本地路径等敏感信息提交到版本库。
首次运行时若缺少该文件，会自动从 config/url_config.json.template 复制。
"""

import json
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
URL_CONFIG_PATH = os.path.join(CONFIG_DIR, "url_config.json")
URL_CONFIG_TEMPLATE = os.path.join(CONFIG_DIR, "url_config.json.template")

TELESCOPE_NAMES = ["GY1", "GY2", "GY3", "GY4", "GY5", "GY6"]
K_NUMBERS = [f"K{i:03d}" for i in range(1, 100)]  # K001 - K099


class AppConfig:
    """封装 URL 配置文件的读取与 URL 构建。"""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or URL_CONFIG_PATH
        self.config: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.config_path) and os.path.exists(URL_CONFIG_TEMPLATE):
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            shutil.copyfile(URL_CONFIG_TEMPLATE, self.config_path)
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(
                f"未找到 URL 配置文件: {self.config_path}\n"
                "请从 config/url_config.json.template 复制并填写真实配置。"
            )
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, ensure_ascii=False)

    # ----- 基础数据源 -----
    def get_base_url(self) -> str:
        return self.config.get("base_urls", {}).get("primary", "")

    def get_template_type(self) -> str:
        return self.config.get("url_template_type", "standard")

    def set_template_type(self, template_type: str) -> None:
        if template_type in self.config.get("url_templates", {}):
            self.config["url_template_type"] = template_type
            self.save()

    def get_url_template(self) -> str:
        templates = self.config.get("url_templates", {})
        template = templates.get(self.get_template_type(), "")
        return template.replace("{base_url}", self.get_base_url())

    def get_url_template_options(self) -> Dict[str, str]:
        return {
            "standard": "标准格式 (/{date}/)",
            "with_year": "包含年份 (/{year}/{date}/)",
        }

    def _format_params(self, tel_name: str, date: str, k_number: str = "") -> Dict[str, str]:
        params = {
            "base_url": self.get_base_url(),
            "tel_name": tel_name,
            "date": date,
            "k_number": k_number,
        }
        templates = self.config.get("url_templates", {})
        template = templates.get(self.get_template_type(), "")
        if "{year_of_date}" in template:
            try:
                params["year_of_date"] = date[:4] if len(date) >= 4 else datetime.now().strftime("%Y")
            except Exception:
                params["year_of_date"] = datetime.now().strftime("%Y")
        return params

    def build_url(self, tel_name: str, date: str, k_number: str = "") -> str:
        template = self.get_url_template()
        return template.format(**self._format_params(tel_name, date, k_number))

    def build_base_url(self, tel_name: str, date: str) -> str:
        """构建不包含天区编号的基础 URL（用于扫描可用天区）。"""
        template = self.get_url_template()
        full_url = template.format(**self._format_params(tel_name, date, ""))
        return full_url.rstrip("/")

    # ----- 天区坐标 -----
    def get_region_data(self) -> Dict[str, List[float]]:
        data = self.config.get("regionData", {})
        return data if isinstance(data, dict) else {}

    # ----- ASTAP -----
    def get_astap_exe(self) -> str:
        return self.config.get("astap_exe", "astap")

    def get_astap_cmd_template(self) -> str:
        return self.config.get("astap_cmd_template", "")

    # ----- 校验 -----
    @staticmethod
    def validate_date(date_str: str) -> bool:
        try:
            datetime.strptime(date_str, "%Y%m%d")
            return True
        except (ValueError, TypeError):
            return False
