from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Literal, Optional, Set, Tuple, Type

from PySide6.QtWidgets import QWidget

from core.config_manager import get_config_manager
from core.event_bus import get_event_bus
from core.logging_config import get_plugin_logger
from core.plugin_api import get_plugin_api
from core.plugin_bin import (
    PluginBinCorruptError,
    PluginBinError,
    PluginBinSecurityError,
    PluginBinValidationError,
    extract_files_to_directory,
    make_temp_extract_root,
    parse_plugin_bin_file,
    remove_tree_quiet,
)
from core.service_manager import get_service_manager


PLUGIN_CLASS_NAME = "Plugin"

DiscoveredKind = Literal["source_file", "source_dir", "bin"]
DiscoveredInfo = Tuple[DiscoveredKind, Path, Optional[Path]]


def _read_plugin_source(path: Path) -> str:
    """读取插件入口源码（UTF-8 / UTF-8-BOM / UTF-16 LE|BE），不依赖 ``__pycache__``。"""

    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    return raw.decode("utf-8")


class _PluginEntryLoader(importlib.abc.Loader):
    """仅用内存 ``compile`` + ``exec`` 执行入口 ``.py``，不向磁盘写 ``__pycache__``。"""

    def __init__(self, fullname: str, path: Path) -> None:
        self._fullname = fullname
        self._path = path

    def exec_module(self, module: ModuleType) -> None:
        source = _read_plugin_source(self._path)
        code = compile(source, str(self._path), "exec")
        exec(code, module.__dict__)


@dataclass
class PluginMeta:
    id: str
    module_name: str
    module: ModuleType
    cls: Type
    instance: Any
    enabled: bool = True
    category: str | None = None
    icon: str | None = None
    widget: Optional[QWidget] = None
    file_path: Path | None = None
    root_dir: Path | None = None
    bin_source_path: Path | None = None
    bin_extract_root: Path | None = None


class PluginManager:
    """插件管理器：负责扫描、加载、卸载和热重载插件。

    支持：``plugins/<name>.py``、``plugins/<id>/plugin.py``、``plugins/<id>.bin``。

    目录型 ``.bin`` 解包至临时目录并加入 ``sys.path``，便于同目录 ``import sibling``；
    **不支持** ``from . import``（入口非 package 子模块装载模型）。

    打包后将 ``plugins/`` 置于与 ``main.exe`` 同级；根目录由 ``get_app_base_dir()`` 传入。
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir).resolve()
        self._plugins_dir = (self._base_dir / "plugins").resolve()
        self._plugins_dir.mkdir(parents=True, exist_ok=True)

        self._logger = get_plugin_logger("PluginManager")
        self._config_manager = get_config_manager()
        self._event_bus = get_event_bus()
        self._service_manager = get_service_manager()

        self._plugins: Dict[str, PluginMeta] = {}
        self._folder_plugin_paths: Set[str] = set()
        self._plugin_api = get_plugin_api()

    @property
    def plugins(self) -> Dict[str, PluginMeta]:
        return self._plugins

    def scan_and_load(self) -> None:
        """卸载当前全部插件后重新扫描并加载。"""
        self._unload_all()

        discovered = self._discover_plugin_files()
        for plugin_id in sorted(discovered.keys()):
            info = discovered[plugin_id]
            try:
                self._load_or_reload_single(plugin_id, info)
            except Exception as exc:
                self._logger.error(
                    "Failed to load plugin '%s' from '%s': %s",
                    plugin_id,
                    info[1],
                    exc,
                )

    def _unload_all(self) -> None:
        for meta in list(self._plugins.values()):
            try:
                inst = meta.instance
                if hasattr(inst, "on_unload"):
                    inst.on_unload()
            except Exception as exc:
                self._logger.error(
                    "Error in on_unload of plugin '%s': %s", meta.id, exc
                )

        for p in list(self._folder_plugin_paths):
            if p in sys.path:
                try:
                    sys.path.remove(p)
                except ValueError:
                    pass
        self._folder_plugin_paths.clear()

        for meta in list(self._plugins.values()):
            if meta.bin_extract_root is not None:
                remove_tree_quiet(meta.bin_extract_root, logger=self._logger)

        for meta in list(self._plugins.values()):
            name = meta.module_name
            if name in sys.modules:
                del sys.modules[name]

        self._plugins.clear()

    def _discover_plugin_files(self) -> Dict[str, DiscoveredInfo]:
        """plugin_id -> (类型, 主路径, 可选根目录)。

        优先级：目录 ``plugin.py`` > 根目录 ``*.py`` > ``*.bin``。
        """

        results: Dict[str, DiscoveredInfo] = {}
        if not self._plugins_dir.is_dir():
            return results

        for subdir in sorted(self._plugins_dir.iterdir()):
            if not subdir.is_dir():
                continue
            if subdir.name.startswith("_") or subdir.name.startswith("."):
                continue
            entry = subdir / "plugin.py"
            if entry.is_file():
                results[subdir.name] = (
                    "source_dir",
                    entry.resolve(),
                    subdir.resolve(),
                )

        for path in sorted(self._plugins_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            stem = path.stem
            if stem.startswith("test_"):
                continue
            if stem in results:
                continue
            results[stem] = (
                "source_file",
                path.resolve(),
                self._plugins_dir.resolve(),
            )

        for path in sorted(self._plugins_dir.glob("*.bin")):
            stem = path.stem
            if stem in results:
                self._logger.info(
                    "Skipping bin '%s' — source plugin with same id exists (%s)",
                    path.name,
                    stem,
                )
                continue
            results[stem] = ("bin", path.resolve(), None)

        return results

    @staticmethod
    def _folder_plugin_module_name(plugin_id: str) -> str:
        safe = plugin_id.replace(".", "_")
        return f"plugin_{safe}"

    def _load_or_reload_single(self, plugin_id: str, info: DiscoveredInfo) -> None:
        kind, primary, root_hint = info
        if kind == "bin":
            self._load_plugin_from_bin(plugin_id, primary)
            return

        file_path = primary.resolve()
        root_dir = (root_hint or self._plugins_dir).resolve()

        is_folder_plugin = (
            file_path.name == "plugin.py" and root_dir != self._plugins_dir
        )
        if is_folder_plugin:
            module_name = self._folder_plugin_module_name(plugin_id)
            root_s = str(root_dir)
            if root_s not in self._folder_plugin_paths:
                if root_s not in sys.path:
                    sys.path.insert(0, root_s)
                self._folder_plugin_paths.add(root_s)
        else:
            module_name = plugin_id

        module = self._exec_plugin_module(module_name, file_path)

        self._finalize_plugin_instance(
            plugin_id=plugin_id,
            module_name=module_name,
            module=module,
            file_path=file_path,
            root_dir=root_dir,
            bin_source_path=None,
            bin_extract_root=None,
        )

    def _load_plugin_from_bin(self, plugin_id: str, bin_path: Path) -> None:
        """解析 ``.bin``、校验、解包、``compile`` + ``exec`` 入口。"""

        bin_path = bin_path.resolve()
        try:
            _header, manifest, files = parse_plugin_bin_file(bin_path)
        except (
            PluginBinCorruptError,
            PluginBinValidationError,
            PluginBinSecurityError,
            PluginBinError,
        ) as e:
            raise PluginBinError(f"invalid or unreadable bin {bin_path}: {e}") from e

        if manifest.plugin_id != plugin_id:
            self._logger.warning(
                "bin manifest plugin_id %r != file stem %r — using stem",
                manifest.plugin_id,
                plugin_id,
            )

        extract_root: Path | None = None
        injected_sys_path: str | None = None
        module_name_for_cleanup: str | None = None

        try:
            extract_root = make_temp_extract_root(plugin_id)
            extract_files_to_directory(files, extract_root, logger=self._logger)

            entry_rel = manifest.entry.replace("\\", "/")
            entry_path = (extract_root / entry_rel).resolve()
            try:
                entry_path.relative_to(extract_root)
            except ValueError as e:
                raise PluginBinSecurityError(
                    f"entry escapes extract root: {entry_rel!r}"
                ) from e

            if not entry_path.is_file():
                raise PluginBinError(f"entry file missing after extract: {entry_path}")

            is_directory = manifest.package_type == "directory"
            if is_directory:
                module_name = self._folder_plugin_module_name(plugin_id)
                root_s = str(extract_root)
                if root_s not in self._folder_plugin_paths:
                    if root_s not in sys.path:
                        sys.path.insert(0, root_s)
                    self._folder_plugin_paths.add(root_s)
                injected_sys_path = root_s
            else:
                module_name = plugin_id

            module_name_for_cleanup = module_name
            module = self._exec_plugin_module(module_name, entry_path)

            self._finalize_plugin_instance(
                plugin_id=plugin_id,
                module_name=module_name,
                module=module,
                file_path=entry_path,
                root_dir=extract_root,
                bin_source_path=bin_path,
                bin_extract_root=extract_root,
            )
        except Exception:
            if extract_root is not None and extract_root.exists():
                remove_tree_quiet(extract_root, logger=self._logger)
            if injected_sys_path is not None:
                self._folder_plugin_paths.discard(injected_sys_path)
                if injected_sys_path in sys.path:
                    try:
                        sys.path.remove(injected_sys_path)
                    except ValueError:
                        pass
            if (
                module_name_for_cleanup is not None
                and module_name_for_cleanup in sys.modules
            ):
                del sys.modules[module_name_for_cleanup]
            raise

    def _exec_plugin_module(self, module_name: str, file_path: Path) -> ModuleType:
        """对入口文件执行 ``compile`` + ``exec``，返回模块对象。"""

        if module_name in sys.modules:
            self._logger.info("Reloading plugin module %s", module_name)
            module = importlib.reload(sys.modules[module_name])
            return module

        loader = _PluginEntryLoader(module_name, file_path)
        spec = importlib.util.spec_from_file_location(
            module_name, str(file_path), loader=loader
        )
        if spec is None:
            raise ImportError(f"Cannot load spec for {file_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        loader.exec_module(module)
        module.__file__ = str(file_path)
        module.__loader__ = loader
        module.__package__ = ""
        return module

    def _finalize_plugin_instance(
        self,
        *,
        plugin_id: str,
        module_name: str,
        module: ModuleType,
        file_path: Path,
        root_dir: Path,
        bin_source_path: Path | None,
        bin_extract_root: Path | None,
    ) -> None:
        plugin_cls = self._find_plugin_class(module)
        if plugin_cls is None:
            self._logger.warning(
                "Module %s does not define required Plugin class, skipped",
                module_name,
            )
            if bin_extract_root is not None:
                remove_tree_quiet(bin_extract_root, logger=self._logger)
                br = str(bin_extract_root)
                self._folder_plugin_paths.discard(br)
                if br in sys.path:
                    try:
                        sys.path.remove(br)
                    except ValueError:
                        pass
            if module_name in sys.modules:
                del sys.modules[module_name]
            return

        default_config_path = root_dir / "config.json"
        if default_config_path.exists():
            self._config_manager.register_default_config(plugin_id, default_config_path)

        instance = self._instantiate_plugin(
            plugin_cls, plugin_id=plugin_id, plugin_dir=root_dir
        )

        try:
            instance.on_load()
        except Exception as exc:
            self._logger.error("Error in on_load of plugin '%s': %s", plugin_id, exc)

        category = getattr(instance, "category", None)
        icon = getattr(instance, "icon", None)

        meta = PluginMeta(
            id=plugin_id,
            module_name=module_name,
            module=module,
            cls=plugin_cls,
            instance=instance,
            category=category,
            icon=icon,
            file_path=file_path,
            root_dir=root_dir,
            bin_source_path=bin_source_path,
            bin_extract_root=bin_extract_root,
        )

        self._plugins[plugin_id] = meta
        self._logger.info(
            "Loaded plugin '%s' (%s - %s)",
            plugin_id,
            getattr(instance, "name", plugin_cls.__name__),
            getattr(instance, "version", "unknown"),
        )

    def _find_plugin_class(self, module: ModuleType) -> Optional[Type]:
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if _name != PLUGIN_CLASS_NAME:
                continue
            if getattr(obj, "__module__", None) != module.__name__:
                continue
            return obj
        return None

    def _instantiate_plugin(
        self, plugin_cls: Type, *, plugin_id: str, plugin_dir: Path
    ) -> Any:
        """按插件 ``__init__`` 形参动态注入。"""
        sig = inspect.signature(plugin_cls.__init__)
        logger = get_plugin_logger(plugin_id)
        candidates = {
            "event_bus": self._event_bus,
            "config_manager": self._config_manager,
            "logger": logger,
            "plugin_id": plugin_id,
            "plugin_dir": plugin_dir,
            "services": self._service_manager,
            "plugin_api": self._plugin_api,
            "api": self._plugin_api,
        }
        kwargs: Dict[str, Any] = {}
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            if name in candidates:
                kwargs[name] = candidates[name]
            elif param.default is inspect.Parameter.empty:
                self._logger.warning(
                    "Plugin '%s' has unhandled required parameter '%s'",
                    plugin_id,
                    name,
                )

        return plugin_cls(**kwargs)

    def create_widget_for(
        self, plugin_id: str, parent: Optional[QWidget] = None
    ) -> Optional[QWidget]:
        meta = self._plugins.get(plugin_id)
        if not meta:
            return None
        inst = meta.instance
        getter = getattr(inst, "get_widget", None)
        if getter is None:
            self._logger.warning("Plugin '%s' has no get_widget()", plugin_id)
            return None
        try:
            widget = getter(parent)
        except Exception as exc:
            self._logger.error("get_widget failed for '%s': %s", plugin_id, exc)
            return None
        meta.widget = widget
        return widget


__all__ = ["PluginManager", "PluginMeta", "PLUGIN_CLASS_NAME"]
