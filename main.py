"""
应用入口：开发运行（``python main.py``）与 PyInstaller 打包（``pyinstaller -F main.py``）共用。
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_project_root_on_path() -> None:
    """保证从任意工作目录启动时仍能 import ``core`` / ``ui`` / ``bluetooth``。"""
    root = Path(__file__).resolve().parent
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)


def _ensure_hidden_imports_for_pyinstaller() -> None:
    """PyInstaller 不会分析外部插件里的 import；在此显式引用以便打进 exe。

    若子模块尚未添加，忽略 ImportError。
    """
    try:
        pass
    except ImportError:
        pass


def main() -> None:
    _ensure_project_root_on_path()
    _ensure_hidden_imports_for_pyinstaller()

    from core.logging_config import setup_logging
    from ui.main_window import run_app

    setup_logging()
    run_app()


if __name__ == "__main__":
    main()
