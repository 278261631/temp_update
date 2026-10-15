#!/usr/bin/env python3
"""FITS 文件扫描器：从网页/目录列表页解析可下载的 FITS 文件。"""

import re
import logging
from typing import List, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

FITS_EXTENSIONS = (".fits", ".fit", ".fts")
EXCLUDE_PATTERNS = ("calibration", "_fz", "flat", "fiel")


class _BaseScanner:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        self.session.proxies = {"http": None, "https": None}
        self.session.trust_env = False  # 忽略系统/环境中的代理设置，避免代理不可用导致失败
        self.session.verify = False
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    def _get(self, url: str) -> str:
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            return response.text
        except Exception as e:
            self.logger.warning("requests 失败，尝试 urllib: %s", e)
            return self._get_content_with_urllib(url)

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

    @staticmethod
    def _is_fits_file(name: str) -> bool:
        lower = name.lower()
        if not lower.endswith(FITS_EXTENSIONS):
            return False
        return not any(p in lower for p in EXCLUDE_PATTERNS)

    @staticmethod
    def _extract_filename(href: str) -> str:
        return href.split("?")[0].split("/")[-1]


class WebFitsScanner(_BaseScanner):
    """扫描普通网页获取 FITS 文件列表。"""

    def scan_fits_files(self, base_url: str) -> List[Tuple[str, str, int]]:
        content = self._get(base_url)
        soup = BeautifulSoup(content, "html.parser")
        fits_files = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if self._is_fits_file(href):
                full_url = urljoin(base_url, href)
                filename = self._extract_filename(href)
                size = self._get_file_size(full_url)
                fits_files.append((filename, full_url, size))
        self.logger.info("扫描完成，找到 %d 个 FITS 文件", len(fits_files))
        return fits_files

    def _get_file_size(self, url: str) -> int:
        try:
            response = self.session.head(url, timeout=10)
            if response.status_code == 200:
                length = response.headers.get("content-length")
                if length:
                    return int(length)
        except Exception:
            pass
        return 0


class DirectoryScanner(_BaseScanner):
    """扫描 Apache 风格目录列表页获取 FITS 文件。"""

    def scan_directory_listing(self, url: str) -> List[Tuple[str, str, int]]:
        content = self._get(url)
        fits_files = []

        pattern = r'<a\s+href="([^"]*\.fits?)"[^>]*>([^<]*)</a>'
        for href, _display in re.findall(pattern, content, re.IGNORECASE):
            if self._is_fits_file(href):
                full_url = urljoin(url, href)
                filename = self._extract_filename(href)
                size = self._extract_size_from_listing(content, href)
                fits_files.append((filename, full_url, size))

        if not fits_files:
            fits_files = self._parse_with_beautifulsoup(url, content)

        self.logger.info("目录扫描完成，找到 %d 个 FITS 文件", len(fits_files))
        return fits_files

    @staticmethod
    def _extract_size_from_listing(html_content: str, filename: str) -> int:
        try:
            for line in html_content.split("\n"):
                if filename in line:
                    m = re.search(r"(\d+(?:\.\d+)?)\s*([KMGT]?B)", line, re.IGNORECASE)
                    if m:
                        size = float(m.group(1))
                        unit = m.group(2).upper()
                        multipliers = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
                        return int(size * multipliers.get(unit, 1))
        except Exception:
            pass
        return 0

    def _parse_with_beautifulsoup(self, base_url: str, html_content: str) -> List[Tuple[str, str, int]]:
        soup = BeautifulSoup(html_content, "html.parser")
        fits_files = []
        for link in soup.find_all("a", href=True):
            href = link["href"]
            if self._is_fits_file(href):
                fits_files.append((self._extract_filename(href), urljoin(base_url, href), 0))
        return fits_files
