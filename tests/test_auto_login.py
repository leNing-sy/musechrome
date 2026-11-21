"""邮箱辅助登录纯逻辑测试，不连接真实邮箱服务。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musechrome import auto_login


class MailAssistTests(unittest.TestCase):
    def test_csv_optional_address_column_preserves_first_entry_and_ignores_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / "mail.csv"
            csv_path.write_text(
                "邮箱,收件地址,密码\n"
                "first@example.test,https://mail.example.test/{email},ignored-secret\n"
                "FIRST@example.test,https://other.example.test,ignored-secret\n"
                "second@example.test,,ignored-secret\n"
                "third@example.test,https://outlook.live.com/mail/,ignored-secret\n",
                encoding="utf-8-sig",
            )
            entries = auto_login.load_mail_entries(csv_path)
            self.assertEqual(entries, [
                {"email": "first@example.test", "address": "https://mail.example.test/{email}"},
                {"email": "second@example.test", "address": ""},
                {"email": "third@example.test", "address": "https://outlook.live.com/mail/"},
            ])
            self.assertNotIn("ignored-secret", repr(entries))
            self.assertEqual(auto_login.load_mailboxes(csv_path), [item["email"] for item in entries])

    def test_invalid_csv_address_reports_line_without_echoing_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / "mail.csv"
            csv_path.write_text(
                "邮箱,收件地址\nfirst@example.test,https://user:fixture-secret@example.test\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(auto_login.MailAssistError, "第 2 行") as raised:
                auto_login.load_mail_entries(csv_path)
            self.assertNotIn("fixture-secret", str(raised.exception))
            self.assertNotIn("first@example.test", str(raised.exception))

    def test_address_modes_and_template_encoding(self):
        email = "a+b&x#y@example.test"
        self.assertEqual(auto_login.resolve_mail_url("  ", email), "")
        self.assertEqual(auto_login.resolve_mail_url(" https://outlook.live.com/mail/ ", email),
                         "https://outlook.live.com/mail/")
        self.assertEqual(
            auto_login.resolve_mail_url("https://mail.example.test/{email}?account={email}#inbox", email),
            "https://mail.example.test/a%2Bb%26x%23y%40example.test?account=a%2Bb%26x%23y%40example.test#inbox",
        )
        self.assertEqual(auto_login.resolve_mail_url("http://[::1]:8080/#/{email}", "a@example.test"),
                         "http://[::1]:8080/#/a%40example.test")

    def test_invalid_mail_addresses_are_rejected_without_echoing_them(self):
        invalid_addresses = [
            None, 42, "javascript:alert(1)", "file:///C:/fixture", "https:///missing",
            "https://{email}/inbox", "https://host.example:{email}/", "https://[bad",
            "https://mail.example.test:bad", "https://mail.example.test:0",
            "https://mail.example.test:65536", "https://user:fixture-secret@example.test/",
            "https://mail.example.test/{other}", "https://mail.example.test/{email",
            "https://mail.example.test/{{email}}", "https://mail.example.test/has space",
            "https://mail.example.test\\path", "https://mail.example.test/\nnext",
            "\thttps://mail.example.test", "https://mail.example.test/\x00", "https://mail.example.test/\x7f",
        ]
        for address in invalid_addresses:
            with self.subTest(address=address), self.assertRaises(auto_login.MailAssistError) as raised:
                auto_login.validate_mail_address(address)
            self.assertNotIn("fixture-secret", str(raised.exception))

    def test_explicit_address_including_blank_never_reads_legacy_config(self):
        with patch.object(auto_login, "load_mail_api_base", side_effect=AssertionError("legacy config read")):
            self.assertEqual(auto_login.configured_mail_address({}), "")
            for address in ("", "https://mail.example.test/{email}"):
                self.assertEqual(auto_login.configured_mail_address(
                    {"mail_address": address, "mail_config_path": "missing.json"}), address)

    def test_legacy_configured_address_retains_default_and_custom_service(self):
        self.assertEqual(auto_login.configured_mail_address({"mail_address": None}),
                         auto_login.DEFAULT_MAIL_API_BASE + "/{email}")
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder) / "mail.json"
            config_path.write_text(json.dumps({"mail": {"apiBase": "https://legacy.example.test/base"}}),
                                   encoding="utf-8")
            self.assertEqual(auto_login.configured_mail_address(
                {"mail_address": None, "mail_config_path": str(config_path)}),
                "https://legacy.example.test/base/{email}")
            with self.assertRaises(auto_login.MailAssistError):
                auto_login.configured_mail_address(
                    {"mail_address": None, "mail_config_path": str(Path(folder) / "missing.json")})

    def test_email_only_csv_is_sufficient_with_default_service(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / "未售邮箱.csv"
            csv_path.write_text('"邮箱"\n"first@example.test"\n', encoding="utf-8-sig")
            emails = auto_login.load_mailboxes(csv_path)
            self.assertEqual(emails, ["first@example.test"])
            self.assertEqual(
                auto_login.mailbox_url(auto_login.load_mail_api_base(""), emails[0]),
                "https://mail.xiongmaodianjing.top/first%40example.test",
            )

    def test_csv_errors_distinguish_missing_header_empty_and_bad_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / "mail_accounts.csv"
            for data, message in [
                (b"email\nfirst@example.test\n", "缺少"),
                ("邮箱\n\n无效记录\n".encode("utf-8"), "没有可用邮箱"),
                (b"\xff\xff", "编码和格式"),
            ]:
                with self.subTest(message=message):
                    csv_path.write_bytes(data)
                    with self.assertRaisesRegex(auto_login.MailAssistError, message):
                        auto_login.load_mailboxes(csv_path)

    def test_missing_csv_has_csv_specific_error(self):
        with self.assertRaisesRegex(auto_login.MailAssistError, "邮箱 CSV"):
            auto_login.load_mailboxes("")

    def test_blank_optional_config_uses_default(self):
        self.assertEqual(auto_login.load_mail_api_base("   "), auto_login.DEFAULT_MAIL_API_BASE)

    def test_explicit_missing_config_does_not_silently_change_service(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(auto_login.MailAssistError, "自定义取件配置"):
                auto_login.load_mail_api_base(Path(folder) / "missing.json")

    def test_invalid_service_address_is_reported_as_config_error(self):
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder) / "config.json"
            for base in ["http://mail.example.test", "https://[bad", "https://mail.example.test:bad",
                         "https://mail.example.test:99999", "https://mail.example.test?token=x",
                         "https://mail.example.test/#fragment", "https://user:secret@mail.example.test",
                         "https://mail.example.test/has space", "https://mail.example.test\\path"]:
                with self.subTest(base=base):
                    config_path.write_text(json.dumps({"mail": {"apiBase": base}}), encoding="utf-8")
                    with self.assertRaisesRegex(auto_login.MailAssistError, "mail.apiBase"):
                        auto_login.load_mail_api_base(config_path)

    def test_mailbox_reader_uses_only_email_column_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            csv_path = Path(folder) / "mail_accounts.csv"
            csv_path.write_text(
                "邮箱,密码,状态\nfirst@example.test,ignored-secret,active\n"
                "FIRST@example.test,another-ignored-secret,active\nsecond@example.test,x,active\n",
                encoding="utf-8-sig",
            )
            self.assertEqual(
                auto_login.load_mailboxes(csv_path),
                ["first@example.test", "second@example.test"],
            )

    def test_config_reads_mail_api_base(self):
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder) / "config.json"
            config_path.write_text(
                json.dumps({"mail": {"apiBase": "https://mail.example.test/"}, "other": "ignored"}),
                encoding="utf-8",
            )
            self.assertEqual(
                auto_login.load_mail_api_base(config_path), "https://mail.example.test"
            )

    def test_mailbox_url_encodes_email_segment(self):
        self.assertEqual(
            auto_login.mailbox_url("https://mail.example.test/", "a+b@example.test"),
            "https://mail.example.test/a%2Bb%40example.test",
        )

if __name__ == "__main__":
    unittest.main()
