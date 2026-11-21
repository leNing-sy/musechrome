"""Chrome/Edge parity and process ownership without launching real browsers."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QPushButton

from musechrome import app, store


class BrowserParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])
        cls.qt_app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="musechrome-browser-ui-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.start_patch("musechrome.store.base_dir", return_value=self.folder)
        self.warning = self.start_patch("musechrome.app.QMessageBox.warning")
        self.critical = self.start_patch("musechrome.app.QMessageBox.critical")
        self.information = self.start_patch("musechrome.app.QMessageBox.information")
        self.start_patch("musechrome.app.socket.create_connection",
                         side_effect=AssertionError("Unexpected network operation"))
        self.start_patch("musechrome.app.QDesktopServices.openUrl",
                         side_effect=AssertionError("Unexpected system browser"))
        self.popen = self.start_patch("musechrome.app.subprocess.Popen")
        self.find_browser = self.start_patch("musechrome.store.find_browser_exe",
                                            return_value=str(self.folder / "fixture.exe"))
        store.save_accounts([
            {"id": "fixture-chrome", "name": "Chrome fixture", "browser": "chrome"},
            {"id": "fixture-edge", "name": "Edge fixture", "browser": "edge"},
        ])
        self.window = app.MainWindow(store.load_config())
        self.addCleanup(self.close_window)

    def start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def close_window(self):
        self.window.close()
        self.window.deleteLater()
        self.qt_app.processEvents()

    def account(self, browser="chrome"):
        return next(a for a in self.window.accounts if a["id"] == f"fixture-{browser}")

    @staticmethod
    def process():
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 999999
        return proc

    def mark_running(self, acc, proc):
        self.window._procs[acc["id"]] = proc
        self.window._launched.add(acc["id"])
        self.window._launched_at[acc["id"]] = 100.0

    def switch_controls(self, acc):
        open_button = QPushButton()
        assist_button = QPushButton()
        chrome = QPushButton()
        edge = QPushButton()
        chrome.setCheckable(True)
        edge.setCheckable(True)
        chrome.setChecked(acc["browser"] == "chrome")
        edge.setChecked(acc["browser"] == "edge")
        return open_button, assist_button, chrome, edge

    def test_target_mail_url_uses_selected_browser_profile_and_account_proxy(self):
        for browser in ("chrome", "edge"):
            with self.subTest(browser=browser):
                acc = self.account(browser)
                acc["proxy"] = {"type": "socks5", "host": "127.0.0.1", "port": 7891}
                proc = self.process()
                self.popen.return_value = proc
                url = "https://mail.example.test/inbox?email=chosen%40example.test"
                self.assertTrue(self.window._launch_account(acc, target_url=url))
                self.find_browser.assert_called_with(self.window.cfg, browser)
                args = self.popen.call_args.args[0]
                self.assertEqual(args[0], str(self.folder / "fixture.exe"))
                expected_profile = store.profile_dir_for_browser(acc["id"], browser)
                self.assertIn(f"--user-data-dir={expected_profile}", args)
                self.assertIn("--proxy-server=socks5://127.0.0.1:7891", args)
                self.assertEqual(args[-1], url)
                self.assertEqual(args.count(url), 1)
                self.assertNotIn(self.window.cfg["home_url"], args)
                self.assertIn("--disable-signin", args)
                self.assertIn("--disable-sync", args)
                self.assertIs(self.window._procs[acc["id"]], proc)

    def test_regular_open_still_uses_home_url(self):
        acc = self.account()
        self.window._selected_id = acc["id"]
        self.popen.return_value = self.process()
        self.window.open_account()
        args = self.popen.call_args.args[0]
        self.assertEqual(args[-1], self.window.cfg["home_url"])
        self.assertIn(acc["id"], self.window._launched)

    def test_repeated_open_keeps_original_live_process(self):
        acc = self.account()
        original = self.process()
        self.mark_running(acc, original)
        transient = self.process()
        transient.poll.return_value = 0
        self.popen.return_value = transient
        self.assertTrue(self.window._launch_account(acc, target_url="https://mail.example.test"))
        self.assertIs(self.window._procs[acc["id"]], original)
        self.assertEqual(self.window._launched_at[acc["id"]], 100.0)

    def test_failed_launch_does_not_mark_account_running(self):
        acc = self.account()
        self.popen.side_effect = OSError("fixture launch denied")
        self.assertFalse(self.window._launch_account(acc))
        self.assertNotIn(acc["id"], self.window._procs)
        self.assertNotIn(acc["id"], self.window._launched)
        self.critical.assert_called_once()

    def test_running_account_cannot_switch_browser_or_change_saved_selection(self):
        acc = self.account()
        proc = self.process()
        self.mark_running(acc, proc)
        buttons = self.switch_controls(acc)
        # Simulate Qt toggling the clicked segment before the slot is invoked.
        buttons[2].setChecked(False)
        buttons[3].setChecked(True)
        with patch.object(store, "save_accounts", wraps=store.save_accounts) as save:
            self.window._set_account_browser(acc["id"], "edge", *buttons)
            save.assert_not_called()
        self.assertEqual(acc["browser"], "chrome")
        self.assertTrue(buttons[2].isChecked())
        self.assertFalse(buttons[3].isChecked())
        self.assertIs(self.window._procs[acc["id"]], proc)
        self.assertEqual(store.load_accounts()[0]["browser"], "chrome")

    def test_stopped_account_can_switch_browser_without_hiding_mail_assist(self):
        acc = self.account()
        buttons = self.switch_controls(acc)
        self.window._set_account_browser(acc["id"], "edge", *buttons)
        self.assertEqual(acc["browser"], "edge")
        self.assertEqual(store.load_accounts()[0]["browser"], "edge")
        self.assertEqual(buttons[0].text(), "打开 Edge")
        self.assertTrue(buttons[3].isChecked())
        self.assertFalse(buttons[2].isChecked())
        self.assertFalse(buttons[1].isHidden())
        self.window._set_account_browser(acc["id"], "chrome", *buttons)
        self.assertEqual(acc["browser"], "chrome")
        self.assertFalse(buttons[1].isHidden())

    def test_running_card_disables_browser_segments(self):
        acc = self.account()
        self.mark_running(acc, self.process())
        self.window._rebuild_cards()
        card = self.window._card_widgets[acc["id"]]
        segments = [button for button in card.findChildren(QPushButton)
                    if button.objectName() == "segBtn"]
        self.assertEqual(len(segments), 2)
        self.assertTrue(all(not button.isEnabled() for button in segments))

    def test_failed_termination_retains_process_and_running_state(self):
        acc = self.account()
        proc = self.process()
        proc.terminate.side_effect = PermissionError("fixture denied")
        self.mark_running(acc, proc)
        with patch.object(app, "_close_windows_for_pid", return_value=0), \
             patch.object(app.time, "sleep"):
            self.window.close_account_browser(acc["id"])
        self.assertIs(self.window._procs[acc["id"]], proc)
        self.assertIn(acc["id"], self.window._launched)
        self.assertEqual(self.window._launched_at[acc["id"]], 100.0)
        self.assertTrue(self.warning.called or self.critical.called)

    def test_successful_close_clears_only_target_process(self):
        target = self.account("chrome")
        other = self.account("edge")
        target_proc = self.process()
        target_proc.poll.side_effect = [None, 0, 0, 0]
        other_proc = self.process()
        self.mark_running(target, target_proc)
        self.mark_running(other, other_proc)
        with patch.object(app, "_close_windows_for_pid", return_value=1), \
             patch.object(app.time, "sleep"):
            self.window.close_account_browser(target["id"])
        self.assertNotIn(target["id"], self.window._procs)
        self.assertNotIn(target["id"], self.window._launched)
        self.assertIs(self.window._procs[other["id"]], other_proc)
        self.assertIn(other["id"], self.window._launched)
        self.assertNotIn(target["id"], self.window._launched_at)

    def test_termination_timeout_keeps_process_tracked(self):
        acc = self.account()
        proc = self.process()
        proc.wait.side_effect = app.subprocess.TimeoutExpired("fixture", 2)
        self.mark_running(acc, proc)
        with patch.object(app, "_close_windows_for_pid", return_value=0), \
             patch.object(app.time, "sleep"):
            self.window.close_account_browser(acc["id"])
        proc.terminate.assert_called_once()
        self.assertIs(self.window._procs[acc["id"]], proc)
        self.assertIn(acc["id"], self.window._launched)
        self.warning.assert_called_once()

    def test_polling_exited_process_releases_switch_lock(self):
        acc = self.account()
        proc = self.process()
        proc.poll.return_value = 0
        self.mark_running(acc, proc)
        self.window._poll_procs()
        self.assertNotIn(acc["id"], self.window._procs)
        self.assertNotIn(acc["id"], self.window._launched)
        card = self.window._card_widgets[acc["id"]]
        segments = [button for button in card.findChildren(QPushButton)
                    if button.objectName() == "segBtn"]
        self.assertTrue(all(button.isEnabled() for button in segments))


if __name__ == "__main__":
    unittest.main()
