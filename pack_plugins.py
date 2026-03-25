#!/usr/bin/env python3
"""
批量将 ``plugins/`` 下的源码插件打包为 ``.bin``。

用法示例::

    python pack_plugins.py --plugins-dir plugins --out-dir dist/plugins_bin
    python pack_plugins.py --dry-run -v
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Literal

# 保证可从项目根目录运行
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.plugin_bin import (
    MANIFEST_API_VERSION,
    PluginBinError,
    PluginBinManifest,
    pack_plugin_bin,
    utc_now_iso,
)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(message)s",
    )


def _should_skip_path(rel_posix: str, name: str) -> bool:
    parts = rel_posix.split("/")
    if "__pycache__" in parts:
        return True
    if name.endswith(".pyc"):
        return True
    if name in (".DS_Store", "Thumbs.db"):
        return True
    if name.startswith(".") and name not in (".", ".."):
        return True
    return False


def collect_directory_payload(root: Path) -> dict[str, bytes]:
    """收集目录插件内所有应打入 bin 的文件（相对 posix 路径 -> 内容）。"""

    root = root.resolve()
    out: dict[str, bytes] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if _should_skip_path(rel, p.name):
            continue
        try:
            out[rel] = p.read_bytes()
        except OSError as e:
            raise PluginBinError(f"read failed {p}: {e}") from e
    return out


def pack_directory_plugin(
    plugin_id: str,
    dir_path: Path,
    *,
    plugin_name: str | None,
) -> bytes:
    """打包 ``plugins/<id>/`` 目录型插件。"""

    entry_path = dir_path / "plugin.py"
    if not entry_path.is_file():
        raise PluginBinError(f"missing {entry_path.name} in {dir_path}")

    files = collect_directory_payload(dir_path)
    if "plugin.py" not in files:
        raise PluginBinError("payload must contain plugin.py")

    manifest = PluginBinManifest(
        plugin_id=plugin_id,
        plugin_name=plugin_name or plugin_id,
        version="packed",
        entry="plugin.py",
        package_type="directory",
        source_layout="directory_tree",
        created_at=utc_now_iso(),
        api_version=MANIFEST_API_VERSION,
        optional_dependencies=[],
        description="",
    )
    return pack_plugin_bin(manifest, files)


def pack_single_file_plugin(
    plugin_id: str,
    py_path: Path,
    *,
    plugin_name: str | None,
) -> bytes:
    """打包 ``plugins/<id>.py`` 单文件插件。"""

    key = py_path.name
    files = {key: py_path.read_bytes()}
    manifest = PluginBinManifest(
        plugin_id=plugin_id,
        plugin_name=plugin_name or plugin_id,
        version="packed",
        entry=key,
        package_type="single_file",
        source_layout="flat_py",
        created_at=utc_now_iso(),
        api_version=MANIFEST_API_VERSION,
        optional_dependencies=[],
        description="",
    )
    return pack_plugin_bin(manifest, files)


def discover_and_pack(
    plugins_dir: Path,
    out_dir: Path,
    *,
    dry_run: bool,
    force: bool,
    verbose: bool,
) -> int:
    """扫描并打包；返回进程退出码。"""

    log = logging.getLogger("pack_plugins")
    plugins_dir = plugins_dir.resolve()
    out_dir = out_dir.resolve()

    if not plugins_dir.is_dir():
        log.error("plugins dir does not exist: %s", plugins_dir)
        return 2

    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    # plugin_id -> already claimed by higher priority
    claimed: set[str] = set()
    jobs: list[tuple[str, Literal["dir", "py"], Path]] = []

    for sub in sorted(plugins_dir.iterdir()):
        if not sub.is_dir():
            continue
        if sub.name.startswith("_") or sub.name.startswith("."):
            continue
        if (sub / "plugin.py").is_file():
            jobs.append((sub.name, "dir", sub))
            claimed.add(sub.name)

    for py in sorted(plugins_dir.glob("*.py")):
        if py.name.startswith("_"):
            continue
        stem = py.stem
        if stem.startswith("test_"):
            continue
        if stem in claimed:
            log.debug("skip loose py %s (directory plugin wins)", py.name)
            continue
        jobs.append((stem, "py", py))
        claimed.add(stem)

    failures: list[str] = []
    ok_count = 0

    for plugin_id, kind, path in jobs:
        out_path = out_dir / f"{plugin_id}.bin"
        if out_path.exists() and not force and not dry_run:
            log.warning("skip %s: %s exists (use --force)", plugin_id, out_path)
            failures.append(f"{plugin_id}: output exists")
            continue
        try:
            if kind == "dir":
                blob = pack_directory_plugin(plugin_id, path, plugin_name=None)
            else:
                blob = pack_single_file_plugin(plugin_id, path, plugin_name=None)
        except PluginBinError as e:
            log.error("pack failed %s: %s", plugin_id, e)
            failures.append(f"{plugin_id}: {e}")
            continue

        log.info(
            "packed %s (%s) -> %s bytes",
            plugin_id,
            kind,
            len(blob),
        )
        if dry_run:
            ok_count += 1
            continue
        try:
            out_path.write_bytes(blob)
        except OSError as e:
            log.error("write failed %s: %s", out_path, e)
            failures.append(f"{plugin_id}: write {e}")
            continue
        ok_count += 1

    log.info("done: %s ok, %s failed", ok_count, len(failures))
    if failures and verbose:
        for f in failures:
            log.info("  failure: %s", f)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pack plugins/ sources into .bin bundles.",
    )
    parser.add_argument(
        "--plugins-dir",
        type=Path,
        default=Path("plugins"),
        help="源码插件根目录（默认: plugins）",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("dist/plugins_bin"),
        help="输出 .bin 目录（默认: dist/plugins_bin）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只演练打包逻辑，不写文件",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的输出 .bin",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="更详细的日志",
    )
    args = parser.parse_args()
    _configure_logging(args.verbose)
    return discover_and_pack(
        args.plugins_dir,
        args.out_dir,
        dry_run=args.dry_run,
        force=args.force,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    raise SystemExit(main())
