#!/usr/bin/env python3
"""SWarp 合成后端：调用 swarp.exe 完成 重采样 + 背景匹配 + 合成。

接口：输入 FITS 文件列表 -> 合成图 + 头（均值合成、双线性重采样、背景匹配）。
"""

import os
import shutil
import subprocess
from typing import List, Optional, Tuple

DEFAULT_MINGW_BIN = r"C:\msys64\mingw64\bin"

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 已知的 swarp.exe 位置（按顺序探测）
KNOWN_SWARP_PATHS = (
    os.path.join(_PROJECT_ROOT, "tools", "swarp", "swarp.exe"),
    r"E:\github\swarp_win\build\src\swarp.exe",
    r"C:\msys64\mingw64\bin\swarp.exe",
    r"C:\swarp\swarp.exe",
)


def resolve_swarp_exe(configured: str = "") -> str:
    """按优先级解析 swarp.exe：显式配置 > 环境变量 SWARP_EXE > PATH > 已知路径。"""
    for cand in (configured, os.environ.get("SWARP_EXE")):
        if cand and os.path.isfile(cand):
            return cand
    found = shutil.which("swarp") or shutil.which("swarp.exe")
    if found:
        return found
    for cand in KNOWN_SWARP_PATHS:
        if os.path.isfile(cand):
            return cand
    return configured or ""


class SwarpBackend:
    def __init__(self, exe: str, mingw_bin: Optional[str] = DEFAULT_MINGW_BIN, timeout: int = 3600):
        self.exe = exe or ""
        self.mingw_bin = mingw_bin
        self.timeout = timeout
        self.last_message = ""

    def available(self) -> bool:
        return bool(self.exe) and os.path.isfile(self.exe)

    def coadd(self, input_files: List[str], out_path: str,
              combine_type: str = "AVERAGE", resampling: str = "BILINEAR",
              threads: int = 0, subtract_back: bool = True,
              resample_dir: Optional[str] = None) -> Tuple[bool, str]:
        """对 input_files 做 coadd，输出 out_path（及 .weight.fits）。

        input_files 需为绝对路径；运行目录取第一个文件所在目录，
        临时 resample 文件写到 resample_dir（默认同目录下的 _swarp_tmp）。
        """
        if not self.available():
            return False, f"swarp 不可用: {self.exe}"

        workdir = os.path.dirname(os.path.abspath(input_files[0]))
        out_abs = os.path.abspath(out_path)
        weight_abs = out_abs.replace(".fits", ".weight.fits")
        resample_dir = resample_dir or os.path.join(workdir, "_swarp_tmp")
        os.makedirs(resample_dir, exist_ok=True)

        # 输入必须是相对文件名（SWarp 用输入名拼临时文件路径，绝对路径会出问题）
        rel_inputs = [os.path.basename(f) for f in input_files]
        args = [self.exe] + rel_inputs + [
            "-IMAGEOUT_NAME", out_abs,
            "-WEIGHTOUT_NAME", weight_abs,
            "-COMBINE", "Y",
            "-RESAMPLE", "Y",
            "-COMBINE_TYPE", combine_type,
            "-RESAMPLING_TYPE", resampling,
            "-SUBTRACT_BACK", "Y" if subtract_back else "N",
            "-RESAMPLE_DIR", resample_dir,
            "-DELETE_TMPFILES", "Y",
            "-VERBOSE_TYPE", "QUIET",
            "-NTHREADS", str(int(threads)),
        ]
        env = os.environ.copy()
        if self.mingw_bin:
            env["PATH"] = self.mingw_bin + os.pathsep + env.get("PATH", "")
        try:
            r = subprocess.run(args, cwd=workdir, env=env, capture_output=True,
                               text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return False, "swarp 超时"
        self.last_message = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0 and os.path.isfile(out_abs) and os.path.getsize(out_abs) > 0
        return ok, self.last_message[-500:]
