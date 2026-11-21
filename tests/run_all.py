"""全量验收入口：一次性跑完快速回归与 unittest 套件。

背景：`tests/run_tests.py` 只收集自己登记的 70 项，不发现 `test_*.py`，
导致 339 项 unittest 用例长期游离在验收之外。本脚本把两层口径合并，
任一失败都返回非零退出码，可直接用于本地自检或 CI。

用法：
    .venv\\Scripts\\python.exe tests\\run_all.py
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def run_fast() -> bool:
    print("\n=== 1/2 快速回归（tests/run_tests.py --fast）===", flush=True)
    r = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "run_tests.py"), "--fast"],
        cwd=str(ROOT),
    )
    return r.returncode == 0


def run_unittest() -> bool:
    print("\n=== 2/2 unittest 全量套件（tests/test_*.py）===", flush=True)
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return result.wasSuccessful()


def main() -> int:
    ok_fast = run_fast()
    ok_unit = run_unittest()
    print(f"\n全量验收：快速回归 {'通过' if ok_fast else '失败'} · "
          f"unittest {'通过' if ok_unit else '失败'}", flush=True)
    return 0 if (ok_fast and ok_unit) else 1


if __name__ == "__main__":
    sys.exit(main())
