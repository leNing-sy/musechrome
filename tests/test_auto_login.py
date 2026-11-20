"""自动登录邮箱/API 纯逻辑测试，不连接真实邮箱服务。"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musechrome import auto_login


class AutoLoginTests(unittest.TestCase):
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

    def test_extracts_code_only_from_newest_mail_after_cutoff(self):
        after = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)
        payload = {
            "emails": [
                {"from": "Meta Security", "date": "2026-11-20T09:59:00Z", "body": "old code 111111"},
                {"from": "Account Notification", "date": "2026-11-20T10:01:00Z", "html_body": "<p>code: 222222</p>"},
                {"from": "Security Notification", "date": "2026-11-20T10:02:00Z", "subject": "Muse sign in", "body": "333333"},
                {"from": "Unrelated service", "date": "2026-11-20T10:03:00Z", "body": "code 555555"},
                {"from": "Meta Security 666666", "date": "2026-11-20T10:04:00Z", "body": "no code"},
            ]
        }
        self.assertEqual(auto_login.extract_new_otp(payload, after), "333333")

    def test_fetch_uses_configured_email_endpoint_and_get(self):
        after = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"emails":[{"from":"Meta Security","date":"2026-11-20T10:01:00Z","html_body":"code 444444"}]}'

        with patch("musechrome.auto_login.urlopen", return_value=Response()) as opened:
            self.assertEqual(
                auto_login.fetch_new_otp(
                    "https://mail.example.test", "a+b@example.test", after
                ),
                "444444",
            )
        request = opened.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(
            request.full_url,
            "https://mail.example.test/a%2Bb%40example.test",
        )


if __name__ == "__main__":
    unittest.main()
