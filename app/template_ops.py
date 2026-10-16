#!/usr/bin/env python3
"""天区收集 / 升级模板 的核心逻辑（与 GUI 解耦，便于测试）。"""

import os
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional

from .config import AppConfig
from .region_scanner import RegionScanner
from .fits_scanner import WebFitsScanner, DirectoryScanner
from .downloader import FitsDownloader
from .region_index import load_gy1_index, save_gy1_index
from .targets import parse_targets, find_recent_dates_for_region

LogFn = Callable[[str], None]
StatusFn = Callable[[str, str], None]


def _noop_log(_msg: str) -> None:
    pass


def _noop_status(_msg: str, _color: str = "black") -> None:
    pass


def format_bytes(num) -> str:
    if num is None:
        return "未知"
    try:
        num = float(num)
    except Exception:
        return str(num)
    for unit in ["B", "KB", "MB", "GB"]:
        if num < 1024.0:
            return f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}TB"


@dataclass
class UpdateSettings:
    recent_days: int = 3
    min_images: int = 30
    preprocess_threads: int = 20
    lm_thres: float = 15.0
    ell_thres: float = 0.2
    fwhm_thres: float = 10.0
    swarp_threads: int = 0  # SWarp 线程数（0=自动）
    swarp_resampling: str = "BILINEAR"  # SWarp 重采样核


def collect_regions(config: AppConfig, start: str, end: str,
                    log: LogFn = _noop_log, status: StatusFn = _noop_status) -> dict:
    """按日期范围收集 GY1 天区：优先读取本地缓存，缺失时才扫描远端。"""
    scanner = RegionScanner()
    index = load_gy1_index()
    total = from_cache = scanned = 0

    dt = datetime.strptime(start, "%Y%m%d")
    end_dt = datetime.strptime(end, "%Y%m%d")
    one_day = timedelta(days=1)

    while dt <= end_dt:
        date_str = dt.strftime("%Y%m%d")
        total += 1
        regions = index.get(date_str)
        if isinstance(regions, list) and regions:
            from_cache += 1
            log(f"[GY1][{date_str}] 本地已有缓存，跳过扫描。")
        else:
            try:
                base_url = config.build_base_url("GY1", date_str)
                scanned_regions = scanner.scan_available_regions(base_url)
                normalized = sorted({str(r).upper() for r in scanned_regions if r})
                index[date_str] = normalized
                scanned += 1
                log(f"[GY1][{date_str}] 扫描完成，{len(normalized)} 个天区 -> 已缓存。")
                time.sleep(0.5)  # 保护服务器
            except Exception as e:
                log(f"[GY1][{date_str}] 扫描失败: {e}")
        status(f"进度: {from_cache + scanned}/{total} 天；缓存: {from_cache} 天；扫描: {scanned} 天", "green")
        dt += one_day

    save_gy1_index(index)
    status(f"完成：合计{total}天，其中缓存{from_cache}天，扫描{scanned}天。", "green")
    return index


class TemplateEngine:
    """下载 -> 预处理 -> 生成模板 的成套操作。"""

    def __init__(self, config: AppConfig, settings: UpdateSettings,
                 targets_text: str, workspace_root: str,
                 log: LogFn = _noop_log, status: StatusFn = _noop_status):
        self.config = config
        self.settings = settings
        self.targets_text = targets_text
        self.workspace_root = workspace_root
        self.update_root = os.path.join(workspace_root, "update")
        self.output_root = os.path.join(workspace_root, "output")
        self.bad_dir = os.path.join(workspace_root, "bad_img")
        self.log = log
        self.status = status

    # ---------- 通用 ----------
    def _load_targets(self):
        system_name, problems = parse_targets("", self.targets_text)
        if not system_name:
            self.status("无法识别系统名称，请在天区中带上系统前缀（例如 GY6_K002-8）。", "red")
            return None, None
        tel = system_name.lower()
        self.update_root = os.path.join(self.workspace_root, "update", tel)
        self.output_root = os.path.join(self.workspace_root, "output", tel)
        return system_name, problems

    def _select_candidates(self, fits_files, k_full):
        pattern = k_full.lower()
        return [(fn, url, size) for fn, url, size in fits_files if pattern in fn.lower()]

    def _scan_region_url(self, url: str):
        try:
            files = self.directory_scanner.scan_directory_listing(url)
            if not files:
                files = self.web_scanner.scan_fits_files(url)
            return files
        except Exception as e:
            self.log(f"[扫描] 失败 {url}: {e}")
            return []

    @property
    def directory_scanner(self) -> DirectoryScanner:
        scanner = getattr(self, "_dir_scanner", None)
        if scanner is None:
            scanner = DirectoryScanner()
            self._dir_scanner = scanner
        return scanner

    @property
    def web_scanner(self) -> WebFitsScanner:
        scanner = getattr(self, "_web_scanner", None)
        if scanner is None:
            scanner = WebFitsScanner()
            self._web_scanner = scanner
        return scanner

    def _make_downloader(self) -> FitsDownloader:
        return FitsDownloader(max_workers=1, retry_times=3, timeout=30)

    def _compute_lm5sig(self, fits_path: Path) -> Optional[float]:
        import numpy as np
        from astropy.io import fits
        try:
            check_path = fits_path.with_name(f"{fits_path.stem}.check.fits")
            if not check_path.exists():
                return None
            with fits.open(check_path) as chdul:
                data = chdul[0].data
            if data is None:
                return None
            data = np.asarray(data, dtype=np.float64)
            med = float(np.median(data))
            if med <= 0:
                return None
            with fits.open(str(fits_path), mode="update") as hdul2:
                hdr = hdul2[0].header
                exptime = float(hdr.get("EXPOSURE", hdr.get("EXPTIME", 1.0)) or 1.0)
                if exptime <= 0:
                    exptime = 1.0
                zp = None
                for key in ("ZP", "PHOTZP", "MAGZP", "ZP_MAG"):
                    if key in hdr:
                        zp = hdr.get(key)
                        break
                zp = float(zp) if zp is not None else 25.0
                lmag = float(round(zp - 2.5 * np.log10(med * 5.0 * np.pi * 4.0 ** 2 / exptime), 3))
                hdr["LM5SIG"] = (lmag, "5-sigma limiting magnitude")
                hdul2.flush()
            return lmag
        except Exception as e:
            self.log(f"[LM5SIG] 计算失败 {fits_path}: {e}")
            return None

    def _quality_of(self, file_path: str):
        """返回 (is_bad, reasons)。"""
        import numpy as np
        from astropy.io import fits
        from .quality import FITSQualityAnalyzer

        analyzer = getattr(self, "_quality", None)
        if analyzer is None:
            analyzer = FITSQualityAnalyzer()
            self._quality = analyzer

        reasons: List[str] = []
        is_bad = False

        try:
            if not self.astap_processor.process_fits_file(file_path):
                reasons.append("ASTAP失败")
                is_bad = True
        except Exception as e:
            self.log(f"[预处理] ASTAP异常 {file_path}: {e}")
            reasons.append("ASTAP异常")
            is_bad = True

        fwhm = ellip = float("nan")
        lm_val = None
        try:
            with fits.open(file_path) as hdul:
                data = hdul[0].data
            if data is not None:
                metrics = analyzer.calculate_quality_metrics(data.astype(np.float64))
                if metrics:
                    fwhm = float(metrics.get("fwhm", float("nan")))
                    ellip = float(metrics.get("ellipticity", float("nan")))
                    lm_val = metrics.get("lm5sig")
        except Exception as e:
            self.log(f"[预处理] 质量分析失败 {file_path}: {e}")

        lm_from_check = self._compute_lm5sig(Path(file_path))
        if lm_from_check is not None:
            lm_val = lm_from_check

        s = self.settings
        if lm_val is None or not (isinstance(lm_val, (int, float)) and np.isfinite(lm_val)):
            reasons.append("LM未知")
            is_bad = True
        elif lm_val < s.lm_thres:
            reasons.append(f"LM5SIG={lm_val:.2f}<{s.lm_thres}")
            is_bad = True
        if not (isinstance(fwhm, (int, float)) and np.isfinite(fwhm)) or fwhm > s.fwhm_thres:
            reasons.append(f"FWHM={fwhm}")
            is_bad = True
        if not (isinstance(ellip, (int, float)) and np.isfinite(ellip)) or ellip > s.ell_thres:
            reasons.append(f"ELL={ellip}")
            is_bad = True

        return is_bad, reasons

    @property
    def astap_processor(self):
        proc = getattr(self, "_astap", None)
        if proc is None:
            from .astap_processor import ASTAPProcessor
            proc = ASTAPProcessor(self.config)
            self._astap = proc
        return proc

    # ---------- 下载目标天区对应观测文件 ----------
    def download_problem_templates(self) -> None:
        system_name, selected = self._load_targets()
        if system_name is None:
            return
        if not selected:
            self.status("请输入要处理的天区列表（例如 GY6_K002-8,GY6_K002-9）。", "orange")
            return
        index = load_gy1_index()
        if not index:
            self.status("GY1索引为空，请先执行“天区收集”。", "red")
            return

        system_upper = system_name.upper()
        os.makedirs(self.update_root, exist_ok=True)
        downloader = self._make_downloader()

        tasks = []
        for item in selected:
            dates = find_recent_dates_for_region(index, item["k_number"], self.settings.recent_days)
            if not dates:
                self.log(f"[模板更新] 天区 {item['k_number']} 在 GY1 索引中没有找到任何日期，跳过。")
                continue
            for date in dates:
                tasks.append((item, date))

        if not tasks:
            self.status("没有需要下载的文件（所有天区在索引中都不存在）", "orange")
            return

        total = len(tasks)
        completed = 0
        for idx, (problem, date) in enumerate(tasks, 1):
            region = problem["k_number"]
            k_full = problem["k_full"]
            region_url = self.config.build_url(system_upper, date, region)
            self.log(f"[模板更新] 扫描 {system_upper} {date} {k_full} - {region_url}")
            fits_files = self._scan_region_url(region_url)
            if not fits_files:
                self.log(f"[模板更新] {system_upper} {date} {k_full} 未找到任何FITS文件")
                continue
            candidates = self._select_candidates(fits_files, k_full)
            if not candidates:
                self.log(f"[模板更新] {system_upper} {date} 未找到包含 {k_full} 的文件（共 {len(fits_files)} 个）")
                continue
            for j, (filename, file_url, _size) in enumerate(candidates, 1):
                self.log(f"[模板更新] ({idx}/{total}) #{j}/{len(candidates)} 下载 {system_upper} {date} {k_full} -> {filename}")
                result = downloader.download_single_file(file_url, self.update_root, self._progress_cb("模板更新下载中"))
                if isinstance(result, str) and result.startswith("跳过已存在文件"):
                    self.status(f"模板更新: 已存在，跳过 - {k_full} -> {result}", "orange")
                else:
                    completed += 1
                    self.status(f"模板更新: 已下载 {completed} 个文件 - {k_full} -> {result}", "green")

        if completed:
            self.status(f"模板更新完成：成功 {completed}/{total} 个下载任务。", "green")
        else:
            self.status("模板更新完成：没有成功下载任何文件。", "orange")

    def _progress_cb(self, prefix: str):
        start_time = time.time()
        last = [start_time]

        def cb(downloaded, total, fn):
            now = time.time()
            if now - last[0] < 0.3 and (total is None or downloaded < (total or 0)):
                return
            last[0] = now
            speed = format_bytes(downloaded / max(now - start_time, 1e-3))
            total_str = format_bytes(total) if total is not None else "未知"
            self.status(f"{prefix}: {fn} {format_bytes(downloaded)}/{total_str} @ {speed}/s", "blue")

        return cb

    # ---------- 预处理 ----------
    def preprocess_images(self) -> None:
        system_name, _ = self._load_targets()
        if not system_name:
            return
        update_root_path = Path(self.update_root)
        if not update_root_path.exists():
            self.status("下载目录不存在，请先点击“下载目标天区对应观测文件”。", "red")
            return

        fits_files = []
        for ext in (".fits", ".fit", ".fts"):
            fits_files.extend(update_root_path.glob(f"*{ext}"))
            fits_files.extend(update_root_path.glob(f"*{ext.upper()}"))
        fits_files = sorted(set(fits_files))
        if not fits_files:
            self.status("预处理失败：下载目录中未找到任何FITS文件。", "orange")
            return

        os.makedirs(self.bad_dir, exist_ok=True)
        total = len(fits_files)
        counters = {"processed": 0, "bad": 0}
        lock = threading.Lock()

        def _process(path: Path):
            is_bad, reasons = self._quality_of(str(path))
            if is_bad:
                try:
                    shutil.move(str(path), os.path.join(self.bad_dir, path.name))
                    self.log(f"[预处理] 移动到 bad_img: {path.name} ({'; '.join(reasons)})")
                except Exception as e:
                    self.log(f"[预处理] 移动坏图像失败 {path}: {e}")
            with lock:
                counters["processed"] += 1
                if is_bad:
                    counters["bad"] += 1
                processed, bad = counters["processed"], counters["bad"]
            self.status(f"预处理: ({processed}/{total}) {path.name} -> {'BAD' if is_bad else 'OK'}，坏图像 {bad} 张",
                        "orange" if is_bad else "green")

        from concurrent.futures import ThreadPoolExecutor
        workers = max(1, self.settings.preprocess_threads)
        self.status(f"开始预处理 {total} 个FITS文件（ASTAP多线程 + 质量筛选）...", "blue")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            executor.map(_process, fits_files)
        self.status(f"预处理完成：共处理 {total} 个文件，坏图像 {counters['bad']} 个。", "green")

    # ---------- 生成模板（SWarp 后端）----------
    def _coadd_swarp(self, input_files, field_name: str):
        import numpy as np
        from astropy.io import fits
        from .swarp_backend import SwarpBackend, SWARP_EXE

        backend = SwarpBackend()
        if not backend.available():
            self.log(f"[生成] 项目内置 swarp 不可用: {SWARP_EXE}")
            return None, None

        out_dir = Path(self.output_root)
        out_dir.mkdir(parents=True, exist_ok=True)
        coadd_path = str(out_dir / f"_swarp_{field_name}.fits")
        weight_path = coadd_path.replace(".fits", ".weight.fits")

        ok, msg = backend.coadd(
            input_files, coadd_path,
            combine_type="AVERAGE", resampling=self.settings.swarp_resampling,
            threads=self.settings.swarp_threads,
        )
        if not ok:
            self.log(f"[生成] swarp 失败: {msg}")
            return None, None

        with fits.open(coadd_path) as h:
            data = h[0].data.astype("float32")
            header = h[0].header.copy()
        weight = None
        if os.path.exists(weight_path):
            try:
                with fits.open(weight_path) as w:
                    weight = w[0].data
            except Exception:
                weight = None
        if weight is not None:
            data = np.where(weight > 0, data, np.nan).astype("float32")
        for p in (coadd_path, weight_path):
            try:
                os.remove(p)
            except OSError:
                pass
        return data, header

    def _write_template(self, field_name: str, input_files) -> bool:
        import numpy as np
        from astropy.io import fits

        start_time = time.time()
        self.status(f"模板生成: {field_name} - 使用 SWarp 合成...", "blue")
        data, header = self._coadd_swarp(input_files, field_name)
        if data is None:
            return False

        out_dir = Path(self.output_root)
        out_dir.mkdir(parents=True, exist_ok=True)
        fits.writeto(out_dir / f"{field_name}_no_trim.fits", data, header, overwrite=True)

        mask = np.isfinite(data)
        if not mask.any():
            self.log(f"[生成] {field_name} 合成结果全部为NaN，跳过裁剪。")
            return False

        ys, xs = np.where(mask)
        y_min, y_max = int(ys.min()), int(ys.max())
        x_min, x_max = int(xs.min()), int(xs.max())
        trimmed = data[y_min:y_max + 1, x_min:x_max + 1].astype("float32")

        header = header.copy()
        if "CRPIX1" in header:
            header["CRPIX1"] -= x_min
        if "CRPIX2" in header:
            header["CRPIX2"] -= y_min
        fits.writeto(out_dir / f"{field_name}.fits", trimmed, header, overwrite=True)

        self.log(f"[生成] 完成 {field_name} (swarp) -> {out_dir / (field_name + '.fits')}，"
                 f"用时 {time.time() - start_time:.1f} 秒")
        return True

    def make_templates(self) -> None:
        from astropy.io import fits
        from astropy.wcs import WCS

        system_name, selected = self._load_targets()
        if system_name is None:
            return
        if not selected:
            self.status("请输入要处理的天区列表（例如 GY6_K002-8,GY6_K002-9）。", "orange")
            return
        update_root_path = Path(self.update_root)
        if not update_root_path.exists():
            self.status("下载目录不存在，请先点击“下载目标天区对应观测文件”。", "red")
            return

        os.makedirs(self.output_root, exist_ok=True)

        self.status("正在对下载文件执行ASTAP解算...", "blue")

        def _astap_progress(current, total, file_path, success):
            self.status(f"ASTAP解算: ({current}/{total}) {'成功' if success else '失败'} - {os.path.basename(file_path)}",
                        "blue" if success else "orange")

        try:
            self.astap_processor.process_directory(self.update_root, progress_callback=_astap_progress)
        except Exception as e:
            self.log(f"[模板生成] ASTAP解算过程中出错: {e}")

        total_templates = len(selected)
        made = 0
        for idx, item in enumerate(selected, 1):
            k_number = item.get("k_number")
            suffix = item.get("suffix")
            k_full = item.get("k_full")
            if not (k_number and suffix and k_full):
                continue
            try:
                field_name = f"K{int(str(k_number).lstrip('Kk')):03d}-{int(suffix)}"
            except Exception:
                self.log(f"[模板生成] 无法解析天区编号: {k_number}-{suffix}")
                continue

            if (Path(self.output_root) / f"{field_name}.fits").exists():
                self.log(f"[模板生成] 已存在输出模板 {k_full}，跳过。")
                continue

            pattern = k_full.lower()
            fits_files = [str(p) for ext in ("*.fits", "*.fit", "*.fts")
                          for p in update_root_path.glob(ext) if pattern in p.name.lower()]
            if not fits_files:
                self.log(f"[模板生成] {k_full} 在下载目录中未找到任何FITS文件，跳过。")
                continue

            self.status(f"模板生成: ({idx}/{total_templates}) {k_full} - 使用 {len(fits_files)} 个文件合成...", "blue")
            used_files = []
            for fpath in fits_files:
                try:
                    with fits.open(fpath, memmap=True) as hdul:
                        if not WCS(hdul[0].header).has_celestial:
                            self.log(f"[模板生成] 文件缺少天球WCS，跳过: {fpath}")
                            continue
                    used_files.append(fpath)
                except Exception as e:
                    self.log(f"[模板生成] 打开文件失败 {fpath}: {e}")
            if not used_files:
                continue
            if len(used_files) < max(1, self.settings.min_images):
                self.log(f"[模板生成] {k_full} 可用图像数 {len(used_files)} 小于最少参与图像数 {self.settings.min_images}，跳过。")
                continue

            try:
                if self._write_template(field_name, used_files):
                    made += 1
                    self.status(f"模板生成: ({idx}/{total_templates}) {k_full} -> {field_name}.fits", "green")
            except Exception as e:
                self.log(f"[模板生成] {k_full} 处理失败: {e}")

        if made:
            self.status(f"模板生成完成：成功生成 {made}/{total_templates} 个模板，输出目录: {self.output_root}", "green")
        else:
            self.status("模板生成完成：没有成功生成任何新模板。", "orange")


