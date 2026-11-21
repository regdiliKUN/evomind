"""A3 · FAISS HNSW 向量索引层（vector_ann）的离线验收。

零依赖：不连 Ollama、不碰真实 knowledge.db、不启动服务。所有数据来自一个
临时 SQLite，向量用固定种子生成，结果可复现。

覆盖：
  1. 默认关闭 → available()=False，search() 回退 None，hybrid_search 走 memory.search
  2. 开启后 rebuild 建索引、search() 的 recall@10 与暴力余弦一致（近重复口径）
  3. vector_hits 返回形状与 memory.search 兼容（含 id / score）
  4. hybrid_search 路由：开启时优先 ANN、关闭时回退 memory.search
  5. status / mark_stale / ensure_ready 的新鲜度闭环

运行：
    python tests/test_vector_ann.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = "PASS", "FAIL"
_results: list[dict] = []


def case(name: str, fn) -> None:
    try:
        note = fn()
        _results.append({"name": name, "status": PASS, "note": str(note)[:100]})
    except Exception as e:  # noqa: BLE001
        _results.append({"name": name, "status": FAIL,
                         "note": f"{type(e).__name__}: {str(e)[:80]}",
                         "trace": traceback.format_exc()[-500:]})


def _make_db(db_path: Path, n: int = 80, dim: int = 16, seed: int = 42):
    if db_path.exists():
        db_path.unlink()
    c = sqlite3.connect(str(db_path))
    c.executescript(
        """
        CREATE TABLE knowledge(
            id TEXT PRIMARY KEY, rowid INTEGER, topic TEXT, claim TEXT,
            detail TEXT, embedding BLOB, status TEXT DEFAULT 'active',
            tags TEXT DEFAULT '[]', source_url TEXT, source_title TEXT,
            source_kind TEXT, confidence REAL, created_at TEXT
        );
        """
    )
    import numpy as np
    rng = np.random.RandomState(seed)
    vecs = rng.standard_normal((n, dim)).astype("float32")
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    for i in range(n):
        pid = f"k{i:03d}"
        blob = vecs[i].tobytes()
        c.execute(
            "INSERT INTO knowledge(id,rowid,topic,claim,detail,embedding,status) "
            "VALUES(?,?,?,?,?,?,?)",
            (pid, i + 1, f"topic{i % 5}", f"claim {i}", f"detail {i}", blob, "active"),
        )
    c.commit()
    c.close()
    return vecs


def _brute_topk(vecs, q, k):
    import numpy as np
    sims = vecs @ (q / np.linalg.norm(q))
    order = np.argsort(-sims)
    return [f"k{i:03d}" for i in order[:k]]


def main() -> int:
    from backend import config, search_index, vector_ann

    tmp = Path(tempfile.mkdtemp(prefix="evomind_vann_"))
    db = tmp / "knowledge.db"
    vecs = _make_db(db, n=80, dim=16)

    patcher = patch.multiple(
        config, DATA=tmp, KNOWLEDGE=tmp, DB_FILE=db,
        CONFIG_FILE=tmp / "config.json", _cache=None,
    )
    patcher.start()
    try:
        # 默认关闭
        def default_off():
            vector_ann.reset_cache()
            assert vector_ann.available() is False, "默认应不可用"
            assert vector_ann.search(vecs[0], 10) is None, "关闭时 search 应回退 None"
            return "available=False, search→None"

        # 开启 + 建索引 + recall 一致
        def build_and_recall():
            config.update({"vector_ann_enabled": True, "vector_ann_ef_search": 128})
            vector_ann.reset_cache()
            assert vector_ann.available() is True, "开启后应可用"
            rep = vector_ann.rebuild()
            assert rep.get("indexed") == 80, f"应索引 80 条，实际 {rep}"
            # 用第 0 条自身作查询（近重复口径）
            got = vector_ann.search(vecs[0], 10)
            assert got is not None and len(got) == 10, f"应返回 10 条，实际 {got}"
            brute = _brute_topk(vecs, vecs[0], 10)
            hit = len(set(got) & set(brute))
            assert got[0] == "k000", f"top1 应为 k000，实际 {got[0]}"
            assert hit >= 9, f"recall@10 应≥9，实际 {hit}"
            return f"indexed={rep['indexed']}, top1=k000, recall@10={hit}/10"

        # vector_hits 形状兼容 memory.search
        def hits_shape():
            h = vector_ann.vector_hits("q", 5, vec=list(vecs[3]))
            assert h is not None and len(h) == 5, f"应返回 5 条，实际 {h and len(h)}"
            for d in h:
                assert "id" in d and "score" in d, f"缺少 id/score：{list(d)}"
                assert "embedding" not in d, "不应泄露 embedding BLOB"
            assert h[0]["id"] == "k003", f"top1 应为 k003，实际 {h[0]['id']}"
            return f"top1={h[0]['id']}, score={h[0]['score']}"

        # hybrid_search 路由：开启时优先 ANN
        def route_ann():
            config.update({"vector_ann_enabled": True})
            vector_ann.reset_cache()
            vector_ann.rebuild()
            with patch("backend.memory.search", return_value=[]) as mocked:
                res = search_index.hybrid_search(
                    "dummy-query", k=5, vec=list(vecs[7]), lexical_hits=[])
                assert not mocked.called, "开启 ANN 时不应回退 memory.search"
                assert res["items"], "应返回 ANN 结果"
                assert res["items"][0]["id"] == "k007", res["items"][0]["id"]
            return f"ANN 命中 top1={res['items'][0]['id']}, memory.search 未调用"

        # hybrid_search 路由：关闭时回退 memory.search
        def route_fallback():
            config.update({"vector_ann_enabled": False})
            vector_ann.reset_cache()
            sentinel = [{"id": "mem1", "score": 0.9}]
            with patch("backend.memory.search", return_value=sentinel) as mocked:
                res = search_index.hybrid_search(
                    "dummy-query", k=5, vec=list(vecs[0]), lexical_hits=[])
                assert mocked.called, "关闭 ANN 时必须回退 memory.search"
                assert res["items"][0]["id"] == "mem1"
            return "memory.search 被调用，回退生效"

        # 新鲜度闭环：mark_stale → status.stale → ensure_ready 重建
        def freshness_loop():
            config.update({"vector_ann_enabled": True})
            vector_ann.reset_cache()
            vector_ann.rebuild()
            assert vector_ann.status().get("stale") is False, "刚建完不应过期"
            vector_ann.mark_stale()
            assert vector_ann.status().get("stale") is True, "mark_stale 后应变过期"
            ok = vector_ann.ensure_ready()
            assert ok is True and vector_ann.status().get("stale") is False, "ensure_ready 应重建"
            return "mark_stale→stale=True→ensure_ready→stale=False"

        # 关闭 ANN 时 rebuild 不应产出索引（纯派生、开关可控）
        def rebuild_respects_flag():
            config.update({"vector_ann_enabled": False})
            vector_ann.reset_cache()
            # 先清掉本轮前面用例留下的派生索引，单独验证「关闭时不会新建」。
            for f in ("vector_ann.index", "vector_ann.ids.json"):
                try:
                    (tmp / f).unlink()
                except OSError:
                    pass
            rep = vector_ann.rebuild()
            assert rep.get("available") is False, "关闭时 rebuild 应声明不可用"
            assert not (tmp / "vector_ann.index").exists(), "关闭时不应写索引文件"
            return "关闭时不建索引文件"

        case("默认关闭：available=False, search→None", default_off)
        case("开启+建索引+recall@10 与暴力一致", build_and_recall)
        case("vector_hits 形状兼容 memory.search", hits_shape)
        case("路由：开启时优先 ANN", route_ann)
        case("路由：关闭时回退 memory.search", route_fallback)
        case("新鲜度闭环 mark_stale→ensure_ready", freshness_loop)
        case("关闭时 rebuild 不写索引文件", rebuild_respects_flag)
    finally:
        patcher.stop()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 70)
    print("  vector_ann 离线验收")
    print("=" * 70)
    for r in _results:
        icon = "✓" if r["status"] == PASS else "✗"
        print(f"  {icon} {r['name']:<40} {r['note']}")
        if r.get("trace"):
            print("      " + r["trace"].replace("\n", "\n      ")[-380:])
    p = sum(1 for r in _results if r["status"] == PASS)
    f = sum(1 for r in _results if r["status"] == FAIL)
    print("-" * 70)
    print(f"  通过 {p} · 失败 {f} · 共 {len(_results)}")
    print("=" * 70)
    # 清理：把派生索引留在临时目录，已被 rmtree 删掉，真实库零写入。
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
