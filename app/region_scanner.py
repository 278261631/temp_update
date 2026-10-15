#!/usr/bin/env python3
"""天区扫描器：从远端目录列表页解析可用的天区（K 开头的目录）。"""

import re
import logging
from typing import List

import requests
from bs4 import BeautifulSoup


class RegionScanner:
    """从给定基础 URL 中获取可用的天区列表。"""

    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.logger = logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
        })
        self.session.proxies = {"http": None, "https": None}
        self.session.trust_env = False  # 忽略系统/环境中的代理设置，避免代理不可用导致失败
        self.session.verify = False

        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    def scan_available_regions(self, base_url: str) -> List[str]:
        try:
            self.logger.info("开始扫描天区: %s", base_url)
            response = self.session.get(base_url, timeout=self.timeout)
            response.raise_for_status()
            content = response.text
        except Exception as e:
            self.logger.warning("requests 失败，尝试 urllib: %s", e)
            content = self._get_content_with_urllib(base_url)

        soup = BeautifulSoup(content, "html.parser")
        regions = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if self._is_region_directory(href):
                name = self._extract_region_name(href)
                if name and name not in regions:
                    regions.append(name)
        regions.sort()
        self.logger.info("扫描完成，找到 %d 个天区", len(regions))
        return regions

    @staticmethod
    def _is_region_directory(href: str) -> bool:
        clean_href = href.split("?")[0].split("#")[0]
        dir_name = clean_href.strip("/").split("/")[-1]
        return bool(re.match(r"^K\d{3}$", dir_name, re.IGNORECASE))

    @staticmethod
    def _extract_region_name(href: str) -> str:
        clean_href = href.split("?")[0].split("#")[0]
        return clean_href.strip("/").split("/")[-1].upper()

    def _get_content_with_urllib(self, url: str) -> str:
        import ssl
        import urllib.request

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        proxy_handler = urllib.request.ProxyHandler({})
        https_handler = urllib.request.HTTPSHandler(context=context)
        opener = urllib.request.build_opener(proxy_handler, https_handler)

        req = urllib.request.Request(url, headers={"User-Agent": self.session.headers["User-Agent"]})
        with opener.open(req, timeout=self.timeout) as response:
            return response.read().decode("utf-8", errors="ignore")
