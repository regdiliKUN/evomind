"""Continuous learning with a 1–30 second idle deadline and heavy-task awareness."""
from __future__ import annotations
import threading
import time
from . import config, evolve, memory, tasks

_lock = threading.RLock()
_state = {"enabled": False, "last_run": 0., "next_run": 0., "runs": 0,
          "thread": None, "stop": threading.Event(), "waiting": ""}
TICK = 1


def _loop(stop: threading.Event | None = None) -> None:
    # 每个调度线程只认自己启动时拿到的那个 Event：disable() 给旧线程发的停止
    # 信号不能被随后 enable() 起的新线程误读，反之亦然。
    stop = stop or _state["stop"]
    while not stop.is_set():
        try:
            _tick()
        except Exception as e:
            memory.log_event("warn", f"调度器异常：{e}")
        delay = max(.05, min(TICK, (_state["next_run"] or time.time() + TICK) - time.time()))
        stop.wait(delay)


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
        th = _state["thread"]
        # 「线程还活着」不等于「线程还会继续跑」：disable() 已 set 停止信号、正在锁外
        # join 时，旧线程只是还没退出。原来此时什么都不做，旧线程随后退出、disable()
        # 再把 thread 置空——开关显示已开启，调度却永久停摆。凡是停止信号已发出，
        # 就换一个新 Event 起新线程；旧线程拿着旧 Event 自行退出。
        if th is None or not th.is_alive() or _state["stop"].is_set():
            stop = threading.Event()
            _state["stop"] = stop
            _state["thread"] = threading.Thread(target=_loop, args=(stop,), daemon=True)
            _state["thread"].start()
    return state()


def disable() -> dict:
    with _lock:
        _state.update(enabled=False, next_run=0., waiting="")
        # 只置 enabled=False 的话，daemon 线程仍每秒醒一次空转，且 stop
        # Event 在整个进程生命周期里从未被 set。这里显式停线程。
        _state["stop"].set()
        th = _state["thread"]
    # join 必须在锁外：_loop -> _tick() 会争抢同一把 RLock，持锁等待会死锁。
    if th is not None and th is not threading.current_thread():
        th.join(timeout=2)
    with _lock:
        # 只清理自己停掉的那个线程：join 期间若 enable() 已换上新线程，不能把它抹掉
        if th is not None and _state["thread"] is th and not th.is_alive():
            _state["thread"] = None
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
