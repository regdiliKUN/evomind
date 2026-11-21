"""桌面图标管理：随时加一个或删掉。

双击「③ 桌面图标.cmd」运行。会先告诉你现在有没有，再问你要怎么做。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

if os.name == "nt":
    os.system("")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

C_DIM = "\033[90m"
C_OK = "\033[92m"
C_WARN = "\033[93m"
C_B = "\033[96m"
C_END = "\033[0m"


def ask_yes(prompt: str, default_yes: bool = True) -> bool:
    hint = "[Y/n]" if default_yes else "[y/N]"
    try:
        ans = input(f"  {prompt} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default_yes
    if not ans:
        return default_yes
    return ans in ("y", "yes", "是", "1", "要", "好")


def main() -> int:
    import shortcut

    print()
    print(f"{C_B}  EvoMind · 桌面图标{C_END}")
    print()

    if not shortcut.is_windows():
        print("  仅支持 Windows。")
        return 1

    desktop = shortcut.get_desktop()
    lnk = shortcut.link_path()
    has = shortcut.exists()

    print(f"  {C_DIM}桌面位置：{desktop}{C_END}")
    if has:
        print(f"  {C_OK}✓{C_END} 当前状态：桌面上有 EvoMind 图标")
    else:
        print(f"  {C_WARN}!{C_END} 当前状态：桌面上没有图标")
    print()

    if has:
        if ask_yes("要删掉桌面上的图标吗？", default_yes=False):
            good, info = shortcut.remove()
            if good:
                print(f"\n  {C_OK}✓{C_END} 已删除：{info}")
            else:
                print(f"\n  {C_WARN}!{C_END} 删除失败：{info}")
        else:
            print(f"\n  {C_OK}✓{C_END} 保持原样，没有改动")
    else:
        if ask_yes("要在桌面创建 EvoMind 图标吗？", default_yes=True):
            good, info = shortcut.create()
            if good:
                print(f"\n  {C_OK}✓{C_END} 已创建：{info}")
                print(f"  {C_DIM}双击桌面上的图标就能启动{C_END}")
            else:
                print(f"\n  {C_WARN}!{C_END} 创建失败：{info}")
        else:
            print(f"\n  {C_OK}✓{C_END} 已跳过")

    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n  已取消。")
        sys.exit(130)
