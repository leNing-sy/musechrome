"""UI and proxy-check regressions for the reduced app, using isolated data."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt6.QtWidgets import QApplication, QPushButton

from musechrome import app, store


class ReducedAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])
        cls.qt_app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="musechrome-pruning-")
        self.addCleanup(self.temp.cleanup)
        self.base_patch = patch.object(store, "base_dir", return_value=Path(self.temp.name))
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.window = app.MainWindow(store.load_config())
        self.addCleanup(self.close_window)

    def close_window(self):
        self.window.close()
        self.window.deleteLater()
        self.qt_app.processEvents()

    def test_core_pages_remain_and_removed_entry_points_are_absent(self):
        self.assertEqual(self.window.stack.count(), 4)
        self.assertEqual(self.window.btn_nav_home.text(), "账号列表")
        self.assertEqual(self.window.btn_nav_settings.text(), "设置")
        self.assertEqual(self.window.btn_nav_health.text(), "代理检测")
        self.assertEqual(self.window.btn_nav_about.text(), "关于软件")
        self.assertIsInstance(self.window.page_settings.btn_export, QPushButton)
        self.assertFalse(hasattr(self.window.page_settings, "btn_import"))

        labels = [button.text() for button in self.window.findChildren(QPushButton)]
        for retired_label in ("📢 公告", "插件助手", "检查更新", "打开全部"):
            self.assertNotIn(retired_label, labels)
        for retired_api in ("page_notice", "page_plugins", "btn_open_all", "import_backup"):
            self.assertFalse(hasattr(self.window, retired_api))
        self.assertFalse(hasattr(store, "import_backup"))

    def test_proxy_checker_uses_only_configured_proxy_and_never_scans(self):
        cfg = {**store.load_config(), "proxy_mode": "local", "proxy_type": "socks5",
               "proxy_host": "127.0.0.1", "proxy_port": 7891}
        events = []
        checker = app.HealthChecker(cfg)
        checker.item.connect(lambda *event: events.append(event))
        with patch.object(app, "_timed_probe", return_value=(True, "HTTP 204", 19)) as probe:
            checker.run()
        probe.assert_called_once_with("socks5", "127.0.0.1", 7891, timeout=8)
        self.assertEqual(events[0][0], "代理连接")
        self.assertEqual(events[1][0], "请求耗时")

    def test_direct_mode_does_not_probe_any_host_or_port(self):
        cfg = {**store.load_config(), "proxy_mode": "off"}
        events = []
        checker = app.HealthChecker(cfg)
        checker.item.connect(lambda *event: events.append(event))
        with patch.object(app, "_timed_probe", side_effect=AssertionError("unexpected probe")):
            with patch.object(store, "system_proxy_url", side_effect=AssertionError("unexpected lookup")):
                checker.run()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "直连模式")


if __name__ == "__main__":
    unittest.main()
