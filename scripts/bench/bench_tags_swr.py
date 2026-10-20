"""第十三轮 A/B：`/api/tags` 从「同步 TTL 探测」改为「先返回、后台刷新 + 失败退避」。

对照口径
--------
旧实现（旧 `tags()` / `list_models()`，逐字复刻）：TTL 一到就**同步**打一次
`/api/tags`，调用方（`/api/status`、`/api/models`）等它返回才继续。
新实现（生产 `llm.tags()`）：永远返回最近一次结果，过期只在后台线程刷新，
失败按指数退避（8s→16s→32s→60s），调用方不再被远端往返阻塞。

远端用桩模拟：RTT 120ms（真实 Ollama 在模型较多时 `/api/tags` 就是这个量级；
Ollama 没启动时本机实测是 2 秒才失败，为了把 24 轮控制在可跑时间内按比例缩小）。

每轮做 8 次「TTL 已到期」的调用（等价于真实使用里每 8 秒撞上的那一次轮询），
这是旧实现的阻塞上界，也是新实现要干掉的那部分。
"""
from __future__ import annotations

import statistics
import sys
import time

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")

import _env  # noqa: E402

_env.isolate()
_env.fake_llm()

from backend import llm  # noqa: E402

ROUNDS = 24
CALLS_PER_ROUND = 8
RTT = 0.12  # 秒，模拟远端往返

PAYLOAD = {
    "models": [
        {"name": "qwen2.5:14b", "size": 9_000_000_000,
         "details": {"parameter_size": "14B", "quantization_level": "Q4_K_M"},
         "capabilities": ["completion", "tools"]},
        {"name": "bge-m3:latest", "size": 1_200_000_000,
         "details": {"parameter_size": "568M", "quantization_level": "F16"},
         "capabilities": ["embedding"]},
    ]
}


class _Resp:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


class _Remote:
    def __init__(self) -> None:
        self.down = False
        self.calls = 0

    def client(self, *_a, **_kw):
        remote = self

        class _C:
            def get(self, path, timeout=None):  # noqa: ARG002
                remote.calls += 1
                time.sleep(RTT)
                if remote.down:
                    raise ConnectionError("connection refused")
                return _Resp(PAYLOAD)

        return _C()


remote = _Remote()
llm._client = remote.client  # type: ignore[assignment]

# ---------------- 旧实现（逐字复刻改动前行为） ----------------
_old_cache = {"ts": 0.0, "healthy": False, "models": []}


def old_tags():
    now = time.monotonic()
    if now - _old_cache["ts"] < llm.TAGS_TTL:
        return _old_cache["healthy"], [dict(m) for m in _old_cache["models"]]
    try:
        r = llm._client().get("/api/tags", timeout=10)
        if r.status_code != 200:
            _old_cache.update(ts=now, healthy=False, models=[])
            return False, []
        models = llm._parse_tags(r.json())
    except Exception:
        _old_cache.update(ts=now, healthy=False, models=[])
        return False, []
    _old_cache.update(ts=now, healthy=True, models=models)
    return True, [dict(m) for m in models]


def old_list_models():
    try:
        r = llm._client().get("/api/tags", timeout=10)
        r.raise_for_status()
        return llm._parse_tags(r.json())
    except Exception:
        return []


# ---------------- 新实现（生产代码） ----------------
def new_tags():
    return llm.tags()


def new_list_models():
    return llm.list_models()


def reset_old() -> None:
    _old_cache.update(ts=0.0, healthy=False, models=[])


def reset_new() -> None:
    llm._invalidate_tags()
    with llm._tags_lock:
        llm._tags_cache.update(
            probed=False, healthy=False, models=[], ts=0.0,
            refreshing=False, fail_streak=0, next_try=0.0,
        )


def expire_old() -> None:
    """把旧实现的 TTL 拨到「已过期」——真实场景里每 8 秒就是这一次轮询。"""
    _old_cache["ts"] = 0.0


def expire_new() -> None:
    with llm._tags_lock:
        llm._tags_cache["ts"] = 0.0
        llm._tags_cache["next_try"] = 0.0


def drain(seconds: float = RTT + 0.06) -> None:
    time.sleep(seconds)


def measure(fn_tags, expire, reset) -> tuple[float, float, int]:
    """返回 (阻塞总毫秒, 单次最大毫秒, 远端实际请求数)。"""
    reset()
    fn_tags()          # 首次：新实现是「立刻返回 + 后台探测」，旧实现直接阻塞一次
    drain()
    remote.calls = 0
    blocked = 0.0
    worst = 0.0
    for _ in range(CALLS_PER_ROUND):
        expire()
        t0 = time.perf_counter()
        fn_tags()
        ms = (time.perf_counter() - t0) * 1000
        blocked += ms
        worst = max(worst, ms)
        time.sleep(0.005)
    drain()
    return blocked, worst, remote.calls


print(f"桩：RTT={RTT * 1000:.0f}ms，每轮 {CALLS_PER_ROUND} 次「TTL 到期」调用，"
      f"新旧交替 {ROUNDS} 轮\n")

rows = []
for i in range(ROUNDS):
    if i % 2 == 0:
        b, w, c = measure(old_tags, expire_old, reset_old)
        rows.append(("old", b, w, c))
        print(f"  第 {i + 1:2d} 轮 旧实现  阻塞 {b:7.1f} ms  最慢 {w:6.1f} ms  请求 {c}")
    else:
        b, w, c = measure(new_tags, expire_new, reset_new)
        rows.append(("new", b, w, c))
        print(f"  第 {i + 1:2d} 轮 新实现  阻塞 {b:7.1f} ms  最慢 {w:6.1f} ms  请求 {c}")

old_b = [r[1] for r in rows if r[0] == "old"]
new_b = [r[1] for r in rows if r[0] == "new"]
old_w = [r[2] for r in rows if r[0] == "old"]
new_w = [r[2] for r in rows if r[0] == "new"]

print("\n---- 汇总 ----")
print(f"旧实现：中位阻塞 {statistics.median(old_b):7.1f} ms / 轮，"
      f"单次最慢中位 {statistics.median(old_w):6.1f} ms")
print(f"新实现：中位阻塞 {statistics.median(new_b):7.1f} ms / 轮，"
      f"单次最慢中位 {statistics.median(new_w):6.1f} ms")
ratio = statistics.median(old_b) / max(statistics.median(new_b), 1e-6)
print(f"加速比：{ratio:.1f}x")
wins = sum(1 for o, n in zip(old_b, new_b) if n < o)
print(f"更快轮次：{wins}/{len(old_b)}")

# ------------------------- 等价性 -------------------------
print("\n---- 等价性 ----")
fails = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {name}{('  ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


def settled_new():
    """新实现是异步的：触发一次探测，等后台线程落地后再取结果。"""
    llm.tags()
    drain(0.35)
    return llm.tags()


# ① 远端健康：两者拿到完全相同的模型清单
reset_old()
reset_new()
o = old_tags()
n = settled_new()
check("远端健康时健康状态一致", o[0] is True and n[0] is True, f"old={o[0]} new={n[0]}")
check("远端健康时模型清单逐项一致",
      [m["name"] for m in o[1]] == [m["name"] for m in n[1]],
      f"{[m['name'] for m in n[1]]}")

# ② /api/models 与 /api/status 用同一份数据（list_models 不再单独往返）
remote.calls = 0
lm = new_list_models()
check("list_models() 不再额外请求远端", remote.calls == 0, f"calls={remote.calls}")
check("list_models() 与 tags() 同源", [m["name"] for m in lm] == [m["name"] for m in n[1]])

# ③ 远端挂掉：两者都判定为离线；新实现保留上一次的模型清单（界面不闪空）
remote.down = True
with llm._tags_lock:
    llm._tags_cache.update(ts=0.0, next_try=0.0)
n_down = settled_new()
check("远端挂掉时判为离线", n_down[0] is False)
check("挂掉时保留上次的模型清单（不清空下拉）", len(n_down[1]) == len(PAYLOAD["models"]),
      f"{len(n_down[1])} 个")
remote.down = False

# ④ 远端恢复：新实现最多一个刷新周期就自愈
with llm._tags_lock:
    llm._tags_cache.update(ts=0.0, next_try=0.0)
check("远端恢复后自愈为在线", settled_new()[0] is True)

# ⑤ 失败退避真的生效：连续失败时远端请求次数被压住
remote.down = True
reset_new()
llm.tags()                     # 首次触发探测
drain(0.3)
llm.tags()                     # 第二次：探测已失败，进入退避
drain(0.3)
remote.calls = 0
for _ in range(6):
    llm.tags()                 # 退避期内反复调用
    time.sleep(0.02)
drain(0.25)
check("退避期内不重复探测", remote.calls <= 1, f"calls={remote.calls}")
remote.down = False

print("\n" + ("全部等价性检查通过" if not fails else f"{len(fails)} 项不通过：{fails}"))
sys.exit(0 if not fails else 1)
