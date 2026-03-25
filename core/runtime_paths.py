"""
应用根目录解析（开发 / PyInstaller 打包通用）。

PyInstaller 使用 ``python -F main.py`` 生成单文件 exe 时，运行时代码位于临时解压目录
(``sys._MEIPASS``)，此时 ``Path(__file__)`` 指向临时目录，**不是** exe 所在目录。
因此 ``plugins/``、``configs/``、``logs/`` 等用户可写目录必须以 ``sys.executable``
所在目录为基准，才能与「exe 旁边放 plugins」的预期一致。
"""

from __future__ import annotations

import sys
from pathlib import Path


def get_app_base_dir() -> Path:
    """返回应用「数据根目录」：开发时为项目根；打包后为 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        # PyInstaller onefile / onedir：可执行文件所在目录
        return Path(sys.executable).resolve().parent
    # 源码运行：本文件位于 core/runtime_paths.py，上一级为项目根
    return Path(__file__).resolve().parents[1]


__all__ = ["get_app_base_dir"]
