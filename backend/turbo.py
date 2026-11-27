"""狂暴模式：挂机时把检索 / 抓取 / 提炼的并发和节奏全部拉满。

开关就是配置项 ``turbo_mode``。打开后：

* 学习引擎每轮的检索式、抓取数量翻倍，检索 / 抓取 / 模型提炼的线程池放大；
* 书库导入的提炼并发放大；
* 持续自学两段之间的空闲间隔压到 1 秒；
* Ollama 请求带 ``keep_alive=-1``，模型常驻显存，不会因 5 分钟空闲被卸载再重载；
* Windows 上阻止系统睡眠（屏幕仍可关闭），并把本进程与 Ollama 进程调到「高于正常」优先级。

关闭后全部回到原来的数值，系统睡眠与优先级也恢复。
"""
from __future__ import annotations

import os
import threading

from . import config

#: 普通模式数值 → 狂暴模式数值。键名是调用处用的「旋钮」名。
_WORKERS_TURBO = {
    # 检索不放大：搜索引擎按频率限流，实测并发翻倍后「全部检索源返回空」更频繁，
    # 冷却时间被越拉越长，整体反而更慢。
    "search": 4,      # 并发检索式（与普通一致）
    "fetch": 12,      # 并发抓取网页（普通 4）
    "extract": 4,     # 并发调用大模型提炼（普通 2）
    "library": 4,     # 书库导入并发提炼（普通 2）
}

#: 每轮数量类配置：狂暴模式下翻倍，但不超过配置校验允许的上限。
_PER_ROUND_CAP = {"fetch_per_round": 50, "media_per_round": 6}  # 检索式条数不翻倍（同上）


def on() -> bool:
    return bool(config.get("turbo_mode", False))


def workers(kind: str, normal: int) -> int:
    """线程池大小：普通模式返回 normal，狂暴模式返回放大值（不会比 normal 小）。"""
    if not on():
        return normal
    return max(normal, _WORKERS_TURBO.get(kind, normal))


def per_round(key: str, default: int) -> int:
    """每轮数量：读用户配置，狂暴模式下翻倍并封顶。"""
    base = int(config.get(key, default))
    if not on() or base <= 0 or key not in _PER_ROUND_CAP:
        return base
    return max(base, min(_PER_ROUND_CAP.get(key, base * 2), base * 2))


def cpu_ocr_wanted() -> bool:
    return on() and bool(config.get("turbo_cpu_ocr", True))


def idle_seconds(normal: int) -> int:
    return 1 if on() else normal


def keep_alive() -> int | None:
    """Ollama keep_alive：狂暴模式下 -1 = 常驻；普通模式不传，用 Ollama 默认 5 分钟。"""
    return -1 if on() else None


# --------------------------------------------------------------------------
# 系统层：防睡眠 + 进程优先级（仅 Windows）
# --------------------------------------------------------------------------
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_NORMAL = 0x20
_ABOVE_NORMAL = 0x8000
_OLLAMA_NAMES = ("ollama.exe", "ollama_llama_server.exe", "llama-server.exe", "ollama app.exe")

_lock = threading.Lock()
_awake: dict = {"thread": None, "stop": None}
_applied = {"value": None}


def _keep_awake_loop(stop: threading.Event) -> None:
    # SetThreadExecutionState 的 ES_CONTINUOUS 状态绑定在调用线程上：线程一结束
    # 就失效。所以用一个专属线程持有它，而不是在随手的请求线程里调一次。
    import ctypes

    k32 = ctypes.windll.kernel32
    k32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
    try:
        stop.wait()
    finally:
        k32.SetThreadExecutionState(_ES_CONTINUOUS)


def _set_keep_awake(enable: bool) -> None:
    if os.name != "nt":
        return
    th, stop = _awake["thread"], _awake["stop"]
    if enable:
        if th is not None and th.is_alive():
            return
        stop = threading.Event()
        th = threading.Thread(target=_keep_awake_loop, args=(stop,), daemon=True,
                              name="turbo-keep-awake")
        _awake.update(thread=th, stop=stop)
        th.start()
    elif stop is not None:
        stop.set()
        if th is not None:
            th.join(timeout=2)
        _awake.update(thread=None, stop=None)


def _set_priority(enable: bool) -> list[str]:
    """调整本进程与 Ollama 进程的优先级，返回成功调整的进程名。"""
    if os.name != "nt":
        return []
    cls = _ABOVE_NORMAL if enable else _NORMAL
    done: list[str] = []
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        if k32.SetPriorityClass(k32.GetCurrentProcess(), cls):
            done.append("EvoMind")
    except Exception:  # noqa: BLE001
        pass
    try:
        import psutil  # 可选依赖：没装就只调本进程
    except Exception:  # noqa: BLE001
        return done
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() in _OLLAMA_NAMES:
                p.nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS if enable
                       else psutil.NORMAL_PRIORITY_CLASS)
                done.append(p.info["name"])
        except Exception:  # noqa: BLE001
            continue
    return done


def apply() -> dict:
    """把系统层状态同步到当前开关。重复调用是幂等的。"""
    enable = on()
    from . import cpu_ocr

    if cpu_ocr_wanted():
        cpu_ocr.start()
    else:
        cpu_ocr.stop()
    with _lock:
        _set_keep_awake(enable)
        boosted = _set_priority(enable) if _applied["value"] != enable or enable else []
        _applied["value"] = enable
    return {"boosted": boosted}


def set_enabled(enable: bool) -> dict:
    config.update({"turbo_mode": bool(enable)})
    info = apply()
    return state() | info


def table() -> list[dict]:
    """普通 vs 狂暴的对照表（给界面说明面板用）。"""
    idle = max(1, min(30, int(config.get("idle_seconds", 15))))

    def boost(key, default):
        base = int(config.get(key, default))
        if base <= 0 or key not in _PER_ROUND_CAP:
            return base
        return max(base, min(_PER_ROUND_CAP[key], base * 2))

    rows = [
        ("同时搜索几条检索式", 4, max(4, _WORKERS_TURBO["search"]), "路"),
        ("同时抓取几个网页", 4, max(4, _WORKERS_TURBO["fetch"]), "路"),
        ("同时让大模型提炼几段", 2, max(2, _WORKERS_TURBO["extract"]), "路"),
        ("书库导入同时提炼几段", 2, max(2, _WORKERS_TURBO["library"]), "路"),
        ("每轮生成检索式", int(config.get("queries_per_round", 4)),
         boost("queries_per_round", 4), "条"),
        ("每轮最多读资料", int(config.get("fetch_per_round", 8)),
         boost("fetch_per_round", 8), "篇"),
        ("两段自学之间歇多久", idle, 1, "秒"),
    ]
    out = [{"name": n, "normal": f"{a} {u}", "turbo": f"{b} {u}"} for n, a, b, u in rows]
    out += [
        {"name": "模型留在显存", "normal": "空闲 5 分钟卸载", "turbo": "一直常驻"},
        {"name": "电脑睡眠", "normal": "照常", "turbo": "阻止（屏幕可熄）"},
        {"name": "进程优先级", "normal": "正常", "turbo": "高于正常"},
        {"name": "CPU 后台识别扫描书", "normal": "不做",
         "turbo": "4 路并行" if config.get("turbo_cpu_ocr", True) else "已关闭"},
    ]
    return out


def state() -> dict:
    enable = on()
    return {
        "table": table(),
        "enabled": enable,
        "keep_awake": bool(_awake["thread"] and _awake["thread"].is_alive()),
        "workers": {k: workers(k, n) for k, n in
                    (("search", 4), ("fetch", 4), ("extract", 2), ("library", 2))},
        "per_round": {k: per_round(k, d) for k, d in
                      (("queries_per_round", 4), ("fetch_per_round", 8),
                       ("media_per_round", 2))},
        "idle_seconds": idle_seconds(int(config.get("idle_seconds", 15))),
        "keep_alive": keep_alive(),
        "cpu_ocr": _cpu_ocr_state(),
    }


def _cpu_ocr_state() -> dict:
    from . import cpu_ocr

    return cpu_ocr.state()
