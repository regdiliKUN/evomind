"""负载基准 · 事件台账写入与归档（AUDIT_REVIEW §10 P3「无负载基准」）。

    .venv\\Scripts\\python.exe tests\\bench_load_events.py [--events 54634] [--writes 2000] [--repeat 5] [--json]

临时目录里造一份与真实库同量级的 events（默认 54,634 行，其中 warn 占 37%，
时间跨度约 60 天）+ learning_queries + sessions，然后测：

  log_event   info / warn 两种级别逐条写入（warn 要多走 rollup 读回与告警判定）
  archive     dry_run（接口默认路径，只报数）与真实归档（每批 5000，直到搬空）
  rollups / alerts / ops_status  运维面板的三个读接口

每项重复 N 次取中位数；A/B（--legacy-dry-run 对照）用同一份库交替执行。
"""
from __future__ import annotations

import argparse
import gc
import json
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from backend import config, event_ops, memory  # noqa: E402
from bench_load_backup import _close_memory  # noqa: E402


def summarize(samples: list[float]) -> dict:
    s = sorted(samples)
    p95 = s[min(len(s) - 1, max(0, int(round(.95 * (len(s) - 1)))))]
    return {"n": len(s), "p50_ms": round(statistics.median(s) * 1000, 3),
            "p95_ms": round(p95 * 1000, 3), "max_ms": round(s[-1] * 1000, 3)}


def build(db: Path, n_events: int) -> None:
    memory.conn()
    _close_memory()
    c = sqlite3.connect(str(db))
    now = time.time()
    span = 60 * 86400
    with c:
        c.executemany(
            "INSERT INTO events(ts,kind,message,payload) VALUES (?,?,?,?)",
            [(now - span + i * span / n_events, "warn" if i % 100 < 37 else "info",
              f"基准事件 {i}：抓取 https://example.com/{i} 超时，已重试",
              json.dumps({"i": i, "url": f"https://example.com/{i}"}, ensure_ascii=False))
             for i in range(n_events)])
        c.executemany("INSERT OR IGNORE INTO learning_queries(topic,query,used_at) VALUES (?,?,?)",
                      [(f"主题{i % 50}", f"检索式 {i}", now - span + i * span / 20000) for i in range(20000)])
        c.executemany("INSERT INTO sessions(id,direction,started_at) VALUES (?,?,?)",
                      [(f"s{i}", f"方向{i % 30}", now - span + i * span / 3000) for i in range(3000)])
    c.close()


def legacy_dry_run(c: sqlite3.Connection, cutoff: float, batch: int) -> dict:
    """改动前 dry_run：把至多 batch 行整行读成 dict 只为了数个数。"""
    remaining, moved = batch, {}
    for origin, sql in (("events", "SELECT * FROM events WHERE ts < ? LIMIT ?"),
                        ("learning_queries", "SELECT * FROM learning_queries WHERE used_at < ? LIMIT ?"),
                        ("sessions", "SELECT * FROM sessions WHERE started_at < ? LIMIT ?")):
        rows = [dict(r) for r in c.execute(sql, (cutoff, remaining))] if remaining > 0 else []
        moved[origin] = len(rows)
        remaining -= len(rows)
    return moved


def timed(fn, *a, **k) -> float:
    t0 = time.perf_counter()
    fn(*a, **k)
    return time.perf_counter() - t0


def run(work: Path, n_events: int, writes: int, repeat: int) -> dict:
    kdir = work / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    db = kdir / "knowledge.db"
    out: dict = {"events": n_events}
    with patch.multiple(config, DATA=work, LOGS=work / "logs", MODELS=work / "models",
                        DATASETS=work / "datasets", KNOWLEDGE=kdir,
                        CONFIG_FILE=work / "config.json", DB_FILE=db, _cache=None):
        build(db, n_events)

        # 写入：info 只 UPSERT rollup；warn 还要读回计数并判定告警
        for kind in ("info", "warn"):
            lat = [timed(memory.log_event, kind, f"基准写入 {kind} {i}", {"i": i}) for i in range(writes)]
            out[f"log_event_{kind}"] = summarize(lat) | {"per_s": round(writes / sum(lat), 0)}

        c = memory.conn()
        cutoff = time.time() - 30 * 86400
        dry_cur, dry_old = [], []
        for _ in range(repeat):
            dry_old.append(timed(legacy_dry_run, c, cutoff, event_ops.ARCHIVE_BATCH))
            dry_cur.append(timed(event_ops.archive, dry_run=True))
        out["archive_dry_run"] = {"legacy": summarize(dry_old), "current": summarize(dry_cur)}
        gc.collect()
        tracemalloc.start()
        legacy_dry_run(c, cutoff, event_ops.ARCHIVE_BATCH)
        out["archive_dry_run"]["legacy"]["peak_py_mb"] = round(tracemalloc.get_traced_memory()[1] / 1e6, 2)
        tracemalloc.stop()
        gc.collect()
        tracemalloc.start()
        event_ops.archive(dry_run=True)
        out["archive_dry_run"]["current"]["peak_py_mb"] = round(tracemalloc.get_traced_memory()[1] / 1e6, 2)
        tracemalloc.stop()

        for name, fn in (("rollups_7d", lambda: event_ops.rollups(7)),
                         ("alerts", lambda: event_ops.alerts(20)),
                         ("ops_status", event_ops.ops_status)):
            out[name] = summarize([timed(fn) for _ in range(repeat * 4)])

        # 真实归档：每批 5000 行，搬到没有过期行为止
        batches, total = [], 0
        gc.collect()
        tracemalloc.start()
        while True:
            t0 = time.perf_counter()
            r = event_ops.archive(dry_run=False)
            batches.append(time.perf_counter() - t0)
            n = sum(r["moved"].values())
            total += n
            if n == 0:
                batches.pop()
                break
        peak = round(tracemalloc.get_traced_memory()[1] / 1e6, 1)
        tracemalloc.stop()
        out["archive_real"] = summarize(batches) | {"batches": len(batches), "moved": total, "peak_py_mb": peak}
        out["after"] = event_ops.ops_status()
        _close_memory()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=54634)
    ap.add_argument("--writes", type=int, default=2000)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="evomind-bench-events-"))
    try:
        out = run(work, args.events, args.writes, max(1, args.repeat))
    finally:
        _close_memory()
        gc.collect()
        shutil.rmtree(work, ignore_errors=True)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    print(f"规模：events {out['events']} 行（warn 37%）+ learning_queries 20000 + sessions 3000")
    for kind in ("info", "warn"):
        v = out[f"log_event_{kind}"]
        print(f"log_event[{kind}] p50 {v['p50_ms']} ms · p95 {v['p95_ms']} · max {v['max_ms']} · {v['per_s']:.0f} 条/s")
    for mode in ("legacy", "current"):
        v = out["archive_dry_run"][mode]
        print(f"archive dry_run [{mode:<7}] p50 {v['p50_ms']} ms · p95 {v['p95_ms']} · Python 峰值 {v['peak_py_mb']} MB")
    for name in ("rollups_7d", "alerts", "ops_status"):
        v = out[name]
        print(f"{name:<11} p50 {v['p50_ms']} ms · p95 {v['p95_ms']} · max {v['max_ms']}")
    v = out["archive_real"]
    print(f"真实归档：{v['batches']} 批 · 共搬 {v['moved']} 行 · 每批 p50 {v['p50_ms']} ms · max {v['max_ms']}"
          f" · Python 峰值 {v['peak_py_mb']} MB")
    print(f"归档后：{json.dumps(out['after'], ensure_ascii=False)}")


if __name__ == "__main__":
    main()
