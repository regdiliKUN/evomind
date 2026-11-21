"""台账查询基准：`attempted_urls` 在十万级台账上还够不够快。

每一轮学习都要用「这批候选 URL 在本方向下读过没有」过滤一次，候选通常几十条，
台账随使用线性增长（每个 url×topic 一行）。本脚本造 20 万行台账，对比两种索引：

    legacy  = (topic, last_at)        —— 主题+时间能走索引，URL 只能回表逐行比
    current = (topic, url, last_at)   —— 主题、URL 都走索引，时间做残余过滤

    .venv\\Scripts\\python.exe tests\\bench_attempts.py [--rows 200000] [--urls 60]
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def build(db: Path, rows: int, topics: int = 200) -> None:
    import sqlite3

    c = sqlite3.connect(db)
    c.execute(
        "CREATE TABLE document_attempts (url TEXT NOT NULL, topic TEXT NOT NULL,"
        " attempts INTEGER DEFAULT 1, yielded INTEGER DEFAULT 0,"
        " first_at REAL, last_at REAL, PRIMARY KEY (url, topic))"
    )
    now = time.time()
    c.executemany(
        "INSERT INTO document_attempts(url,topic,attempts,yielded,first_at,last_at)"
        " VALUES (?,?,1,?,?,?)",
        [
            (f"https://corpus.example/doc/{i}", f"方向 {i % topics}", 0, now - 86400, now - (i % 30) * 3600)
            for i in range(rows)
        ],
    )
    c.commit()
    c.close()


def measure(db: Path, index_sql: str | None, urls: int, repeat: int) -> dict:
    import sqlite3

    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    if index_sql:
        c.execute(index_sql)
        c.commit()
    topic = "方向 7"
    wanted = [f"https://corpus.example/doc/{i}" for i in range(0, urls * 3, 3)]
    marks = ",".join("?" * len(wanted))
    sql = f"SELECT url FROM document_attempts WHERE topic=? AND last_at>=? AND url IN ({marks})"
    plan = [r["detail"] for r in c.execute("EXPLAIN QUERY PLAN " + sql, (topic, 0.0, *wanted))]
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        list(c.execute(sql, (topic, 0.0, *wanted)))
        samples.append((time.perf_counter() - t0) * 1000)
    c.close()
    return {
        "median_ms": round(statistics.median(samples), 2),
        "min_ms": round(min(samples), 2),
        "plan": plan,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=200_000)
    ap.add_argument("--urls", type=int, default=60)
    ap.add_argument("--repeat", type=int, default=9)
    ap.add_argument("--topics", type=int, default=200)
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="evomind-attempts-"))
    try:
        base = tmp / "base.db"
        build(base, args.rows, args.topics)
        legacy_db = tmp / "legacy.db"
        current_db = tmp / "current.db"
        shutil.copy(base, legacy_db)
        shutil.copy(base, current_db)
        legacy = measure(
            legacy_db,
            "CREATE INDEX idx_attempts_topic ON document_attempts(topic, last_at)",
            args.urls, args.repeat,
        )
        current = measure(
            current_db,
            "CREATE INDEX idx_attempts_lookup ON document_attempts(topic, url, last_at)",
            args.urls, args.repeat,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    out = {
        "rows": args.rows,
        "topics": args.topics,
        "urls": args.urls,
        "legacy": legacy,
        "current": current,
        "speedup": round(legacy["median_ms"] / current["median_ms"], 1) if current["median_ms"] else 0,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
