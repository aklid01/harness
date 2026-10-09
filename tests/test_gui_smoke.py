"""Offscreen drive of the real MainWindow: a worker-thread turn renders
user text, assistant markdown, and a tool card. Needs no display."""

import json
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QTextBrowser  # noqa: E402

from harness.gui.main_window import MainWindow  # noqa: E402

USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": None, "cached_tokens": None}


def fake_message(content=None, calls=()):
    tcs = [SimpleNamespace(id=f"c{i}", function=SimpleNamespace(name=n, arguments=json.dumps(a)))
           for i, (n, a) in enumerate(calls)]
    return SimpleNamespace(content=content, tool_calls=tcs,
                           model_dump=lambda exclude_none=True: {"role": "assistant", "content": content})


class GuiSmokeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication([])

    def make_window(self):
        win = MainWindow()
        win.show()
        self.addCleanup(win.bridge.shutdown)
        self.addCleanup(win.close)
        return win
        self.addCleanup(win.close)
        return win

    def test_worker_turn_renders(self):
        win = self.make_window()
        done = []
        with mock.patch("harness.session.save"), mock.patch("harness.session.all_sessions", return_value=[]):
            with mock.patch("harness.agent.call_llm") as llm:
                llm.side_effect = [
                    (fake_message(None, [("bash", {"command": "echo gui-smoke"})]), USAGE),
                    (fake_message("smoke done"), USAGE),
                ]
                win.bridge.worker.done.connect(lambda: done.append(True))
                win.bridge.submit("hello gui")
                polls = []
                def check():
                    if done or len(polls) > 150:
                        self.app.quit()
                    else:
                        polls.append(1)
                        QTimer.singleShot(100, check)
                QTimer.singleShot(100, check)
                self.app.exec()
        texts = [w.toPlainText() for w in win.chat.findChildren(QTextBrowser)]
        texts += [w.text() for w in win.chat.findChildren(QLabel)]
        joined = "\n".join(texts)
        self.assertIn("hello gui", joined)
        self.assertIn("smoke done", joined)
        self.assertIn("gui-smoke", joined)

    def drive(self, win, script, text="go"):
        from PySide6.QtCore import QTimer

        done = []
        with mock.patch("harness.session.save"), mock.patch("harness.session.all_sessions", return_value=[]), mock.patch("harness.session.ensure_title"), mock.patch("harness.agent.reminder", return_value={"role": "user", "content": "test-injection"}):
            with mock.patch("harness.agent.call_llm") as llm:
                llm.side_effect = script
                win.bridge.worker.done.connect(lambda: done.append(True))
                win.bridge.submit(text)
                polls = []

                def check():
                    if done or len(polls) > 150:
                        self.app.quit()
                    else:
                        polls.append(1)
                        QTimer.singleShot(100, check)

                QTimer.singleShot(100, check)
                self.app.exec()

    def test_parallel_calls_pair_by_id(self):
        from harness.gui.chat_view import ToolLine

        win = self.make_window()
        self.drive(win, [
            (fake_message(None, [("bash", {"command": "echo first"}), ("bash", {"command": "echo second"})]), USAGE),
            (fake_message("both ran"), USAGE),
        ])
        lines = win.chat.findChildren(ToolLine)
        self.assertEqual(len(lines), 2)
        self.assertIn("first", lines[0].result.text())
        self.assertIn("second", lines[1].result.text())
        self.assertTrue(all(not line.result.isVisible() for line in lines))

    def test_empty_assistant_renders_no_bubble(self):
        win = self.make_window()
        self.drive(win, [(fake_message("   "), USAGE)])
        blanks = [w for w in win.chat.findChildren(QTextBrowser) if not w.toPlainText().strip()]
        self.assertEqual(blanks, [])

    def test_busy_submit_warns_instead_of_doubling(self):
        from PySide6.QtCore import QTimer

        win = self.make_window()
        win.bridge.running = True
        win.bridge.submit("nope")
        QTimer.singleShot(300, self.app.quit)
        self.app.exec()
        notes = [w.text() for w in win.chat.findChildren(QLabel) if "already running" in w.text()]
        self.assertTrue(notes)
        win.bridge.running = False

    def test_mode_notices_never_render(self):
        from harness.agent import PLAN_NOTICE

        win = self.make_window()
        win.chat.render_transcript([
            {"role": "user", "content": "Which mode are you in?"},
            {"role": "user", "content": PLAN_NOTICE},
            {"role": "assistant", "content": "plan mode"},
        ])
        shown = [w.text() for w in win.chat.findChildren(QLabel)]
        self.assertTrue(any("Which mode" in t for t in shown))
        self.assertFalse(any("system-reminder" in t for t in shown))

    def test_worker_applies_gui_mode(self):
        import threading
        from unittest import mock

        from harness.gui.bridge import Worker

        worker = Worker(threading.Event())
        with mock.patch("harness.agent.Runner.handle_input"):
            worker.run_input("hello", "plan")
            self.assertEqual(worker.runner.mode, "plan")
            self.assertIn("planning assistant", worker.runner.messages[0]["content"])
            worker.run_input("again", "build")
            self.assertEqual(worker.runner.mode, "build")

    def test_enter_sends_shift_enter_newlines(self):
        from unittest import mock

        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        win = self.make_window()
        sent = []
        win.bridge.submit = lambda text, mode="build": sent.append((text, mode))
        win.entry.setPlainText("hi")
        QTest.keyClick(win.entry, Qt.Key_Return)
        self.assertEqual(sent, [("hi", "build")])
        self.assertEqual(win.entry.toPlainText(), "")
        QTest.keyClick(win.entry, Qt.Key_Return, Qt.ShiftModifier)
        self.assertEqual(len(sent), 1)

    def test_permission_combo_sets_policy(self):
        from harness import permissions

        win = self.make_window()
        self.addCleanup(permissions.set_mode, "default")
        win.perm_combo.setCurrentText("Strict")
        self.assertEqual(permissions.MODE, "strict")
        win.perm_combo.setCurrentText("Autopilot")
        self.assertEqual(permissions.MODE, "autopilot")

    def test_sidebar_toggles_hide_panels(self):
        win = self.make_window()
        win.toggle_left.setChecked(False)
        self.assertFalse(win.side_panel.isVisible())
        win.toggle_left.setChecked(True)
        self.assertTrue(win.side_panel.isVisible())
        win.toggle_right.setChecked(False)
        self.assertFalse(win.right_panel.isVisible())

    def test_sendstop_toggles_busy(self):
        win = self.make_window()
        self.assertFalse(win.sendstop.busy)
        win.sendstop.set_busy(True)
        self.assertTrue(win.sendstop.timer.isActive())
        win.sendstop.set_busy(False)
        self.assertFalse(win.sendstop.timer.isActive())

    def test_title_fetch_runs_off_thread(self):
        import time
        from unittest import mock

        from PySide6.QtCore import QTimer

        from harness import session as session_mod

        win = self.make_window()
        win.current_id = "20260102-000000"
        with mock.patch.object(session_mod, "SESSION_DIR", session_mod.SESSION_DIR), \
             mock.patch("harness.providers.suggest_title", return_value="Fetched title") as suggest, \
             mock.patch.object(session_mod, "load", return_value=[{"role": "user", "content": "fix things"}]), \
             mock.patch.object(session_mod, "_titles", return_value={}), \
             mock.patch.object(session_mod, "set_title") as set_title:
            win.maybe_name_session()
            for _ in range(100):
                if set_title.called:
                    break
                time.sleep(0.05)
                self.app.processEvents()
            self.assertTrue(set_title.called)
            suggest.assert_called_once_with("fix things")


if __name__ == "__main__":
    unittest.main(verbosity=2)
