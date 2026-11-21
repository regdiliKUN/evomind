"""独立应用窗口（backend/appwindow.py）的判定逻辑。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import appwindow  # noqa: E402


class AppWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_is_window_and_env_can_opt_out(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(appwindow.UI_ENV, None)
            self.assertTrue(appwindow.wants_window())
        with patch.dict(os.environ, {appwindow.UI_ENV: "Browser"}):
            self.assertFalse(appwindow.wants_window())

    def test_missing_or_stale_lock_means_not_running(self):
        lock = self.dir / "lockfile"
        self.assertFalse(appwindow._locked(lock))
        lock.write_text("")  # 崩溃遗留、无人持有的锁文件
        self.assertFalse(appwindow._locked(lock))
        self.assertFalse(lock.exists())

    def test_wait_closed_tracks_lock_lifecycle(self):
        states = iter([False, True, True, True, False])  # 未起 → 运行 3 次轮询 → 关闭
        sleeps = []
        with patch.object(appwindow, "_locked", side_effect=lambda _l: next(states)):
            self.assertTrue(appwindow.wait_closed(self.dir, sleep=sleeps.append))
        self.assertEqual(len(sleeps), 3)  # 一次等启动 + 两次运行中轮询

    def test_wait_closed_gives_up_when_window_never_starts(self):
        with patch.object(appwindow, "_locked", return_value=False):
            self.assertFalse(appwindow.wait_closed(self.dir, start_timeout=1.0, sleep=lambda _s: None))

    def test_open_window_returns_none_without_browser(self):
        with patch.object(appwindow, "find_browser", return_value=None):
            self.assertIsNone(appwindow.open_window("http://127.0.0.1:1/", self.dir))

    def test_open_window_uses_app_mode_and_private_profile(self):
        calls = []
        with patch.object(appwindow.subprocess, "Popen", side_effect=lambda a, **k: calls.append(a) or "proc"):
            self.assertEqual(appwindow.open_window("http://127.0.0.1:9/", self.dir / "p", browser="edge.exe"),
                             "proc")
        args = calls[0]
        self.assertIn("--app=http://127.0.0.1:9/", args)
        self.assertIn(f"--user-data-dir={self.dir / 'p'}", args)
        self.assertTrue((self.dir / "p").is_dir())


if __name__ == "__main__":
    unittest.main()
