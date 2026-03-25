from __future__ import annotations

from typing import Any, Dict

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget


class Plugin:
    """简单按钮插件：点击按钮写日志。"""

    name = "简单按钮示例"
    description = "点击按钮后在日志中输出一条信息。"
    version = "1.0.0"
    author = "Toolbox Framework"
    category = "示例 / 基础"
    icon = ""  # 可替换为实际图标路径

    def __init__(self, event_bus, config_manager, logger, plugin_id: str, plugin_dir) -> None:
        self._event_bus = event_bus
        self._config_manager = config_manager
        self._logger = logger
        self._plugin_id = plugin_id
        self._plugin_dir = plugin_dir

    def on_load(self) -> None:
        self._logger.info("SimpleButton Plugin loaded")

    def on_unload(self) -> None:
        self._logger.info("SimpleButton Plugin unloaded")

    def get_widget(self, parent: QWidget) -> QWidget:
        widget = QWidget(parent)
        layout = QVBoxLayout()
        layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        label = QLabel("这是一个简单的按钮插件示例。")
        button = QPushButton("点我写日志")

        def on_clicked() -> None:
            self._logger.info("Button clicked in SimpleButton plugin")
            # 通过事件总线广播一个事件，供其他插件使用
            self._event_bus.publish(
                "simple_button.clicked", {"message": "Button clicked"}, "simple_button"
            )

        button.clicked.connect(on_clicked)

        layout.addWidget(label)
        layout.addWidget(button)
        layout.addStretch()
        widget.setLayout(layout)
        return widget

