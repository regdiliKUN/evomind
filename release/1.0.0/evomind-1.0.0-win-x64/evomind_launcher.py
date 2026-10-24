"""EvoMind 原生启动器（编译为 EvoMind.exe）。

职责：定位项目根目录 → 检查 .venv → 拉起 `run.py`（同控制台，完整日志）。
不打包任何 Python 依赖，Python 运行环境复用项目内的 .venv，因此 EXE 体积小且稳定。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def find_root() -> Path | None:
    """找到同时包含 .venv/Scripts/python.exe 与 run.py 的目录。

    双击 EXE 时，资源管理器会把进程工作目录设为 EXE 所在目录（=项目根），
    所以优先从 cwd 向上回溯；同时兜底扫描 EXE 所在目录链。
    """
    candidates: list[Path] = []
    try:
        candidates.append(Path(os.getcwd()))
    except Exception:
        pass
    exe_dir = Path(sys.executable).resolve().parent
    candidates.append(exe_dir)
    # 向上回溯两层，覆盖「在子目录里打开」的情况
    for base in list(candidates):
        for _ in range(3):
            if base in candidates:
                pass
            base = base.parent
            candidates.append(base)

    seen = set()
    for c in candidates:
        if c in seen or not c or not c.exists():
            continue
        seen.add(c)
        if (c / ".venv" / "Scripts" / "python.exe").exists() and (c / "run.py").exists():
            return c
    return None


def main() -> int:
    # 控制台切到 UTF-8，否则 PyInstaller 打包后的中文提示会乱码
    # （原 .cmd 启动器里同样有 chcp 65001）。
    if os.name == "nt":
        try:
            os.system("chcp 65001 >nul")
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    root = find_root()
    if root is None:
        # 即使没找到也别静默退出，给用户一个可读的提示
        msg = (
            "找不到 EvoMind 项目目录（需要 .venv 与 run.py 在同一目录）。\n"
            "请把 EvoMind.exe 放在 EvoMind 项目根目录下，再双击运行。\n"
        )
        sys.stderr.write(msg)
        try:
            input("\n按回车关闭…")
        except Exception:
            pass
        return 1

    python = root / ".venv" / "Scripts" / "python.exe"
    run_py = root / "run.py"

    # 把启动器所在目录设为工作目录（run.py 内部用相对路径读配置/数据库）
    env = dict(os.environ)
    env["EVOMIND_LAUNCHED_BY_EXE"] = "1"

    try:
        rc = subprocess.call([str(python), str(run_py)], cwd=str(root), env=env)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"启动失败：{e}\n")
        try:
            input("\n按回车关闭…")
        except Exception:
            pass
        return 1

    # 130 = Ctrl+C，143 = SIGTERM：都是用户主动关闭，不该当成错误弹提示
    if rc not in (0, 130, 143):
        sys.stderr.write(f"\nEvoMind 已退出（退出码 {rc}）。\n")
        try:
            input("按回车关闭…")
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
