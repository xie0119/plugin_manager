"""安装版入口：只编译本文件；业务 main.py 始终由便携 Python 执行。"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

VERSION = "2.1.0"


def install_directory() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def child_path(root: Path, value: str) -> Path:
    if not value or Path(value).is_absolute():
        raise ValueError("配置路径必须是非空相对路径")
    target = (root / value).resolve()
    if target == root.resolve() or not target.is_relative_to(root.resolve()):
        raise ValueError(f"配置路径超出所属目录：{value}")
    return target


@dataclass
class Config:
    repository_url: str = "https://github.com/xie0119/plugin_manager.git"
    branch: str = ""
    repository_dir: str = "repository"
    entry_point: str = "main.py"
    requirements_file: str = "requirements.txt"
    update_on_launch: bool = True
    pip_index_url: str = ""
    command_timeout: int = 1800

    @classmethod
    def load(cls, root: Path) -> Config:
        path = root / "launcher-config.json"
        config = cls(**json.loads(path.read_text(encoding="utf-8-sig"))) if path.exists() else cls()
        url = urlsplit(config.repository_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("仓库必须使用不包含账号密码的 HTTPS 地址")
        if config.branch.startswith("-") or config.command_timeout < 10:
            raise ValueError("分支或超时配置无效")
        repo = child_path(root, config.repository_dir)
        if repo.parts[len(root.resolve().parts)].lower() in {"runtime", "_internal", ".venv", "logs", "tools", "launcher"}:
            raise ValueError("仓库目录与启动器目录冲突")
        child_path(repo, config.entry_point)
        if config.requirements_file:
            child_path(repo, config.requirements_file)
        return config


@contextmanager
def instance_lock(root: Path):
    """每个安装目录一个实例，不允许并发 git / pip。"""
    with (root / "launcher.lock").open("a+b") as handle:
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("此安装目录的启动器已在运行，请使用现有窗口") from error
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def clean_environment(python: Path | None = None, git: Path | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    # PyInstaller 的 Tcl/Qt/DLL 配置不可污染外部便携 Python 与业务进程。
    for key in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY", "QT_PLUGIN_PATH",
                "QT_QPA_PLATFORM_PLUGIN_PATH", "_MEIPASS2", "VIRTUAL_ENV"):
        environment.pop(key, None)
    for key in list(environment):
        if key.startswith("_PYI_"):
            environment.pop(key, None)
    environment.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1",
                       PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1",
                       GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    directories = [str(path.parent) for path in (python, git) if path]
    environment["PATH"] = os.pathsep.join(directories + [environment.get("PATH", "")])
    if python and python.parent.name.lower() == "scripts":
        environment["VIRTUAL_ENV"] = str(python.parent.parent)
    return environment


def spawn(arguments: list[str], **kwargs) -> subprocess.Popen:
    """冻结后的父进程会设置 DLL 搜索目录；启动子进程时恢复系统默认。"""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    previous = None
    kernel = None
    if os.name == "nt" and getattr(sys, "frozen", False):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        buffer = ctypes.create_unicode_buffer(32768)
        kernel.GetDllDirectoryW(len(buffer), buffer)
        previous = buffer.value
        kernel.SetDllDirectoryW(None)
    try:
        return subprocess.Popen(arguments, creationflags=flags, **kwargs)
    finally:
        if kernel:
            kernel.SetDllDirectoryW(previous or None)


class Bootstrap:
    def __init__(self, root: Path, config: Config, log=print, status=lambda text: None,
                 cancelled: threading.Event | None = None):
        self.root = root.resolve()
        self.config = config
        self.log = log
        self.status = status
        self.cancelled = cancelled or threading.Event()
        self.python = self.root / "runtime/python/python.exe"
        self.git = self.root / "runtime/git/cmd/git.exe"
        self.repo = child_path(self.root, config.repository_dir)
        self.environment_python = self.root / ".venv/Scripts/python.exe"

    def run(self, arguments, *, cwd=None, timeout=True, check=True) -> tuple[int, str]:
        if self.cancelled.is_set():
            raise InterruptedError("任务已取消")
        interpreter = self.environment_python if self.environment_python.exists() else self.python
        command = [str(arg) for arg in arguments]
        if cwd and Path(command[0]).resolve() == self.git.resolve():
            directory = Path(cwd).resolve()
            if directory == self.repo or (directory.parent == self.repo.parent and directory.name.startswith(".repository-clone-")):
                # 只信任本次配置的安装仓库，不修改全局 safe.directory，不使用通配符。
                command[1:1] = ["-c", "safe.directory=" + directory.as_posix()]
        process = spawn(command, cwd=str(cwd or self.root),
                        env=clean_environment(interpreter, self.git), stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace")
        lines = deque(maxlen=2000)

        def read_output():
            for line in process.stdout:
                lines.append(line)
                self.log(line.rstrip())

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        try:
            while process.poll() is None:
                if self.cancelled.is_set():
                    raise InterruptedError("任务已取消")
                if timeout and time.monotonic() - started > self.config.command_timeout:
                    raise TimeoutError("命令执行超时，请检查网络后重试")
                self.cancelled.wait(0.1)
        except BaseException:
            if process.poll() is None:
                if os.name == "nt":
                    killer = spawn([str(Path(os.environ["SystemRoot"]) / "System32/taskkill.exe"),
                                    "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                    try:
                        killer.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        killer.kill()
                if process.poll() is None:
                    process.kill()
                process.wait()
            raise
        finally:
            reader.join(timeout=10)
            process.stdout.close()
        output = "".join(lines)
        if check and process.returncode:
            raise RuntimeError(f"{Path(str(arguments[0])).name} 执行失败 ({process.returncode})\n{output[-2000:]}")
        return process.returncode, output

    def check_environment(self) -> None:
        self.status("检查便携 Python 和 Git")
        for path in (self.python, self.git):
            if not path.is_file():
                raise FileNotFoundError(f"缺少安装包内置环境，请重新安装：{path}")
        self.run([self.python, "-I", "-c", "import sys,ssl,venv,ensurepip; print(sys.version)"])
        self.run([self.git, "--version"])

    def sync_repository(self) -> None:
        self.status("拉取业务仓库")
        if not self.repo.exists():
            self.repo.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".repository-clone-", dir=self.repo.parent))
            try:
                args = [self.git, "clone", "--progress"]
                if self.config.branch:
                    args += ["--branch", self.config.branch]
                self.run(args + ["--", self.config.repository_url, staging])
                if (staging / ".gitmodules").is_file():
                    self.run([self.git, "submodule", "update", "--init", "--recursive"], cwd=staging)
                staging.rename(self.repo)
            finally:
                if staging.exists():
                    # 仅清理本次创建的临时克隆，不触碰已有仓库。
                    def writable(function, path, exc_info):
                        os.chmod(path, 0o700)
                        function(path)
                    shutil.rmtree(staging, onerror=writable)
            return
        if not (self.repo / ".git").is_dir():
            raise RuntimeError(f"目标目录已存在但不是 Git 仓库，不会覆盖：{self.repo}")
        _, origin = self.run([self.git, "config", "--get", "remote.origin.url"], cwd=self.repo)
        if origin.strip().rstrip("/") != self.config.repository_url.rstrip("/"):
            raise RuntimeError("现有仓库 origin 与配置不同，不会更新其他仓库")
        if not self.config.update_on_launch:
            return
        _, changes = self.run([self.git, "status", "--porcelain", "--untracked-files=no"], cwd=self.repo)
        if changes.strip():
            self.log("检测到本地修改，保留修改并运行本地版本")
            return
        _, branch = self.run([self.git, "branch", "--show-current"], cwd=self.repo)
        branch = branch.strip()
        if not branch or (self.config.branch and branch != self.config.branch):
            raise RuntimeError("当前分支与配置不同，或处于游离 HEAD，请先切换分支")
        try:
            self.run([self.git, "fetch", "--prune", "origin"], cwd=self.repo)
            self.run([self.git, "merge", "--ff-only", "refs/remotes/origin/" + branch], cwd=self.repo)
        except (RuntimeError, TimeoutError) as error:
            self.log(f"更新失败，保留已有版本继续运行：{error}")
        if (self.repo / ".gitmodules").is_file():
            self.run([self.git, "submodule", "update", "--init", "--recursive"], cwd=self.repo)

    def prepare_dependencies(self) -> None:
        self.status("准备独立 Python 环境")
        environment = self.root / ".venv"
        marker = environment / "launcher-owned.json"
        _, version = self.run([self.python, "-I", "-c", "import sys; print(sys.version)"])
        identity = {"python": str(self.python), "version": version.strip()}
        healthy = self.environment_python.exists() and marker.exists()
        if healthy:
            try:
                healthy = json.loads(marker.read_text(encoding="utf-8")) == identity
                if healthy:
                    self.run([self.environment_python, "-I", "-m", "pip", "--version"])
            except (RuntimeError, ValueError):
                healthy = False
        if not healthy:
            if environment.exists():
                if not marker.is_file() or environment.is_symlink() or getattr(environment, "is_junction", lambda: False)():
                    raise RuntimeError("已有 .venv 不属于启动器，或是目录链接，不会自动删除。请先移动它。")
                if environment.resolve().parent != self.root:
                    raise RuntimeError("独立环境目录超出安装路径")
                shutil.rmtree(environment)
            environment.mkdir()
            marker.write_text("{}", encoding="utf-8")
            self.run([self.python, "-I", "-m", "venv", environment])
            marker.write_text(json.dumps(identity), encoding="utf-8")
        self.status("安装仓库依赖")
        args = [self.environment_python, "-I", "-m", "pip", "install", "--no-input",
                "--disable-pip-version-check"]
        if self.config.pip_index_url:
            args += ["--index-url", self.config.pip_index_url]
        requirements = child_path(self.repo, self.config.requirements_file) if self.config.requirements_file else None
        if requirements and requirements.is_file():
            args += ["-r", requirements]
        elif (self.repo / "pyproject.toml").is_file() or (self.repo / "setup.py").is_file():
            args += ["-e", self.repo]
        else:
            raise FileNotFoundError("仓库没有依赖清单，请检查 requirements_file 配置")
        self.run(args, cwd=self.repo)
        self.run([self.environment_python, "-I", "-m", "pip", "check"], cwd=self.repo)

    def start(self, *, prepare_only=False, application_args=()) -> int:
        self.check_environment()
        self.sync_repository()
        entry = child_path(self.repo, self.config.entry_point)
        if not entry.is_file():
            raise FileNotFoundError(f"仓库缺少程序入口：{entry}")
        self.prepare_dependencies()
        if prepare_only:
            self.status("环境和仓库已准备完成")
            return 0
        self.status("程序运行中，关闭启动器将结束程序")
        code, _ = self.run([self.environment_python, "-u", entry, *application_args], cwd=self.repo,
                           timeout=False, check=False)
        self.status(f"程序已退出，退出码 {code}")
        return code


def gui(root: Path, config: Config, logger: logging.Logger, args) -> int:
    import tkinter as tk
    from tkinter import scrolledtext, ttk

    window = tk.Tk()
    window.title("PluginManager 启动器")
    window.geometry("780x480")
    window.minsize(640, 400)
    frame = ttk.Frame(window, padding=20)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="PluginManager", font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
    state = tk.StringVar(value="准备启动")
    ttk.Label(frame, textvariable=state).pack(anchor="w", pady=(12, 8))
    output = scrolledtext.ScrolledText(frame, wrap="word", state="disabled", font=("Consolas", 10))
    output.pack(fill="both", expand=True)
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(12, 0))
    messages = queue.Queue()
    cancelled = threading.Event()
    working = False
    closing = False

    def log(text):
        logger.info(text)
        messages.put(("log", text))

    def worker():
        try:
            engine = Bootstrap(root, config, log, lambda text: messages.put(("state", text)), cancelled)
            code = engine.start(prepare_only=args.prepare_only, application_args=args.application_args)
            messages.put(("state", "准备完成" if args.prepare_only else f"程序已退出（{code}）"))
        except Exception as error:
            log(str(error))
            messages.put(("state", "已取消" if isinstance(error, InterruptedError) else "启动失败，请查看日志后重试"))
        finally:
            messages.put(("done", ""))

    def launch():
        nonlocal working
        if working:
            return
        working = True
        cancelled.clear()
        retry.configure(state="disabled")
        threading.Thread(target=worker, daemon=True).start()

    def close():
        nonlocal closing
        if working:
            closing = True
            state.set("正在结束程序…")
            cancelled.set()
        else:
            window.destroy()

    def poll():
        nonlocal working
        for _ in range(200):
            try:
                kind, text = messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                output.configure(state="normal")
                output.insert("end", text + "\n")
                if int(output.index("end-1c").split(".")[0]) > 4000:
                    output.delete("1.0", "1000.0")
                output.see("end")
                output.configure(state="disabled")
            elif kind == "state":
                state.set(text)
            elif kind == "done":
                working = False
                retry.configure(state="normal")
                if closing:
                    window.destroy()
                    return
        window.after(100, poll)

    retry = ttk.Button(buttons, text="重新启动", command=launch)
    retry.pack(side="right")
    ttk.Button(buttons, text="取消 / 结束程序", command=cancelled.set).pack(side="right", padx=8)
    ttk.Button(buttons, text="查看日志", command=lambda: os.startfile(root / "logs")).pack(side="left")
    window.protocol("WM_DELETE_WINDOW", close)
    window.after(100, poll)
    window.after(150, launch)
    window.mainloop()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="检查便携环境、拉取仓库、安装依赖并运行 main.py")
    parser.add_argument("--install-dir", type=Path, default=install_directory())
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--check-environment", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("application_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.application_args[:1] == ["--"]:
        args.application_args.pop(0)
    root = args.install_dir.resolve()
    logger = logging.getLogger("bootstrap")
    try:
        (root / "logs").mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(root / "logs/start.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        config = Config.load(root)

        def log(text):
            logger.info(text)
            if sys.stdout:
                print(text, flush=True)

        with instance_lock(root):
            if args.check_environment:
                Bootstrap(root, config, log).check_environment()
                log("便携 Python 和 Git 检查通过")
                return 0
            if args.headless:
                return Bootstrap(root, config, log, log).start(
                    prepare_only=args.prepare_only, application_args=args.application_args)
            return gui(root, config, logger, args)
    except Exception as error:
        logger.exception("启动失败")
        if sys.stderr:
            print(str(error), file=sys.stderr)
        elif os.name == "nt" and not (args.headless or args.check_environment):
            ctypes.windll.user32.MessageBoxW(None, str(error), "PluginManager 启动失败", 0x10)
        return 1
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


if __name__ == "__main__":
    raise SystemExit(main())
