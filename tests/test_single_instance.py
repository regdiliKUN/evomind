"""单实例：连点 EXE 时只能有一个实例真正启动，日志不能互相覆盖。"""
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import evomind_launcher  # noqa: E402
import run  # noqa: E402

HOLD_LOCK = """
import sys, time
sys.path.insert(0, sys.argv[1])
import run
from pathlib import Path
f = run.acquire_instance_lock(Path(sys.argv[2]))
print("locked" if f else "busy", flush=True)
time.sleep(30)
"""


class InstanceLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lock = self.tmp / "run.lock"

    def test_second_process_cannot_take_lock(self):
        # venv 的 python.exe 只是跳板，会再拉起真正的解释器；杀跳板杀不到持锁的子进程。
        # run.py 顶层只用标准库，直接用基础解释器即可。
        exe = getattr(sys, "_base_executable", None) or sys.executable
        holder = subprocess.Popen([exe, "-c", HOLD_LOCK, str(ROOT), str(self.lock)],
                                  stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            self.assertIsNone(run.acquire_instance_lock(self.lock))
        finally:
            holder.kill()
            holder.wait()
        # 持锁进程（哪怕是被强杀）退出后锁自动释放（Windows 释放略有延迟）
        f = None
        for _ in range(50):
            f = run.acquire_instance_lock(self.lock)
            if f:
                break
            time.sleep(0.1)
        self.assertIsNotNone(f)
        f.close()

    def test_port_file_roundtrip(self):
        p = self.tmp / "run.port"
        self.assertIsNone(run.read_port_file(p))
        p.write_text("8766", encoding="utf-8")
        self.assertEqual(run.read_port_file(p), 8766)
        p.write_text("garbage", encoding="utf-8")
        self.assertIsNone(run.read_port_file(p))

    def test_handoff_while_first_instance_still_starting_opens_nothing(self):
        with patch.object(run, "port_open", return_value=False), \
             patch.object(run, "open_ui") as open_ui, patch.object(run, "log"):
            run.handoff_to_running(8765)
            run.handoff_to_running(None)
        open_ui.assert_not_called()

    def test_handoff_to_running_instance_opens_its_window(self):
        with patch.object(run, "port_open", return_value=True), \
             patch.object(run, "open_ui") as open_ui, patch.object(run, "log"), \
             patch.dict("os.environ", {}, clear=False) as env:
            env.pop("EVOMIND_NO_BROWSER", None)
            run.handoff_to_running(8770)
        open_ui.assert_called_once_with("http://127.0.0.1:8770")

    def test_main_exits_cleanly_when_lock_held(self):
        with patch.object(run, "acquire_instance_lock", return_value=None), \
             patch.object(run, "read_port_file", return_value=None), \
             patch.object(run, "ensure_ollama", side_effect=AssertionError("must not start")), \
             patch.object(run, "log"), patch("builtins.print"):
            run.main()  # 不抛异常 = 退出码 0，启动器不会弹错误框

    def test_handoff_while_splash_is_served_opens_nothing(self):
        with patch.object(run, "port_open", return_value=True), \
             patch.object(run, "is_starting", return_value=True), \
             patch.object(run, "open_ui") as open_ui, patch.object(run, "log"):
            run.handoff_to_running(8765)
        open_ui.assert_not_called()


class LegacyScanTests(unittest.TestCase):
    def test_port_in_use_is_instant_and_accurate(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        try:
            self.assertTrue(run.port_in_use(port))
        finally:
            busy.close()
        t0 = time.perf_counter()
        self.assertFalse(run.port_in_use(port))
        self.assertLess(time.perf_counter() - t0, 0.2)

    def test_legacy_scan_only_probes_ports_in_use(self):
        with patch.object(run, "port_in_use", side_effect=lambda p: p == 8770), \
             patch.object(run, "is_evomind", return_value=True) as probe:
            self.assertEqual(run.find_legacy_instance(), 8770)
        probe.assert_called_once_with(8770)


class SplashHandoverTests(unittest.TestCase):
    """端口一次绑定、全程持有：先由临时服务给「正在启动」页，再把同一个 socket 交给 uvicorn。"""

    def test_bound_port_is_held_and_splash_then_app_served(self):
        import urllib.request

        import uvicorn

        with patch.dict("os.environ", {"EVOMIND_PORT": ""}):
            sock = run.bind_port()
        port = sock.getsockname()[1]
        try:
            # 端口被我们持有，别人（包括同时启动的另一个实例）绑不上
            other = socket.socket()
            with self.assertRaises(OSError):
                other.bind(("127.0.0.1", port))
            other.close()

            splash = run.SplashServer(sock, b"<p>starting</p>").start()
            self.assertTrue(run.is_starting(port))

            async def app(scope, receive, send):
                if scope["type"] != "http":
                    return
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"console"})

            server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
            splash.stop()
            t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            t.start()
            for _ in range(100):
                if server.started:
                    break
                time.sleep(0.05)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
                self.assertEqual(r.read(), b"console")
                self.assertIsNone(r.headers.get(run.SPLASH_HEADER))
            self.assertFalse(run.is_starting(port))
            server.should_exit = True
            t.join(timeout=10)
        finally:
            sock.close()


class LauncherLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_previous_log_is_kept(self):
        (self.dir / "launcher.log").write_text("old", encoding="utf-8")
        self.assertEqual(evomind_launcher.pick_log_file(self.dir), self.dir / "launcher.log")
        self.assertEqual((self.dir / "launcher.prev.log").read_text(encoding="utf-8"), "old")

    @unittest.skipUnless(sys.platform == "win32", "Windows 下打开中的文件不能改名")
    def test_log_in_use_by_running_instance_is_not_truncated(self):
        log = self.dir / "launcher.log"
        with open(log, "w", encoding="utf-8") as running:
            running.write("running instance output")
            running.flush()
            self.assertEqual(evomind_launcher.pick_log_file(self.dir), self.dir / "launcher.extra.log")
        self.assertEqual(log.read_text(encoding="utf-8"), "running instance output")


if __name__ == "__main__":
    unittest.main()
