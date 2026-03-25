from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QLabel, QHBoxLayout, QVBoxLayout, QWidget, QLineEdit


class PluginListItemWidget(QWidget):
    """左侧插件列表的自定义项：图标 + 标题 + 描述。"""

    def __init__(
        self,
        name: str,
        description: str,
        icon_path: Optional[str] = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self._icon_label = QLabel()
        self._icon_label.setObjectName("pluginIcon")
        self._icon_label.setFixedSize(0, 0)

        if icon_path:
            path = Path(icon_path)
            if path.is_file():
                pix = QPixmap(str(path))
                if not pix.isNull():
                    self._icon_label.setPixmap(
                        pix.scaled(
                            32,
                            32,
                            Qt.KeepAspectRatio,
                            Qt.SmoothTransformation,
                        )
                    )

        title_label = QLabel(name)
        title_label.setObjectName("pluginTitle")
        title_label.setWordWrap(False)

        desc_label = QLabel(description)
        desc_label.setObjectName("pluginSubtitle")
        desc_label.setWordWrap(True)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        text_layout.addWidget(title_label)
        text_layout.addWidget(desc_label)

        main_layout = QHBoxLayout()
        main_layout.setContentsMargins(12, 8, 12, 8)
        main_layout.setSpacing(8)
        main_layout.addWidget(self._icon_label)
        main_layout.addLayout(text_layout)
        main_layout.addStretch()

        self.setLayout(main_layout)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(240, 66)


class PluginSearchItemWidget(QWidget):
    """嵌入在插件列表顶部的搜索项。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchEdit")
        self.search_edit.setPlaceholderText("搜索插件...")

        layout = QHBoxLayout()
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        layout.addWidget(self.search_edit)

        self.setLayout(layout)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(240, 40)


