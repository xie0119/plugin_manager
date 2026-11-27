"""Opt-in Windows smoke test using only the bundled Python/Git and a real clone."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import ctypes
import time
import struct

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--gui", action="store_true", help="同时验证启动器窗口自动关闭")
    parser.add_argument("--offline-update", action="store_true", help="模拟更新无法连接，验证本地回退")
    parser.add_argument("--reset-plugins", action="store_true", help="验证真实仓库重拉和原目录备份")
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
    def gui_subsystem(path):
        data = path.read_bytes()
        offset = struct.unpack_from("<I", data, 0x3c)[0]
        return struct.unpack_from("<H", data, offset + 24 + 68)[0]
    assert gui_subsystem(executable) == 2, "启动器 EXE 必须使用 GUI 子系统"
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join([str(Path(os.environ["SystemRoot"]) / "System32"), os.environ["SystemRoot"]])
    environment["QT_QPA_PLATFORM"] = "offscreen"
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(key, None)

    def run(*arguments, graphical=False):
        process = subprocess.run([str(executable), *([] if graphical else ["--headless"]), *arguments], env=environment,
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
    if args.reset_plugins:
        marker = repo / "_local_reset_probe.txt"
        marker.write_text("KEEP_IN_BACKUP", encoding="utf-8")
        backup_root = target / "repository-backups"
        before = set(backup_root.iterdir()) if backup_root.exists() else set()
        print("Resetting the real repository and verifying backup preservation", flush=True)
        run("--reset-plugins", "--prepare-only")
        backups = set(backup_root.iterdir()) - before
        assert len(backups) == 1
        assert not marker.exists()
        assert (next(iter(backups)) / marker.name).read_text(encoding="utf-8") == "KEEP_IN_BACKUP"
    probe = repo / "_bootstrap_smoke_probe.py"
    probe.write_text('''from pathlib import Path
import runpy
import sys
import ctypes
from PySide6.QtCore import QTimer
import launcher.app as application

BaseApplication = application.QApplication
class ProbeApplication(BaseApplication):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        QTimer.singleShot(3000, self.verify)
    def verify(self):
        if ctypes.windll.kernel32.GetConsoleWindow():
            print("CONSOLE_WINDOW_FOUND", flush=True)
            self.exit(3)
            return
        print("NO_CONSOLE_WINDOW", flush=True)
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
    if args.offline_update:
        config["proxy_url"] = "http://127.0.0.1:1"
        config["update_on_launch"] = True
        config["update_timeout"] = 1
    application_log = target / "logs/application.log"
    log_offset = application_log.stat().st_size if application_log.exists() else 0
    def current_application_output():
        with application_log.open("rb") as stream:
            stream.seek(log_offset)
            return stream.read().decode("utf-8", errors="replace")
    try:
        config_path.write_text(json.dumps(config), encoding="utf-8")
        print("Running the repository main.py with an offscreen Qt event loop", flush=True)
        run(graphical=args.gui)
        running = json.loads((target / "logs/application.json").read_text(encoding="utf-8"))
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, running["pid"])
        assert handle, "主程序在启动器退出后没有继续运行"
        try:
            exit_code = ctypes.c_uint()
            assert kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) and exit_code.value == 259
        finally:
            kernel.CloseHandle(handle)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if "MAIN_PY_WINDOW_OK" in current_application_output():
                break
            time.sleep(0.1)
        else:
            raise AssertionError("启动器退出后，主程序未继续完成窗口验证和日志输出")
    finally:
        config_path.write_text(original, encoding="utf-8")
        probe.unlink(missing_ok=True)
    assert "MAIN_PY_WINDOW_OK" in current_application_output()
    assert "NO_CONSOLE_WINDOW" in current_application_output()
    assert gui_subsystem(target / ".venv/Scripts/pythonw.exe") == 2
    if args.offline_update:
        assert "已保留本地版本" in (target / "logs/start.log").read_text(encoding="utf-8")
    revision = subprocess.check_output([str(target / "runtime/git/cmd/git.exe"), "-c", "safe.directory=" + repo.as_posix(), "rev-parse", "HEAD"],
                                       cwd=repo, env=environment, stdin=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW, text=True).strip()
    report = {"compiled_launcher": "passed", "system_python_git_path_removed": True,
              "portable_environments": "passed", "real_repository_clone": "passed",
              "repository_dependency_installation": "passed", "main_py_offscreen_window": "passed",
              "launcher_exited_after_ready": "passed", "application_survived_launcher_exit": "passed",
              "application_logging_after_launcher_exit": "passed", "gui_auto_close_tested": args.gui,
              "offline_update_fallback_tested": args.offline_update,
              "launcher_gui_subsystem": "passed", "application_console_window_absent": "passed",
              "repository_reset_and_backup_tested": args.reset_plugins,
              "repository_revision": revision, "test_directory": str(target)}
    (ROOT / "dist/smoke-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
