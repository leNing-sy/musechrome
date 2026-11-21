"""Mailbox UI regressions with isolated data, fake clipboard and no real browser."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QPushButton

from musechrome import app, auto_login, store


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
        self.critical = self.start_patch("musechrome.app.QMessageBox.critical")
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
        self.start_patch("musechrome.app.socket.create_connection",
                         side_effect=AssertionError("Unexpected network operation"))
        self.start_patch("musechrome.app.subprocess.Popen",
                         side_effect=AssertionError("Unexpected process launch"))
        self.csv = self.folder / "mail_accounts.csv"
        self.csv.write_text(
            "邮箱\nfirst@example.test\nsecond@example.test\n", encoding="utf-8-sig"
        )
        store.save_accounts([
            {"id": "test-edge", "name": "Edge fixture", "browser": "edge"},
            {"id": "test-chrome", "name": "Chrome fixture", "browser": "chrome"},
        ])
        self.window = app.MainWindow(store.load_config())
        self.addCleanup(self.close_window, self.window)
        self.window.show_settings_page()
        self.launch_account = self.start_patch(
            "musechrome.app.MainWindow._launch_account", return_value=True
        )

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

    def set_address(self, address):
        field = self.window.page_settings.edt_mail_address
        field.setText(address)
        field.setModified(True)
        field.editingFinished.emit()

    def set_target(self, target):
        field = self.window.page_settings.cmb_mail_target
        field.setCurrentIndex(field.findData(target))

    def warning_text(self):
        self.assertTrue(self.warning.called)
        return self.warning.call_args.args[-1]

    def test_csv_selection_saves_without_save_button_and_survives_restart(self):
        self.select_csv()
        self.window.page_settings.btn_back.click()
        self.assertEqual(self.window.stack.currentIndex(), 0)
        self.assertEqual(self.window.cfg["mail_accounts_csv"], str(self.csv))
        self.assertEqual(store.load_config()["mail_accounts_csv"], str(self.csv))
        restarted = app.MainWindow(store.load_config())
        self.addCleanup(self.close_window, restarted)
        restarted.show_settings_page()
        self.assertEqual(restarted.page_settings.edt_mail_csv.text(), str(self.csv))
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
        self.choose_file.return_value = (str(self.folder / "missing.csv"), "")
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

    def test_address_and_target_autosave_and_survive_restart(self):
        self.set_address("https://mail.example.test/inbox?email={email}")
        self.set_target("system")
        saved = store.load_config()
        self.assertEqual(saved["mail_address"], "https://mail.example.test/inbox?email={email}")
        self.assertEqual(saved["mail_open_target"], "system")
        restarted = app.MainWindow(saved)
        self.addCleanup(self.close_window, restarted)
        restarted.show_settings_page()
        self.assertEqual(restarted.page_settings.edt_mail_address.text(), saved["mail_address"])
        self.assertEqual(restarted.page_settings.cmb_mail_target.currentData(), "system")
        self.warning.assert_not_called()

    def test_invalid_address_does_not_replace_saved_address(self):
        self.set_address("https://mail.example.test/inbox")
        before = dict(self.window.cfg)
        self.set_address("javascript:alert(1)")
        self.assertEqual(self.window.cfg, before)
        self.assertEqual(store.load_config()["mail_address"], before["mail_address"])
        self.assertEqual(self.window.page_settings.edt_mail_address.text(), before["mail_address"])
        self.warning.assert_called_once()

    def test_address_save_failure_preserves_previous_value(self):
        self.set_address("https://mail.example.test/inbox")
        before = dict(self.window.cfg)
        with patch.object(store, "save_config", side_effect=PermissionError("fixture denied")):
            self.set_address("https://new.example.test/{email}")
        self.assertEqual(self.window.cfg, before)
        self.assertEqual(store.load_config()["mail_address"], before["mail_address"])
        self.assertEqual(self.window.page_settings.edt_mail_address.text(), before["mail_address"])
        self.warning.assert_called_once()

    def test_copy_only_preset_clears_saved_address(self):
        self.set_address("https://mail.example.test/{email}")
        self.window.page_settings._apply_mail_preset("")
        self.assertEqual(self.window.cfg["mail_address"], "")
        self.assertEqual(store.load_config()["mail_address"], "")
        self.assertEqual(self.window.page_settings.edt_mail_address.text(), "")

    def test_clearing_csv_removes_saved_selection(self):
        self.select_csv()
        self.window.page_settings.edt_mail_csv.clear()
        self.window.page_settings.edt_mail_csv.editingFinished.emit()
        self.assertEqual(self.window.cfg["mail_accounts_csv"], "")
        self.assertEqual(store.load_config()["mail_accounts_csv"], "")

    def test_save_settings_commits_mail_options_without_external_json(self):
        self.window.page_settings.edt_mail_csv.setText(str(self.csv))
        self.window.page_settings.edt_mail_address.setText("https://mail.example.test/{email}")
        self.window.page_settings.edt_home.setText("https://saved.example.test")
        self.window.page_settings.btn_save.click()
        saved = store.load_config()
        self.assertEqual(saved["mail_accounts_csv"], str(self.csv))
        self.assertEqual(saved["mail_address"], "https://mail.example.test/{email}")
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

    def test_reselecting_settings_page_preserves_pending_input(self):
        self.window.page_settings.edt_home.setText("https://pending.example.test")
        self.window.show_settings_page()
        self.assertEqual(self.window.page_settings.edt_home.text(), "https://pending.example.test")

    def test_both_browsers_have_same_mail_assist_entry(self):
        for account_id in ("test-edge", "test-chrome"):
            card = self.window._card_widgets[account_id]
            buttons = [button for button in card.findChildren(QPushButton)
                       if "邮箱" in button.text()]
            self.assertEqual(len(buttons), 1)
            self.assertFalse(buttons[0].isHidden())

    def test_new_configuration_copies_only_for_both_browsers(self):
        self.select_csv()
        self.assertEqual(self.window.cfg["mail_address"], "")
        for account_id in ("test-edge", "test-chrome"):
            with self.subTest(account_id=account_id):
                self.clipboard.reset_mock()
                self.window.start_mail_assist(account_id)
                self.clipboard.setText.assert_called_once_with("second@example.test")
        self.open_url.assert_not_called()
        self.launch_account.assert_not_called()
        self.warning.assert_not_called()

    def test_template_opens_encoded_selected_mailbox_in_system_browser(self):
        self.select_csv()
        self.set_address("https://mail.example.test/inbox?email={email}")
        self.set_target("system")
        self.window.start_mail_assist("test-chrome")
        self.clipboard.setText.assert_called_once_with("second@example.test")
        self.open_url.assert_called_once_with(
            app.QUrl("https://mail.example.test/inbox?email=second%40example.test")
        )
        self.launch_account.assert_not_called()

    def test_fixed_address_opens_in_target_account_browser(self):
        self.select_csv()
        self.set_address("https://mail.example.test/inbox")
        self.window.start_mail_assist("test-chrome")
        self.launch_account.assert_called_once_with(
            next(a for a in self.window.accounts if a["id"] == "test-chrome"),
            target_url="https://mail.example.test/inbox",
        )
        self.open_url.assert_not_called()
        self.clipboard.setText.assert_called_once_with("second@example.test")

    def test_csv_row_address_overrides_global_address(self):
        self.csv.write_text(
            "邮箱,收件地址\nfirst@example.test,\n"
            "second@example.test,https://second.example.test/{email}\n", encoding="utf-8-sig"
        )
        self.select_csv()
        self.set_address("https://global.example.test/{email}")
        self.set_target("system")
        self.window.start_mail_assist("test-edge")
        self.open_url.assert_called_once_with(
            app.QUrl("https://second.example.test/second%40example.test")
        )

    def test_blank_row_address_uses_global_address(self):
        self.csv.write_text(
            "邮箱,收件地址\nsecond@example.test,\n", encoding="utf-8-sig"
        )
        self.select_csv()
        self.set_address("https://global.example.test/{email}")
        self.set_target("system")
        self.window.start_mail_assist("test-edge")
        self.open_url.assert_called_once_with(
            app.QUrl("https://global.example.test/second%40example.test")
        )

    def test_invalid_selected_address_does_not_copy_or_open_anything(self):
        self.csv.write_text(
            "邮箱,收件地址\nsecond@example.test,javascript:alert(1)\n", encoding="utf-8-sig"
        )
        self.window.cfg["mail_accounts_csv"] = str(self.csv)
        self.window.start_mail_assist("test-edge")
        self.clipboard.setText.assert_not_called()
        self.open_url.assert_not_called()
        self.launch_account.assert_not_called()
        self.warning.assert_called_once()

    def test_legacy_custom_config_is_usable_without_editing_external_file(self):
        config_file = self.folder / "mail-service.json"
        original = json.dumps({"mail": {"apiBase": "https://legacy.example.test"}})
        config_file.write_text(original, encoding="utf-8")
        self.select_csv()
        self.window.cfg.update(mail_address=None, mail_config_path=str(config_file),
                               mail_open_target="system")
        self.window.start_mail_assist("test-edge")
        self.open_url.assert_called_once_with(
            app.QUrl("https://legacy.example.test/second%40example.test")
        )
        self.assertEqual(config_file.read_text(encoding="utf-8"), original)

    def test_missing_legacy_config_does_not_fall_back_to_other_service(self):
        self.select_csv()
        self.window.cfg.update(mail_address=None, mail_config_path=str(self.folder / "missing.json"))
        self.window.start_mail_assist("test-edge")
        self.clipboard.setText.assert_not_called()
        self.open_url.assert_not_called()
        self.launch_account.assert_not_called()
        self.warning.assert_called_once()

    def test_unreadable_legacy_config_is_not_silently_replaced_by_empty_field(self):
        self.select_csv()
        self.window.cfg.update(mail_address=None, mail_config_path=str(self.folder / "missing.json"))
        store.save_config(self.window.cfg)
        previous = dict(self.window.cfg)
        self.window.page_settings.load(self.window.cfg)
        self.assertEqual(self.window.page_settings.edt_mail_address.text(), "")
        self.window.page_settings.edt_mail_address.editingFinished.emit()
        self.window.page_settings.edt_home.setText("https://pending.example.test")
        self.window.page_settings.btn_save.click()
        self.assertEqual(self.window.cfg, previous)
        self.assertEqual(store.load_config(), previous)
        self.warning.assert_called_once()
        self.window.page_settings._apply_mail_preset("")
        self.assertEqual(self.window.cfg["mail_address"], "")
        self.assertEqual(self.window.cfg["mail_config_path"], "")

    def test_target_save_failure_restores_selection_without_recursion(self):
        store.save_config(self.window.cfg)
        previous = dict(self.window.cfg)
        with patch.object(store, "save_config", side_effect=PermissionError("fixture denied")) as save:
            self.set_target("system")
            save.assert_called_once()
        self.assertEqual(self.window.cfg, previous)
        self.assertEqual(store.load_config(), previous)
        self.assertEqual(self.window.page_settings.cmb_mail_target.currentData(), "account")
        self.warning.assert_called_once()

    def test_invalid_saved_address_is_preserved_when_saving_unrelated_settings(self):
        self.select_csv()
        for invalid in ("javascript:fixture()", {"address": "invalid"}, ["invalid"]):
            with self.subTest(invalid=invalid):
                self.warning.reset_mock()
                self.window.cfg.update(mail_address=invalid, mail_config_path="")
                store.save_config(self.window.cfg)
                previous = dict(self.window.cfg)
                config_path = store.data_dir() / "config.json"
                original_bytes = config_path.read_bytes()
                with patch.object(auto_login, "load_mail_api_base",
                                  return_value="https://unexpected.example.test") as legacy:
                    self.window.page_settings.load(self.window.cfg)
                    self.assertEqual(self.window.page_settings.edt_mail_address.text(), "")
                    self.window.page_settings.edt_mail_address.editingFinished.emit()
                    self.window.page_settings.edt_home.setText("https://pending.example.test")
                    with patch.object(store, "save_config", wraps=store.save_config) as save:
                        self.window.page_settings.btn_save.click()
                        save.assert_not_called()
                    legacy.assert_not_called()
                self.warning.assert_called_once()
                self.assertEqual(self.window.cfg, previous)
                self.assertEqual(store.load_config(), previous)
                self.assertEqual(config_path.read_bytes(), original_bytes)
                self.assertEqual(self.window.cfg["mail_address"], invalid)
                self.open_url.assert_not_called()
                self.launch_account.assert_not_called()

    def test_explicit_empty_address_ignores_missing_legacy_config(self):
        self.select_csv()
        self.window.cfg.update(mail_address="", mail_config_path=str(self.folder / "missing.json"))
        self.window.start_mail_assist("test-edge")
        self.clipboard.setText.assert_called_once_with("second@example.test")
        self.open_url.assert_not_called()
        self.launch_account.assert_not_called()
        self.warning.assert_not_called()

    def test_cancelled_selection_does_not_touch_clipboard_or_browser(self):
        self.select_csv()
        self.choose_email.return_value = ("first@example.test", False)
        self.window.start_mail_assist("test-edge")
        self.clipboard.setText.assert_not_called()
        self.open_url.assert_not_called()
        self.launch_account.assert_not_called()
        self.information.assert_not_called()

    def test_system_browser_failure_keeps_copied_email_and_reports_failure(self):
        self.select_csv()
        self.set_address("https://mail.example.test")
        self.set_target("system")
        self.open_url.return_value = False
        self.window.start_mail_assist("test-edge")
        self.clipboard.setText.assert_called_once_with("second@example.test")
        self.assertIn("邮箱已复制", self.warning_text())
        self.information.assert_not_called()

    def test_account_browser_failure_does_not_claim_success(self):
        self.select_csv()
        self.set_address("https://mail.example.test")
        self.launch_account.return_value = False
        self.window.start_mail_assist("test-edge")
        self.clipboard.setText.assert_called_once_with("second@example.test")
        self.information.assert_not_called()
        self.open_url.assert_not_called()

    def test_invalid_account_id_cannot_start_mail_assist(self):
        self.select_csv()
        self.window.start_mail_assist("missing-account")
        self.choose_email.assert_not_called()
        self.clipboard.setText.assert_not_called()
        self.open_url.assert_not_called()
        self.launch_account.assert_not_called()


if __name__ == "__main__":
    unittest.main()
