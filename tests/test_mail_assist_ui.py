"""Mailbox settings regressions using real Qt widgets and isolated local data.

Run with the existing PyQt6 dependency: python tests/test_mail_assist_ui.py.
Missing PyQt6 is an error, so CI cannot silently skip the UI regression suite.
No real mailbox, browser, clipboard, network, or application data is used.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QWidget

from musechrome import app, auto_login, store


class MailSettingsWindow(QMainWindow):
    """Exercise production methods without MainWindow's background services."""

    save_mail_path = app.MainWindow.save_mail_path
    save_settings = app.MainWindow.save_settings
    start_mail_assist = app.MainWindow.start_mail_assist
    show_settings_page = app.MainWindow.show_settings_page

    def __init__(self):
        super().__init__()
        self.cfg = store.load_config()
        self.accounts = [{"id": "test-edge", "name": "Fixture", "browser": "edge"}]
        self.page_settings = app.SettingsPage()
        self.page_settings.mail_path_selected.connect(self.save_mail_path)
        self.page_settings.btn_save.clicked.connect(self.save_settings)
        self.page_settings.btn_back.clicked.connect(lambda: self._show_page(0))
        self.stack = QStackedWidget(self)
        for _ in range(3):
            self.stack.addWidget(QWidget())
        self.stack.addWidget(self.page_settings)
        self.setCentralWidget(self.stack)
        self.show_settings_page()

    def _show_page(self, index):
        self.stack.setCurrentIndex(index)

    def _refresh_proxy_label(self):
        pass


class MailAssistUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])
        cls.qt_app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="musechrome-mail-ui-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.start_patch("musechrome.store.base_dir", return_value=self.folder)
        self.warning = self.start_patch("musechrome.app.QMessageBox.warning")
        self.information = self.start_patch("musechrome.app.QMessageBox.information")
        self.clipboard = MagicMock()
        self.start_patch("musechrome.app.QApplication.clipboard", return_value=self.clipboard)
        self.open_url = self.start_patch("musechrome.app.QDesktopServices.openUrl", return_value=True)
        self.choose_email = self.start_patch(
            "musechrome.app.QInputDialog.getItem", return_value=("second@example.test", True)
        )
        self.choose_file = self.start_patch(
            "musechrome.app.QFileDialog.getOpenFileName", return_value=("", "")
        )
        for target in (
            "musechrome.app.socket.create_connection",
            "musechrome.app.subprocess.Popen",
        ):
            self.start_patch(target, side_effect=AssertionError("Unexpected external operation"))
        self.csv = self.folder / "mail_accounts.csv"
        self.csv.write_text(
            "邮箱\nfirst@example.test\nsecond@example.test\n", encoding="utf-8-sig"
        )
        self.mail_config = self.folder / "mail_service.json"
        self.mail_config.write_text(
            json.dumps({"mail": {"apiBase": "https://mail.example.test"}}), encoding="utf-8"
        )
        self.window = MailSettingsWindow()
        self.addCleanup(self.close_window, self.window)

    def start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def close_window(self, window):
        window.close()
        window.deleteLater()
        self.qt_app.processEvents()

    def select_csv(self):
        self.choose_file.return_value = (str(self.csv), "CSV files (*.csv)")
        self.window.page_settings._browse_mail_csv()

    def select_mail_config(self):
        self.choose_file.return_value = (str(self.mail_config), "JSON files (*.json)")
        self.window.page_settings._browse_mail_config()

    def warning_text(self):
        self.assertTrue(self.warning.called)
        return self.warning.call_args.args[-1]

    def test_csv_selection_saves_without_save_button_and_survives_restart(self):
        self.select_csv()
        self.window.page_settings.btn_back.click()
        self.assertEqual(self.window.stack.currentIndex(), 0)
        self.assertEqual(self.window.cfg["mail_accounts_csv"], str(self.csv))
        self.assertEqual(store.load_config()["mail_accounts_csv"], str(self.csv))
        restarted = MailSettingsWindow()
        self.addCleanup(self.close_window, restarted)
        self.assertEqual(restarted.page_settings.edt_mail_csv.text(), str(self.csv))
        self.warning.assert_not_called()

    def test_real_main_window_wiring_saves_selection_and_uses_it(self):
        store.save_accounts(self.window.accounts)
        window = app.MainWindow(store.load_config())
        self.addCleanup(self.close_window, window)
        window.show_settings_page()
        self.choose_file.return_value = (str(self.csv), "CSV files (*.csv)")
        window.page_settings._browse_mail_csv()
        window.page_settings.btn_back.click()
        self.assertEqual(window.stack.currentIndex(), 0)
        self.assertEqual(store.load_config()["mail_accounts_csv"], str(self.csv))
        window.start_mail_assist("test-edge")
        self.clipboard.setText.assert_called_once_with("second@example.test")
        self.open_url.assert_called_once()
        self.warning.assert_not_called()

    def test_manual_csv_edit_saves_on_editing_finished(self):
        field = self.window.page_settings.edt_mail_csv
        field.setText(f"  {self.csv}  ")
        field.editingFinished.emit()
        self.assertEqual(self.window.cfg["mail_accounts_csv"], str(self.csv))
        self.assertEqual(store.load_config()["mail_accounts_csv"], str(self.csv))
        self.assertEqual(field.text(), str(self.csv))

    def test_path_autosave_does_not_save_other_pending_settings(self):
        original_home = self.window.cfg["home_url"]
        self.window.page_settings.edt_home.setText("https://pending.example.test")
        self.select_csv()
        self.assertEqual(self.window.cfg["home_url"], original_home)
        self.assertEqual(store.load_config()["home_url"], original_home)
        self.assertEqual(self.window.page_settings.edt_home.text(), "https://pending.example.test")

    def test_cancelled_picker_and_unchanged_path_do_not_write(self):
        self.select_csv()
        self.choose_file.return_value = ("", "")
        with patch.object(store, "save_config", wraps=store.save_config) as save:
            self.window.page_settings._browse_mail_csv()
            self.window.page_settings.edt_mail_csv.editingFinished.emit()
            save.assert_not_called()
        self.assertEqual(self.window.cfg["mail_accounts_csv"], str(self.csv))
        self.assertEqual(self.window.page_settings.edt_mail_csv.text(), str(self.csv))

    def test_invalid_csv_selection_preserves_previous_saved_path(self):
        self.select_csv()
        invalid = self.folder / "invalid.csv"
        invalid.write_text("wrong_header\nfixture@example.test\n", encoding="utf-8")
        self.choose_file.return_value = (str(invalid), "")
        with patch.object(store, "save_config", wraps=store.save_config) as save:
            self.window.page_settings._browse_mail_csv()
            save.assert_not_called()
        self.assertEqual(self.window.cfg["mail_accounts_csv"], str(self.csv))
        self.assertEqual(self.window.page_settings.edt_mail_csv.text(), str(self.csv))
        self.assertEqual(store.load_config()["mail_accounts_csv"], str(self.csv))
        self.assertIn("邮箱", self.warning_text())

    def test_unreadable_csv_selection_preserves_previous_saved_path(self):
        self.select_csv()
        missing = str(self.folder / "missing.csv")
        self.choose_file.return_value = (missing, "")
        self.window.page_settings._browse_mail_csv()
        self.assertEqual(self.window.cfg["mail_accounts_csv"], str(self.csv))
        self.assertEqual(self.window.page_settings.edt_mail_csv.text(), str(self.csv))
        self.assertEqual(store.load_config()["mail_accounts_csv"], str(self.csv))
        self.warning.assert_called_once()

    def test_path_save_failure_restores_field_and_preserves_config(self):
        self.select_csv()
        another = self.folder / "another.csv"
        another.write_text("邮箱\nnew@example.test\n", encoding="utf-8")
        previous = dict(self.window.cfg)
        self.choose_file.return_value = (str(another), "")
        with patch.object(store, "save_config", side_effect=PermissionError("fixture denied")):
            self.window.page_settings._browse_mail_csv()
        self.assertEqual(self.window.cfg, previous)
        self.assertEqual(store.load_config()["mail_accounts_csv"], str(self.csv))
        self.assertEqual(self.window.page_settings.edt_mail_csv.text(), str(self.csv))
        self.warning.assert_called_once()

    def test_optional_json_selection_and_clearing_are_saved(self):
        self.select_mail_config()
        self.assertEqual(self.window.cfg["mail_config_path"], str(self.mail_config))
        self.assertEqual(store.load_config()["mail_config_path"], str(self.mail_config))
        field = self.window.page_settings.edt_mail_config
        field.setText("  ")
        field.editingFinished.emit()
        self.assertEqual(self.window.cfg["mail_config_path"], "")
        self.assertEqual(store.load_config()["mail_config_path"], "")
        self.assertEqual(auto_login.load_mail_api_base(""), auto_login.DEFAULT_MAIL_API_BASE)
        self.warning.assert_not_called()

    def test_invalid_optional_json_is_not_saved(self):
        self.select_mail_config()
        invalid = self.folder / "invalid.json"
        invalid.write_text("{}", encoding="utf-8")
        self.choose_file.return_value = (str(invalid), "")
        self.window.page_settings._browse_mail_config()
        self.assertEqual(self.window.cfg["mail_config_path"], str(self.mail_config))
        self.assertEqual(self.window.page_settings.edt_mail_config.text(), str(self.mail_config))
        self.assertEqual(store.load_config()["mail_config_path"], str(self.mail_config))
        self.warning.assert_called_once()

    def test_clearing_csv_removes_saved_selection(self):
        self.select_csv()
        self.window.page_settings.edt_mail_csv.clear()
        self.window.page_settings.edt_mail_csv.editingFinished.emit()
        self.assertEqual(self.window.cfg["mail_accounts_csv"], "")
        self.assertEqual(store.load_config()["mail_accounts_csv"], "")

    def test_save_settings_commits_csv_without_external_json(self):
        self.window.page_settings.edt_mail_csv.setText(str(self.csv))
        self.window.page_settings.edt_home.setText("https://saved.example.test")
        self.window.page_settings.btn_save.click()
        saved = store.load_config()
        self.assertEqual(saved["mail_accounts_csv"], str(self.csv))
        self.assertEqual(saved["mail_config_path"], "")
        self.assertEqual(saved["home_url"], "https://saved.example.test")
        self.assertEqual(self.window.cfg, saved)
        self.warning.assert_not_called()

    def test_save_settings_validation_failure_keeps_previous_config(self):
        store.save_config(self.window.cfg)
        previous = dict(self.window.cfg)
        self.window.page_settings.edt_mail_csv.setText(str(self.folder / "missing.csv"))
        self.window.page_settings.edt_home.setText("https://pending.example.test")
        self.window.page_settings.btn_save.click()
        self.assertEqual(self.window.cfg, previous)
        self.assertEqual(store.load_config(), previous)
        self.assertEqual(self.window.page_settings.edt_home.text(), "https://pending.example.test")
        self.warning.assert_called_once()

    def test_save_settings_disk_failure_keeps_old_config_and_pending_input(self):
        store.save_config(self.window.cfg)
        previous = dict(self.window.cfg)
        self.window.page_settings.edt_mail_csv.setText(str(self.csv))
        self.window.page_settings.edt_home.setText("https://pending.example.test")
        with patch.object(store, "save_config", side_effect=PermissionError("fixture denied")):
            self.window.page_settings.btn_save.click()
        self.assertEqual(self.window.cfg, previous)
        self.assertEqual(store.load_config(), previous)
        self.assertEqual(self.window.page_settings.edt_home.text(), "https://pending.example.test")
        self.assertEqual(self.window.page_settings.edt_mail_csv.text(), str(self.csv))
        self.warning.assert_called_once()

    def test_reselecting_current_settings_page_preserves_pending_input(self):
        self.window.page_settings.edt_home.setText("https://pending.example.test")
        self.window.show_settings_page()
        self.assertEqual(self.window.page_settings.edt_home.text(), "https://pending.example.test")

    def test_csv_only_assist_copies_selected_email_and_opens_default_service(self):
        self.select_csv()
        self.window.start_mail_assist("test-edge")
        self.choose_email.assert_called_once()
        self.assertEqual(
            self.choose_email.call_args.args[3], ["first@example.test", "second@example.test"]
        )
        self.clipboard.setText.assert_called_once_with("second@example.test")
        self.open_url.assert_called_once()
        self.assertEqual(
            self.open_url.call_args.args[0],
            app.QUrl(f"{auto_login.DEFAULT_MAIL_API_BASE}/second%40example.test"),
        )
        self.warning.assert_not_called()
        self.information.assert_called_once()

    def test_assist_uses_selected_optional_service(self):
        self.select_csv()
        self.select_mail_config()
        self.window.start_mail_assist("test-edge")
        self.assertEqual(
            self.open_url.call_args.args[0],
            app.QUrl("https://mail.example.test/second%40example.test"),
        )

    def test_cancelled_email_selection_does_not_touch_clipboard_or_browser(self):
        self.select_csv()
        self.choose_email.return_value = ("first@example.test", False)
        self.window.start_mail_assist("test-edge")
        self.clipboard.setText.assert_not_called()
        self.open_url.assert_not_called()
        self.information.assert_not_called()
        self.warning.assert_not_called()

    def test_browser_open_failure_keeps_copied_email_and_reports_failure(self):
        self.select_csv()
        self.open_url.return_value = False
        self.window.start_mail_assist("test-edge")
        self.clipboard.setText.assert_called_once_with("second@example.test")
        self.assertIn("邮箱已复制", self.warning_text())
        self.information.assert_not_called()

    def test_missing_optional_json_reports_config_error_without_csv_advice(self):
        self.select_csv()
        self.window.cfg["mail_config_path"] = str(self.folder / "missing.json")
        self.window.start_mail_assist("test-edge")
        self.assertNotIn("CSV", self.warning_text())
        self.choose_email.assert_not_called()
        self.clipboard.setText.assert_not_called()
        self.open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
