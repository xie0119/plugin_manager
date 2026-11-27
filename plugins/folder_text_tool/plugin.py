"""An independent tool: python plugin.py, or create_widget() when embedded."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

PLUGIN_DIR = Path(__file__).resolve().parent


class TextToolWidget(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings_path = PLUGIN_DIR / "settings.json"
        self._settings = self._read_settings()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)
        root.addWidget(QLabel("这是一个放在目录中的文本工具插件。"))
        row = QHBoxLayout()
        row.addWidget(QLabel("输入文本："))
        self._input = QLineEdit()
        self._input.setPlaceholderText("在这里输入内容...")
        self._input.setAccessibleName("待处理文本")
        self._input.setText(self._settings.get("last_text", ""))
        row.addWidget(self._input)
        root.addLayout(row)
        button = QPushButton("处理为反转字符串")
        button.clicked.connect(self._process)
        self._input.returnPressed.connect(self._process)
        root.addWidget(button)
        self._result = QLabel("结果：")
        self._result.setTextFormat(Qt.PlainText)
        self._result.setWordWrap(True)
        root.addWidget(self._result)
        root.addStretch()

    def _read_settings(self) -> dict:
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("last_text", ""), str):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _process(self) -> None:
        text = self._input.text()
        self._result.setText(f"结果：{text[::-1]}")
        self._settings["last_text"] = text
        temporary = self._settings_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(self._settings, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self._settings_path)
        except OSError as exc:
            self._result.setText(f"结果：{text[::-1]}\n设置保存失败：{exc}")


def create_widget(parent: QWidget | None = None) -> QWidget:
    return TextToolWidget(parent)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    # A standalone tool owns its theme. Embedded tools inherit the launcher's QSS.
    local_theme = PLUGIN_DIR / "style.qss"
    if local_theme.is_file():
        app.setStyleSheet(local_theme.read_text(encoding="utf-8"))
    window = create_widget()
    window.setWindowTitle("目录插件-文本工具")
    window.resize(800, 600)
    window.show()
    sys.exit(app.exec())
