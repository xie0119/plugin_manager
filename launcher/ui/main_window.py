from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from launcher.plugin_manager import PluginLoadError, PluginManager, PluginUnloadError
from launcher.ui.plugin_list_item import PluginListItemWidget, PluginSearchItemWidget


class MainWindow(QMainWindow):
    """Original two-pane interface backed by config-only discovery."""

    def __init__(self, plugins_dir: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._manager = PluginManager(plugins_dir)
        self._current_id: str | None = None
        self._close_retry = QTimer(self)
        self._close_retry.setInterval(250)
        self._close_retry.timeout.connect(self.close)
        self._build_ui()
        self.refresh_plugins()

    def _build_ui(self) -> None:
        self.setWindowTitle("Python 工具箱平台")
        self.resize(1200, 800)
        central = QWidget()
        central.setObjectName("rootCentral")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        left = QWidget()
        left.setObjectName("leftPanel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        self._list = QListWidget()
        self._list.setObjectName("pluginList")
        self._list.setAccessibleName("插件列表")
        self._list.setSelectionMode(QListWidget.SingleSelection)
        self._list.currentItemChanged.connect(self._selection_changed)
        left_layout.addWidget(self._list)
        right = QWidget()
        right.setObjectName("rightPanel")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self._stack = QStackedWidget()
        self._stack.setObjectName("pluginStack")
        right_layout.addWidget(self._stack)
        self._message_page = QWidget()
        message_layout = QVBoxLayout(self._message_page)
        message_layout.setContentsMargins(32, 24, 32, 24)
        message_layout.addStretch()
        self._message = QLabel()
        self._message.setTextFormat(Qt.PlainText)
        self._message.setAlignment(Qt.AlignCenter)
        self._message.setWordWrap(True)
        self._message.setTextInteractionFlags(Qt.TextSelectableByMouse)
        message_layout.addWidget(self._message)
        self._retry = QPushButton("重新加载")
        self._retry.clicked.connect(self._retry_current)
        message_layout.addWidget(self._retry, alignment=Qt.AlignCenter)
        message_layout.addStretch()
        self._stack.addWidget(self._message_page)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 840])
        root.addWidget(splitter)
        menu = self.menuBar().addMenu("视图")
        refresh = QAction("刷新插件", self)
        refresh.setShortcut(QKeySequence("F5"))
        refresh.triggered.connect(self.refresh_plugins)
        menu.addAction(refresh)
        search = QAction("搜索插件", self)
        search.setShortcut(QKeySequence.Find)
        search.triggered.connect(self._focus_search)
        menu.addAction(search)

    def refresh_plugins(self) -> None:
        previous_id = self._current_id
        query = self._search.text() if hasattr(self, "_search") else ""
        try:
            result = self._manager.scan()
        except PluginUnloadError as exc:
            self.statusBar().showMessage(str(exc))
            return
        while self._stack.count() > 1:
            self._stack.removeWidget(self._stack.widget(1))
        self._current_id = None
        selected = None
        with QSignalBlocker(self._list):
            self._list.clear()
            search_item = QListWidgetItem()
            search_item.setFlags(Qt.ItemIsEnabled)
            search_widget = PluginSearchItemWidget()
            self._search = search_widget.search_edit
            self._search.setText(query)
            self._search.textChanged.connect(self._filter_plugins)
            search_item.setSizeHint(search_widget.sizeHint())
            self._list.addItem(search_item)
            self._list.setItemWidget(search_item, search_widget)
            for plugin in result.plugins:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, plugin.id)
                widget = PluginListItemWidget(plugin)
                item.setSizeHint(widget.sizeHint())
                self._list.addItem(item)
                self._list.setItemWidget(item, widget)
                if plugin.id == previous_id:
                    selected = item
        status = f"{len(result.plugins)} 个插件"
        if result.errors:
            status += f" · {len(result.errors)} 个配置错误"
        self.statusBar().showMessage(status)
        self.statusBar().setToolTip("\n".join(result.errors))
        if not result.plugins:
            message = "尚未发现插件\n将含 config.json 的插件文件夹放入：\n" + str(self._manager.directory)
        else:
            message = "从左侧选择工具"
        if result.errors:
            message += "\n\n配置错误：\n" + "\n".join(result.errors)
        self._show_message(message)
        self._filter_plugins(query)
        if selected is not None and not selected.isHidden():
            self._list.setCurrentItem(selected)

    def _show_message(self, text: str, *, retry: bool = False) -> None:
        self._message.setText(text)
        self._retry.setVisible(retry)
        self._stack.setCurrentWidget(self._message_page)

    def _selection_changed(self, current, previous) -> None:
        if current is None:
            return
        plugin_id = current.data(Qt.UserRole)
        if plugin_id:
            self._current_id = plugin_id
            self._show_plugin(plugin_id)

    def _show_plugin(self, plugin_id: str) -> None:
        try:
            widget = self._manager.load_widget(plugin_id, self._stack)
        except PluginLoadError as exc:
            self._show_message(f"插件加载失败\n\n{exc}", retry=True)
            return
        if self._stack.indexOf(widget) < 0:
            self._stack.addWidget(widget)
        self._stack.setCurrentWidget(widget)

    def _retry_current(self) -> None:
        if self._current_id:
            self._show_plugin(self._current_id)

    def _focus_search(self) -> None:
        self._search.setFocus()
        self._search.selectAll()

    def _filter_plugins(self, text: str) -> None:
        query = text.strip().casefold()
        for row in range(1, self._list.count()):
            item = self._list.item(row)
            plugin = self._manager.plugins[item.data(Qt.UserRole)]
            item.setHidden(query not in plugin.search_text)

    def shutdown(self) -> bool:
        try:
            self._manager.unload_all()
        except PluginUnloadError as exc:
            self.statusBar().showMessage(str(exc))
            return False
        return True

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.shutdown():
            self._close_retry.start()
            event.ignore()
            return
        self._close_retry.stop()
        super().closeEvent(event)
