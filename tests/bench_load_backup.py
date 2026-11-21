"""负载基准 · 大库备份导出 / 校验 / 恢复（AUDIT_REVIEW §10 P3「无负载基准」）。

    .venv\\Scripts\\python.exe tests\\bench_load_backup.py [--points 17426] [--events 54634]
        [--ocr-pages 3000] [--repeat 3] [--json]

规模默认对齐真实库量级（README：知识 1.7 万条、events 5.4 万行），全部写在临时目录，
不读写 data/。向量用固定种子随机生成（1024 维 float32，与真实嵌入同样几乎不可压缩）。

每项操作：重复 N 次取中位数 / p95 / 最大值；另跑一次 tracemalloc 量 Python 峰值内存。
A/B（同一份库、同一进程、交替执行）：

  legacy  = 改动前的恢复流程：validate（流式算 SHA256 一遍 + 解压到临时目录一遍 + quick_check）
            → 再解压到 staging 一遍 → 再 quick_check 一遍
  current = backup.restore_backup：在 staging 里一次解压、边写边算 SHA256，只做一次 quick_check

另测「恢复时有线程持有空闲连接」（真实运行态）：legacy 在 Windows 上 WinError 32 失败，
current 自动改走 SQLite 在线备份覆盖。

另给出导出阶段拆分（VACUUM INTO / SHA256 / deflate 6 / deflate 1 / stored），说明导出耗时
主要花在哪一步。
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import tracemalloc
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from backend import backup, config, library, memory, ocr  # noqa: E402


# --------------------------------------------------------------------------
# 造库
# --------------------------------------------------------------------------
def build_db(db: Path, n_points: int, n_events: int, n_pages: int, seed: int = 7) -> dict:
    """用真实 schema 建一份大库：memory.conn() 建主表，library/ocr 建书库表。"""
    memory.conn()  # 真实 SCHEMA + _upgrade
    _close_memory()
    c = sqlite3.connect(str(db))
    c.executescript(library.SCHEMA)
    c.executescript(ocr.SCHEMA)
    rng = np.random.default_rng(seed)
    now = time.time()
    with c:
        rows = []
        for i in range(n_points):
            v = rng.normal(size=1024).astype(np.float32)
            v /= np.linalg.norm(v)
            rows.append((
                f"p{i:06d}", f"基准主题 {i % 40}",
                f"基准结论第 {i} 条：在条件 {i % 97} 下参数取 {i * 3} 单位时效果最好，误差不超过 2%。",
                "补充说明：用于模拟真实知识点 detail 的长度与字符分布。" * 4,
                f"https://example.com/bench/{i}", f"bench {i % 300}", "web", '["bench"]', .7,
                v.tobytes(), "bench", "", now - (n_points - i),
            ))
        c.executemany(
            "INSERT INTO knowledge(id,topic,claim,detail,source_url,source_title,source_kind,"
            "tags,confidence,embedding,session_id,book_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        kinds = ("info", "ok", "warn", "stage")
        c.executemany(
            "INSERT INTO events(ts,kind,message,payload) VALUES (?,?,?,?)",
            [(now - (n_events - i) * 30, kinds[i % 4], f"基准事件 {i}：抓取 https://example.com/{i} 完成",
              json.dumps({"i": i, "topic": f"基准主题 {i % 40}"}, ensure_ascii=False))
             for i in range(n_events)])
        c.execute("INSERT INTO books(id,path,title,status,created_at) VALUES (?,?,?,?,?)",
                  ("b1", "C:/bench/book.pdf", "基准书", "done", now))
        para = "这是一段模拟 OCR 识别出的正文，包含中文标点、数字 123 与英文 term。" * 30
        c.executemany(
            "INSERT INTO ocr_pages(book_id,page_no,text,engine,chars,created_at) VALUES (?,?,?,?,?,?)",
            [("b1", i, para, "bench", len(para), now) for i in range(n_pages)])
    c.close()
    return {"db_mb": round(db.stat().st_size / 1e6, 1), "points": n_points,
            "events": n_events, "ocr_pages": n_pages}


def _close_memory() -> None:
    c = getattr(memory._local, "c", None)
    if c is not None:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
        memory._local.c = None


# --------------------------------------------------------------------------
# 改动前的恢复流程（原样保留作对照）
# --------------------------------------------------------------------------
def legacy_validate(zip_path: Path) -> dict:
    import hashlib
    manifest: dict = {}
    with tempfile.TemporaryDirectory(prefix="evomind-validate-") as td:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            if backup.DB_MEMBER not in names:
                raise ValueError("备份里缺少 knowledge.db")
            if backup.MANIFEST_MEMBER in names:
                manifest = json.loads(zf.read(backup.MANIFEST_MEMBER).decode("utf-8"))
            known = set(names)
            for member, meta in (manifest.get("members") or {}).items():
                if member not in known or not isinstance(meta, dict) or not meta.get("sha256"):
                    continue
                h = hashlib.sha256()
                with zf.open(member) as src:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != meta["sha256"]:
                    raise ValueError(f"备份成员校验失败：{member}")
            zf.extract(backup.DB_MEMBER, td)
        db = Path(td) / backup.DB_MEMBER
        ok, msg = backup._quick_check(db)
        if not ok:
            raise ValueError(msg)
        counts = backup._counts(db)
    return {"name": zip_path.name, "manifest": manifest, "counts": counts}


def legacy_restore(zip_path: Path) -> dict:
    reason = backup.busy_reason()
    if reason:
        raise RuntimeError(reason)
    info = legacy_validate(zip_path)
    target = Path(config.DB_FILE)
    backup._restoring.set()
    staging_dir = None
    try:
        staging_dir = tempfile.mkdtemp(prefix=".evomind-restore-", dir=str(target.parent))
        with zipfile.ZipFile(zip_path) as zf:
            zf.extract(backup.DB_MEMBER, staging_dir)
        staged = Path(staging_dir) / backup.DB_MEMBER
        ok, msg = backup._quick_check(staged)
        if not ok:
            raise ValueError(msg)
        pre = backup.export_backup("pre-restore")
        for side in ("-wal", "-shm", "-journal"):
            Path(str(target) + side).unlink(missing_ok=True)
        os.replace(staged, target)
        memory.bump_db_generation()
    finally:
        backup._restoring.clear()
        if staging_dir:
            shutil.rmtree(staging_dir, ignore_errors=True)
    return {"restored": info["name"], "pre_backup": pre["name"]}


# --------------------------------------------------------------------------
# 计时工具
# --------------------------------------------------------------------------
def summarize(samples: list[float]) -> dict:
    s = sorted(samples)
    p95 = s[min(len(s) - 1, max(0, int(round(.95 * (len(s) - 1)))))]
    return {"n": len(s), "p50_ms": round(statistics.median(s) * 1000, 1),
            "p95_ms": round(p95 * 1000, 1), "max_ms": round(s[-1] * 1000, 1)}


def timed(fn, *a, **k) -> float:
    gc.collect()
    t0 = time.perf_counter()
    fn(*a, **k)
    return time.perf_counter() - t0


def peak_mb(fn, *a, **k) -> float:
    gc.collect()
    tracemalloc.start()
    try:
        fn(*a, **k)
        return round(tracemalloc.get_traced_memory()[1] / 1e6, 2)
    finally:
        tracemalloc.stop()


def stage_breakdown(work: Path) -> dict:
    """导出各阶段单独计时：解释导出耗时花在哪里。"""
    snap = work / "stage-snap.db"
    out: dict = {}
    out["vacuum_into_ms"] = round(timed(backup._snapshot_db, snap) * 1000, 1)
    out["sha256_ms"] = round(timed(backup._sha256, snap) * 1000, 1)
    for label, mode, level in (("deflate6", zipfile.ZIP_DEFLATED, 6),
                               ("deflate1", zipfile.ZIP_DEFLATED, 1),
                               ("stored", zipfile.ZIP_STORED, None)):
        z = work / f"stage-{label}.zip"

        def write() -> None:
            with zipfile.ZipFile(z, "w", mode, compresslevel=level) as zf:
                zf.write(snap, backup.DB_MEMBER)
        out[f"{label}_ms"] = round(timed(write) * 1000, 1)
        out[f"{label}_mb"] = round(z.stat().st_size / 1e6, 1)
        z.unlink()
    snap.unlink()
    return out


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def run(work: Path, n_points: int, n_events: int, n_pages: int, repeat: int) -> dict:
    kdir = work / "knowledge"
    kdir.mkdir(parents=True, exist_ok=True)
    db = kdir / "knowledge.db"
    with patch.multiple(config, DATA=work, LOGS=work / "logs", MODELS=work / "models",
                        DATASETS=work / "datasets", KNOWLEDGE=kdir,
                        CONFIG_FILE=work / "config.json", DB_FILE=db, _cache=None), \
            patch.object(memory, "log_event", lambda *a, **k: None), \
            patch.object(backup, "KEEP_LATEST", 1000):
        t0 = time.perf_counter()
        scale = build_db(db, n_points, n_events, n_pages)
        scale["build_s"] = round(time.perf_counter() - t0, 1)

        result: dict = {"scale": scale, "stages": stage_breakdown(work)}

        # 导出
        exp, names = [], []
        for _ in range(repeat):
            t0 = time.perf_counter()
            r = backup.export_backup("bench")
            exp.append(time.perf_counter() - t0)
            names.append(r["name"])
        zip_path = backup.backup_path(names[0])
        result["export"] = summarize(exp) | {
            "zip_mb": round(zip_path.stat().st_size / 1e6, 1),
            "peak_py_mb": peak_mb(backup.export_backup, "bench-mem"),
        }

        # 校验（A/B）
        val_cur, val_old = [], []
        for _ in range(repeat):
            val_old.append(timed(legacy_validate, zip_path))
            val_cur.append(timed(backup.validate_backup, zip_path))
        result["validate"] = {
            "legacy": summarize(val_old) | {"peak_py_mb": peak_mb(legacy_validate, zip_path)},
            "current": summarize(val_cur) | {"peak_py_mb": peak_mb(backup.validate_backup, zip_path)},
        }

        # 恢复（A/B，交替执行，含 pre-restore 兜底导出）
        res_cur, res_old = [], []
        for _ in range(repeat):
            res_old.append(timed(legacy_restore, zip_path))
            res_cur.append(timed(backup.restore_backup, zip_path, confirm=True))
        result["restore"] = {
            "legacy": summarize(res_old),
            "current": summarize(res_cur) | {
                "peak_py_mb": peak_mb(backup.restore_backup, zip_path, confirm=True)},
        }
        # 恢复结果正确性：行数与导出时一致
        counts = backup._counts(db)
        assert counts["knowledge"] == n_points, counts
        assert counts["events"] == n_events, counts
        result["restore"]["counts_ok"] = True

        # 真实运行态：线程池里处理过请求的线程各持一条空闲的线程局部连接。
        # Windows 上这会让「删 -wal + os.replace」必然失败；current 应自动改走在线覆盖。
        held: dict = {}
        for mode, fn in (("legacy", legacy_restore),
                         ("current", lambda z: backup.restore_backup(z, confirm=True))):
            samples, outcome = [], ""
            for _ in range(1 if mode == "legacy" else repeat):
                ok, secs, outcome = restore_with_idle_connection(fn, zip_path)
                if not ok:
                    break
                samples.append(secs)
            held[mode] = {"ok": bool(samples), "outcome": outcome} | (summarize(samples) if samples else {})
        counts = backup._counts(db)
        held["counts_ok"] = counts["knowledge"] == n_points and counts["events"] == n_events
        result["restore_with_idle_connection"] = held
        _close_memory()
    return result


def restore_with_idle_connection(fn, zip_path: Path) -> tuple[bool, float, str]:
    """另起一个线程打开 memory.conn() 后保持空闲，期间执行恢复。"""
    import threading

    ready, release = threading.Event(), threading.Event()

    def worker() -> None:
        memory.conn().execute("SELECT COUNT(*) FROM knowledge").fetchone()
        ready.set()
        release.wait(120)
        _close_memory()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    ready.wait(30)
    gc.collect()
    t0 = time.perf_counter()
    try:
        r = fn(zip_path)
        return True, time.perf_counter() - t0, str((r or {}).get("method", "replace"))
    except Exception as e:  # noqa: BLE001
        return False, time.perf_counter() - t0, f"{type(e).__name__}: {str(e)[:80]}"
    finally:
        release.set()
        t.join(10)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", type=int, default=17426)
    ap.add_argument("--events", type=int, default=54634)
    ap.add_argument("--ocr-pages", type=int, default=3000)
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="evomind-bench-backup-"))
    try:
        out = run(work, args.points, args.events, args.ocr_pages, max(1, args.repeat))
    finally:
        _close_memory()
        gc.collect()
        shutil.rmtree(work, ignore_errors=True)
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return
    s = out["scale"]
    print(f"规模：库 {s['db_mb']} MB · 知识 {s['points']} · events {s['events']} · OCR 页 {s['ocr_pages']}"
          f"（造库 {s['build_s']} s）")
    st = out["stages"]
    print("导出阶段拆分：VACUUM INTO {vacuum_into_ms} ms · SHA256 {sha256_ms} ms · "
          "deflate6 {deflate6_ms} ms/{deflate6_mb} MB · deflate1 {deflate1_ms} ms/{deflate1_mb} MB · "
          "stored {stored_ms} ms/{stored_mb} MB".format(**st))
    e = out["export"]
    print(f"导出：p50 {e['p50_ms']} ms · p95 {e['p95_ms']} · max {e['max_ms']} · zip {e['zip_mb']} MB"
          f" · Python 峰值 {e['peak_py_mb']} MB")
    for op in ("validate", "restore"):
        for mode in ("legacy", "current"):
            r = out[op][mode]
            extra = f" · Python 峰值 {r['peak_py_mb']} MB" if "peak_py_mb" in r else ""
            print(f"{op:<8} [{mode:<7}] p50 {r['p50_ms']} ms · p95 {r['p95_ms']} · max {r['max_ms']}{extra}")
    h = out["restore_with_idle_connection"]
    for mode in ("legacy", "current"):
        r = h[mode]
        timing = f"p50 {r['p50_ms']} ms · max {r['max_ms']}" if r["ok"] else "失败"
        print(f"恢复（有空闲连接）[{mode:<7}] {timing} · {r['outcome']}")
    print(f"恢复后行数一致：{h['counts_ok']}")


if __name__ == "__main__":
    main()
