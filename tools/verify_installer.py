"""Extract the actual installer, verify every payload hash, and run its bootstrap."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("installer", type=Path)
    parser.add_argument("--sevenzip", type=Path)
    args = parser.parse_args()
    executable = args.sevenzip or Path(shutil.which("7z") or r"C:\Program Files\7-Zip\7z.exe")
    installer = args.installer.resolve()
    destination = Path(tempfile.mkdtemp(prefix="installer-verified-", dir=ROOT / "build"))

    def run(command, **kwargs):
        process = subprocess.run([str(value) for value in command], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300,
                                 creationflags=subprocess.CREATE_NO_WINDOW, **kwargs)
        if process.returncode:
            raise RuntimeError(f"Verification command failed ({process.returncode}): {process.stdout[-4000:]!r}")

    run([executable, "t", installer, "-bso0", "-bsp0"])
    run([executable, "x", installer, "-o" + str(destination), "-y", "-bso0", "-bsp0"])
    manifests = list(destination.rglob("payload-files.json"))
    assert len(manifests) == 1, "安装包缺少唯一文件清单"
    root = manifests[0].parent
    files = json.loads(manifests[0].read_text(encoding="utf-8"))
    for relative, expected in files.items():
        path = root / relative
        assert path.is_file(), "缺少内置文件：" + relative
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, "文件哈希不一致：" + relative
    assert not (root / "main.py").exists(), "业务 main.py 不应打入安装包"
    assert not (root / "_internal/PySide6").exists(), "启动器不应内置业务 PySide6"
    environment = os.environ.copy()
    environment["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32")
    run([root / "PluginManager.exe", "--headless", "--check-environment"], env=environment)
    report = {"installer": str(installer), "bytes": installer.stat().st_size,
              "sha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
              "payload_files_verified": len(files), "payload_sha256": "passed",
              "extracted_bootstrap_environment_check": "passed",
              "business_main_compiled": False, "system_install_uninstall_tested": False,
              "verification_directory": str(root)}
    (ROOT / "dist/verification-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
