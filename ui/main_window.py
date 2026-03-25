from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core.logging_config import get_plugin_logger
from core.plugin_manager import PluginManager, PluginMeta
from ui.plugin_list_item import PluginListItemWidget, PluginSearchItemWidget


class MainWindow(QMainWindow):
    """主窗口：左侧插件列表 + 右侧内容区 + 顶部工具栏。"""

    def __init__(self, base_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._logger = get_plugin_logger("MainWindow")
        self._base_dir = base_dir
        self._plugin_manager = PluginManager(base_dir)

        self._plugin_items: Dict[str, QListWidgetItem] = {}
        self._current_plugin_id: Optional[str] = None

        self._init_ui()
        self._load_style()
        self.refresh_plugins()

    # ---------------- UI 构建 ----------------
    def _init_ui(self) -> None:
        self.setWindowTitle("Python 工具箱平台")
        self.resize(1200, 800)

        central = QWidget()
        central.setObjectName("rootCentral")
        self.setCentralWidget(central)

        root_layout = QVBoxLayout()
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        central.setLayout(root_layout)

        # 中央左右布局
        splitter = QSplitter(Qt.Horizontal)

        left_panel = QWidget()
        left_panel.setObjectName("leftPanel")
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_panel.setLayout(left_layout)

        self._plugin_list = QListWidget()
        self._plugin_list.setObjectName("pluginList")
        self._plugin_list.setSelectionMode(QListWidget.SingleSelection)
        self._plugin_list.currentItemChanged.connect(self._on_plugin_item_changed)

        left_layout.addWidget(self._plugin_list)

        right_panel = QWidget()
        right_panel.setObjectName("rightPanel")
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_panel.setLayout(right_layout)

        self._stack = QStackedWidget()
        self._stack.setObjectName("pluginStack")
        right_layout.addWidget(self._stack)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 840])

        root_layout.addWidget(splitter)

        # 菜单（可扩展）
        menubar = self.menuBar()
        view_menu = menubar.addMenu("视图")
        reload_action = QAction("刷新插件", self)
        reload_action.triggered.connect(self.refresh_plugins)
        view_menu.addAction(reload_action)

    def _load_style(self) -> None:
        style_path = self._base_dir / "ui" / "style.qss"
        if not style_path.exists():
            return
        try:
            with style_path.open("r", encoding="utf-8") as f:
                self.setStyleSheet(f.read())
        except Exception as exc:
            self._logger.error("Failed to load style.qss: %s", exc)

    # ---------------- 插件相关 ----------------
    def refresh_plugins(self) -> None:
        """触发插件热重载：卸载、reload module、重新扫描和实例化。"""
        self._logger.info("Refreshing plugins")

        # 清空 UI 和旧 widget
        self._plugin_list.clear()
        while self._stack.count():
            w = self._stack.widget(0)
            self._stack.removeWidget(w)
            w.setParent(None)
            w.deleteLater()

        self._plugin_items.clear()
        self._current_plugin_id = None

        # 让 PluginManager 进行扫描和加载（内部使用 importlib.reload）
        self._plugin_manager.scan_and_load()

        # 重建左侧列表和右侧 stack
        for plugin_id, meta in sorted(
            self._plugin_manager.plugins.items(), key=lambda kv: kv[0]
        ):
            self._add_plugin_to_ui(plugin_id, meta)

        # 插件列表顶部插入搜索行
        self._add_search_row()

        # 默认选中第一个插件项（跳过搜索行）
        if self._plugin_list.count() > 1:
            self._plugin_list.setCurrentRow(1)

    def _add_search_row(self) -> None:
        """在列表顶部添加搜索项。"""
        item = QListWidgetItem()
        # 搜索行不参与选择
        item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
        widget = PluginSearchItemWidget(parent=self._plugin_list)
        widget.search_edit.textChanged.connect(self._filter_plugins)
        item.setSizeHint(widget.sizeHint())
        self._plugin_list.insertItem(0, item)
        self._plugin_list.setItemWidget(item, widget)

    def _add_plugin_to_ui(self, plugin_id: str, meta: PluginMeta) -> None:
        inst = meta.instance
        name = getattr(inst, "name", plugin_id)
        desc = getattr(inst, "description", "")

        item = QListWidgetItem()
        item.setData(Qt.UserRole, plugin_id)
        widget = PluginListItemWidget(name=name, description=desc, icon_path=meta.icon)
        item.setSizeHint(widget.sizeHint())

        self._plugin_list.addItem(item)
        self._plugin_list.setItemWidget(item, widget)

        self._plugin_items[plugin_id] = item

        # 右侧 widget 延迟创建，首次点击时再 create_widget_for

    def _on_plugin_item_changed(
        self,
        current: QListWidgetItem | None,
        previous: QListWidgetItem | None,
    ) -> None:
        if current is None:
            return
        plugin_id = current.data(Qt.UserRole)
        if not plugin_id:
            return

        self._current_plugin_id = plugin_id
        self._show_plugin(plugin_id)

    def _show_plugin(self, plugin_id: str) -> None:
        # 查找已有 widget
        meta = self._plugin_manager.plugins.get(plugin_id)
        if not meta:
            return

        if meta.widget is None:
            widget = self._plugin_manager.create_widget_for(plugin_id, parent=self)
            if widget is None:
                return
            self._stack.addWidget(widget)
        else:
            widget = meta.widget

        index = self._stack.indexOf(widget)
        if index >= 0:
            self._stack.setCurrentIndex(index)

    # ---------------- 搜索过滤 ----------------
    def _filter_plugins(self, text: str) -> None:
        text_lower = text.strip().lower()
        # 跳过第 0 行搜索项
        for i in range(1, self._plugin_list.count()):
            item = self._plugin_list.item(i)
            plugin_id = item.data(Qt.UserRole)
            meta = self._plugin_manager.plugins.get(plugin_id)
            if not meta:
                item.setHidden(True)
                continue
            inst = meta.instance
            name = getattr(inst, "name", "")
            desc = getattr(inst, "description", "")
            category = getattr(inst, "category", "") or ""
            key = f"{name} {desc} {category}".lower()
            item.setHidden(text_lower not in key)


def run_app(base_dir: Path | None = None) -> None:
    import sys

    from core.runtime_paths import get_app_base_dir
    from core.service_manager import get_service_manager

    if base_dir is None:
        base_dir = get_app_base_dir()
    base_dir = base_dir.resolve()

    app = QApplication(sys.argv)
    # 蓝牙 / 设备管理等 Qt 定时器依赖 QApplication，需在此初始化
    get_service_manager().init_core(app)
    win = MainWindow(base_dir)
    win.show()
    sys.exit(app.exec())


__all__ = ["MainWindow", "run_app"]

