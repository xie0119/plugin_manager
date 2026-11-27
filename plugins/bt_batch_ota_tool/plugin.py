"""Qt interface for the independent Bluetooth batch OTA tool."""

from __future__ import annotations

import datetime
import importlib.util
import json
import queue
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

if __package__:
    from .btbatchtool import __version__
    from .btbatchtool.runner import BatchRunner
    from .btbatchtool.settings import (
        Settings,
        modem_versions_from_filename,
        version_from_filename,
    )
    from .btbatchtool.workflow import Workflow
else:
    # A direct run gets its own package context without exposing vendor packages
    # as global btbatchtool/btotatool modules in the host process.
    import importlib

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    package = importlib.import_module("plugins.bt_batch_ota_tool.plugin")
    __version__ = package.__version__
    BatchRunner = package.BatchRunner
    Settings = package.Settings
    Workflow = package.Workflow
    modem_versions_from_filename = package.modem_versions_from_filename
    version_from_filename = package.version_from_filename


class BatchOtaWidget(QWidget):
    """Own the job and release it only after all active upgrades finish."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("btBatchOtaTool")
        self.setStyleSheet(Path(__file__).with_name("style.qss").read_text(encoding="utf-8"))
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.running = False
        self.runner = None
        self._thread = None
        self._close_when_done = False
        self._auto_modem_versions = None
        self.queue = queue.Queue()
        self.rows = {}
        self.information = {}
        self.log_records = []
        self.probe_records = []
        self.entries = {}
        self.controls = []
        self._build_ui()
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _entry(self, key, value):
        widget = QLineEdit(value)
        widget.setAccessibleName(key)
        self.entries[key] = widget
        self.controls.append(widget)
        return widget

    def _check(self, text, checked=False):
        widget = QCheckBox(text)
        widget.setChecked(checked)
        self.controls.append(widget)
        return widget

    def _paired(self, *fields):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        for label, widget in fields:
            layout.addWidget(QLabel(label))
            layout.addWidget(widget, 1)
        return row

    def _file_row(self, entry, callback):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(entry, 1)
        button = QPushButton("浏览")
        button.clicked.connect(callback)
        self.controls.append(button)
        layout.addWidget(button)
        return row

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        title = QLabel(f"蓝牙批量 OTA · {__version__}")
        title.setObjectName("pluginTitle")
        root.addWidget(title)

        splitter = QSplitter(Qt.Vertical)
        scroll = QScrollArea()
        scroll.setObjectName("otaFormScroll")
        scroll.viewport().setObjectName("otaFormViewport")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        form_box = QGroupBox("筛选与升级内容")
        form_box.setObjectName("otaForm")
        form = QFormLayout(form_box)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.prefix = self._entry("蓝牙名筛选", "XFN_")
        self.rssi = self._entry("最低 RSSI (dBm)", "-80")
        self.max_attempts = self._entry("最大尝试次数", "3")
        self.concurrency = self._entry("同时台数", "3")
        self.firmware = self._entry("手表 OTA 文件", "")
        self.local_version = self._entry("固件版本号", "")
        self.reconnect_timeout = self._entry("回连超时（秒）", "120")
        self.skip_same_version = self._check("同版本号跳过", True)
        self.shine = self._check("ShineFit UUID（E852/E853/E854）")
        self.modem = self._check("升级 modem（可单独执行；选择 OTA 时在手表升级后执行）")
        self.modem_file = self._entry("modem 本地文件", "")
        self.modem_remote = self._entry("手表目标文件路径", "/NOR:/modem_update.bin")
        self.overwrite = self._check("允许覆盖目标同名文件", True)
        self.modem_source = self._entry("modem 当前版本", "")
        self.modem_target = self._entry("modem 目标版本", "")
        self.send_timeout = self._entry("发送超时（秒）", "20")
        self.wait_timeout = self._entry("升级超时（秒）", "20")
        self.alert_on_success = self._check("升级完成亮屏和长震", True)
        form.addRow("蓝牙名筛选", self.prefix)
        form.addRow("最低 RSSI (dBm)", self.rssi)
        form.addRow("批量执行", self._paired(
            ("最大尝试次数", self.max_attempts), ("同时台数", self.concurrency)))
        form.addRow("手表 OTA 文件", self._file_row(self.firmware, self._browse_firmware))
        form.addRow("固件版本号", self.local_version)
        form.addRow("回连超时（秒）", self.reconnect_timeout)
        form.addRow("", self.skip_same_version)
        form.addRow("", self.shine)
        form.addRow("", self.modem)
        form.addRow("modem 本地文件", self._file_row(self.modem_file, self._browse_modem))
        form.addRow("手表目标文件路径", self.modem_remote)
        form.addRow("", self.overwrite)
        form.addRow("modem 版本", self._paired(
            ("当前版本", self.modem_source), ("目标版本", self.modem_target)))
        form.addRow("modem 超时（秒）", self._paired(
            ("发送超时", self.send_timeout), ("升级超时", self.wait_timeout)))
        form.addRow("", self.alert_on_success)
        help_box = QWidget()
        help_layout = QVBoxLayout(help_box)
        help_layout.setContentsMargins(0, 6, 0, 2)
        help_layout.setSpacing(4)
        for text in (
            "蓝牙名无逗号时按前缀匹配；英文逗号分隔时匹配完整名称。",
            "OTA 文件留空可单独升级 modem；v030_v031_delta.par 自动填入版本 030 → 031。",
            "同时台数可设 1～20 台；停止后等待正在处理的设备完成，回连核验优先。",
            "发送与升级超时按进度连续不变计时；结果未知时保留记录并转下一台。",
        ):
            help_text = QLabel(text)
            help_text.setObjectName("otaHelpText")
            help_text.setTextFormat(Qt.PlainText)
            help_text.setWordWrap(True)
            hint_font = help_text.font()
            hint_font.setLetterSpacing(QFont.AbsoluteSpacing, 0.5)
            help_text.setFont(hint_font)
            help_layout.addWidget(help_text)
        form.addRow(help_box)
        scroll.setWidget(form_box)
        splitter.addWidget(scroll)

        results = QWidget()
        result_layout = QVBoxLayout(results)
        result_layout.setContentsMargins(0, 4, 0, 0)
        actions = QHBoxLayout()
        self.start_button = QPushButton("开始批量执行")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("停止（完成正在处理的设备）")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.request_stop)
        export = QPushButton("导出记录")
        export.clicked.connect(self._export)
        actions.addWidget(self.start_button)
        actions.addWidget(self.stop_button)
        actions.addStretch()
        actions.addWidget(export)
        result_layout.addLayout(actions)
        self.status = QLabel("未开始")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        result_layout.addWidget(self.status)
        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setHeaderLabels(["RSSI", "地址", "名称", "固件版本", "modem 版本", "结果"])
        self.tree.setMinimumHeight(120)
        header = self.tree.header()
        for column, width in enumerate((65, 140, 130, 155, 195, 300)):
            self.tree.setColumnWidth(column, width)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setMinimumHeight(90)
        result_splitter = QSplitter(Qt.Vertical)
        result_splitter.addWidget(self.tree)
        result_splitter.addWidget(self.log)
        result_splitter.setSizes([180, 110])
        result_layout.addWidget(result_splitter, 1)
        splitter.addWidget(results)
        splitter.setSizes([400, 300])
        root.addWidget(splitter, 1)
        self.firmware.textChanged.connect(self._file_version)
        self.modem_file.textChanged.connect(self._modem_file_changed)

    def _file_version(self, path):
        self.local_version.setText(version_from_filename(path))

    def _modem_file_changed(self, path):
        self.modem.setChecked(bool(path.strip()))
        versions = modem_versions_from_filename(path)
        fields = (self.modem_source, self.modem_target)
        if versions:
            for field, value in zip(fields, versions):
                field.setText(value)
        elif self._auto_modem_versions:
            for field, previous in zip(fields, self._auto_modem_versions):
                if field.text() == previous:
                    field.clear()
        self._auto_modem_versions = versions

    def _browse_firmware(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择手表 OTA 文件", "", "所有文件 (*)")
        if path:
            self.firmware.setText(path)

    def _browse_modem(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 modem 补丁包", "", "所有文件 (*)")
        if path:
            self.modem_file.setText(path)

    def settings(self):
        return Settings(
            prefix=self.prefix.text().strip(), min_rssi=int(self.rssi.text()),
            max_attempts=int(self.max_attempts.text()), concurrency=int(self.concurrency.text()),
            firmware=self.firmware.text().strip(), local_version=self.local_version.text().strip(),
            reconnect_timeout=(float(self.reconnect_timeout.text())
                               if self.firmware.text().strip() else 120),
            skip_same=self.skip_same_version.isChecked(), shine=self.shine.isChecked(),
            alert_on_success=self.alert_on_success.isChecked(), modem=self.modem.isChecked(),
            modem_file=self.modem_file.text().strip(), remote=self.modem_remote.text().strip(),
            overwrite=self.overwrite.isChecked(), modem_source=self.modem_source.text().strip(),
            modem_target=self.modem_target.text().strip(),
            send_timeout=float(self.send_timeout.text()),
            wait_timeout=float(self.wait_timeout.text()),
        ).validate()

    def _event(self, kind, data):
        self.queue.put((kind, data))

    def start(self):
        if self.running:
            return
        try:
            settings = self.settings()
            missing = [name for name in ("bleak", "serial")
                       if importlib.util.find_spec(name) is None]
            if missing:
                raise RuntimeError("缺少蓝牙运行依赖，请先安装插件 requirements.txt："
                                   + "、".join(missing))
            workflow = Workflow(
                settings,
                on_version=lambda *values: self._event("version", values),
                on_information=lambda *values: self._event("information", values),
            )
            runner = BatchRunner(settings, workflow, self._event)
        except Exception as error:
            QMessageBox.warning(self, "无法开始", str(error))
            return
        self.runner = runner
        self._close_when_done = False
        self.tree.clear()
        self.rows.clear()
        self.information.clear()
        self.log_records.clear()
        self.probe_records.clear()
        self.log.clear()
        self.running = True
        self._set_controls(False)
        rule = f"仅允许 {settings.prefix}" if "," in settings.prefix else f"名称以 {settings.prefix} 开头"
        self.status.setText(
            f"持续扫描：{rule}，RSSI ≥ {settings.min_rssi}；"
            f"同时{settings.concurrency}台；每台最多{settings.max_attempts}次")
        tasks = []
        if settings.watch_enabled:
            tasks.append(f"手表目标版本 {settings.target_watch_version}")
        if settings.modem:
            tasks.append(f"modem {settings.modem_source} → {settings.modem_target}")
        self._event("log", "开始批量任务：" + "；".join(tasks))
        self._thread = threading.Thread(target=self._run, name="plugin-batch-ota", daemon=False)
        try:
            self._thread.start()
        except Exception as error:
            self._thread = None
            self.running = False
            self._set_controls(True)
            QMessageBox.warning(self, "无法开始", str(error))

    def _run(self):
        try:
            self.runner.run()
        except Exception as error:
            self._event("log", f"批量任务异常：{error}")
        finally:
            self._event("worker_finished", None)

    def _set_controls(self, enabled):
        for widget in self.controls:
            widget.setEnabled(enabled)
        self.start_button.setEnabled(enabled)
        self.stop_button.setEnabled(not enabled)

    def request_stop(self):
        if self.runner:
            self.runner.stop.set()
        if self.running:
            self.stop_button.setEnabled(False)
            self.status.setText("已停止接收新设备；等待正在处理的设备完成，不强制中断刷写")

    def _row(self, raw_address):
        address = raw_address.upper()
        if address not in self.rows:
            item = QTreeWidgetItem(["", raw_address, "", "", "", ""])
            for column in range(5):
                item.setTextAlignment(column, Qt.AlignCenter)
            self.tree.addTopLevelItem(item)
            self.rows[address] = item
        return self.rows[address]

    def _poll(self):
        for _ in range(200):
            try:
                kind, data = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind == "device":
                address, name, rssi, status = data
                item = self._row(address)
                if isinstance(rssi, (int, float)) and -127 <= rssi <= 0:
                    item.setText(0, str(rssi))
                item.setText(2, name)
                item.setText(5, status)
                item.setToolTip(5, status)
            elif kind == "version":
                address, version_kind, value = data
                column = {2: 3, 5: 4}.get(version_kind)
                if column is not None:
                    label = "手表固件版本来源" if version_kind == 2 else "modem 版本来源"
                    source = self.information.get(address.upper(), {}).get(label, "")
                    source = "DIS" if source.startswith("DIS") else source
                    text = value + (f" [{source}]" if source else "")
                    item = self._row(address)
                    item.setText(column, text)
                    item.setToolTip(column, text)
            elif kind == "information":
                address, label, value = data
                self.information.setdefault(address.upper(), {})[label] = value
            elif kind == "diagnostic":
                self.probe_records.append(data)
            elif kind == "log":
                stamp = datetime.datetime.now().strftime("%H:%M:%S")
                text = f"[{stamp}] {data}"
                self.log_records.append(text)
                self.log.appendPlainText(text)
            elif kind == "worker_finished":
                # The runner emits 'done' just before returning. Release only
                # after its owning thread has actually left the backend.
                if self._thread and self._thread.is_alive():
                    self._event(kind, data)
                    break
                self._thread = None
                self.running = False
                self._set_controls(True)
                self.status.setText(f"已停止，本轮处理 {len(self.rows)} 台；重新开始将建立新一轮记录")
                if self._close_when_done:
                    self.close()
                    return

    def export_record(self, path):
        record = dict(
            devices=self.information,
            results={address: [item.text(column) for column in range(6)]
                     for address, item in self.rows.items()},
            log="\n".join(self.log_records),
            verification_diagnostics=self.probe_records,
        )
        Path(path).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出升级记录", "升级记录.json", "JSON 记录 (*.json)")
        if path:
            try:
                self.export_record(path)
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, "导出失败", str(error))

    def can_unload(self):
        if self.running or (self._thread and self._thread.is_alive()):
            self.request_stop()
            return False
        return True

    def closeEvent(self, event):
        if not self.can_unload():
            self._close_when_done = True
            event.ignore()
            return
        self._timer.stop()
        event.accept()


def create_widget(parent=None):
    return BatchOtaWidget(parent)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = create_widget()
    window.setAttribute(Qt.WA_QuitOnClose, True)
    window.setWindowTitle("蓝牙批量 OTA")
    window.resize(1100, 900)
    window.show()
    sys.exit(app.exec())
