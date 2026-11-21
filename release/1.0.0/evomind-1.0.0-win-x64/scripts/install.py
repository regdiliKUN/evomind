"""EvoMind 首次安装器。

做四件事：
  1. 建虚拟环境
  2. 装运行依赖
  3. 检查 Ollama
  4. 问你要不要在桌面放一个图标（可选）

全部幂等，重复运行不会出问题。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
PY = VENV / "Scripts" / "python.exe"
REQ = ROOT / "requirements.txt"

if os.name == "nt":
    os.system("")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

C_DIM = "\033[90m"
C_OK = "\033[92m"
C_WARN = "\033[93m"
C_ERR = "\033[91m"
C_B = "\033[96m"
C_END = "\033[0m"


def line(msg: str = "") -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    line(f"  {C_OK}✓{C_END} {msg}")


def warn(msg: str) -> None:
    line(f"  {C_WARN}!{C_END} {msg}")


def fail(msg: str) -> None:
    line(f"  {C_ERR}✗{C_END} {msg}")


def step(msg: str) -> None:
    line(f"\n{C_B}▸ {msg}{C_END}")


def ask_yes(prompt: str, default_yes: bool = True) -> bool:
    hint = "[Y/n]" if default_yes else "[y/N]"
    try:
        ans = input(f"  {prompt} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        line("")
        return default_yes
    if not ans:
        return default_yes
    return ans in ("y", "yes", "是", "1", "要", "好")


def banner() -> None:
    line()
    line(f"{C_B}  ╔══════════════════════════════════════════════╗{C_END}")
    line(f"{C_B}  ║        EvoMind · 自进化学习引擎              ║{C_END}")
    line(f"{C_B}  ║        首次安装                              ║{C_END}")
    line(f"{C_B}  ╚══════════════════════════════════════════════╝{C_END}")
    line(f"{C_DIM}  安装位置：{ROOT}{C_END}")


def check_python() -> bool:
    v = sys.version_info
    if v < (3, 10):
        fail(f"Python 版本过低：{v.major}.{v.minor}，需要 3.10 以上")
        return False
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    return True


def ensure_venv() -> bool:
    if PY.exists():
        ok("虚拟环境已存在，跳过创建")
        return True
    line(f"  {C_DIM}正在创建虚拟环境…{C_END}")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "venv", str(VENV)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        fail(f"创建失败：{e}")
        return False
    if r.returncode != 0 or not PY.exists():
        fail("创建虚拟环境失败")
        if r.stderr:
            line(f"  {C_DIM}{r.stderr.strip()[-300:]}{C_END}")
        return False
    ok("虚拟环境已创建")
    return True


def install_deps() -> bool:
    line(f"  {C_DIM}正在安装依赖（首次约 60-150MB，需要几分钟）…{C_END}")
    cmd = [str(PY), "-m", "pip", "install", "--upgrade", "pip", "-q"]
    subprocess.run(cmd, capture_output=True)

    if not REQ.exists():
        fail("找不到 requirements.txt")
        return False

    proc = subprocess.Popen(
        [str(PY), "-m", "pip", "install", "-r", str(REQ), "--progress-bar", "off"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert proc.stdout is not None
    for raw in proc.stdout:
        s = raw.rstrip()
        if not s:
            continue
        if "already satisfied" in s:
            continue
        if s.startswith("Requirement already"):
            continue
        if "Downloading" in s or "Installing" in s or "Successfully" in s:
            line(f"  {C_DIM}{s[:100]}{C_END}")
    proc.wait()
    if proc.returncode != 0:
        fail("依赖安装失败，请运行「② 修复环境.cmd」重试")
        return False
    ok("依赖安装完成")
    return True


def check_ollama() -> None:
    exe = shutil.which("ollama")
    if not exe:
        for p in [
            Path.home() / "AppData/Local/Programs/Ollama/ollama.exe",
            Path("C:/Program Files/Ollama/ollama.exe"),
        ]:
            if p.exists():
                exe = str(p)
                break
    if exe:
        ok("Ollama 已安装")
        line(f"  {C_DIM}提示：首次学习时若缺模型，会自动下载{C_END}")
    else:
        warn("没有找到 Ollama")
        line(f"  {C_DIM}EvoMind 需要它来跑模型。下载地址：https://ollama.com/download{C_END}")
        line(f"  {C_DIM}装完记得回来跑一次：ollama pull qwen3.8:27b{C_END}")


def setup_shortcut() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import shortcut
    except Exception as e:  # noqa: BLE001
        warn(f"无法加载快捷方式模块：{e}")
        return

    if not shortcut.is_windows():
        warn("非 Windows 系统，跳过桌面图标")
        return

    exists = shortcut.exists()
    desktop = shortcut.get_desktop()

    line()
    line(f"  {C_B}桌面图标{C_END}")
    line(f"  {C_DIM}会在桌面放一个 EvoMind 图标，双击就能启动，不用每次进文件夹找{C_END}")
    line(f"  {C_DIM}桌面位置：{desktop}{C_END}")

    if exists:
        line()
        ok("桌面上已经有 EvoMind 图标了")
        if ask_yes("要删掉它吗？", default_yes=False):
            good, info = shortcut.remove()
            if good:
                ok("已删除桌面图标")
            else:
                warn(f"删除失败：{info}")
        else:
            ok("保留桌面图标")
        return

    line()
    if ask_yes("要在桌面创建 EvoMind 图标吗？", default_yes=True):
        good, info = shortcut.create()
        if good:
            ok("桌面图标已创建")
            line(f"  {C_DIM}位置：{info}{C_END}")
        else:
            warn(f"创建失败：{info}")
            line(f"  {C_DIM}不影响使用，直接双击「① 双击这里启动.cmd」也能启动{C_END}")
    else:
        ok("已跳过，不创建桌面图标")
        line(f"  {C_DIM}以后想要的话，双击「③ 桌面图标.cmd」随时能加{C_END}")


def done() -> None:
    line()
    line(f"{C_OK}  ────────────────────────────────────────────{C_END}")
    line(f"{C_OK}   安装完成{C_END}")
    line(f"{C_OK}  ────────────────────────────────────────────{C_END}")
    line()
    line(f"  下一步：双击 {C_B}① 双击这里启动.cmd{C_END}（或桌面图标）")
    line(f"  {C_DIM}浏览器会自动打开控制台，约 10 秒{C_END}")
    line()
    line(f"  {C_DIM}不熟悉怎么用？双击「使用说明.html」，用浏览器打开有图有步骤{C_END}")
    line()


def main() -> int:
    banner()

    step("检查 Python")
    if not check_python():
        line("\n  请先安装 Python 3.10+：https://www.python.org/downloads/")
        return 1

    step("准备运行环境")
    if not ensure_venv():
        return 1
    if not install_deps():
        return 1

    step("检查模型后端")
    check_ollama()

    setup_shortcut()

    done()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        line("\n\n  已取消。")
        sys.exit(130)
