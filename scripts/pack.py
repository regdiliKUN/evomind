"""EvoMind 打包分发：产出一个「绿色包」zip。

双击「⓸ 打包分发.cmd」，或在命令行跑：
    .venv\\Scripts\\python.exe scripts\\pack.py

打进去的是**能跑起来的最小完整集**：代码、前端、启动器、图标、文档、测试。
刻意不打进去的：
  .venv/        运行环境由收包人自己跑「⓪ 首次安装.cmd」生成（几百 MB，且与本机绑定）
  data/         知识库/模型/下载产物都是个人数据，不该随包走
  .git/         版本历史对使用者没有意义
  __pycache__/  编译缓存，跨机器可能不兼容

产出：
  dist/EvoMind-<日期>-<提交号>.zip        绿色包本体
  dist/EvoMind-<日期>-<提交号>.zip.sha256 校验值
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

if os.name == "nt":
    os.system("")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

C_DIM = "\033[90m"
C_OK = "\033[92m"
C_WARN = "\033[93m"
C_ERR = "\033[91m"
C_B = "\033[96m"
C_END = "\033[0m"

# 整目录打进去
INCLUDE_DIRS = ["backend", "web", "scripts", "assets", "tests"]
# 根目录下的单文件
INCLUDE_FILES = [
    "run.py",
    "evomind_launcher.py",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "使用说明.html",
    "性能优化报告.md",
    "⓪ 首次安装.cmd",
    "① 双击这里启动.cmd",
    "② 修复环境.cmd",
    "③ 桌面图标.cmd",
    "⓸ 打包分发.cmd",
]
# 任何层级命中即跳过
SKIP_DIR_NAMES = {
    "__pycache__", ".venv", "venv", "env", "data", ".git", ".idea", ".vscode",
    ".pytest_cache", ".tmpedge", "dist", "build", ".mypy_cache", ".ruff_cache",
}
SKIP_FILE_SUFFIX = {".pyc", ".pyo", ".log", ".tmp", ".db", ".db-wal", ".db-shm"}
# 基准对照产物（可重新生成，且体积大）
SKIP_FILE_NAMES = {"error.log", "desktop.ini", "Thumbs.db", ".DS_Store"}


def git(*args: str) -> str:
    try:
        r = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def iter_payload() -> list[Path]:
    """列出要打进包里的文件（相对 ROOT）。"""
    picked: list[Path] = []

    for name in INCLUDE_FILES:
        p = ROOT / name
        if p.is_file():
            picked.append(Path(name))

    for d in INCLUDE_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_dir():
                continue
            rel = p.relative_to(ROOT)
            if any(part in SKIP_DIR_NAMES for part in rel.parts[:-1]):
                continue
            if p.suffix.lower() in SKIP_FILE_SUFFIX:
                continue
            if p.name in SKIP_FILE_NAMES:
                continue
            # 基准的 before/after 对照 json 属于可再生产物
            if "bench" in rel.parts and p.suffix.lower() == ".json":
                continue
            picked.append(rel)

    # 运行时要用的空目录占位
    keep = ROOT / "data" / ".gitkeep"
    if keep.exists():
        picked.append(Path("data/.gitkeep"))

    return sorted(set(picked), key=lambda x: str(x).lower())


def build_notes(sha: str, subject: str, n_files: int, when: str) -> str:
    return f"""EvoMind · 自进化学习引擎 — 绿色包
========================================
构建时间：{when}
版本提交：{sha or '（非 git 目录）'}
本次内容：{subject or '—'}
文件数量：{n_files}

怎么用（三步）
----------------------------------------
1. 解压到任意目录（路径别带 # 和 % 之外的特殊符号即可）
2. 双击「⓪ 首次安装.cmd」——建虚拟环境、装依赖（只需一次，约 3-8 分钟）
3. 双击「① 双击这里启动.cmd」——浏览器会自动打开控制台

想放到桌面天天用：双击「③ 桌面图标.cmd」，会在桌面生成带图标的快捷方式。

包内还有什么
----------------------------------------
使用说明.html     完整功能说明（双击用浏览器打开，比 README 更直观）
README.md         项目说明与开发向文档
性能优化报告.md   十六轮性能优化的定位、方案与 24 轮 A/B 数据
tests/            离线回归测试，可自行运行：
                  ① .venv\\Scripts\\python.exe tests\\run_tests.py --fast
                  ② .venv\\Scripts\\python.exe -m unittest tests.test_regressions
                  ③ node --test tests/frontend_tests.cjs

前提条件
----------------------------------------
- Windows 10/11，Python 3.10+（安装时务必勾选 Add python.exe to PATH）
- Ollama（https://ollama.com/download）—— 学习/推理要用；不装也能开控制台，只是不能真正学习

包里没有的东西（故意的）
----------------------------------------
.venv/   与本机绑定的运行环境，请用「⓪ 首次安装.cmd」自己生成
data/    知识库、下载的资料、微调产出——都是个人数据，不随包分发
"""


def main() -> int:
    print()
    print(f"{C_B}  EvoMind · 打包分发{C_END}")
    print()

    if git("rev-parse", "--is-inside-work-tree") != "true":
        print(f"  {C_WARN}!{C_END} 当前目录不是 git 仓库，版本号将留空")
    sha = git("rev-parse", "--short", "HEAD")
    subject = git("log", "-1", "--pretty=%s")

    stamp = time.strftime("%Y-%m-%d")
    tag = f"{stamp}-{sha}" if sha else stamp
    DIST.mkdir(exist_ok=True)
    out = DIST / f"EvoMind-{tag}.zip"

    files = iter_payload()
    if not files:
        print(f"  {C_ERR}✗{C_END} 没找到要打包的文件，确认在项目根目录运行")
        return 1

    when = time.strftime("%Y-%m-%d %H:%M:%S")
    notes = build_notes(sha, subject, len(files), when)

    # 不压缩二进制已经压过的格式，省时间
    stored = {".png", ".ico", ".jpg", ".jpeg", ".zip", ".gz", ".whl"}
    raw_total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            src = ROOT / rel
            try:
                data = src.read_bytes()
            except Exception as e:  # noqa: BLE001
                print(f"  {C_WARN}!{C_END} 跳过 {rel}：{e}")
                continue
            raw_total += len(data)
            comp = zipfile.ZIP_STORED if src.suffix.lower() in stored else zipfile.ZIP_DEFLATED
            info = zipfile.ZipInfo(f"EvoMind/{rel.as_posix()}", time.localtime(src.stat().st_mtime)[:6])
            info.compress_type = comp
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
        z.writestr("EvoMind/包说明.txt", notes)
        z.writestr(
            "EvoMind/VERSION.txt",
            f"build_time={when}\ngit_sha={sha}\ngit_subject={subject}\nfiles={len(files)}\n",
        )

    size = out.stat().st_size
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (out.parent / (out.name + ".sha256")).write_text(
        f"{digest}  {out.name}\n", encoding="utf-8"
    )

    def mb(n: int) -> str:
        return f"{n / 1024 / 1024:.2f} MB"

    print(f"  {C_OK}✓{C_END} 打包完成")
    print()
    print(f"  {C_DIM}文件数　{len(files)} 个（另含 包说明.txt / VERSION.txt）{C_END}")
    print(f"  {C_DIM}原始体积{mb(raw_total):>10}{C_END}")
    print(f"  {C_DIM}压缩后　{mb(size):>10}{C_END}")
    print(f"  {C_DIM}压缩率　{(1 - size / raw_total) * 100 if raw_total else 0:>9.1f}%{C_END}")
    print()
    print(f"  产出：{out}")
    print(f"  SHA256：{digest}")
    print()
    print(f"  {C_DIM}收包人解压后跑「⓪ 首次安装.cmd」→「① 双击这里启动.cmd」即可{C_END}")
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n  已取消。")
        sys.exit(130)
