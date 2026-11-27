"""Application bootstrap without business services or dependency installers."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from launcher.ui.main_window import MainWindow


def application_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    source_root = Path(__file__).resolve().parents[1]
    if (source_root / "main.py").is_file():
        return source_root
    return Path.cwd()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="轻量 PySide6 工具启动器")
    parser.add_argument("--plugins-dir", type=Path, help="指定插件目录，默认使用程序旁的 plugins/")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")

    app = QApplication([sys.argv[0]])
    app.setApplicationName("PluginManager")
    app.setOrganizationName("PluginManager")
    style = Path(__file__).parent / "ui" / "style.qss"
    app.setStyleSheet(style.read_text(encoding="utf-8"))
    directory = args.plugins_dir or application_directory() / "plugins"
    window = MainWindow(directory)
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    return app.exec()
