"""Opt-in Windows smoke test using only the bundled Python/Git and a real clone."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    target = args.directory.resolve()
    boundary = (ROOT / "build").resolve()
    if target == boundary or not target.is_relative_to(boundary) or (target.exists() and not args.resume):
        raise ValueError("测试目录必须是 build 下尚不存在的目录")
    if args.resume:
        if json.loads((target / "build-info.json").read_text())["entry"] != "start.py":
            raise ValueError("测试目录不属于启动器")
        shutil.copy2(ROOT / "dist/PluginManager/PluginManager.exe", target)
        shutil.copy2(ROOT / "dist/PluginManager/start.py", target)
        shutil.copytree(ROOT / "dist/PluginManager/_internal", target / "_internal", dirs_exist_ok=True)
    else:
        shutil.copytree(ROOT / "dist/PluginManager", target)
    executable = target / "PluginManager.exe"
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join([str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"]])
    environment["QT_QPA_PLATFORM"] = "offscreen"
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(key, None)

    def run(*arguments):
        process = subprocess.run([str(executable), "--headless", *arguments], env=environment,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=1800, creationflags=subprocess.CREATE_NO_WINDOW)
        log = target / "logs/start.log"
        if process.returncode:
            raise RuntimeError(f"Frozen bootstrap failed: {process.returncode}\n" +
                               (log.read_text(encoding="utf-8")[-6000:] if log.exists() else repr(process.stderr)))

    print("Checking frozen launcher and bundled runtimes with system PATH removed", flush=True)
    run("--check-environment")
    print("Cloning the configured repository and installing requirements", flush=True)
    run("--prepare-only")
    config_path = target / "launcher-config.json"
    original = config_path.read_text(encoding="utf-8")
    config = json.loads(original)
    repo = target / config["repository_dir"]
    probe = repo / "_bootstrap_smoke_probe.py"
    probe.write_text('''from pathlib import Path
import runpy
import sys
from PySide6.QtCore import QTimer
import launcher.app as application

BaseApplication = application.QApplication
class ProbeApplication(BaseApplication):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        QTimer.singleShot(1500, self.verify)
    def verify(self):
        visible = [widget for widget in self.topLevelWidgets() if widget.isVisible()]
        print("MAIN_PY_WINDOW_OK" if visible else "NO_WINDOW", flush=True)
        self.exit(0 if visible else 2)
application.QApplication = ProbeApplication
entry = Path(__file__).with_name("main.py")
sys.argv = [str(entry)]
runpy.run_path(str(entry), run_name="__main__")
''', encoding="utf-8")
    config["entry_point"] = probe.name
    config["update_on_launch"] = False
    try:
        config_path.write_text(json.dumps(config), encoding="utf-8")
        print("Running the repository main.py with an offscreen Qt event loop", flush=True)
        run()
    finally:
        config_path.write_text(original, encoding="utf-8")
        probe.unlink(missing_ok=True)
    assert "MAIN_PY_WINDOW_OK" in (target / "logs/start.log").read_text(encoding="utf-8")
    revision = subprocess.check_output([str(target / "runtime/git/cmd/git.exe"), "-c", "safe.directory=" + repo.as_posix(), "rev-parse", "HEAD"],
                                       cwd=repo, env=environment, stdin=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW, text=True).strip()
    report = {"compiled_launcher": "passed", "system_python_git_path_removed": True,
              "portable_environments": "passed", "real_repository_clone": "passed",
              "repository_dependency_installation": "passed", "main_py_offscreen_window": "passed",
              "repository_revision": revision, "test_directory": str(target)}
    (ROOT / "dist/smoke-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
