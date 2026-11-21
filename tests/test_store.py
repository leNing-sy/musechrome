"""Core store regressions; all files and profiles are isolated fixtures."""

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from musechrome import store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="musechrome-store-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base_patch = patch.object(store, "base_dir", return_value=self.root)
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)
        self.cfg = store.load_config()
        self.accounts_path = store.data_dir() / "accounts.json"

    def write_accounts(self, value):
        self.accounts_path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_defaults_and_email_path_roundtrip(self):
        self.assertTrue(self.cfg["proxy_enabled"])
        self.assertEqual(self.cfg["proxy_port"], 10808)
        self.assertTrue(self.cfg["webrtc_protect"])
        self.assertTrue(self.cfg["lang_en"])
        self.assertEqual(store.data_dir().name, "MuseChromeData")
        self.assertEqual(self.cfg["mail_accounts_csv"], "")
        self.assertEqual(self.cfg["mail_config_path"], "")
        store.save_config({**self.cfg, "mail_accounts_csv": r"D:\fixtures\mail.csv",
                           "mail_config_path": r"D:\fixtures\mail.json"})
        loaded = store.load_config()
        self.assertEqual(loaded["mail_accounts_csv"], r"D:\fixtures\mail.csv")
        self.assertEqual(loaded["mail_config_path"], r"D:\fixtures\mail.json")
        store.save_config({**self.cfg, "mail_accounts_csv": None, "mail_config_path": None})
        self.assertEqual(store.load_config()["mail_accounts_csv"], "")
        self.assertEqual(store.load_config()["mail_config_path"], "")

    def test_invalid_configuration_is_normalized(self):
        store.save_config({"proxy_port": -5, "proxy_type": "bad", "home_url": "x"})
        cfg = store.load_config()
        self.assertEqual((cfg["proxy_port"], cfg["proxy_type"], cfg["home_url"]),
                         (10808, "http", "https://muse.ai"))

    def test_browser_arguments_retain_isolation_and_edge_restrictions(self):
        profile = self.root / "fixture-profile"
        chrome = store.build_chrome_args(self.cfg, profile)
        edge = store.build_chrome_args(self.cfg, profile, browser="edge")
        self.assertEqual(chrome[0], f"--user-data-dir={profile}")
        self.assertEqual(chrome[-1], self.cfg["home_url"])
        self.assertEqual(sum(arg.startswith("https://") for arg in chrome), 1)
        for flag in ("--no-first-run", "--no-default-browser-check",
                     "--proxy-server=http://127.0.0.1:10808",
                     "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--lang=en-US"):
            self.assertIn(flag, chrome)
        for flag in ("--disable-signin", "--disable-sync", "--disable-features=msImplicitSignin"):
            self.assertIn(flag, edge)
            self.assertNotIn(flag, chrome)
        disabled = store.build_chrome_args({**self.cfg, "proxy_enabled": False,
                                            "webrtc_protect": False, "lang_en": False}, profile)
        self.assertFalse(any(arg.startswith("--proxy-server") for arg in disabled))
        self.assertFalse(any("webrtc" in arg for arg in disabled))
        self.assertNotIn("--lang=en-US", disabled)

    def test_legacy_proxy_modes_and_per_account_overrides(self):
        for enabled, mode in ((True, "local"), (False, "off")):
            store.save_config({"proxy_enabled": enabled})
            self.assertEqual(store.load_config()["proxy_mode"], mode)
        self.assertIsNone(store.proxy_server_url({**self.cfg, "proxy_mode": "system"}))
        self.assertEqual(store.effective_cfg(self.cfg, {"proxy": ""}), self.cfg)
        direct = store.effective_cfg(self.cfg, {"proxy": "direct"})
        self.assertEqual(direct["proxy_mode"], "off")
        self.assertIsNone(store.proxy_server_url(direct))
        custom = store.effective_cfg(self.cfg, {"proxy": {"type": "socks5",
                                                  "host": "127.0.0.1", "port": 7891}})
        self.assertEqual(store.proxy_server_url(custom), "socks5://127.0.0.1:7891")
        self.assertEqual(len(store.PROXY_PRESETS), 3)

    def test_absent_and_empty_accounts_are_distinct_from_invalid_data(self):
        self.assertEqual(store.load_accounts(), [])
        self.assertFalse(self.accounts_path.exists())
        self.write_accounts([])
        self.assertEqual(store.load_accounts(), [])

    def test_legacy_account_defaults_and_proxy_normalization(self):
        self.write_accounts([
            {"id": "legacy-1", "name": "Fixture"},
            {"id": "legacy-2", "name": "Other", "browser": "unsupported",
             "proxy": {"type": "bad", "host": "", "port": 7897}},
            {"id": "edge-1", "name": "Edge", "browser": "edge",
             "proxy": {"type": "http", "host": "host", "port": 99999}},
        ])
        loaded = store.load_accounts()
        self.assertEqual([a["browser"] for a in loaded], ["chrome", "chrome", "edge"])
        self.assertEqual(loaded[1]["proxy"], {"type": "http", "host": "127.0.0.1", "port": 7897})
        self.assertEqual(loaded[2]["proxy"], "")
        self.assertNotIn(store.next_account_name(loaded), {a["name"] for a in loaded})

    def test_retired_note_and_color_survive_name_edit_roundtrip(self):
        note = "保留已有备注" * 20
        self.write_accounts([{"id": "old-metadata", "name": "Before",
                              "note": note, "color": "legacy-color-value"}])
        accounts = store.load_accounts()
        accounts[0]["name"] = "After"
        store.save_accounts(accounts)
        loaded = store.load_accounts()[0]
        self.assertEqual(loaded["name"], "After")
        self.assertEqual(loaded["note"], note)
        self.assertEqual(loaded["color"], "legacy-color-value")

    def test_invalid_json_or_schema_is_reported_without_modification(self):
        invalid_payloads = [b"{broken", b"\xff\xff", b"{}", b"null", b"[null]",
                            b"[{}]", b'[ {"id":"ok","name":"   "} ]',
                            b'[ {"id": 1,"name":"Fixture"} ]']
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                self.accounts_path.write_bytes(payload)
                with self.assertRaises(store.AccountDataError):
                    store.load_accounts()
                self.assertEqual(self.accounts_path.read_bytes(), payload)
                self.assertFalse(self.accounts_path.with_suffix(".tmp").exists())

    def test_unreadable_accounts_file_is_not_treated_as_empty(self):
        self.write_accounts([{"id": "safe", "name": "Fixture"}])
        before = self.accounts_path.read_bytes()
        with patch.object(Path, "read_text", side_effect=PermissionError("fixture denied")):
            with self.assertRaises(store.AccountDataError):
                store.load_accounts()
        self.assertEqual(self.accounts_path.read_bytes(), before)

    def test_duplicate_ids_including_windows_case_aliases_are_rejected(self):
        for second_id in ("same", "SAME"):
            accounts = [{"id": "same", "name": "First"},
                        {"id": second_id, "name": "Second"}]
            self.write_accounts(accounts)
            before = self.accounts_path.read_bytes()
            with self.assertRaises(store.AccountDataError):
                store.load_accounts()
            with self.assertRaises(store.AccountDataError):
                store.save_accounts(accounts)
            self.assertEqual(self.accounts_path.read_bytes(), before)

    def test_invalid_ids_never_reach_directory_creation_or_deletion(self):
        invalid_ids = ("..", ".", "../outside", "..\\outside",
                       str(self.root / "outside"), r"C:\outside", "a/b", "a:b",
                       "trailing.", "trailing ", "NUL", "con", "COM1",
                       "LPT9", "x" * 65, "", None, 42)
        for acc_id in invalid_ids:
            with self.subTest(acc_id=acc_id), patch.object(store.shutil, "rmtree") as remove:
                for operation in (store.profile_dir_for, store.edge_profile_dir_for,
                                  store.delete_profile_dir, store.delete_edge_profile_dir):
                    with self.assertRaises(store.AccountDataError):
                        operation(acc_id)
                remove.assert_not_called()
                self.write_accounts([{"id": acc_id, "name": "Fixture"}])
                original = self.accounts_path.read_bytes()
                with self.assertRaises(store.AccountDataError):
                    store.load_accounts()
                with self.assertRaises(store.AccountDataError):
                    store.save_accounts([{"id": acc_id, "name": "Fixture"}])
                self.assertEqual(self.accounts_path.read_bytes(), original)
        self.assertFalse((self.root / "outside").exists())

    def test_resolved_profile_boundary_blocks_redirected_account_path(self):
        root = store.profiles_dir().resolve()
        outside = self.root / "outside"
        real_resolve = Path.resolve
        def fixture_resolve(path, *args, **kwargs):
            if path == root / "redirected":
                return outside
            return real_resolve(path, *args, **kwargs)
        with patch.object(Path, "resolve", fixture_resolve), patch.object(store.shutil, "rmtree") as remove:
            with self.assertRaises(store.AccountDataError):
                store.delete_profile_dir("redirected")
            with self.assertRaises(store.AccountDataError):
                store.profile_dir_for("redirected")
            remove.assert_not_called()
        self.assertFalse(outside.exists())

    def test_profile_redirect_to_other_account_or_root_is_rejected(self):
        root = store.profiles_dir().resolve()
        real_resolve = Path.resolve
        for redirect in (root / "other-account", self.root / "outside-root"):
            def fixture_resolve(path, *args, **kwargs):
                if path == root / "redirected":
                    return redirect
                return real_resolve(path, *args, **kwargs)
            with self.subTest(redirect=redirect), patch.object(Path, "resolve", fixture_resolve), \
                 patch.object(store.shutil, "rmtree") as remove:
                with self.assertRaises(store.AccountDataError):
                    store.delete_profile_dir("redirected")
                remove.assert_not_called()
        redirected_root = store.data_dir() / "edge-profiles"
        def fixture_root_resolve(path, *args, **kwargs):
            if path == root:
                return redirected_root
            return real_resolve(path, *args, **kwargs)
        with patch.object(Path, "resolve", fixture_root_resolve), patch.object(store.shutil, "rmtree") as remove:
            with self.assertRaises(store.AccountDataError):
                store.delete_profile_dir("fixture")
            remove.assert_not_called()

    def test_profile_isolation_and_delete_target_scope(self):
        chrome = store.profile_dir_for_browser("fixture-1", "chrome")
        edge = store.profile_dir_for_browser("fixture-1", "edge")
        other = store.profile_dir_for("fixture-2")
        self.assertNotEqual(chrome, edge)
        self.assertEqual(chrome.parent.name, "chrome-profiles")
        self.assertEqual(edge.parent.name, "edge-profiles")
        with patch.object(store.shutil, "rmtree") as remove:
            store.delete_profile_dir("fixture-1")
            store.delete_edge_profile_dir("fixture-1")
            self.assertEqual([call.args[0] for call in remove.call_args_list], [chrome, edge])
        self.assertTrue(other.is_dir())

    def test_manual_browser_paths_and_discovery_are_isolated(self):
        chrome = self.root / "chrome.exe"
        edge = self.root / "msedge.exe"
        chrome.write_bytes(b"MZ")
        edge.write_bytes(b"MZ")
        self.assertEqual(store.find_browser_exe({**self.cfg, "chrome_path": str(chrome)}, "chrome"), str(chrome))
        self.assertEqual(store.find_browser_exe({**self.cfg, "edge_path": str(edge)}, "edge"), str(edge))
        with patch.object(store, "chrome_candidates", return_value=[chrome]), \
             patch.object(store, "edge_candidates", return_value=[edge]):
            self.assertEqual(store.find_chrome({"chrome_path": str(self.root / "missing")}), str(chrome))
            self.assertEqual(store.find_edge({"edge_path": str(self.root / "missing")}), str(edge))
        with patch.object(store, "chrome_candidates", return_value=[]):
            self.assertEqual(store.find_chrome({"chrome_path": ""}), "")

    def test_backup_exports_only_requested_data_without_modifying_source(self):
        accounts = [{"id": "fixture-1", "name": "Fixture"}]
        store.save_accounts(accounts)
        store.save_config(self.cfg)
        chrome_file = store.profile_dir_for("fixture-1") / "profile-fixture.txt"
        edge_file = store.edge_profile_dir_for("fixture-1") / "profile-fixture.txt"
        chrome_file.write_text("chrome fixture", encoding="utf-8")
        edge_file.write_text("edge fixture", encoding="utf-8")
        before = {path: path.read_bytes() for path in (self.accounts_path, chrome_file, edge_file)}
        for include_profiles in (False, True):
            archive_path = self.root / f"backup-{include_profiles}.zip"
            store.export_backup(str(archive_path), include_profiles=include_profiles)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertIsNone(archive.testzip())
                self.assertIn("MuseChromeData/accounts.json", archive.namelist())
                self.assertIn("MuseChromeData/config.json", archive.namelist())
                self.assertEqual(any("-profiles/" in n for n in archive.namelist()), include_profiles)
                if include_profiles:
                    self.assertEqual(archive.read("MuseChromeData/edge-profiles/fixture-1/profile-fixture.txt"), b"edge fixture")
        for path, contents in before.items():
            self.assertEqual(path.read_bytes(), contents)


if __name__ == "__main__":
    unittest.main()
