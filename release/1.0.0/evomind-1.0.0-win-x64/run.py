"""EvoMind 启动器：检查环境 → 拉起 Ollama → 启动控制台 → 打开浏览器。"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

CANDIDATE_MODEL_DIRS = [
    Path("I:/ollama/models"),
    Path("D:/ollama/models"),
    Path("E:/ollama/models"),
    Path.home() / ".ollama" / "models",
]


def log(msg: str) -> None:
    print(msg, flush=True)


def find_ollama_exe() -> str | None:
    import shutil

    exe = shutil.which("ollama")
    if exe:
        return exe
    for p in [
        Path.home() / "AppData/Local/Programs/Ollama/ollama.exe",
        Path("C:/Program Files/Ollama/ollama.exe"),
    ]:
        if p.exists():
            return str(p)
    return None


def detect_models_dir() -> str | None:
    """找到真正存着模型的目录。"""
    env = os.environ.get("OLLAMA_MODELS")
    if env and Path(env).exists():
        return env
    for d in CANDIDATE_MODEL_DIRS:
        if (d / "manifests").exists():
            return str(d)
    return None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def ensure_ollama() -> bool:
    from backend import llm

    if llm.tags(fresh=True)[0]:
        log("[OK] Ollama 已在运行")
        return True

    exe = find_ollama_exe()
    if not exe:
        log("[!] 没找到 Ollama，请先安装：https://ollama.com/download")
        return False

    env = dict(os.environ)
    md = detect_models_dir()
    if md:
        env["OLLAMA_MODELS"] = md
        log(f"[..] 模型目录：{md}")

    log("[..] 正在启动 Ollama 服务…")
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(
            [exe, "serve"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception as e:  # noqa: BLE001
        log(f"[!] 启动失败：{e}")
        return False

    for _ in range(40):
        time.sleep(0.5)
        if llm.healthy():
            log("[OK] Ollama 已就绪")
            return True
    log("[!] Ollama 启动超时")
    return False


# 候选端口：先 8765..8784，再备用段 18765..18784。
# Windows 的 Hyper-V / WSL / Docker 会把整段端口（如 8716-8815）划为保留，
# 段内端口没人监听、connect 探测显示「空闲」，但 bind 报 WinError 10013；
# 主段整段落在保留区时只靠顺延逃不出去，所以要有一段离得远的备用端口。
CANDIDATE_PORTS = [*range(8765, 8785), *range(18765, 18785)]


def can_bind(port: int, host: str = "127.0.0.1") -> bool:
    """真正 bind 一次：既排除已被占用的，也排除被系统保留的端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port() -> int:
    env = os.environ.get("EVOMIND_PORT", "").strip()
    if env.isdigit():
        return int(env)
    for p in CANDIDATE_PORTS:
        if can_bind(p):
            return p
    return CANDIDATE_PORTS[0]


def is_evomind(port: int) -> bool:
    """判断端口上跑的是不是 EvoMind 自己。"""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        return "stats" in data and "engine" in data
    except Exception:
        return False


def has_model(models: list[str], name: str) -> bool:
    """判断模型是否已安装，忽略 tag。

    ollama 列出来的是 `bge-m3:latest`，配置里写的是 `bge-m3`，
    直接字符串比较会误报「缺少模型」。
    """
    base = name.split(":")[0]
    return any(m == name or m.split(":")[0] == base for m in models)


def main() -> None:
    os.system("")  # 开启 Windows 控制台 ANSI
    print("=" * 58)
    print("  EvoMind · 自进化学习引擎")
    print("=" * 58)

    # 单实例保护：已经有实例在跑就不要再起一个。
    # 两个实例会共用同一个 SQLite，但内存里的任务锁互相看不见，
    # 结果就是 OCR 和导入能在两个进程里同时跑，互相抢 GPU、抢数据库。
    # 端口可能顺延或落到备用段，所以要遍历全部候选端口。
    running = next((p for p in CANDIDATE_PORTS
                    if port_open(p) and is_evomind(p)), None)
    if running:
        url = f"http://127.0.0.1:{running}"
        log("[i] EvoMind 已经在运行，直接打开现有窗口")
        log(f"    地址：{url}")
        try:
            if not os.environ.get("EVOMIND_NO_BROWSER"):
                webbrowser.open(url)
        except Exception:
            pass
        return

    if not ensure_ollama():
        log("\n无法连接 Ollama，控制台仍会启动，但学习功能不可用。")
        log("请手动运行 Ollama 后刷新页面。\n")

    from backend import config, llm

    cfg = config.load()
    log(f"[..] 推理模型：{cfg['model']}")

    if llm.healthy():
        models = [m["name"] for m in llm.list_models()]
        if not has_model(models, cfg["model"]):
            log(f"[..] 缺少 {cfg['model']}，首次学习时会自动下载")
        if not has_model(models, cfg["embed_model"]):
            log(f"[..] 缺少向量模型 {cfg['embed_model']}，首次学习时会自动下载")

    port = pick_port()
    url = f"http://127.0.0.1:{port}"
    log(f"\n[OK] 控制台地址：{url}")
    log("[..] 按 Ctrl+C 停止服务\n")

    try:
        if not os.environ.get("EVOMIND_NO_BROWSER"):
            webbrowser.open(url)
    except Exception:
        pass

    from backend.main import run

    try:
        run(port=port)
    except KeyboardInterrupt:
        log("\n已停止。")


if __name__ == "__main__":
    main()
