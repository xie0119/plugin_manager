"""Compile only start.py as onedir, then add portable Python and Git."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from start import VERSION  # noqa: E402
from tools.portable_runtime import digest, portable_runtime  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", action="store_true", help="同时输出便携 ZIP")
    args = parser.parse_args()
    if sys.platform != "win32" or struct.calcsize("P") != 8 or sys.version_info < (3, 10):
        raise SystemExit("请使用 Windows x64 Python 3.10+ 构建启动器")
    environment = os.environ.copy()
    environment["PYINSTALLER_CONFIG_DIR"] = str(ROOT / "build/pyinstaller-cache")
    subprocess.run([sys.executable, "-B", "-m", "PyInstaller", "--clean", "--noconfirm",
                    "--workpath", str(ROOT / "build/bootstrap"), "--distpath", str(ROOT / "dist"),
                    str(ROOT / "main.spec")], cwd=ROOT, env=environment, check=True)
    release = ROOT / "dist/PluginManager"
    if not (release / "PluginManager.exe").is_file() or not (release / "_internal").is_dir():
        raise RuntimeError("未生成目录版启动器")
    shutil.copytree(portable_runtime(), release / "runtime", dirs_exist_ok=True)
    shutil.copy2(ROOT / "launcher-config.json", release)
    shutil.copy2(ROOT / "start.py", release)
    shutil.copy2(ROOT / "BOOTSTRAP.md", release / "使用说明.md")
    info = {"application": "PluginManager", "version": VERSION,
            "entry": "start.py", "mode": "onedir", "business_source_compiled": False,
            "executable_sha256": digest(release / "PluginManager.exe")}
    (release / "build-info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    if args.zip:
        archive = ROOT / "dist" / f"PluginManager-{VERSION}-portable-x64.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(release.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(release.parent))
        print(f"Portable ZIP: {archive}")
    print(f"Directory launcher: {release / 'PluginManager.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
