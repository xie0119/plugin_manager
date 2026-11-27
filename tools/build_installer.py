"""Package the onedir bootstrap and environments into an offline .exe installer."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from start import VERSION  # noqa: E402
from tools.portable_runtime import digest, nsis_compiler  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true", help="使用已构建的 dist/PluginManager")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("请在 Windows 上构建安装程序")
    if not args.skip_build:
        subprocess.run([sys.executable, "-B", str(ROOT / "tools/build_release.py")], check=True)
    source = ROOT / "dist/PluginManager"
    for relative in ["PluginManager.exe", "runtime/python/python.exe", "runtime/git/cmd/git.exe"]:
        if not (source / relative).is_file():
            raise FileNotFoundError(f"发布目录缺少文件：{relative}")
    files = {path.relative_to(source).as_posix(): digest(path)
             for path in sorted(source.rglob("*")) if path.is_file() and path.name != "payload-files.json"}
    (source / "payload-files.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
    paths = [Path(name) for name in [*files, "payload-files.json"]]
    uninstall = ROOT / "build/uninstall-payload.nsh"
    lines = ['Delete "$INSTDIR\\' + str(path).replace("$", "$$") + '"'
             for path in paths if str(path) != "launcher-config.json"]
    directories = {parent for path in paths for parent in path.parents if parent != Path(".")}
    lines += ['RMDir "$INSTDIR\\' + str(path) + '"'
              for path in sorted(directories, key=lambda p: (len(p.parts), str(p)), reverse=True)]
    uninstall.parent.mkdir(parents=True, exist_ok=True)
    uninstall.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    output = ROOT / "dist" / f"PluginManager-{VERSION}-Setup-x64.exe"
    compiler = nsis_compiler()
    environment = os.environ.copy()
    environment["NSISDIR"] = str(compiler.parent.parent / "share/nsis")
    subprocess.run([str(compiler), "/V2", f"/DPAYLOAD={source}", f"/DOUTPUT={output}",
                    f"/DUNINSTALL_PAYLOAD={uninstall}", str(ROOT / "tools/installer.nsi")],
                   env=environment, stdin=subprocess.DEVNULL, check=True)
    (ROOT / "dist/installer-info.json").write_text(json.dumps({
        "version": VERSION, "installer": output.name, "bytes": output.stat().st_size,
        "sha256": digest(output), "embedded_files": len(files), "entry": "start.py",
        "mode": "onedir", "python": "3.12.10", "git": "2.56.1",
    }, indent=2), encoding="utf-8")
    print(f"Installer: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
