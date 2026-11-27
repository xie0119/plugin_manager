"""Build a Windows executable and a portable release with external plugins."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if sys.version_info < (3, 10):
        raise SystemExit("请使用 Python 3.10 或更高版本运行本打包脚本。")
    if sys.platform != "win32":
        raise SystemExit("Windows EXE 需要在 Windows 上打包。")

    environment = os.environ.copy()
    # An optional offline tool directory belongs to the build only, never to the app.
    local_tools = ROOT / ".build-tools" / "site-packages"
    if (local_tools / "PyInstaller").is_dir():
        environment["PYTHONPATH"] = str(local_tools)
    environment["PYINSTALLER_CONFIG_DIR"] = str(ROOT / "build" / "pyinstaller-cache")
    command = [
        sys.executable,
        "-B",
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--workpath",
        str(ROOT / "build" / "PluginManager"),
        "--distpath",
        str(ROOT / "dist"),
        str(ROOT / "main.spec"),
    ]
    subprocess.run(command, cwd=ROOT, env=environment, check=True)

    executable = ROOT / "dist" / "PluginManager.exe"
    if not executable.is_file():
        raise RuntimeError("打包进程结束，但没有生成 PluginManager.exe")
    release = ROOT / "dist" / "PluginManager"
    if release.is_symlink() or release.resolve().parent != (ROOT / "dist").resolve():
        raise RuntimeError(f"发布目录超出 dist：{release}")
    if release.exists():
        shutil.rmtree(release)
    release.mkdir()
    shutil.move(str(executable), release / executable.name)
    shutil.copytree(
        ROOT / "plugins",
        release / "plugins",
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.pyo", ".venv", ".git",
            "settings.json", "settings.json.tmp",
        ),
    )
    (release / "使用说明.txt").write_text(
        "PluginManager 2.0.0\n\n"
        "双击 PluginManager.exe 启动，无需安装 Python。\n"
        "请将 plugins 文件夹与 EXE 放在一起；新增插件后按 F5 刷新。\n"
        "点击左侧工具后才会加载插件；Ctrl+F 搜索。\n"
        "插件用户设置由插件自行保存，不会覆盖 config.json。\n"
        "自带文本工具已随本发布包验证。新增第三方依赖需另外准备和验证。\n",
        encoding="utf-8-sig",
    )
    build_info = {
        "application": "PluginManager",
        "version": "2.0.0",
        "python": sys.version.split()[0],
        "platform": "windows-x64",
        "executable": "PluginManager.exe",
        "sha256": hashlib.file_digest((release / "PluginManager.exe").open("rb"), "sha256").hexdigest()
        if sys.version_info >= (3, 11)
        else hashlib.sha256((release / "PluginManager.exe").read_bytes()).hexdigest(),
    }
    (release / "build-info.json").write_text(
        json.dumps(build_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    archive = ROOT / "dist" / "PluginManager-2.0.0-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(release.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(release.parent))
    print(f"Executable: {release / 'PluginManager.exe'}")
    print(f"Portable ZIP: {archive}")
    print(f"SHA256: {build_info['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
