from __future__ import annotations

from argparse import Namespace
import logging
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import start


class ResetRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.engine = start.Bootstrap(self.root, start.Config(), log=lambda text: None)
        self.engine.repo.mkdir()
        (self.engine.repo / "main.py").write_text("OLD")
        (self.engine.repo / "settings.json").write_text("USER_SETTINGS")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def new_clone(path):
        (path / "main.py").write_text("NEW")

    def assert_original(self):
        self.assertEqual((self.engine.repo / "main.py").read_text(), "OLD")
        self.assertEqual((self.engine.repo / "settings.json").read_text(), "USER_SETTINGS")
        self.assertFalse(list(self.root.glob(".repository-clone-*")))

    def test_success_replaces_repository_and_keeps_backup(self):
        self.engine._clone_into = self.new_clone
        backup = self.engine.reset_repository()
        self.assertEqual((self.engine.repo / "main.py").read_text(), "NEW")
        self.assertEqual((backup / "main.py").read_text(), "OLD")
        self.assertEqual((backup / "settings.json").read_text(), "USER_SETTINGS")
        self.assertFalse((self.engine.repo / "settings.json").exists())

    def test_network_failure_keeps_original(self):
        def fail(path):
            (path / "partial.txt").write_text("partial")
            raise RuntimeError("offline")
        self.engine._clone_into = fail
        with self.assertRaises(RuntimeError):
            self.engine.reset_repository()
        self.assert_original()
        self.assertFalse((self.root / "repository-backups").exists())

    def test_missing_entry_keeps_original(self):
        self.engine._clone_into = lambda path: None
        with self.assertRaises(FileNotFoundError):
            self.engine.reset_repository()
        self.assert_original()

    def test_failed_switch_restores_original(self):
        self.engine._clone_into = self.new_clone
        move = self.engine._move_owned_directory
        def fail_commit(source, destination):
            if source.name.startswith(".repository-clone-"):
                raise OSError("simulated failed switch")
            move(source, destination)
        self.engine._move_owned_directory = fail_commit
        with self.assertRaises(OSError):
            self.engine.reset_repository()
        self.assert_original()

    def test_cancel_after_clone_keeps_original(self):
        def cancel(path):
            self.new_clone(path)
            self.engine.cancelled.set()
        self.engine._clone_into = cancel
        with self.assertRaises(InterruptedError):
            self.engine.reset_repository()
        self.assert_original()

    def test_missing_repository_can_be_reset(self):
        engine = start.Bootstrap(self.root, start.Config(repository_dir="fresh"), log=lambda text: None)
        engine._clone_into = self.new_clone
        self.assertIsNone(engine.reset_repository())
        self.assertEqual((engine.repo / "main.py").read_text(), "NEW")
        self.assert_original()


@unittest.skipUnless(os.name == "nt", "Windows Tk interface")
class ResetButtonTests(unittest.TestCase):
    def test_button_is_right_of_title_and_interrupts_then_resets(self):
        import tkinter as tk
        real_tk = tk.Tk
        calls, errors, cancellations = [], [], []
        class Engine:
            application_process = None
            def __init__(self, root, config, log, status, cancelled):
                self.cancelled = cancelled
                cancellations.append(cancelled)
            def start(self, **options):
                resetting = options["reset_plugins"]
                calls.append(resetting)
                if not resetting:
                    self.cancelled.wait(2)
                    raise InterruptedError("cancelled for reset")
                return 0

        def factory():
            window = real_tk()
            def widgets(parent):
                for child in parent.winfo_children():
                    yield child
                    yield from widgets(child)
            def click():
                try:
                    children = list(widgets(window))
                    button = next(w for w in children if w.winfo_class() == "TButton" and w.cget("text") == "重置插件")
                    title = next(w for w in children if w.winfo_class() == "TLabel" and w.cget("text") == "PluginManager")
                    self.assertGreater(button.winfo_rootx(), title.winfo_rootx() + title.winfo_width())
                    self.assertLess(abs(button.winfo_rooty() + button.winfo_height()/2 - title.winfo_rooty() - title.winfo_height()/2), 8)
                    button.invoke()
                except BaseException as error:
                    errors.append(error)
                    window.destroy()
            def timeout():
                errors.append(AssertionError("GUI did not complete reset"))
                for event in cancellations:
                    event.set()
                window.destroy()
            window.after(400, click)
            window.after(4000, timeout)
            return window

        with tempfile.TemporaryDirectory() as temporary, patch("tkinter.Tk", side_effect=factory), patch("start.Bootstrap", Engine):
            start.gui(Path(temporary), start.Config(), logging.getLogger("reset-ui-test"),
                      Namespace(prepare_only=False, application_args=[], reset_plugins=False))
        if errors:
            raise errors[0]
        self.assertEqual(calls, [False, True])


if __name__ == "__main__":
    unittest.main()
