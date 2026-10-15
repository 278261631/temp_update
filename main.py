#!/usr/bin/env python3
"""入口：启动天区收集 / 升级模板 GUI（PySide6）。

运行: python main.py
"""

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from app.config import AppConfig
from app.gui import MainWindow
from app.gui_config import GuiConfig


def main() -> int:
    app = QApplication(sys.argv)
    try:
        config = AppConfig()
    except Exception as e:
        QMessageBox.critical(None, "配置错误", str(e))
        return 1

    window = MainWindow(config, GuiConfig())
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
