"""Offline regression tests; every test uses a disposable configuration and database.

Run: .venv/Scripts/python -m unittest discover -s tests -p test_*.py -v
"""
from __future__ import annotations

import concurrent.futures as cf
import gc
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import analytics, config, evolve, library, llm, memory


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="evomind_regression_")
        root = Path(self.tmp.name)
        self.paths = patch.multiple(
            config, DATA=root, LOGS=root, MODELS=root / "models",
            DATASETS=root / "datasets", KNOWLEDGE=root,
            CONFIG_FILE=root / "config.json", DB_FILE=root / "knowledge.db", _cache=None,
        )
        self.paths.start()
        self.local = patch.object(memory, "_local", threading.local())
        self.local.start()
        self.connections = []
        original_connect = memory.sqlite3.connect

        def connect(*args, **kwargs):
            c = original_connect(*args, **kwargs)
            self.connections.append(c)
            return c

        self.db_factory = patch.object(memory.sqlite3, "connect", side_effect=connect)
        self.db_factory.start()
        memory._invalidate()
        # 分析聚合按「写入版本号」缓存；每例换一个新库，必须清掉上一例的缓存，
        # 否则旧版本号会误命中（生产进程内库不会换，无需担心）
        analytics._reset_caches()
        # 书库建表守卫按库文件身份记忆；每例换库后要让它重新建表
        library._INIT_DONE["db"] = None

    def tearDown(self):
        for c in self.connections:
            c.close()
        self.db_factory.stop()
        memory._invalidate()
        self.local.stop()
        self.paths.stop()
        gc.collect()
        self.tmp.cleanup()

    def point(self, i=0, topic="default", vec=None):
        return memory.add_point(
            topic=topic, claim=f"Regression knowledge point number {i}",
            vec=[1.0, 0.0] if vec is None else vec, dedup=False,
        )


class MemoryTests(IsolatedTest):
    def test_cache_reuses_normalized_vectors(self):
        self.point(vec=[3., 4.])
        memory.search("q", vec=[1., 0.])
        with patch.object(memory, "_normalize", wraps=memory._normalize) as normalize:
            memory.search("q", vec=[1., 0.])
        self.assertEqual(normalize.call_count, 1)  # only the query is normalized again

    def test_invalid_vectors_are_not_persisted(self):
        for vec in ([], [float("nan"), 1.], [float("inf"), 0.], [0., 0.]):
            with self.subTest(vec=vec), self.assertRaises(ValueError):
                self.point(vec=vec)
        self.assertEqual(memory.count_points(), 0)

    def test_requested_k_above_twenty(self):
        for i in range(60):
            self.point(i, vec=[1.0, i / 100])
        hits = memory.search("query", k=45, vec=[1.0, 0.0])
        self.assertEqual(len(hits), 45)
        self.assertEqual(hits[0]["claim"], "Regression knowledge point number 0")

    def test_topic_filter_applied_before_ranking(self):
        for i in range(25):
            self.point(i, topic="other", vec=[1.0, 0.0])
        pid = self.point(26, topic="wanted", vec=[0.7, 0.7])
        hits = memory.search("query", k=1, topic="wanted", vec=[1.0, 0.0])
        self.assertEqual([p["id"] for p in hits], [pid])

    def test_mixed_dimensions_and_corrupt_vectors(self):
        pid = self.point(1)
        other = self.point(2, vec=[0.0, 0.0, 1.0])
        bad = self.point(3)
        c = memory.conn()
        c.execute("UPDATE knowledge SET embedding=? WHERE id=?", (b"bad", bad))
        c.commit()
        memory._invalidate()
        self.assertEqual([p["id"] for p in memory.search("q", vec=[1., 0.])], [pid])
        self.assertEqual([p["id"] for p in memory.search("q", vec=[0., 0., 1.])], [other])

    def test_empty_queries_and_nonpositive_k_do_not_embed(self):
        with patch.object(llm, "embed", side_effect=AssertionError("unneeded inference")):
            self.assertEqual(memory.search(""), [])
            self.assertEqual(memory.search("q", k=0), [])
            self.assertEqual(memory.search("q", k=-1), [])

    def test_similar_points_excludes_self_and_orders_by_similarity(self):
        base = self.point(1, vec=[1.0, 0.0, 0.0])
        near = self.point(2, vec=[0.98, 0.19, 0.0])
        far = self.point(3, vec=[0.0, 0.0, 1.0])
        sims = memory.similar_points(base, k=5)
        self.assertEqual([p["id"] for p in sims], [near, far])
        self.assertNotIn(base, [p["id"] for p in sims])
        self.assertGreater(sims[0]["similarity"], sims[1]["similarity"])
        self.assertAlmostEqual(sims[1]["similarity"], 0.0, places=4)

    def test_similar_points_respects_k_and_missing_input(self):
        self.point(1, vec=[1.0, 0.0])
        for i in range(2, 8):
            self.point(i, vec=[1.0, i / 100])
        self.assertEqual(len(memory.similar_points(self.point(99, vec=[1.0, 0.0]), k=3)), 3)
        self.assertEqual(memory.similar_points("no-such-id", k=3), [])
        # k 夹紧到 0 直接返回空，不做任何计算
        self.assertEqual(memory.similar_points(self.point(100, vec=[1.0, 0.0]), k=0), [])

    def test_similar_points_without_vector_returns_empty(self):
        pid = self.point(1, vec=[1.0, 0.0])
        c = memory.conn()
        c.execute("UPDATE knowledge SET embedding=NULL WHERE id=?", (pid,))
        c.commit()
        memory._invalidate()
        self.assertEqual(memory.similar_points(pid, k=3), [])

    def test_scale_warn_single_source_of_truth(self):
        """stats 与学习建议共用同一套阈值（重构后不能各写一份）"""
        self.assertEqual(memory.scale_warn(0), "")
        self.assertIn("开始变慢", memory.scale_warn(5000))
        self.assertIn("明显变慢", memory.scale_warn(20000))
        stats = memory.stats()
        self.assertEqual(stats["scale_warn"], memory.scale_warn(stats["points"]))

    def test_concurrent_dedup_is_atomic(self):
        config.load()
        barrier = threading.Barrier(2)
        original = memory.similarity

        def compare(*args, **kwargs):
            try:
                barrier.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass
            return original(*args, **kwargs)

        def add(_):
            try:
                return memory.add_point(topic="race", claim="Identical concurrent knowledge",
                                        vec=[1., 0.])
            finally:
                memory.conn().close()

        with patch.object(memory, "similarity", side_effect=compare):
            with cf.ThreadPoolExecutor(max_workers=2) as pool:
                ids = list(pool.map(add, range(2)))
        self.assertEqual(sum(pid is not None for pid in ids), 1)

    def test_purge_book_invalidates_vector_cache(self):
        library.init_db()
        pid = self.point()
        c = memory.conn()
        c.execute("INSERT INTO books(id,path,title) VALUES ('book','missing.txt','Test')")
        c.execute("UPDATE knowledge SET book_id='book' WHERE id=?", (pid,))
        c.commit()
        memory.search("q", vec=[1., 0.])  # prime the cache before removing the book
        library.delete_book("book", purge_points=True)
        # The deleted vector must not suppress the same claim on re-import.
        added = memory.add_point(topic="test", claim="Reimport after book deletion", vec=[1., 0.])
        self.assertIsNotNone(added)
        self.assertEqual([p["id"] for p in memory.search("q", vec=[1., 0.])], [added])


class ConfigTests(IsolatedTest):
    def test_invalid_stored_value_falls_back_individually(self):
        config.CONFIG_FILE.write_text(json.dumps({"idle_minutes": "bad", "model": "custom"}),
                                      encoding="utf-8")
        self.assertEqual(config.get("idle_minutes"), config.DEFAULTS["idle_minutes"])
        self.assertEqual(config.get("model"), "custom")

    def test_load_returns_snapshot(self):
        cfg = config.load()
        cfg["model"] = "unpersisted mutation"
        self.assertEqual(config.get("model"), config.DEFAULTS["model"])

    def test_failed_atomic_save_preserves_file_and_cache(self):
        original = config.load()
        before = config.CONFIG_FILE.read_bytes()
        with patch.object(config.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                config.update({"model": "must not be saved"})
        self.assertEqual(config.CONFIG_FILE.read_bytes(), before)
        self.assertEqual(config.load(), original)
        self.assertEqual(list(config.DATA.glob("*.tmp")), [])

    def test_unknown_keys_ignored(self):
        config.update({"__unknown__": 5})
        self.assertIsNone(config.get("__unknown__"))

    def test_invalid_values_rejected_without_partial_update(self):
        original = config.load()
        for values in ({"fetch_per_round": 0}, {"idle_minutes": "bad"},
                       {"dedup_threshold": 1.1}, {"auto_download": "false"},
                       {"temperature": float("nan")}, {"model": None}):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    config.update({"last_direction": "must not change", **values})
                self.assertEqual(config.load(), original)

    def test_parallel_updates_keep_all_fields(self):
        values = {"model": "test-model", "embed_model": "test-embed", "idle_minutes": 31,
                  "disk_budget_gb": 50, "max_rounds": 3, "last_direction": "test-direction"}
        config.load()
        with cf.ThreadPoolExecutor(max_workers=len(values)) as pool:
            list(pool.map(lambda item: config.update(dict([item])), values.items()))
        saved = json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))
        for k, v in values.items():
            self.assertEqual(saved[k], v)


class EngineTests(IsolatedTest):
    def test_invalid_rounds_do_not_create_session(self):
        for rounds in (0, -1, 101, "two", True):
            with self.subTest(rounds=rounds):
                self.assertFalse(evolve.Engine().start("test", rounds)["ok"])
        self.assertEqual(memory.list_sessions(), [])

    def test_thread_start_failure_releases_running_state(self):
        engine = evolve.Engine()
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start")):
            with self.assertRaises(RuntimeError):
                engine.start("test", 1)
        self.assertFalse(engine.state["running"])
        self.assertEqual(memory.list_sessions()[0]["status"], "error")

    def test_ollama_unavailable_finishes_session(self):
        engine = evolve.Engine()
        sid = memory.start_session("test")
        engine.state["running"] = True
        with patch.object(llm, "tags", return_value=(False, [])):
            engine._run("test", 1, sid)
        session = memory.list_sessions()[0]
        self.assertEqual(session["status"], "error")
        self.assertIsNotNone(session["ended_at"])
        self.assertFalse(engine.state["running"])

    def test_concurrent_start_reserves_one_session(self):
        engine = evolve.Engine()
        entered, release = threading.Event(), threading.Event()
        duplicate = threading.Event()
        results = []

        def create_session(_):
            if entered.is_set():
                duplicate.set()
            entered.set()
            release.wait(timeout=1)
            return "session"

        workers = [threading.Thread(target=lambda: results.append(engine.start("test", 1)))
                   for _ in range(2)]
        with patch.object(memory, "start_session", side_effect=create_session) as create:
            with patch.object(evolve.threading, "Thread"):
                workers[0].start()
                self.assertTrue(entered.wait(timeout=1))
                workers[1].start()
                duplicate.wait(timeout=0.2)
                release.set()
                for worker in workers:
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertEqual(create.call_count, 1)


class LibraryTests(IsolatedTest):
    def test_add_directory_to_fresh_database(self):
        self.assertTrue(library.add_dir(self.tmp.name)["ok"])

    def test_book_source_link_committed_before_progress_callback(self):
        path = Path(self.tmp.name) / "book.txt"
        path.write_text("Useful source material for the test. " * 40, encoding="utf-8")
        library.init_db()
        c = memory.conn()
        c.execute("INSERT INTO books(id,path,title,format) VALUES ('book',?,'Test','txt')",
                  (str(path),))
        c.commit()
        links = []

        def progress(level, message, payload):
            if level == "learn":
                other = memory.sqlite3.connect(config.DB_FILE)
                try:
                    links.append(other.execute("SELECT book_id FROM knowledge").fetchone()[0])
                finally:
                    other.close()

        points = {"points": [{"claim": "A useful verified claim from the source",
                               "relevance": 1., "confidence": 0.9}]}
        with patch.object(llm, "chat_json", return_value=points):
            with patch.object(llm, "embed", return_value=[[1., 0.]]):
                result = library.import_book("book", max_chunks=1, on_event=progress)
        self.assertTrue(result["ok"])
        self.assertEqual(links, ["book"])


class ApiTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        from backend.main import app
        self.client = TestClient(app)  # omit startup: it warms external search services

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def test_invalid_config_returns_422(self):
        before = config.load()
        response = self.client.post("/api/config", json={"queries_per_round": -10})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(config.load(), before)

    def test_knowledge_topic_search_does_not_scan_disk(self):
        wanted = self.point(1, topic="wanted")
        self.point(2, topic="other")
        with patch.object(llm, "embed", return_value=[[1., 0.]]):
            with patch.object(config, "disk_usage_gb", side_effect=AssertionError("disk scan")):
                response = self.client.get("/api/knowledge", params={"q": "test", "topic": "wanted"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([p["id"] for p in response.json()["items"]], [wanted])
        self.assertEqual(response.json()["total"], 1)

    def test_negative_event_limit_is_bounded(self):
        memory.log_event("info", "test")
        response = self.client.get("/api/events/recent", params={"limit": -1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_static_console_served(self):
        for path in ("/", "/static/app.js", "/static/style.css"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_duplicates_endpoint_not_shadowed_by_detail_route(self):
        memory.add_point(topic="甲", claim="同一条结论的第一种说法原文", vec=[1., 0.],
                         dedup=False)
        memory.add_point(topic="甲", claim="同一条结论的第二种说法原文", vec=[1., 0.],
                         dedup=False)
        response = self.client.get("/api/knowledge/duplicates", params={"threshold": 0.9})
        self.assertEqual(response.status_code, 200, "duplicates 不能被 /{pid} 吞掉")
        body = response.json()
        self.assertIn("pairs", body)
        self.assertIn("threshold", body)
        self.assertEqual(len(body["pairs"]), 1)

    def test_status_reports_probe_pending_instead_of_blocking(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertIn("checking", response.json()["llm"])

    def test_analytics_payload_carries_write_version(self):
        response = self.client.get("/api/analytics")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("ver", body)
        self.assertEqual(body["ver"], memory.version())

    def test_health_endpoint_shape(self):
        empty = self.client.get("/api/analytics/health").json()
        self.assertTrue(empty["empty"])
        self.assertEqual(empty["score"], 0)
        for i in range(5):
            self.point(i, topic="方向甲", vec=[1.0, 0.0])
        h = self.client.get("/api/analytics/health").json()
        self.assertFalse(h["empty"])
        self.assertEqual(len(h["dims"]), 4)
        self.assertTrue(0 <= h["score"] <= 100)

    def test_knowledge_detail_endpoint(self):
        pid = self.point(1, topic="方向甲", vec=[1.0, 0.0])
        other = self.point(2, topic="方向甲", vec=[0.9, 0.1])
        body = self.client.get(f"/api/knowledge/{pid}").json()
        self.assertEqual(body["item"]["id"], pid)
        self.assertNotIn("embedding", body["item"])
        self.assertEqual([s["id"] for s in body["similar"]], [other])
        self.assertEqual(self.client.get("/api/knowledge/nope").status_code, 404)

    def test_detail_route_does_not_shadow_keyword_search(self):
        """/api/knowledge/{pid} 声明在 /api/knowledge/search 之后，别把 search 当 id"""
        self.point(1, topic="方向甲", vec=[1.0, 0.0])
        with patch.object(llm, "embed", return_value=[[1.0, 0.0]]):
            r = self.client.get("/api/knowledge/search", params={"q": "回归", "k": 3})
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.json(), list)

    def test_suggestions_endpoint_returns_items(self):
        for i in range(40):
            self.point(i, topic="方向甲", vec=[1.0, i / 100])
        items = self.client.get("/api/analytics/suggestions").json()["items"]
        self.assertTrue(items)
        for it in items:
            self.assertEqual(set(it), {"level", "icon", "title", "detail", "view", "btn"})


class AnalyticsCacheTests(IsolatedTest):
    """第十一轮：写入版本号 + 版本键聚合缓存。"""

    def test_write_version_increments_on_add_and_delete(self):
        v = memory.version()
        pid = self.point(vec=[1., 0.])
        self.assertEqual(memory.version(), v + 1)
        memory.delete_point(pid)
        self.assertEqual(memory.version(), v + 2)

    def test_snapshot_cache_hit_only_without_writes(self):
        v0 = memory.version()
        s1 = analytics.snapshot()
        self.assertEqual(s1["ver"], v0)
        self.assertIs(analytics.snapshot(), s1)  # 没写过：直接复用
        self.point(vec=[1., 0.])
        s2 = analytics.snapshot()
        self.assertIsNot(s1, s2)  # 写过：必须重算
        self.assertEqual(s2["ver"], v0 + 1)
        self.assertEqual(s2["total"], s1["total"] + 1)
        # 缓存路径与强制重算路径必须一致（接线正确性）
        self.assertEqual(analytics.snapshot(fresh=True), s2)

    def test_topic_graph_cache_keyed_by_version(self):
        for i in range(6):
            self.point(i, topic=f"方向{i % 3}", vec=[float(i + 1), 1.0])
        g1 = analytics.topic_graph()
        self.assertIs(analytics.topic_graph(), g1)
        self.assertEqual(len(g1["nodes"]), 3)
        self.point(99, topic="方向新", vec=[0.5, 0.5])
        g2 = analytics.topic_graph()
        self.assertIsNot(g1, g2)
        self.assertEqual(len(g2["nodes"]), 4)
        # edge_min 是缓存键的一部分：不同阈值各算各的
        self.assertIsNot(analytics.topic_graph(edge_min=0.9), g2)

    def test_health_empty_and_populated(self):
        h = analytics.health()
        self.assertTrue(h["empty"])
        self.assertEqual(h["score"], 0)
        self.assertIsNone(h["weakest"])
        for i in range(5):
            self.point(i, topic="方向甲", vec=[1.0, 0.0])
        h = analytics.health()
        self.assertFalse(h["empty"])
        self.assertEqual([d["key"] for d in h["dims"]],
                         ["scale", "confidence", "sources", "freshness"])
        self.assertIn(h["weakest"]["key"], {d["key"] for d in h["dims"]})
        self.assertTrue(0 <= h["score"] <= 100)
        self.assertIn(h["level"], ("待补强", "起步", "良好", "优秀"))

    def test_stats_carries_write_version(self):
        self.assertIn("ver", memory.stats())
        v = memory.stats()["ver"]
        self.point(vec=[1., 0.])
        self.assertEqual(memory.stats()["ver"], v + 1)


class SuggestionsCacheTests(IsolatedTest):
    """第十二轮：学习建议改吃版本缓存的聚合，不再每次轮询重算。"""

    def test_suggestions_reuse_cached_aggregates(self):
        for i in range(12):
            self.point(i, topic="方向甲", vec=[1.0, i / 100])
        analytics._reset_caches()
        analytics.snapshot()  # 真实场景里分析页/健康卡片已拉过一次
        first = analytics.suggestions()
        with patch.object(analytics, "topic_stats", side_effect=AssertionError("重算了聚合")), \
             patch.object(analytics, "growth_by_day", side_effect=AssertionError("重算了聚合")), \
             patch.object(analytics, "source_breakdown", side_effect=AssertionError("重算了聚合")), \
             patch.object(memory, "stats", side_effect=AssertionError("重算了 stats")):
            second = analytics.suggestions()
        self.assertEqual(first, second)

    def test_suggestions_follow_new_writes(self):
        for i in range(5):
            self.point(i, topic="方向甲", vec=[1.0, i / 100])
        first = analytics.suggestions()
        self.assertTrue(any("目前 5 条" in i["detail"] for i in first))
        for i in range(5, 35):
            self.point(i, topic="方向甲", vec=[1.0, i / 100])
        second = analytics.suggestions()
        self.assertTrue(any("已有 35 条" in i["detail"] for i in second))
        self.assertNotEqual(first, second)

    def test_suggestions_empty_library_guides_onboarding(self):
        items = analytics.suggestions()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["view"], "learn")
        self.assertEqual(items[0]["level"], "act")

    def test_suggestions_do_not_mutate_cached_snapshot(self):
        for i in range(8):
            self.point(i, topic="方向甲", vec=[1.0, i / 100])
        analytics._reset_caches()
        before = analytics.snapshot()
        analytics.suggestions()
        after = analytics.snapshot()
        self.assertEqual(before, after)


class DuplicateTests(IsolatedTest):
    """第十三轮新功能：近重复知识点整理。"""

    def add(self, topic: str, claim: str, vec: list[float]) -> None:
        memory.add_point(topic=topic, claim=claim, detail="细节", source_url="u://" + claim,
                         source_title="t", source_kind="web", tags=[], confidence=0.6,
                         vec=vec, dedup=False)

    def test_finds_near_duplicate_pair_inside_topic(self):
        self.add("甲方向", "同一条结论的第一种说法", [1.0, 0.0, 0.0])
        self.add("甲方向", "同一条结论的第二种说法", [0.99, 0.02, 0.0])
        self.add("甲方向", "完全无关的另一条结论", [0.0, 1.0, 0.0])
        r = memory.near_duplicates(threshold=0.9)
        self.assertEqual(len(r["pairs"]), 1)
        self.assertGreaterEqual(r["pairs"][0]["sim"], 0.9)
        self.assertNotEqual(r["pairs"][0]["a"]["id"], r["pairs"][0]["b"]["id"])

    def test_cross_topic_pairs_are_never_reported(self):
        self.add("甲方向", "甲方向里的一条结论原文", [1.0, 0.0])
        self.add("乙方向", "乙方向里同样的一条结论", [1.0, 0.0])
        r = memory.near_duplicates(threshold=0.9)
        self.assertEqual(r["pairs"], [])

    def test_threshold_controls_sensitivity(self):
        self.add("甲方向", "两条相似但不相同的结论之一", [1.0, 0.0])
        self.add("甲方向", "两条相似但不相同的结论之二", [0.9, 0.436])
        self.assertEqual(memory.near_duplicates(threshold=0.99)["pairs"], [])
        self.assertEqual(len(memory.near_duplicates(threshold=0.8)["pairs"]), 1)

    def test_scan_is_cached_by_write_version(self):
        self.add("甲方向", "两条相似但不相同的结论之一", [1.0, 0.0])
        self.add("甲方向", "两条相似但不相同的结论之二", [1.0, 0.0])
        first = memory.near_duplicates(threshold=0.9)
        with patch.object(memory, "_all_vectors", side_effect=AssertionError("重复扫描")):
            second = memory.near_duplicates(threshold=0.9)
        self.assertEqual([p["sim"] for p in first["pairs"]], [p["sim"] for p in second["pairs"]])
        # 写入后必须重算（版本号接线）
        self.add("甲方向", "新加进来的第三条结论原文", [1.0, 0.0])
        third = memory.near_duplicates(threshold=0.9)
        self.assertGreater(len(third["pairs"]), len(first["pairs"]))

    def test_empty_library_returns_empty_shape(self):
        r = memory.near_duplicates()
        self.assertEqual(r["pairs"], [])
        self.assertEqual(r["scanned"], 0)
        self.assertIn("threshold", r)


class LlmTagsTests(IsolatedTest):
    """第十三轮：/api/tags 改「先返回、后台刷新」，调用方不再被远端往返阻塞。"""

    def setUp(self):
        super().setUp()
        self.remote = {"down": False, "calls": 0, "rtt": 0.0}
        self._reset()
        self.patch = patch.object(llm, "_client", side_effect=lambda *a, **k: _FakeClient(self.remote))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self._reset()
        super().tearDown()

    def _reset(self) -> None:
        with llm._tags_lock:
            llm._tags_cache.update(
                probed=False, healthy=False, models=[], ts=0.0,
                refreshing=False, fail_streak=0, next_try=0.0,
            )

    def _settle(self, wait: float = 0.3) -> None:
        llm.tags()
        time.sleep(wait)

    def test_first_call_returns_immediately_while_probing(self):
        self.remote["rtt"] = 0.25
        t0 = time.perf_counter()
        healthy, models = llm.tags()
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.1, "首次调用不应等远端往返")
        self.assertFalse(healthy)
        self.assertEqual(models, [])
        self.assertTrue(llm.tags_pending())
        time.sleep(0.4)
        healthy, models = llm.tags()
        self.assertTrue(healthy)
        self.assertEqual([m["name"] for m in models], ["m1"])
        self.assertFalse(llm.tags_pending())

    def test_list_models_shares_the_same_cache(self):
        self._settle()
        self.remote["calls"] = 0
        models = llm.list_models()
        self.assertEqual([m["name"] for m in models], ["m1"])
        self.assertEqual(self.remote["calls"], 0, "list_models 不应再单独往返")

    def test_failure_is_cached_with_backoff(self):
        self.remote["down"] = True
        self._settle()
        calls = self.remote["calls"]
        for _ in range(6):
            llm.tags()
        time.sleep(0.1)
        self.assertEqual(self.remote["calls"], calls, "退避期内不应重复撞远端")

    def test_invalidate_schedules_exactly_one_refresh(self):
        self._settle()
        self.remote["rtt"] = 0.2
        self.remote["calls"] = 0
        llm._invalidate_tags()
        for _ in range(5):
            llm.tags()
        time.sleep(0.4)
        self.assertEqual(self.remote["calls"], 1, "并发调用只应触发一次刷新")


class _FakeResp:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"models": [{"name": "m1", "size": 1024 ** 3,
                            "details": {"parameter_size": "7B"}}]}


class _FakeClient:
    def __init__(self, remote: dict) -> None:
        self.remote = remote

    def get(self, path: str, timeout: float | None = None) -> _FakeResp:
        self.remote["calls"] += 1
        if self.remote["rtt"]:
            time.sleep(self.remote["rtt"])
        if self.remote["down"]:
            raise ConnectionError("refused")
        return _FakeResp()


class TestStatsCache(IsolatedTest):
    """stats() 版本键缓存：库没变过直接命中；写入后失效并反映新内容。"""

    def _seed(self, n: int) -> None:
        import hashlib

        def fake_embed(texts, **_):
            return [[(b - 127.5) / 127.5 for b in hashlib.sha256(t.encode()).digest()[:64]]
                    for t in texts]

        with patch.object(llm, "embed", side_effect=fake_embed):
            for i in range(n):
                memory.add_point(
                    topic="t", claim=f"用于缓存测试的知识点陈述内容第 {i} 条",
                    detail="", source_url="", source_title="", source_kind="web",
                    tags=[], confidence=0.8, session_id="", dedup=False,
                )

    def _db_fields(self, d: dict) -> dict:
        return {k: d[k] for k in d if k not in ("disk_gb", "budget_gb", "free_disk_gb", "ver")}

    def test_cache_is_transparent(self):
        self._seed(5)
        a = self._db_fields(memory.stats())
        b = self._db_fields(memory.stats())  # 版本未变，应命中缓存
        self.assertEqual(a, b)
        self.assertEqual(a["points"], 5)

    def test_cache_invalidates_on_write(self):
        self._seed(3)
        before = memory.stats()["points"]
        self.assertEqual(before, 3)
        self._seed(1)  # 写入会 bump 版本号并失效缓存
        after = memory.stats()["points"]
        self.assertEqual(after, 4)

    def test_disabled_path_is_equivalent(self):
        self._seed(6)
        memory._DISABLE_STATS_CACHE = True
        recomputed = self._db_fields(memory.stats())
        memory._DISABLE_STATS_CACHE = False
        cached = self._db_fields(memory.stats())
        self.assertEqual(recomputed, cached)
        self.assertEqual(cached["points"], 6)


class TestLibraryReadPath(IsolatedTest):
    """第十六轮：书库读路径去冗余。

    init_db() 挂在 8 个读路径上却每次都重跑全套 DDL（还带一条靠抛异常
    判断「列已存在」的 ALTER）；list_dirs() 每个目录两条 COUNT。
    这两处都要在「变快」的同时保证结果一模一样，且换库后能自动重建表。
    """

    def _dirs(self, n=8, books_per_dir=5):
        library.init_db()
        c = memory.conn()
        for d in range(n):
            c.execute(
                "INSERT OR REPLACE INTO library_dirs(id,path,label,enabled,excludes,created_at)"
                " VALUES (?,?,?,?,?,?)",
                (f"d{d}", f"/lib/{d}", f"目录{d}", 1, '["*.tmp"]' if d % 2 else "[]",
                 time.time() - d * 60),
            )
            for b in range(books_per_dir):
                c.execute(
                    "INSERT OR REPLACE INTO books(id,path,title,format,size_bytes,chars,"
                    "pages,status,points_count,dir_id,source_type,created_at,topic) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"b{d}_{b}", f"/lib/{d}/b{b}.pdf", f"书 {d}-{b}", "pdf",
                     1000, 100, 10, "done" if b % 2 == 0 else "ready", b,
                     f"d{d}", "dir", time.time() - b * 60, "t"),
                )
        c.commit()

    def _sql_count(self, fn):
        seen: list[str] = []
        c = memory.conn()
        c.set_trace_callback(lambda s: seen.append(s))
        try:
            fn()
        finally:
            c.set_trace_callback(None)
        return len(seen)

    def test_init_db_runs_ddl_only_once(self):
        library.init_db()
        first = self._sql_count(library.init_db)
        second = self._sql_count(library.init_db)
        self.assertEqual(second, 1, f"守卫失效，仍在重复 DDL：{second} 条")
        self.assertLess(first, 10)  # 首轮允许建表，但不应膨胀

    def test_init_db_guard_is_equivalent(self):
        """守卫路径与全量 DDL 路径建出来的表结构必须一致。"""
        library.init_db()
        guarded = [r[1] for r in memory.conn().execute(
            "SELECT * FROM library_dirs LIMIT 0").description]
        library._INIT_DONE["db"] = None
        library._DISABLE_INIT_GUARD = True
        try:
            memory.conn().executescript("DROP TABLE IF EXISTS books")
            library.init_db()
        finally:
            library._DISABLE_INIT_GUARD = False
        rebuilt = [r[1] for r in memory.conn().execute(
            "SELECT * FROM library_dirs LIMIT 0").description]
        self.assertEqual(guarded, rebuilt)

    def test_init_db_rebuilds_when_db_replaced(self):
        """库文件被换掉（表没了）时，守卫要能发现并重建，不能返回空壳。"""
        library.init_db()
        memory.conn().executescript("DROP TABLE library_dirs")
        library.init_db()  # 不应抛异常
        n = memory.conn().execute(
            "SELECT COUNT(*) n FROM library_dirs").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_list_dirs_matches_n_plus_one(self):
        """聚合版与旧的每目录两条 COUNT 必须逐字段一致。"""
        self._dirs(8, 5)
        fast = library.list_dirs()
        library._DISABLE_DIRS_TALLY = True
        try:
            slow = library.list_dirs()
        finally:
            library._DISABLE_DIRS_TALLY = False
        self.assertEqual(fast, slow)
        self.assertEqual(len(fast), 8)
        self.assertEqual(sum(d["book_count"] for d in fast), 40)
        self.assertEqual(sum(d["imported_count"] for d in fast), 8 * 3)
        self.assertTrue(all(isinstance(d["excludes"], list) for d in fast))

    def test_list_dirs_sql_is_constant(self):
        """语句数不能随目录数增长（N+1 的判据）。"""
        self._dirs(4, 2)
        few = self._sql_count(library.list_dirs)
        self._dirs(20, 2)
        many = self._sql_count(library.list_dirs)
        self.assertEqual(few, many, f"仍随目录数增长：{few} → {many}")
        self.assertLessEqual(many, 2)

    def test_books_count_still_correct(self):
        """未改动的 list_books 双跑路径：总数与分页仍需正确（防回归）。"""
        self._dirs(4, 5)
        r = library.list_books(limit=3, offset=0)
        self.assertEqual(r["total"], 20)
        self.assertEqual(len(r["items"]), 3)
        r2 = library.list_books(dir_id="d1", limit=100)
        self.assertEqual(r2["total"], 5)
        self.assertTrue(all(b["dir_id"] == "d1" for b in r2["items"]))


if __name__ == "__main__":
    unittest.main()
