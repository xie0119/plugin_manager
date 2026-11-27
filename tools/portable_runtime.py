"""Build-time downloads only; end users receive all environments in the installer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"
DOWNLOADS = BUILD / "downloads"
PYTHON_VERSION = "3.12.10"
GIT_VERSION = "2.56.1"
NSIS_VERSION = "3.13"
PYTHON_HASHES = {
    "core": "0075fe3c252b2af487443a7fbf666c8055e2d111ce7a75de7df0a5d45efb8794",
    "exe": "64bef9bfc893cce8c697020fd4564a4e62c7f06e8f4b8ac3c5dfec37bdc56458",
    "lib": "699b862a3a0330114f8ddae9b92e394b4ee93341ba90e19710982cae62cd4079",
    "dev": "77b151b8d36f8bb4cde20ee6f0ff0681a2f88b2072ceac6abaa8e173f5a1eb78",
    "tcltk": "55c96ffad69b1c834aa52e11b9ce41637a178ba6ad6607e83956044834276e2a",
}
GIT_HASH = "4a92850b6e917da4fd010669b614b55dccf5b0ec333a534cc4be4700dfefdafd"
NSIS_HASH = "262c2453ee69924e4995a6f3c685edb96e57e57e749d38f8f89732be6ffccff7"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def remove_tree(path: Path, parent: Path) -> None:
    resolved, boundary = path.resolve(), parent.resolve()
    if resolved == boundary or not resolved.is_relative_to(boundary):
        raise ValueError(f"构建删除路径越界：{resolved}")
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise ValueError(f"构建目录不能是目录链接：{path}")
    if path.exists():
        shutil.rmtree(path)


def download(name: str, url: str, expected: str) -> Path:
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    target = DOWNLOADS / name
    if not target.exists():
        partial = target.with_suffix(target.suffix + ".partial")
        for attempt in range(3):
            try:
                print(f"Downloading {name}", flush=True)
                with urllib.request.urlopen(url, timeout=90) as source, partial.open("wb") as output:
                    shutil.copyfileobj(source, output)
                partial.replace(target)
                break
            except OSError:
                partial.unlink(missing_ok=True)
                if attempt == 2:
                    raise
                time.sleep(1)
    if digest(target) != expected:
        raise RuntimeError(f"下载文件 SHA256 不一致，请删除缓存后重试：{target}")
    return target


def extract_zip(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            if not (target / entry.filename).resolve().is_relative_to(target.resolve()):
                raise ValueError("ZIP 文件路径越界")
        bundle.extractall(target)


def portable_runtime() -> Path:
    from start import clean_environment
    from tools.extract_msi import extract_msi

    target = BUILD / "portable-runtime"
    stamp = target / "runtime-info.json"
    expected = {"python": PYTHON_VERSION, "git": GIT_VERSION}
    if stamp.exists() and json.loads(stamp.read_text()) == expected:
        return target
    remove_tree(target, BUILD)
    python = target / "python"
    python.mkdir(parents=True)
    for component, checksum in PYTHON_HASHES.items():
        msi = download(f"python-{PYTHON_VERSION}-{component}-amd64.msi",
                       f"https://www.python.org/ftp/python/{PYTHON_VERSION}/amd64/{component}.msi", checksum)
        extract_msi(msi, python, BUILD / "python-components")
    executable = python / "python.exe"
    env = clean_environment(executable)
    subprocess.run([str(executable), "-I", "-c",
                    "import ssl,venv,ensurepip,tkinter; print(tkinter.Tcl().call('info','patchlevel'))"],
                   env=env, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    subprocess.run([str(executable), "-I", "-m", "ensurepip", "--default-pip"], env=env, check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    git = download(f"MinGit-{GIT_VERSION}-64-bit.zip",
                   f"https://github.com/git-for-windows/git/releases/download/v{GIT_VERSION}.windows.1/MinGit-{GIT_VERSION}-64-bit.zip", GIT_HASH)
    extract_zip(git, target / "git")
    subprocess.run([str(target / "git/cmd/git.exe"), "--version"], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    stamp.write_text(json.dumps(expected, indent=2), encoding="utf-8")
    return target


def nsis_compiler() -> Path:
    name = f"mingw-w64-ucrt-x86_64-nsis-{NSIS_VERSION}-2-any.pkg.tar.zst"
    archive = download(name, "https://repo.msys2.org/mingw/ucrt64/" + name, NSIS_HASH)
    destination = BUILD / "tooling/nsis-native"
    compiler = destination / "ucrt64/bin/makensis.exe"
    if not compiler.is_file():
        try:
            import zstandard
        except ImportError as error:
            raise RuntimeError("构建需要 zstandard，请运行 python -m pip install zstandard") from error
        destination.mkdir(parents=True, exist_ok=True)
        with archive.open("rb") as source, zstandard.ZstdDecompressor().stream_reader(source) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as bundle:
                for entry in bundle:
                    if (entry.issym() or entry.islnk() or
                            not (destination / entry.name).resolve().is_relative_to(destination.resolve())):
                        raise ValueError("NSIS 构建工具归档路径无效")
                    bundle.extract(entry, destination)
    if not compiler.is_file():
        raise RuntimeError("缺少原生 64 位 NSIS 编译器")
    return compiler
