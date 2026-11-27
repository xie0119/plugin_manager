from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from launcher.manifest import PluginManifest


class PluginListItemWidget(QWidget):
    """Preserve the original icon, title and description list layout."""

    def __init__(self, plugin: PluginManifest, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        if plugin.icon:
            pixmap = QPixmap(str(plugin.icon))
            if not pixmap.isNull():
                icon = QLabel()
                icon.setObjectName("pluginIcon")
                icon.setFixedSize(32, 32)
                icon.setPixmap(pixmap.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                layout.addWidget(icon)
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(2)
        title = QLabel(plugin.name)
        title.setTextFormat(Qt.PlainText)
        title.setObjectName("pluginTitle")
        description = QLabel(plugin.description)
        description.setTextFormat(Qt.PlainText)
        description.setObjectName("pluginSubtitle")
        description.setWordWrap(True)
        text.addWidget(title)
        text.addWidget(description)
        layout.addLayout(text, 1)
        self.setToolTip("\n".join(value for value in (plugin.name, plugin.description, plugin.version) if value))

    def sizeHint(self) -> QSize:
        return QSize(240, max(66, super().sizeHint().height()))


class PluginSearchItemWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchEdit")
        self.search_edit.setPlaceholderText("搜索插件...")
        self.search_edit.setAccessibleName("搜索插件")
        self.search_edit.setClearButtonEnabled(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        layout.addWidget(self.search_edit)

    def sizeHint(self) -> QSize:
        return QSize(240, 40)
