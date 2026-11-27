from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import start


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.logs = []
        self.engine = start.Bootstrap(self.root, start.Config(startup_timeout=1), log=self.logs.append)
        self.engine.repo.mkdir()
        self.engine.environment_python = Path(sys.executable)

    def tearDown(self):
        self.temporary.cleanup()

    def entry(self, text):
        path = self.engine.repo / "test_main.py"
        path.write_text(text, encoding="utf-8")
        return path

    def test_immediate_application_failure_is_reported(self):
        entry = self.entry("print('APP_FAILED', flush=True)\nraise SystemExit(7)\n")
        with self.assertRaisesRegex(RuntimeError, "APP_FAILED"):
            self.engine.start_application(entry)
        self.assertIsNone(self.engine.launched_pid)

    def test_successful_short_program_completes(self):
        entry = self.entry("print('APP_DONE', flush=True)\n")
        self.assertEqual(self.engine.start_application(entry), 0)
        self.assertIn("APP_DONE", (self.root / "logs/application.log").read_text())

    def test_unready_application_times_out_and_releases_lock(self):
        entry = self.entry("import time;time.sleep(30)\n")
        began = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.engine.start_application(entry)
        self.assertLess(time.monotonic() - began, 8)
        with start.instance_lock(self.root, "application.lock"):
            pass

    def test_cancel_during_application_start(self):
        entry = self.entry("import time;time.sleep(30)\n")
        timer = threading.Timer(0.3, self.engine.cancelled.set)
        timer.start()
        try:
            with self.assertRaises(InterruptedError):
                self.engine.start_application(entry)
        finally:
            timer.join()

    def test_numeric_command_timeout_is_used(self):
        began = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.engine.run([sys.executable, "-c", "import time;time.sleep(30)"], timeout=0.3)
        self.assertLess(time.monotonic() - began, 5)

    def test_update_timeout_preserves_local_start(self):
        (self.engine.repo / ".git").mkdir()
        calls = []

        def command(arguments, **kwargs):
            calls.append((arguments, kwargs))
            if "config" in arguments:
                return 0, self.engine.config.repository_url
            if "status" in arguments:
                return 0, ""
            if "branch" in arguments:
                return 0, "master"
            if "fetch" in arguments:
                raise TimeoutError("offline")
            raise AssertionError(arguments)

        self.engine.run = command
        self.engine.sync_repository()
        fetch = [kwargs for args, kwargs in calls if "fetch" in args]
        self.assertEqual(fetch[0]["timeout"], 5)
        self.assertTrue(any("本地版本" in line for line in self.logs))

    def test_proxy_changes_only_child_environment(self):
        with patch.dict(os.environ, {"http_proxy": "", "https_proxy": "", "HTTP_PROXY": "", "HTTPS_PROXY": ""}):
            with patch("start.getproxies", return_value={"https": "http://system-proxy:8080"}):
                environment = start.clean_environment()
                self.assertEqual(environment["https_proxy"], "http://system-proxy:8080")
                explicit = start.clean_environment(proxy_url="http://explicit-proxy:8888")
                self.assertEqual(explicit["https_proxy"], "http://explicit-proxy:8888")
                self.assertEqual(os.environ["https_proxy"], "")

    def test_old_configuration_gets_new_defaults(self):
        (self.root / "launcher-config.json").write_text(json.dumps({"update_on_launch": True}), encoding="utf-8")
        config = start.Config.load(self.root)
        self.assertEqual(config.startup_timeout, 60)
        self.assertEqual(config.update_timeout, 5)


if __name__ == "__main__":
    unittest.main()
