"""微调环境检测不能阻塞 /api/status：不在服务进程里导入 torch，CUDA 在后台子进程探测。"""
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import finetune  # noqa: E402

FRESH_PROCESS_CHECK = """
import sys, time
sys.path.insert(0, sys.argv[1])
from backend import finetune
t = time.perf_counter()
env = finetune.check_env()
dt = time.perf_counter() - t
heavy = [m for m in ("torch", "transformers", "peft", "datasets") if m in sys.modules]
print(f"{dt:.3f} {','.join(heavy) or '-'}")
"""


class FinetuneEnvTests(unittest.TestCase):
    def setUp(self):
        finetune._env_cache.update(ts=0.0, value=None)
        finetune._gpu.update(state="idle", value={"cuda": None, "gpu": None})

    def tearDown(self):
        self.setUp()

    def test_check_env_does_not_import_heavy_modules(self):
        p = subprocess.run([sys.executable, "-c", FRESH_PROCESS_CHECK, str(ROOT)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        dt, heavy = p.stdout.strip().splitlines()[-1].split()
        self.assertEqual(heavy, "-")
        self.assertLess(float(dt), 1.0)

    def test_cuda_probe_runs_in_background_without_blocking(self):
        gate = {"go": False}

        def slow_probe():
            while not gate["go"]:
                time.sleep(0.01)
            return {"cuda": True, "gpu": "Test GPU", "vram_gb": 24.0}

        with patch.object(finetune, "_module_versions",
                          return_value={"torch": "2.5", "transformers": "4.4", "peft": "0.1", "datasets": None}), \
             patch.object(finetune, "_probe_cuda", side_effect=slow_probe) as probe:
            t = time.perf_counter()
            env = finetune.check_env()
            self.assertLess(time.perf_counter() - t, 0.2)
            self.assertTrue(env["ready"])
            self.assertIsNone(env["cuda"])
            self.assertTrue(env["cuda_checking"])
            finetune.check_env()  # 探测进行中不会重复启动
            gate["go"] = True
            for _ in range(200):
                env = finetune.check_env()
                if not env["cuda_checking"]:
                    break
                time.sleep(0.01)
            self.assertTrue(env["cuda"])
            self.assertEqual(env["gpu"], "Test GPU")
            self.assertEqual(probe.call_count, 1)

    def test_no_probe_when_torch_missing(self):
        with patch.object(finetune, "_module_versions",
                          return_value={"torch": None, "transformers": None, "peft": None, "datasets": None}), \
             patch.object(finetune, "_probe_cuda", side_effect=AssertionError("no torch, no probe")):
            env = finetune.check_env()
        self.assertFalse(env["ready"])
        self.assertFalse(env["cuda_checking"])
        self.assertIsNone(env["cuda"])

    def test_probe_failure_reports_cuda_unavailable(self):
        with patch.object(finetune.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(finetune._probe_cuda(), {"cuda": False, "gpu": None})


if __name__ == "__main__":
    unittest.main()
