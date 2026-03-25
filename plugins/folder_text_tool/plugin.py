from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class Plugin:
    """目录形式的文本工具插件示例。

    目录结构：
    plugins/
      folder_text_tool/
        plugin.py       # 本文件
        config.json     # 默认配置（可选）
        icon.png        # 图标（可选）
    """

    name = "目录插件-文本工具"
    description = "演示目录插件：带默认 config.json 与相对资源路径。"
    version = "1.0.0"
    author = "Toolbox Framework"
    category = "示例 / 目录插件"
    icon = ""  # 会在 __init__ 中根据 plugin_dir 动态设置

    def __init__(
        self,
        event_bus,
        config_manager,
        logger,
        plugin_id: str,
        plugin_dir: Path,
    ) -> None:
        self._event_bus = event_bus
        self._config_manager = config_manager
        self._logger = logger
        self._plugin_id = plugin_id
        self._plugin_dir = plugin_dir

        # 如果目录下有 icon.png，则使用相对路径注册图标
        icon_path = self._plugin_dir / "icon.png"
        if icon_path.exists():
            # 注意：这里保存的是字符串路径，主程序会按此加载图标
            self.icon = str(icon_path)

        # 先从统一配置目录加载；如果不存在，会自动回退到本目录下的 config.json
        self._config = self._config_manager.load(self._plugin_id)

    def on_load(self) -> None:
        self._logger.info("FolderTextTool Plugin loaded from %s", self._plugin_dir)

    def on_unload(self) -> None:
        self._logger.info("FolderTextTool Plugin unloaded")

    def get_widget(self, parent: QWidget) -> QWidget:
        widget = QWidget(parent)

        root_layout = QVBoxLayout()
        root_layout.setContentsMargins(24, 24, 24, 24)
        root_layout.setSpacing(16)

        title = QLabel("这是一个放在目录中的文本工具插件。")

        input_row = QHBoxLayout()
        input_label = QLabel("输入文本：")
        input_edit = QLineEdit()
        input_edit.setPlaceholderText("在这里输入内容...")

        # 从配置恢复上次输入（若 config.json 中有 last_text）
        last_text = self._config.get("last_text", "")
        input_edit.setText(last_text)

        input_row.addWidget(input_label)
        input_row.addWidget(input_edit)

        button = QPushButton("处理为反转字符串")
        result_label = QLabel("结果：")

        def on_process() -> None:
            text = input_edit.text()
            reversed_text = text[::-1]
            result_label.setText(f"结果：{reversed_text}")

            # 保存配置
            self._config["last_text"] = text
            self._config_manager.save(self._plugin_id, self._config)

            self._logger.info(
                "FolderTextTool processed text '%s' -> '%s'", text, reversed_text
            )

            # 发布事件，方便其他插件监听
            self._event_bus.publish(
                "folder_text_tool.processed",
                {"original": text, "result": reversed_text},
                self._plugin_id,
            )

        button.clicked.connect(on_process)

        root_layout.addWidget(title)
        root_layout.addLayout(input_row)
        root_layout.addWidget(button)
        root_layout.addWidget(result_label)
        root_layout.addStretch()

        widget.setLayout(root_layout)
        return widget

