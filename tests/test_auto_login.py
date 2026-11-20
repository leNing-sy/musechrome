"""邮箱辅助登录纯逻辑测试，不连接真实邮箱服务。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musechrome import auto_login


class MailAssistTests(unittest.TestCase):
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
