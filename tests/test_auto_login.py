"""邮箱辅助登录纯逻辑测试，不连接真实邮箱服务。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musechrome import auto_login


class MailAssistTests(unittest.TestCase):
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
