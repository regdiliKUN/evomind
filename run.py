"""EvoMind 启动器：检查环境 → 拉起 Ollama → 启动控制台 → 打开浏览器。"""
from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# detect_models_dir() 会优先读 OLLAMA_MODELS；这里只保留跨平台默认值，
# 不再携带开发机私有盘符（绿色包分发到别的机器后它们恒为无效路径）。
CANDIDATE_MODEL_DIRS = [
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


def bind_port(host: str = "127.0.0.1") -> socket.socket:
    """绑定并监听第一个可用端口，返回 socket（之后原样交给 uvicorn）。

    原来先「试绑再放开」挑端口，到 uvicorn 真正 bind 中间隔着二三十秒初始化，
    这段时间端口随时可能被别的进程（或同时启动的另一个实例）拿走 → WinError 10048。
    现在一次绑定、一直持有。真正 bind 也顺带排除了被系统保留的端口（WinError 10013）。
    """
    env = os.environ.get("EVOMIND_PORT", "").strip()
    ports = [int(env)] if env.isdigit() else CANDIDATE_PORTS
    last: OSError | None = None
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((host, p))
            s.listen(128)
            return s
        except OSError as e:
            s.close()
            last = e
    raise OSError(f"没有可用端口（试过 {ports[0]}…{ports[-1]}）：{last}")


#: 启动期间临时服务给出的响应头；前端据此区分「正在启动」页与真正的控制台。
SPLASH_HEADER = "X-EvoMind-Splash"


class SplashServer:
    """主服务初始化期间，在同一个已监听的 socket 上提供「正在启动」页。

    窗口因此能在双击后立刻打开（同源页面轮询，不受浏览器对 file:// 访问本机的限制）；
    主服务就绪时 stop() 停止接客，再把同一个 socket 交给 uvicorn，端口全程不松手，
    交接间隙里到达的连接在内核队列里排队，不会被拒绝。
    """

    def __init__(self, sock: socket.socket, html: bytes):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # 每个请求后关连接，交接时不留长连接

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/favicon"):
                    self.send_response(404)
                    self.send_header(SPLASH_HEADER, "1")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header(SPLASH_HEADER, "1")
                self.end_headers()
                self.wfile.write(html)

            def log_message(self, *args):  # 不往启动日志里刷访问记录
                pass

        self._httpd = HTTPServer(sock.getsockname(), Handler, bind_and_activate=False)
        self._httpd.socket.close()  # bind_and_activate=False 时它自建了一个未用的 socket
        self._httpd.socket = sock
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        daemon=True)

    def start(self) -> "SplashServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        """停止服务但不关闭 socket（server_close 会关，故不调用）。"""
        self._httpd.shutdown()
        self._thread.join(timeout=5)


def splash_html() -> bytes:
    try:
        return SPLASH.read_bytes()
    except OSError:
        return "<meta charset=utf-8><meta http-equiv=refresh content=2><p>EvoMind 正在启动…</p>".encode()


def is_starting(port: int) -> bool:
    """端口上是不是一个仍在「正在启动」阶段的 EvoMind。"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            return r.headers.get(SPLASH_HEADER) == "1"
    except Exception:
        return False


#: 单实例锁与端口登记。锁由操作系统持有，进程退出（含崩溃）时自动释放，不会残留。
LOCK_FILE = ROOT / "data" / "run.lock"
PORT_FILE = ROOT / "data" / "run.port"
SPLASH = ROOT / "web" / "loading.html"


def acquire_instance_lock(path: Path = LOCK_FILE):
    """非阻塞抢单实例锁：抢到返回需一直持有的文件对象，已有实例持锁返回 None。

    原来只靠「探测端口上有没有 EvoMind」判重：启动要二三十秒，期间谁都还没监听端口，
    连点几次 EXE 就会同时起多个实例，都选中 8765，抢输的报 WinError 10048。
    锁在启动最开始就拿，彻底消除这个时间窗。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def read_port_file(path: Path = PORT_FILE) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def handoff_to_running(port: int | None) -> None:
    """另一个实例已持锁：它要么已在服务，要么正在启动（「正在启动」窗口已经开着）。"""
    if port and port_open(port) and not is_starting(port):
        url = f"http://127.0.0.1:{port}"
        log("[i] EvoMind 已经在运行，直接打开现有窗口")
        log(f"    地址：{url}")
        if not os.environ.get("EVOMIND_NO_BROWSER"):
            open_ui(url)
        return
    log("[i] EvoMind 正在启动中，请稍候，窗口马上就会出现（无需重复点击）")


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否有人在用（试绑定，瞬间返回）。

    不用 connect 探测：Windows 上连接本机未监听端口要等满超时，
    逐个探 40 个候选端口光这一步就耗掉二十多秒。被系统保留（WinError 10013）不算占用。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return False
        except OSError as e:
            return e.errno == errno.EADDRINUSE or getattr(e, "winerror", None) == 10048


def find_legacy_instance() -> int | None:
    """找没有进程锁的旧版本实例（升级前启动、还在跑的那种）。"""
    return next((p for p in CANDIDATE_PORTS if port_in_use(p) and is_evomind(p)), None)


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


def open_ui(url: str):
    """独立窗口（Edge/Chrome 应用模式）打开控制台；不可用或 EVOMIND_UI=browser 时用默认浏览器。

    返回窗口进程（可 wait 到用户关窗）；退回浏览器时返回 None。
    """
    from backend import appwindow, config

    if appwindow.wants_window():
        # 每次启动带一个新的查询参数：应用窗口的独立配置目录里可能缓存着旧版首页
        # （旧版服务端没给 "/" 发 Cache-Control），换个缓存键就绕开它，升级后重启即见新界面。
        sep = "&" if "?" in url else "?"
        proc = appwindow.open_window(f"{url}{sep}_={int(time.time())}", config.DATA / "appwindow")
        if proc is not None:
            log("[OK] 已打开 EvoMind 窗口（关闭窗口即退出）")
            return proc
        log("[i] 没找到 Edge / Chrome，改用默认浏览器打开")
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    return None


def _ui_when_ready(server, port: int, url: str) -> None:
    """立即打开窗口（端口上此时是「正在启动」页，主服务就绪后页面自动刷新成控制台）；
    用户关掉窗口后让服务优雅退出。

    原来要等服务起来才开窗口，双击后二三十秒毫无反应，用户以为没点上就连点。
    """
    proc = open_ui(url)
    if proc is None:
        return
    from backend import appwindow, config

    if not appwindow.wait_closed(config.DATA / "appwindow"):
        log("[i] 未检测到窗口启动，服务保持运行（Ctrl+C 停止）")
        return
    log("[i] 窗口已关闭，正在退出…")
    server.should_exit = True


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
    # 第一道：进程锁（覆盖「都还在启动、谁都没监听端口」的时间窗）。
    lock = acquire_instance_lock()
    if lock is None:
        handoff_to_running(read_port_file())
        return

    # 第二道：兼容没有进程锁的旧版本实例——探测端口。
    # 只探 8765 会漏判：pick_port() 在端口被占用时顺延到 8766..8784，
    # 于是第二个实例照样起来，和第一个抢同一份 SQLite 与 GPU。
    running = find_legacy_instance()
    if running:
        url = f"http://127.0.0.1:{running}"
        log("[i] EvoMind 已经在运行，直接打开现有窗口")
        log(f"    地址：{url}")
        if not os.environ.get("EVOMIND_NO_BROWSER"):
            open_ui(url)
        return

    # 端口一开始就绑定并一直持有，并登记下来供后来的实例找到我们；
    # 主服务初始化期间先在这个端口上提供「正在启动」页，窗口立刻就能打开。
    try:
        sock = bind_port()
    except OSError as e:
        log(f"[!] {e}")
        lock.close()
        sys.exit(1)
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    try:
        PORT_FILE.write_text(str(port), encoding="utf-8")
    except OSError:
        pass
    splash = SplashServer(sock, splash_html()).start()

    import uvicorn

    # Server 只是持有配置的对象，此时还不导入 backend.main（那一步最慢）。
    server = uvicorn.Server(uvicorn.Config("backend.main:app", host="127.0.0.1", port=port,
                                           log_level="warning"))
    if not os.environ.get("EVOMIND_NO_BROWSER"):
        threading.Thread(target=_ui_when_ready, args=(server, port, url), daemon=True).start()

    try:
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

        # 提前导入应用（最慢的一步），让「正在启动」页尽量撑到主服务能立即接客为止。
        server.config.load()
        log(f"\n[OK] 控制台地址：{url}")
        log("[..] 按 Ctrl+C 停止服务\n")
        splash.stop()
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        splash.stop()
        sock.close()
        try:
            PORT_FILE.unlink()
        except OSError:
            pass
        lock.close()
    log("\n已停止。")


if __name__ == "__main__":
    main()
