#!/usr/bin/env python3
"""SWarp 合成后端：调用 swarp.exe 完成 重采样 + 背景匹配 + 合成。

接口：输入 FITS 文件列表 -> 合成图 + 头（均值合成、双线性重采样、背景匹配）。
"""

import os
import subprocess
from typing import List, Optional, Tuple

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 项目内置的 swarp 可执行文件（随仓库分发，同目录含 libwinpthread-1.dll）
SWARP_EXE = os.path.join(_PROJECT_ROOT, "tools", "swarp", "swarp.exe")


class SwarpBackend:
    def __init__(self, exe: str = None, timeout: int = 3600):
        self.exe = exe or SWARP_EXE
        self.timeout = timeout
        self.last_message = ""

    def available(self) -> bool:
        return os.path.isfile(self.exe)

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
        try:
            r = subprocess.run(args, cwd=workdir, capture_output=True,
                               text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return False, "swarp 超时"
        self.last_message = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0 and os.path.isfile(out_abs) and os.path.getsize(out_abs) > 0
        return ok, self.last_message[-500:]
