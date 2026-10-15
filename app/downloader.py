#!/usr/bin/env python3
"""FITS 文件下载器（带重试与进度回调）。"""

import os
import time
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable, Optional


class FitsDownloader:
    def __init__(self, max_workers: int = 1, retry_times: int = 3, timeout: int = 30,
                 user_agent: Optional[str] = None):
        self.max_workers = max_workers
        self.retry_times = retry_times
        self.timeout = timeout
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        self.download_stats = {"total": 0, "completed": 0, "skipped": 0, "failed": 0}
        self.stats_lock = threading.Lock()

    @staticmethod
    def get_filename_from_url(url: str) -> str:
        return os.path.basename(url.split("?")[0])

    def download_single_file(self, url: str, download_dir: str,
                             progress_callback: Optional[Callable] = None) -> str:
        """下载单个文件；已存在且非空时返回“跳过已存在文件”。"""
        filename = self.get_filename_from_url(url)
        file_path = os.path.join(download_dir, filename)

        if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            with self.stats_lock:
                self.download_stats["skipped"] += 1
            return f"跳过已存在文件: {filename}"

        for attempt in range(self.retry_times):
            try:
                req = urllib.request.Request(url)
                req.add_header("User-Agent", self.user_agent)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

                with opener.open(req, timeout=self.timeout) as response:
                    total_size = response.headers.get("Content-Length")
                    total_size = int(total_size) if total_size else None
                    downloaded = 0
                    with open(file_path, "wb") as f:
                        while True:
                            chunk = response.read(8192)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if progress_callback:
                                progress_callback(downloaded, total_size, filename)

                if os.path.getsize(file_path) > 0:
                    with self.stats_lock:
                        self.download_stats["completed"] += 1
                    return f"下载成功: {filename}"
                raise Exception("下载的文件大小为0")

            except Exception as e:
                if attempt < self.retry_times - 1:
                    time.sleep(2 ** attempt)
                else:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                    with self.stats_lock:
                        self.download_stats["failed"] += 1
                    return f"下载失败: {filename} - {e}"

        return f"下载失败: {filename}"

    def download_files(self, urls: Iterable[str], download_dir: str) -> None:
        os.makedirs(download_dir, exist_ok=True)
        urls = list(urls)
        self.download_stats["total"] = len(urls)
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(self.download_single_file, url, download_dir): url for url in urls}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    with self.stats_lock:
                        self.download_stats["failed"] += 1
