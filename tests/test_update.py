"""Guard against reintroducing the removed in-app self-updater."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musechrome import app


class RetiredUpdaterTests(unittest.TestCase):
    def test_self_update_entry_points_are_absent(self):
        for name in ("UpdateChecker", "DownloadThread", "build_updater_bat",
                     "build_rename_bat", "_pick_exe_asset"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(app, name))

        self.assertFalse(hasattr(app.MainWindow, "check_update"))
        self.assertFalse(hasattr(app.MainWindow, "_start_self_update"))


if __name__ == "__main__":
    unittest.main()
