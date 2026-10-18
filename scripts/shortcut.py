"""桌面快捷方式的创建与删除。

Windows 的 .lnk 没有纯 Python 标准库的创建方式，这里通过生成一个
带 UTF-8 BOM 的 PowerShell 脚本并执行来实现 —— BOM 是为了让
Windows PowerShell 5.1 正确读取含中文的路径（否则会乱码）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "assets" / "icon.ico"
LAUNCHER = ROOT / "① 双击这里启动.cmd"
LINK_NAME = "EvoMind.lnk"

_PS_TEMPLATE = """$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut('{lnk}')
$s.TargetPath = '{target}'
$s.WorkingDirectory = '{workdir}'
if (Test-Path '{icon}') {{ $s.IconLocation = '{icon},0' }}
$s.Description = 'EvoMind - 自进化学习引擎'
$s.WindowStyle = 1
$s.Save()
Write-Output 'OK'
"""


def is_windows() -> bool:
    return os.name == "nt"


def get_desktop() -> Path:
    """解析真实的桌面目录（用户的桌面可能被重定向到别的盘）。"""
    if is_windows():
        # 1) 注册表 User Shell Folders —— 最准确，能反映重定向
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            val, _ = winreg.QueryValueEx(key, "Desktop")
            winreg.CloseKey(key)
            expanded = os.path.expandvars(val)
            p = Path(expanded)
            if p.exists():
                return p
        except Exception:
            pass
        # 2) SHGetFolderPathW
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf)
            if buf.value:
                p = Path(buf.value)
                if p.exists():
                    return p
        except Exception:
            pass
    return Path.home() / "Desktop"


def link_path() -> Path:
    return get_desktop() / LINK_NAME


def exists() -> bool:
    try:
        return link_path().exists()
    except Exception:
        return False


def create(quiet: bool = False) -> tuple[bool, str]:
    """创建桌面快捷方式。返回 (成功, 说明)。"""
    if not is_windows():
        return False, "仅支持 Windows"
    if not LAUNCHER.exists():
        return False, f"找不到启动器：{LAUNCHER.name}"

    lnk = link_path()
    lnk.parent.mkdir(parents=True, exist_ok=True)

    script = _PS_TEMPLATE.format(
        lnk=str(lnk).replace("'", "''"),
        target=str(LAUNCHER).replace("'", "''"),
        workdir=str(ROOT).replace("'", "''"),
        icon=str(ICON).replace("'", "''"),
    )

    tmp = Path(tempfile.gettempdir()) / "evomind_shortcut.ps1"
    # UTF-8 with BOM：Windows PowerShell 5.1 靠 BOM 判断编码
    tmp.write_text(script, encoding="utf-8-sig")

    try:
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-File", str(tmp),
            ],
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
            timeout=60,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"执行失败：{e}"
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass

    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        return False, err[-1] if err else "未知错误"

    if not lnk.exists():
        return False, "脚本执行完成但快捷方式未生成"

    return True, str(lnk)


def remove() -> tuple[bool, str]:
    lnk = link_path()
    if not lnk.exists():
        return False, "桌面上没有 EvoMind 快捷方式"
    try:
        lnk.unlink()
        return True, str(lnk)
    except Exception as e:  # noqa: BLE001
        return False, str(e)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "create":
        print(create())
    elif action == "remove":
        print(remove())
    else:
        print(f"桌面目录：{get_desktop()}")
        print(f"快捷方式：{link_path()}  存在={exists()}")
