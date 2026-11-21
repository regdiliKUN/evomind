"""Continuous learning with a 1–30 second idle deadline and heavy-task awareness."""
from __future__ import annotations
import threading
import time
from . import config, evolve, memory, tasks

_lock = threading.RLock()
_state = {"enabled": False, "last_run": 0., "next_run": 0., "runs": 0,
          "thread": None, "stop": threading.Event(), "waiting": ""}
TICK = 1


def _loop() -> None:
    while not _state["stop"].is_set():
        try:
            _tick()
        except Exception as e:
            memory.log_event("warn", f"调度器异常：{e}")
        delay = max(.05, min(TICK, (_state["next_run"] or time.time() + TICK) - time.time()))
        _state["stop"].wait(delay)


def interval() -> int:
    return max(1, min(30, int(config.get("idle_seconds", 15))))


def _tick() -> None:
    with _lock:
        if not _state["enabled"]:
            return
        reason = tasks.busy_reason()
        if reason:
            _state.update(next_run=0., waiting=reason)
            return
        _state["waiting"] = ""
        direction = config.get("last_direction") or ""
        if not direction:
            return
        now = time.time()
        last_end = max(_state["last_run"], evolve.engine.state.get("finished_at", 0))
        due = last_end + interval() if last_end else now
        _state["next_run"] = due
        if now < due:
            return
        res = evolve.engine.start(direction, int(config.get("auto_rounds", 2)))
        _state["last_run"] = now
        if res.get("ok"):
            _state["runs"] += 1
            _state["next_run"] = 0.
            evolve.bus.publish({"level": "ok", "msg": f"持续自学：第 {_state['runs']} 次学习"})
        else:
            _state.update(next_run=now + interval(), waiting=res.get("msg", ""))


def enable(direction: str | None = None) -> dict:
    with _lock:
        if direction:
            config.update({"last_direction": direction})
        if not _state["enabled"]:
            _state.update(enabled=True, last_run=0., next_run=time.time())
        if _state["thread"] is None or not _state["thread"].is_alive():
            _state["stop"].clear()
            _state["thread"] = threading.Thread(target=_loop, daemon=True)
            _state["thread"].start()
    return state()


def disable() -> dict:
    with _lock:
        _state.update(enabled=False, next_run=0., waiting="")
    return state()


def state() -> dict:
    with _lock:
        return {k: _state[k] for k in ("enabled", "runs", "last_run", "next_run", "waiting")} | {
            "idle_seconds": interval(), "idle_minutes": interval() / 60,
            "auto_rounds": config.get("auto_rounds", 2),
            "direction": config.get("last_direction", ""),
        }


def start_auto_if_needed() -> None:
    if config.get("continuous_learning"):
        enable()
