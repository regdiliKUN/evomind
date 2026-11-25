"""EvoMind 原生启动器（编译为 EvoMind.exe）。

职责：定位项目根目录 → 检查 .venv → 无控制台拉起 `run.py`，界面在独立窗口中（--console 保留旧的控制台模式）。
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


def _message_box(text: str, title: str = "EvoMind") -> None:
    """无控制台模式下唯一的报错出口。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)  # MB_ICONERROR
    except Exception:  # noqa: BLE001
        pass


def _has_console() -> bool:
    try:
        return sys.stdout is not None and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


def pick_log_file(log_dir: Path) -> Path:
    """本次启动用的日志文件。

    上一份 launcher.log 改名为 launcher.prev.log 保留；改名失败说明另一个实例正开着它在写
    （Windows 下被打开的文件不能改名），这时改写 launcher.extra.log，不再用 "w" 把
    正在运行的那个实例的日志清空。
    """
    log_file = log_dir / "launcher.log"
    if log_file.exists():
        try:
            os.replace(log_file, log_dir / "launcher.prev.log")
        except OSError:
            return log_dir / "launcher.extra.log"
    return log_file


def main_windowed(root: Path) -> int:
    """默认模式：不开黑色控制台，界面在独立窗口里（见 backend/appwindow.py），关窗即退出。

    日志写到 data/logs/launcher.log；启动失败时弹窗显示日志末尾，方便排查。
    """
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    python = pythonw if pythonw.exists() else root / ".venv" / "Scripts" / "python.exe"
    log_dir = root / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = pick_log_file(log_dir)
    env = dict(os.environ, EVOMIND_LAUNCHED_BY_EXE="1", PYTHONIOENCODING="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        with open(log_file, "w", encoding="utf-8", errors="replace") as out:
            rc = subprocess.call([str(python), str(root / "run.py")], cwd=str(root), env=env,
                                 stdout=out, stderr=subprocess.STDOUT, creationflags=flags)
    except Exception as e:  # noqa: BLE001
        _message_box(f"启动失败：{e}")
        return 1
    if rc not in (0, 130, 143):
        try:
            tail = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-15:]
        except OSError:
            tail = []
        _message_box(f"EvoMind 异常退出（退出码 {rc}）。\n\n" + "\n".join(tail) +
                     f"\n\n完整日志：{log_file}\n如需查看实时日志，可用「① 双击这里启动.cmd」启动。")
    return rc


def main() -> int:
    if "--console" not in sys.argv and not _has_console():
        root = find_root()
        if root is None:
            _message_box("找不到 EvoMind 项目目录（需要 .venv 与 run.py 在同一目录）。\n"
                         "请把 EvoMind.exe 放在 EvoMind 项目根目录下，再双击运行。")
            return 1
        return main_windowed(root)
    return main_console()


def main_console() -> int:
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
