"""Load plugin widgets under private Python package namespaces."""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from PySide6.QtWidgets import QWidget
from shiboken6 import isValid

from launcher.manifest import DiscoveryResult, PluginManifest, discover_plugins

logger = logging.getLogger(__name__)


class PluginLoadError(RuntimeError):
    """A plugin failed to create its embedded widget."""


class PluginUnloadError(RuntimeError):
    """A plugin still has work that must finish before unloading."""


class _SourceLoader(importlib.machinery.SourceFileLoader):
    """Plugin refreshes always read current sources, including helper modules."""

    def get_code(self, fullname: str):
        return self.source_to_code(self.get_data(self.path), self.path)


class _PluginFinder(importlib.abc.MetaPathFinder):
    def __init__(self, namespace: str, root: Path) -> None:
        self.namespace = namespace
        self.root = root

    def find_spec(self, fullname: str, path=None, target=None):
        if not fullname.startswith(self.namespace + "."):
            return None
        relative = fullname[len(self.namespace) + 1 :].split(".")
        base = self.root.joinpath(*relative)
        package = base / "__init__.py"
        source = base.with_suffix(".py")
        if package.is_file():
            return importlib.util.spec_from_file_location(
                fullname,
                package,
                loader=_SourceLoader(fullname, str(package)),
                submodule_search_locations=[str(base)],
            )
        if source.is_file():
            return importlib.util.spec_from_file_location(
                fullname, source, loader=_SourceLoader(fullname, str(source))
            )
        return None


@dataclass
class _LoadedPlugin:
    namespace: str
    widget: QWidget
    finder: _PluginFinder


class PluginManager:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self._plugins: dict[str, PluginManifest] = {}
        self._loaded: dict[str, _LoadedPlugin] = {}

    @property
    def plugins(self) -> Mapping[str, PluginManifest]:
        return MappingProxyType(self._plugins)

    def scan(self) -> DiscoveryResult:
        self.unload_all()
        result = discover_plugins(self.directory)
        self._plugins = {plugin.id: plugin for plugin in result.plugins}
        for error in result.errors:
            logger.warning("Plugin discovery: %s", error)
        return result

    @staticmethod
    def _forget_namespace(namespace: str) -> None:
        for name in tuple(sys.modules):
            if name == namespace or name.startswith(namespace + "."):
                del sys.modules[name]

    def load_widget(self, plugin_id: str, parent: QWidget) -> QWidget:
        loaded = self._loaded.get(plugin_id)
        if loaded and isValid(loaded.widget):
            return loaded.widget
        if loaded:
            self._loaded.pop(plugin_id)
            self._forget_namespace(loaded.namespace)
            if loaded.finder in sys.meta_path:
                sys.meta_path.remove(loaded.finder)
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            raise PluginLoadError(f"找不到插件：{plugin_id}")

        # No sys.path changes: relative imports remain in the plugin's own package.
        digest = hashlib.sha256(str(plugin.root).encode("utf-8")).hexdigest()[:20]
        namespace = f"_launcher_plugin_{digest}"
        self._forget_namespace(namespace)
        importlib.invalidate_caches()
        finder = _PluginFinder(namespace, plugin.root)
        sys.meta_path.insert(0, finder)
        previous_children = set(parent.children())
        try:
            spec = importlib.util.spec_from_file_location(
                namespace,
                plugin.entry,
                loader=_SourceLoader(namespace, str(plugin.entry)),
                submodule_search_locations=[str(plugin.root)],
            )
            if spec is None or spec.loader is None:
                raise PluginLoadError("无法创建入口模块")
            module = importlib.util.module_from_spec(spec)
            sys.modules[namespace] = module
            # A quick entry refresh must not reuse stale timestamp-based bytecode.
            source = plugin.entry.read_bytes()
            exec(compile(source, str(plugin.entry), "exec"), module.__dict__)
            factory = getattr(module, "create_widget", None)
            if not callable(factory):
                raise PluginLoadError("入口需提供 create_widget(parent=None) 函数")
            widget = factory(parent=parent)
            if not isinstance(widget, QWidget) or not isValid(widget):
                raise PluginLoadError("create_widget() 必须返回有效的 QWidget")
            if widget is parent:
                raise PluginLoadError("create_widget() 必须返回插件自己的界面")
            widget.setParent(parent)
            self._loaded[plugin_id] = _LoadedPlugin(namespace, widget, finder)
            return widget
        except BaseException as exc:
            if finder in sys.meta_path:
                sys.meta_path.remove(finder)
            self._forget_namespace(namespace)
            for child in parent.children():
                if child not in previous_children and isinstance(child, QWidget):
                    child.close()
                    child.deleteLater()
            # Contain plugin sys.exit(), but retain normal interrupt semantics.
            if isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
                raise
            logger.exception("Failed to load plugin %s", plugin_id)
            raise PluginLoadError(f"{plugin.name}：{exc}") from exc

    def unload_all(self) -> None:
        # Check every plugin before releasing any widgets or Python modules.
        # A busy tool may request a graceful stop while refusing this unload.
        for plugin_id, loaded in tuple(self._loaded.items()):
            if not isValid(loaded.widget):
                continue
            can_unload = getattr(loaded.widget, "can_unload", None)
            if callable(can_unload) and not can_unload():
                name = self._plugins[plugin_id].name
                raise PluginUnloadError(f"{name} 正在完成任务，请等待设备处理结束后再刷新或退出。")
        for plugin_id, loaded in tuple(self._loaded.items()):
            try:
                if isValid(loaded.widget):
                    # Plugins own their resources and release them in closeEvent().
                    loaded.widget.close()
                    loaded.widget.deleteLater()
            except Exception:
                logger.exception("Failed to close plugin %s", plugin_id)
            finally:
                self._forget_namespace(loaded.namespace)
                if loaded.finder in sys.meta_path:
                    sys.meta_path.remove(loaded.finder)
        self._loaded.clear()
