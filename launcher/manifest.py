"""Read plugin descriptions without importing or executing plugin code."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


class ManifestError(ValueError):
    """A plugin description cannot be used by the launcher."""


@dataclass(frozen=True)
class PluginManifest:
    id: str
    name: str
    root: Path
    entry: Path
    description: str = ""
    version: str = ""
    category: str = ""
    icon: Path | None = None
    enabled: bool = True

    @property
    def search_text(self) -> str:
        return " ".join((self.id, self.name, self.description, self.category)).casefold()


@dataclass(frozen=True)
class DiscoveryResult:
    plugins: tuple[PluginManifest, ...]
    errors: tuple[str, ...]


def _text(data: dict, key: str, *, required: bool = False) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ManifestError(f"{key} 必须是字符串")
    value = value.strip()
    if required and not value:
        raise ManifestError(f"缺少必填字段：{key}")
    return value


def _local_file(root: Path, value: str, field: str) -> Path:
    if "\x00" in value:
        raise ManifestError(f"{field} 路径包含非法字符")
    relative = Path(value)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ManifestError(f"{field} 必须是插件目录内的相对路径")
    try:
        target = (root / relative).resolve()
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{field} 路径无效：{value}") from exc
    if not target.is_relative_to(root):
        raise ManifestError(f"{field} 超出插件目录")
    if not target.is_file():
        raise ManifestError(f"{field} 文件不存在：{value}")
    return target


def read_manifest(path: Path) -> PluginManifest:
    root = path.resolve().parent
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"无法读取 config.json：{exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("config.json 顶层必须是对象")

    plugin_id = _text(data, "id", required=True)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", plugin_id):
        raise ManifestError("id 需为 1–64 位字母、数字、点、下划线或短横线，且以字母或数字开头")
    name = _text(data, "name", required=True)
    entry = _local_file(root, _text(data, "entry", required=True), "entry")
    if entry.suffix.lower() != ".py":
        raise ManifestError("entry 必须指向 Python 文件")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ManifestError("enabled 必须是 true 或 false")
    icon_value = _text(data, "icon")
    icon = _local_file(root, icon_value, "icon") if icon_value else None
    return PluginManifest(
        id=plugin_id,
        name=name,
        root=root,
        entry=entry,
        description=_text(data, "description"),
        version=_text(data, "version"),
        category=_text(data, "category"),
        icon=icon,
        enabled=enabled,
    )


def discover_plugins(directory: Path) -> DiscoveryResult:
    """Each immediate child directory can supply one config.json."""
    if not directory.exists():
        return DiscoveryResult((), ())
    if not directory.is_dir():
        return DiscoveryResult((), (f"插件路径不是目录：{directory}",))
    plugins: dict[str, PluginManifest] = {}
    errors: list[str] = []
    try:
        children = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
    except OSError as exc:
        return DiscoveryResult((), (f"无法扫描插件目录：{exc}",))
    for child in children:
        if child.name.startswith((".", "_")) or not child.is_dir():
            continue
        config = child / "config.json"
        if not config.is_file():
            continue
        try:
            plugin = read_manifest(config)
            if plugin.enabled:
                if plugin.id in plugins:
                    raise ManifestError(f"插件 id 重复：{plugin.id}")
                plugins[plugin.id] = plugin
        except ManifestError as exc:
            errors.append(f"{child.name}：{exc}")
    return DiscoveryResult(tuple(plugins.values()), tuple(errors))
