"""持续自学调度器。

开启后，引擎空闲达到设定间隔就自动发起下一轮学习，沿用上次的方向，
并在每次自动学习前重新判断该往哪个子方向深挖。
这样它就能在你不盯着的时候自己慢慢长大。
"""
from __future__ import annotations

import threading
import time

from . import config, evolve, memory

_state = {
    "enabled": False,
    "last_run": 0.0,
    "next_run": 0.0,
    "runs": 0,
    "thread": None,
    "stop": threading.Event(),
}

TICK = 30  # 每 30 秒检查一次


def _loop() -> None:
    while not _state["stop"].is_set():
        try:
            _tick()
        except Exception as e:  # noqa: BLE001
            memory.log_event("warn", f"调度器异常：{e}")
        _state["stop"].wait(TICK)


def _tick() -> None:
    if not _state["enabled"]:
        return
    if evolve.engine.state.get("running"):
        return

    direction = config.get("last_direction") or ""
    if not direction:
        return

    idle_min = float(config.get("idle_minutes", 20))
    gap = max(idle_min * 60, 60)  # 至少间隔 60 秒，避免疯狂循环
    now = time.time()
    # 上一轮结束后至少空闲 gap 秒
    last_end = _state["last_run"] or 0.0
    if last_end and (now - last_end) < gap:
        _state["next_run"] = last_end + gap
        return

    rounds = int(config.get("auto_rounds", 2))
    _state["last_run"] = now
    _state["runs"] += 1
    evolve.bus.publish(
        {
            "level": "ok",
            "msg": f"持续自学：自动发起第 {_state['runs']} 次学习（{rounds} 轮）",
        }
    )
    res = evolve.engine.start(direction, rounds)
    if not res.get("ok"):
        memory.log_event("warn", f"自动学习启动失败：{res.get('msg')}")


def enable(direction: str | None = None) -> dict:
    if direction:
        config.update({"last_direction": direction})
    _state["enabled"] = True
    _state["last_run"] = 0.0
    if _state["thread"] is None or not _state["thread"].is_alive():
        _state["stop"].clear()
        _state["thread"] = threading.Thread(target=_loop, daemon=True)
        _state["thread"].start()
    evolve.bus.publish({"level": "ok", "msg": "持续自学已开启"})
    return state()


def disable() -> dict:
    _state["enabled"] = False
    evolve.bus.publish({"level": "info", "msg": "持续自学已关闭"})
    return state()


def state() -> dict:
    return {
        "enabled": _state["enabled"],
        "runs": _state["runs"],
        "last_run": _state["last_run"],
        "next_run": _state["next_run"],
        "idle_minutes": config.get("idle_minutes", 20),
        "direction": config.get("last_direction", ""),
    }


def start_auto_if_needed() -> None:
    """进程启动时如果配置里开着持续自学，就恢复它。"""
    if config.get("continuous_learning"):
        enable()
