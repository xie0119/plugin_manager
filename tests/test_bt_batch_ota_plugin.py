from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from launcher.plugin_manager import PluginManager, PluginUnloadError
from launcher.ui.main_window import MainWindow

ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    """An in-flight upgrade deliberately outlives a graceful stop request."""

    def __init__(self, settings, workflow, event):
        self.settings = settings
        self.event = event
        self.stop = threading.Event()
        self.started = threading.Event()
        self.finish = threading.Event()

    def run(self):
        self.started.set()
        self.event("device", ("AA", "XFN_test", -45, "正在升级"))
        self.finish.wait(5)
        self.event("device", ("aa", "XFN_test", -43, "升级完成"))
        self.event("done", None)


class BatchOtaPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.parent = QWidget()
        self.manager = PluginManager(ROOT / "plugins")
        self.manager.scan()
        self.widget = self.manager.load_widget("bt_batch_ota_tool", self.parent)
        self.module = sys.modules[type(self.widget).__module__]
        self.temporary = tempfile.TemporaryDirectory()
        self.modem_file = Path(self.temporary.name) / "v030_v031_delta.par"
        self.modem_file.write_bytes(b"fake-modem-data")

    def tearDown(self):
        if self.widget.runner and hasattr(self.widget.runner, "finish"):
            self.widget.runner.finish.set()
        if self.widget._thread:
            self.widget._thread.join(2)
            self.widget._poll()
        self.manager.unload_all()
        self.parent.close()
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.temporary.cleanup()

    def start_fake_upgrade(self, widget=None):
        widget = widget or self.widget
        module = sys.modules[type(widget).__module__]
        widget.modem_file.setText(str(self.modem_file))
        with patch.object(module, "BatchRunner", FakeRunner), \
                patch.object(module.QMessageBox, "warning") as warning:
            widget.start()
        warning.assert_not_called()
        self.assertTrue(widget.runner.started.wait(1))
        return widget.runner

    def test_embedded_loader_uses_private_modules_without_sys_path_changes(self):
        self.assertIs(self.widget.parent(), self.parent)
        self.assertTrue(type(self.widget).__module__.startswith("_launcher_plugin_"))
        self.assertNotIn("btotatool", sys.modules)
        self.assertNotIn("btbatchtool", sys.modules)
        self.assertNotIn(str(ROOT / "plugins" / "bt_batch_ota_tool"), sys.path)
        self.assertFalse(self.widget.running)
        self.assertIsNone(self.widget._thread)

    def test_defaults_and_watch_version_autofill(self):
        self.assertEqual(self.widget.concurrency.text(), "3")
        self.assertEqual(self.widget.modem_remote.text(), "/NOR:/modem_update.bin")
        self.assertTrue(self.widget.skip_same_version.isChecked())
        self.assertTrue(self.widget.overwrite.isChecked())
        self.assertTrue(self.widget.alert_on_success.isChecked())
        self.widget.firmware.setText("ota_firmware_Z11_V1_16_23_.bin")
        self.assertEqual(self.widget.local_version.text(), "1.16.23")

    def test_modem_autofill_and_manual_version_preservation(self):
        self.widget.modem_file.setText("v030_v031_delta.par")
        self.assertTrue(self.widget.modem.isChecked())
        self.assertEqual(self.widget.modem_source.text(), "030")
        self.assertEqual(self.widget.modem_target.text(), "031")
        self.widget.modem_source.setText("Z11_V030.017.pb22.060")
        self.widget.modem_file.setText("other.par")
        self.assertEqual(self.widget.modem_source.text(), "Z11_V030.017.pb22.060")
        self.assertEqual(self.widget.modem_target.text(), "")
        self.widget.modem_file.clear()
        self.assertFalse(self.widget.modem.isChecked())

    def test_invalid_parameters_do_not_start_a_worker(self):
        self.widget.concurrency.setText("21")
        with patch.object(self.module, "BatchRunner") as runner, \
                patch.object(self.module.QMessageBox, "warning") as warning:
            self.widget.start()
        runner.assert_not_called()
        warning.assert_called_once()
        self.assertFalse(self.widget.running)

    def test_real_worker_updates_rows_and_stop_waits_for_completion(self):
        runner = self.start_fake_upgrade()
        self.assertFalse(self.widget.concurrency.isEnabled())
        self.widget._poll()
        self.assertEqual(self.widget.rows["AA"].text(5), "正在升级")
        self.widget.request_stop()
        self.assertTrue(runner.stop.is_set())
        self.assertTrue(self.widget.running)
        self.assertTrue(self.widget._thread.is_alive())
        runner.finish.set()
        self.widget._thread.join(1)
        self.widget._poll()
        self.assertFalse(self.widget.running)
        self.assertTrue(self.widget.concurrency.isEnabled())
        self.assertEqual(self.widget.rows["AA"].text(5), "升级完成")

    def test_export_keeps_versions_results_diagnostics_and_logs(self):
        events = [
            ("information", ("aa", "手表固件版本来源", "DIS 2A26")),
            ("version", ("AA", 2, "1.16.23")),
            ("device", ("aa", "XFN_test", -42, "升级完成")),
            ("version", ("aa", 5, "Z11_V031.017.pb22.060")),
            ("diagnostic", {"address": "AA", "verified": True}),
            ("log", "已核验版本"),
        ]
        for event in events:
            self.widget._event(*event)
        self.widget._poll()
        record_path = Path(self.temporary.name) / "升级记录.json"
        self.widget.export_record(record_path)
        data = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["results"]), 1)
        self.assertEqual(data["results"]["AA"], [
            "-42", "AA", "XFN_test", "1.16.23 [DIS]", "Z11_V031.017.pb22.060", "升级完成"])
        self.assertEqual(data["verification_diagnostics"][0]["verified"], True)
        self.assertIn("已核验版本", data["log"])
        self.assertEqual(self.widget.rows["AA"].toolTip(5), "升级完成")

    def test_refresh_preflight_preserves_all_widgets_until_upgrade_completes(self):
        text_widget = self.manager.load_widget("folder_text_tool", self.parent)
        runner = self.start_fake_upgrade()
        with self.assertRaises(PluginUnloadError):
            self.manager.scan()
        self.assertIs(self.manager.load_widget("folder_text_tool", self.parent), text_widget)
        self.assertIs(self.manager.load_widget("bt_batch_ota_tool", self.parent), self.widget)
        self.assertTrue(runner.stop.is_set())
        runner.finish.set()
        self.widget._thread.join(1)
        self.widget._poll()
        self.assertEqual(len(self.manager.scan().plugins), 2)

    def test_main_window_refresh_and_close_wait_for_active_upgrade(self):
        window = MainWindow(ROOT / "plugins")
        window.show()
        widget = window._manager.load_widget("bt_batch_ota_tool", window._stack)
        window._stack.addWidget(widget)
        window._stack.setCurrentWidget(widget)
        runner = self.start_fake_upgrade(widget)
        try:
            window.refresh_plugins()
            self.assertIs(window._stack.currentWidget(), widget)
            self.assertIn("正在完成任务", window.statusBar().currentMessage())
            self.assertFalse(window.close())
            self.assertTrue(window.isVisible())
            self.assertTrue(runner.stop.is_set())
            runner.finish.set()
            for _ in range(30):
                QTest.qWait(50)
                if not window.isVisible():
                    break
            self.assertFalse(window.isVisible())
        finally:
            runner.finish.set()
            if widget._thread:
                widget._thread.join(1)
                widget._poll()
            window.close()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
