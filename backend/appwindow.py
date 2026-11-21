"""独立应用窗口：用系统自带的 Edge / Chrome「应用模式」打开控制台。

`--app=URL` 打开的是没有地址栏、标签页和书签栏的独立窗口，任务栏里是单独的图标，
看起来和桌面程序一样；不需要额外安装 pywebview / WebView2 运行时之类的依赖
（Windows 10/11 自带 Edge）。

窗口用独立的用户数据目录（data/appwindow）启动，与用户日常浏览器互不干扰；
`wait_closed()` 盯住该目录的锁文件判断窗口何时关闭，调用方据此退出服务——「关窗口 = 退出程序」。
找不到 Edge / Chrome 时返回 None，调用方退回普通浏览器打开。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

#: 环境变量 EVOMIND_UI=browser 时恢复「在默认浏览器里打开」的旧行为
UI_ENV = "EVOMIND_UI"


def wants_window() -> bool:
    return os.environ.get(UI_ENV, "window").strip().lower() != "browser"


def _registry_app_path(exe: str) -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    key = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key) as k:
                value, _ = winreg.QueryValueEx(k, None)
                if value and Path(value).exists():
                    return str(value)
        except OSError:
            continue
    return None


def find_browser() -> str | None:
    """优先 Edge（Windows 必带），其次 Chrome；都没有返回 None。"""
    env = os.environ
    candidates: list[Path] = []
    for base in (env.get("ProgramFiles(x86)"), env.get("ProgramFiles"), env.get("LOCALAPPDATA")):
        if base:
            candidates.append(Path(base) / "Microsoft/Edge/Application/msedge.exe")
    for base in (env.get("ProgramFiles"), env.get("ProgramFiles(x86)"), env.get("LOCALAPPDATA")):
        if base:
            candidates.append(Path(base) / "Google/Chrome/Application/chrome.exe")
    for c in candidates:
        if c.exists():
            return str(c)
    for exe in ("msedge.exe", "chrome.exe"):
        found = _registry_app_path(exe) or shutil.which(exe)
        if found:
            return found
    return None


def open_window(url: str, profile_dir: Path, *, width: int = 1440, height: int = 920,
                browser: str | None = None) -> subprocess.Popen | None:
    """以应用模式打开独立窗口，返回浏览器进程；找不到浏览器或启动失败返回 None。"""
    exe = browser or find_browser()
    if not exe:
        return None
    profile_dir.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        f"--window-size={width},{height}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        # Edge 的「启动增强」会让进程在窗口关闭后常驻后台，wait() 就等不到退出
        "--disable-features=msEdgeStartupBoost,msImplicitSignin",
        "--disable-background-mode",
    ]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags)
    except OSError:
        return None


def _locked(lock: Path) -> bool:
    """Chromium 运行期间独占持有 <user-data-dir>/lockfile（关闭时自动删除）。

    文件不存在 = 没在运行；存在但能删掉 = 上次崩溃留下的陈旧锁，同样视为没在运行。
    """
    if not lock.exists():
        return False
    try:
        lock.unlink()
        return False
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


def wait_closed(profile_dir: Path, *, start_timeout: float = 20.0, poll: float = 1.0,
                sleep=time.sleep) -> bool:
    """阻塞到应用窗口关闭；窗口在 start_timeout 内没起来返回 False。

    不能直接 `Popen.wait()`：Edge / Chrome 的启动进程只是跳板，拉起真正的浏览器
    进程后立刻退出（实测 Edge 8 秒内即返回 0），等它等于没等。改为盯住配置目录的锁文件。
    """
    lock = profile_dir / "lockfile"
    waited = 0.0
    while not _locked(lock):
        if waited >= start_timeout:
            return False
        sleep(0.25)
        waited += 0.25
    while _locked(lock):
        sleep(poll)
    return True
