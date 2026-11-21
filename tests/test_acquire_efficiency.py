"""知识获取效率的回归用例：别把同一篇资料反复读、反复提炼。

覆盖三处改动：
  1. document_attempts 台账：读过（哪怕 0 条产出）就不再重读，但 learned_documents
     的语义不变——没产出知识的页面仍然不算「已学」。
  2. 段落选择：零信号段落与跨文档重复段落不再各花一次提炼调用。
  3. Internet Archive 检索：查询变量名写错导致该源永远返回空。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_regressions import IsolatedTest
from backend import config, evolve, llm, memory, search


DOC = {"ok": True, "url": "https://example.org/a", "title": "A", "text": "", "error": "", "kind": "web"}


class AttemptLedgerTests(IsolatedTest):
    def test_zero_yield_document_is_remembered_but_not_learned(self):
        url = "https://example.org/read-but-empty"
        memory.record_attempts([url], "ZBrush")
        self.assertEqual(memory.attempted_urls([url], "ZBrush"), {url})
        # 「读过」不等于「学到了」：问答与统计仍然只看 learned_documents
        self.assertFalse(memory.doc_seen(url, "ZBrush"))
        self.assertFalse(memory.doc_seen(url))

    def test_ledger_is_scoped_to_topic_and_expires(self):
        url = "https://example.org/topic-scoped"
        memory.record_attempts([url], "ZBrush")
        self.assertEqual(memory.attempted_urls([url], "Blender"), set())
        # 超过重读窗口后允许再读一次（内容可能已经更新）
        self.assertEqual(memory.attempted_urls([url], "ZBrush", days=0), set())

    def test_repeat_attempt_increments_counter_and_keeps_yield_flag(self):
        url = "https://example.org/twice"
        memory.record_attempts([url], "ZBrush")
        memory.record_attempts([url], "ZBrush", yielded=[url])
        stats = memory.attempt_stats("ZBrush")
        self.assertEqual(stats["attempts"], 2)
        self.assertEqual(stats["yielded"], 1)
        self.assertEqual(stats["documents"], 1)

    def test_extraction_marks_documents_it_actually_read(self):
        engine = evolve.Engine()
        doc = dict(DOC, text="DynaMesh resolution controls polygon density. " * 8)
        with patch.object(llm, "chat_json", return_value={"points": []}):
            self.assertEqual(engine._extract_all([doc], "ZBrush", ""), [])
        # 提炼过、0 条产出 → 记台账（下一轮不再抓这一篇）
        self.assertEqual(memory.attempted_urls([doc["url"]], "ZBrush"), {doc["url"]})
        self.assertFalse(memory.doc_seen(doc["url"], "ZBrush"))

    def test_failed_extraction_stays_retryable(self):
        engine = evolve.Engine()
        doc = dict(DOC, text="DynaMesh resolution controls polygon density. " * 8)
        with patch.object(llm, "chat_json", side_effect=llm.LLMError("模型超时")):
            engine._extract_all([doc], "ZBrush", "")
        # 提炼失败（模型/网络问题）不该被记成「读过」，否则这一篇永远补不上
        self.assertEqual(memory.attempted_urls([doc["url"]], "ZBrush"), set())

    def test_stop_does_not_record_attempts(self):
        engine = evolve.Engine()
        engine._stop.set()
        doc = dict(DOC, text="DynaMesh resolution controls polygon density. " * 8)
        with patch.object(llm, "chat_json", return_value={"points": []}):
            engine._extract_all([doc], "ZBrush", "")
        self.assertEqual(memory.attempted_urls([doc["url"]], "ZBrush"), set())

    def test_pick_targets_skips_urls_already_read(self):
        engine = evolve.Engine()
        engine.state["direction"] = "ZBrush"
        read = {"title": "read", "url": "https://example.org/read", "snippet": "", "kind": "web"}
        fresh = {"title": "fresh", "url": "https://example.org/fresh", "snippet": "", "kind": "web"}
        memory.record_attempts([read["url"]], "ZBrush")
        picked = engine._pick_targets([read, fresh])
        self.assertEqual([r["url"] for r in picked], [fresh["url"]])


class SectionSelectionTests(IsolatedTest):
    def test_off_topic_long_page_costs_one_call_instead_of_three(self):
        engine = evolve.Engine()
        noise = "本页介绍订阅价格、发票与售后联系方式。\n" * 200  # 远超 4200 字符
        doc = dict(DOC, text=noise)
        sections = engine._sections([doc], "ZBrush：DynaMesh 分辨率", "")
        self.assertEqual(len(sections), 1)

    def test_mirrored_page_is_extracted_once(self):
        engine = evolve.Engine()
        body = ("DynaMesh 的分辨率参数决定生成网格的拓扑密度，默认 128 适合中等细节。" * 40)
        a = dict(DOC, url="https://a.example/doc", text=body)
        b = dict(DOC, url="https://b.example/mirror", text=body)
        sections = engine._sections([a, b], "ZBrush：DynaMesh 分辨率", "")
        self.assertEqual(len({di for di, _ in sections}), 1)

    def test_mirror_only_documents_still_count_as_read(self):
        engine = evolve.Engine()
        body = "DynaMesh 的分辨率参数决定生成网格的拓扑密度。" * 40
        a = dict(DOC, url="https://a.example/doc", text=body)
        b = dict(DOC, url="https://b.example/mirror", text=body)
        with patch.object(llm, "chat_json", return_value={"points": []}):
            engine._extract_all([a, b], "ZBrush", "")
        self.assertEqual(
            memory.attempted_urls([a["url"], b["url"]], "ZBrush"), {a["url"], b["url"]}
        )


class ArchiveSourceTests(IsolatedTest):
    def test_archive_search_returns_items(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {
            "response": {"docs": [{"identifier": "demo-1", "title": "Demo lecture",
                                   "description": "3d printing"}]}
        }
        with patch.object(search, "_client", return_value=client):
            out = search.search_archive("3d printing tutorial", "audio", 3)
        self.assertEqual([r["url"] for r in out], ["https://archive.org/details/demo-1"])
        self.assertEqual(out[0]["kind"], "audio")

    def test_archive_query_is_strict_and_bounded(self):
        client = MagicMock()
        client.__enter__.return_value = client
        client.get.return_value.status_code = 200
        client.get.return_value.json.return_value = {"response": {"docs": []}}
        with patch.object(search, "_client", return_value=client):
            search.search_archive("resin support settings", "video", 4)
        params = client.get.call_args.kwargs["params"]
        self.assertIn("resin AND support AND settings", params["q"])
        self.assertIn("mediatype:(movies)", params["q"])


class IngestScaleTests(IsolatedTest):
    def test_identical_claim_still_merges_without_full_table_scan(self):
        memory.add_point(topic="ZBrush", claim="DynaMesh 默认分辨率 128",
                         detail="补充 A", vec=[1., 0.], dedup=False)
        out = memory.add_points_batch([
            {"topic": "ZBrush", "claim": "DynaMesh  默认分辨率 128", "detail": "补充 B",
             "vec": [1., 0.]},
        ])
        self.assertEqual(out["added"], 0)
        self.assertEqual(out["dup"], 1)
        self.assertEqual(memory.count_points(), 1)

    def test_conflicting_numbers_are_not_merged(self):
        memory.add_point(topic="ZBrush", claim="DynaMesh 默认分辨率 128",
                         detail="耗时 18 秒", vec=[1., 0.], dedup=False)
        out = memory.add_points_batch([
            {"topic": "ZBrush", "claim": "DynaMesh 默认分辨率 128",
             "detail": "耗时 42 秒", "vec": [1., 0.]},
        ])
        self.assertEqual(out["added"], 1)
        self.assertEqual(memory.count_points(), 2)


class DocRetryConfigTests(IsolatedTest):
    def test_retry_window_is_configurable_and_validated(self):
        self.assertEqual(config.get("doc_retry_days"), 7)
        config.update({"doc_retry_days": 3})
        self.assertEqual(config.get("doc_retry_days"), 3)
        with self.assertRaises(ValueError):
            config.update({"doc_retry_days": 0})


class LedgerScaleTests(IsolatedTest):
    def test_batch_query_crosses_sql_variable_limit(self):
        """一轮候选通常几十条，但补漏/整本书可能上千条；IN 查询要能分批。"""
        urls = [f"https://example.org/bulk/{i}" for i in range(1200)]
        memory.record_attempts(urls, "ZBrush")
        self.assertEqual(memory.attempted_urls(urls, "ZBrush"), set(urls))

    def test_both_tables_are_written_in_one_call(self):
        url = "https://example.org/both"
        memory.record_attempts([url], "ZBrush", yielded=[url], learned=[url])
        self.assertEqual(memory.attempted_urls([url], "ZBrush"), {url})
        self.assertTrue(memory.doc_seen(url, "ZBrush"))
        # 重复记录只累加尝试次数，不会把已学记录写重
        memory.record_attempts([url], "ZBrush", learned=[url])
        self.assertEqual(memory.attempt_stats("ZBrush")["documents"], 1)
        self.assertEqual(memory.attempt_stats("ZBrush")["attempts"], 2)

    def test_docs_learned_matches_doc_seen(self):
        memory.mark_learned("https://example.org/a", "ZBrush")
        memory.mark_learned("https://example.org/b", "Blender")
        urls = ["https://example.org/a", "https://example.org/b", "https://example.org/c"]
        self.assertEqual(memory.docs_learned(urls, "ZBrush"), {"https://example.org/a"})
        for u in urls:
            self.assertEqual(u in memory.docs_learned(urls, "ZBrush"),
                             memory.doc_seen(u, "ZBrush"))


class LedgerRobustnessTests(IsolatedTest):
    def test_ledger_failure_does_not_discard_extracted_points(self):
        engine = evolve.Engine()
        doc = dict(DOC, text="DynaMesh resolution controls polygon density. " * 8)
        point = {"claim": "DynaMesh 分辨率控制多边形密度", "detail": "提高分辨率增加面数。",
                 "evidence": "DynaMesh resolution controls polygon density.",
                 "relevance": .9, "confidence": .8}
        with patch.object(llm, "chat_json", side_effect=[{"points": [point]}, {"verified": [0]}]):
            with patch.object(memory, "record_attempts", side_effect=RuntimeError("database is locked")):
                got = engine._extract_all([doc], "ZBrush", "")
        self.assertEqual(len(got), 1)  # 台账写失败，知识点照样入库

    def test_worker_exception_keeps_other_sections(self):
        engine = evolve.Engine()
        good = dict(DOC, text="DynaMesh resolution controls polygon density. " * 8)
        point = {"claim": "DynaMesh 分辨率控制多边形密度", "detail": "提高分辨率增加面数。",
                 "evidence": "DynaMesh resolution controls polygon density.",
                 "relevance": .9, "confidence": .8}
        sections = [(0, good), (1, "这不是一个字典")]
        with patch.object(engine, "_sections", return_value=sections), \
             patch.object(llm, "chat_json", side_effect=[{"points": [point]}, {"verified": [0]}]):
            got = engine._extract_all([good, good], "ZBrush", "")
        self.assertEqual(len(got), 1)


class VerifyRetryTests(IsolatedTest):
    def _points(self, n):
        return [{"claim": f"结论 {i}：DynaMesh 分辨率影响密度", "detail": f"说明 {i}\n原文依据：分辨率",
                 "source_url": "https://e/x", "confidence": .7} for i in range(n)]

    def test_transient_failure_splits_instead_of_dropping_batch(self):
        calls = {"n": 0}

        def flaky(system, user, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise llm.LLMError("结构化输出失败：截断")
            n = len(json.loads(user))
            return {"verified": list(range(n))}

        with patch.object(llm, "chat_json", side_effect=flaky):
            got = evolve.Engine()._verify_points(self._points(12))
        self.assertEqual(len(got), 12)  # 整批不再因为一次失败全丢
        self.assertEqual(calls["n"], 3)  # 1 次失败 + 2 次拆半

    def test_persistent_failure_gives_up_without_raising(self):
        with patch.object(llm, "chat_json", side_effect=llm.LLMError("模型不可用")):
            got = evolve.Engine()._verify_points(self._points(5))
        self.assertEqual(got, [])


class MediaCacheTests(IsolatedTest):
    def test_repeated_media_query_hits_cache(self):
        hits = {"n": 0}

        def fake(query, kind="image", limit=6):
            hits["n"] += 1
            return [{"title": "t", "url": f"https://m/{kind}/{hits['n']}", "kind": kind}]

        search.media_cache_clear()
        with patch.dict(search.MEDIA_SOURCES, {"commons": fake}, clear=True):
            first = search.search_media("ZBrush 衣褶", kinds=["image"], limit=3)
            second = search.search_media("ZBrush 衣褶", kinds=["image"], limit=3)
        self.assertEqual(hits["n"], 1)
        self.assertEqual([r["url"] for r in first], [r["url"] for r in second])
        # 不同参数不能串味
        with patch.dict(search.MEDIA_SOURCES, {"commons": fake}, clear=True):
            search.search_media("Blender 衣褶", kinds=["image"], limit=3)
        self.assertEqual(hits["n"], 2)
        search.media_cache_clear()

    def test_clear_resets_cache(self):
        hits = {"n": 0}

        def fake(query, kind="image", limit=6):
            hits["n"] += 1
            return [{"title": "t", "url": "https://m/1", "kind": kind}]

        search.media_cache_clear()
        with patch.dict(search.MEDIA_SOURCES, {"commons": fake}, clear=True):
            search.search_media("q1", kinds=["image"], limit=2)
            search.media_cache_clear()
            search.search_media("q1", kinds=["image"], limit=2)
        self.assertEqual(hits["n"], 2)


class PickTargetsQueryCountTests(IsolatedTest):
    def test_candidate_filtering_uses_bounded_queries(self):
        for i in range(24):
            memory.record_attempts([f"https://x/{i}"], "ZBrush", learned=[f"https://x/{i}"])
        results = [{"url": f"https://x/{i}", "title": f"t{i}", "snippet": "", "kind": "web"}
                   for i in range(60)]
        engine = evolve.Engine()
        engine.state["direction"] = "ZBrush"
        counts = {"select": 0}

        def trace(sql):
            if sql.strip().upper().startswith("SELECT"):
                counts["select"] += 1

        conn = memory.conn()
        conn.set_trace_callback(trace)
        try:
            with patch.object(llm, "chat_json", return_value={"pick": [1, 2, 3]}):
                picked = engine._pick_targets(results)
        finally:
            conn.set_trace_callback(None)
        self.assertEqual(len(picked), 3)
        # 60 个候选只查 2 次（台账 + 已学），而不是 60 次
        self.assertLessEqual(counts["select"], 3)


class StallBreakerTests(IsolatedTest):
    def _run(self, rounds, gains):
        engine = evolve.Engine()
        calls = {"n": 0}

        def fake_round(direction, r, total, sid):
            calls["n"] += 1
            gain = gains(calls["n"])
            engine.state["fetched"] += gain

        with patch.object(evolve, "emit", lambda *a, **k: None), \
             patch.object(llm, "tags", return_value=(True, [])), \
             patch.object(llm, "ensure_models", lambda *a, **k: None), \
             patch.object(engine, "_round", side_effect=fake_round):
            engine._run("ZBrush", rounds, "sid-test")
        return calls["n"]

    def test_idle_rounds_stop_early(self):
        self.assertEqual(self._run(100, lambda n: 0), evolve.STALL_LIMIT)

    def test_reading_something_keeps_going(self):
        # 每轮都读到 1 篇新资料：不该被熔断
        self.assertEqual(self._run(5, lambda n: 1), 5)

    def test_exactly_at_limit_with_no_rounds_left_is_fine(self):
        # 跑满 3 轮空转时已经没有剩余轮次，不该报「提前结束」
        self.assertEqual(self._run(evolve.STALL_LIMIT, lambda n: 0), evolve.STALL_LIMIT)


class FunnelTests(IsolatedTest):
    def test_round_funnel_counts_each_stage(self):
        engine = evolve.Engine()
        engine._funnel = {k: 0 for k in evolve.Engine.FUNNEL_KEYS}
        doc = dict(DOC, text="DynaMesh resolution controls polygon density. " * 8)
        point = {"claim": "DynaMesh 分辨率控制多边形密度", "detail": "提高分辨率增加面数。",
                 "evidence": "DynaMesh resolution controls polygon density.",
                 "relevance": .9, "confidence": .8}
        with patch.object(llm, "chat_json", side_effect=[{"points": [point]}, {"verified": [0]}]):
            engine._extract_all([doc], "ZBrush", "")
        self.assertEqual(engine._funnel["sections"], 1)
        self.assertEqual(engine._funnel["candidates"], 1)
        self.assertEqual(engine._funnel["verified"], 1)
        self.assertEqual(engine._funnel["added"], 1)
        self.assertIn("本轮漏斗", engine._funnel_text())


class CjkTermTests(IsolatedTest):
    def test_function_word_bigrams_are_dropped(self):
        terms = search.cjk_terms("ZBrush：衣褶的受力点与主次关系")
        self.assertIn("衣褶", terms)
        self.assertIn("受力", terms)
        self.assertNotIn("的受", terms)
        self.assertNotIn("与主", terms)

    def test_irrelevant_page_no_longer_scores(self):
        query = "ZBrush：衣褶的受力点与主次关系"
        relevant = {"title": "ZBrush 衣褶雕刻：受力点与主次关系",
                    "url": "https://a.example/folds", "snippet": "褶皱的受力与主次",
                    "source": "web", "kind": "web"}
        # 旧口径会靠「与主」命中（「与主机」）混进候选
        junk = {"title": "3D 打印与主机配置指南", "url": "https://b.example/host",
                "snippet": "主板与主机选型", "source": "web", "kind": "web"}
        got = [r["url"] for r in search.rank_results([junk, relevant], query)]
        self.assertEqual(got, [relevant["url"]])

    def test_single_character_run_is_ignored(self):
        self.assertEqual(search.cjk_terms("的"), set())


class AcquisitionApiTests(IsolatedTest):
    def test_endpoint_reports_yield_rate(self):
        from backend.main import api_analytics_acquisition
        memory.record_attempts(["https://e/1", "https://e/2"], "ZBrush",
                               yielded=["https://e/1"], learned=["https://e/1"])
        out = api_analytics_acquisition(limit=5)
        self.assertEqual(out["documents"], 2)
        self.assertEqual(out["yielded"], 1)
        self.assertEqual(out["yield_rate"], 0.5)
        self.assertEqual(out["retry_days"], config.get("doc_retry_days"))
        self.assertEqual(out["topics"][0]["topic"], "ZBrush")


class MaintenanceTests(IsolatedTest):
    def test_backfill_only_takes_recent_documents(self):
        c = memory.conn()
        now = time.time()
        with c:
            c.execute("DELETE FROM maintenance WHERE name='attempts_backfill'")
            c.execute("INSERT INTO documents(url,title,topic,chars,ok,fetched_at) VALUES"
                      " ('https://e/new','n','ZBrush',100,1,?)", (now - 3600,))
            c.execute("INSERT INTO documents(url,title,topic,chars,ok,fetched_at) VALUES"
                      " ('https://e/old','o','ZBrush',100,1,?)", (now - 90 * 86400,))
        memory._backfill_attempts(c)
        c.commit()
        got = memory.attempted_urls(["https://e/new", "https://e/old"], "ZBrush")
        self.assertEqual(got, {"https://e/new"})

    def test_backfill_skips_documents_that_already_yielded(self):
        c = memory.conn()
        now = time.time()
        memory.mark_learned("https://e/learned", "ZBrush")
        with c:
            c.execute("DELETE FROM maintenance WHERE name='attempts_backfill'")
            c.execute("INSERT INTO documents(url,title,topic,chars,ok,fetched_at) VALUES"
                      " ('https://e/learned','l','ZBrush',100,1,?)", (now - 60,))
        memory._backfill_attempts(c)
        c.commit()
        # 已学的不进台账（learned_documents 已经能拦住它）
        self.assertEqual(memory.attempted_urls(["https://e/learned"], "ZBrush"), set())

    def test_daily_prune_removes_only_expired_rows(self):
        c = memory.conn()
        now = time.time()
        memory.record_attempts(["https://e/fresh"], "ZBrush")
        with c:
            c.execute("DELETE FROM maintenance WHERE name='attempts_prune'")
            c.execute("INSERT INTO document_attempts(url,topic,attempts,yielded,first_at,last_at)"
                      " VALUES ('https://e/stale','ZBrush',1,0,?,?)",
                      (now - 400 * 86400, now - 400 * 86400))
        memory._daily_maintenance(c)
        c.commit()
        left = memory.attempted_urls(["https://e/fresh", "https://e/stale"], "ZBrush")
        self.assertEqual(left, {"https://e/fresh"})

    def test_prune_runs_at_most_once_per_day(self):
        c = memory.conn()
        memory._daily_maintenance(c)
        c.commit()
        first = c.execute("SELECT done_at FROM maintenance WHERE name='attempts_prune'").fetchone()
        memory._daily_maintenance(c)
        second = c.execute("SELECT done_at FROM maintenance WHERE name='attempts_prune'").fetchone()
        self.assertEqual(first["done_at"], second["done_at"])


class EmbedCacheTests(IsolatedTest):
    def test_repeated_text_is_embedded_once(self):
        calls = {"n": 0, "payload": []}

        def fake_remote(payload, model, batch):
            calls["n"] += 1
            calls["payload"].append(list(payload))
            return [[float(len(t)), 1.0] for t in payload]

        with patch.object(llm, "_embed_remote", side_effect=fake_remote):
            first = llm.embed(["ZBrush：衣褶"])
            second = llm.embed(["ZBrush：衣褶"])
        self.assertEqual(calls["n"], 1)
        self.assertEqual(first, second)

    def test_duplicate_texts_in_one_batch_are_merged(self):
        seen = {}

        def fake_remote(payload, model, batch):
            seen["payload"] = list(payload)
            return [[1.0, 0.0] for _ in payload]

        with patch.object(llm, "_embed_remote", side_effect=fake_remote):
            out = llm.embed(["a", "b", "a"])
        self.assertEqual(seen["payload"], ["a", "b"])
        self.assertEqual(out, [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]])

    def test_cache_is_bounded(self):
        with patch.object(llm, "_embed_remote", side_effect=lambda p, m, b: [[1.0] for _ in p]):
            with patch.object(llm, "EMBED_CACHE_MAX", 4):
                for i in range(10):
                    llm.embed([f"text-{i}"])
        self.assertLessEqual(len(llm._embed_cache), 4)

    def test_returned_vectors_are_copies(self):
        with patch.object(llm, "_embed_remote", side_effect=lambda p, m, b: [[1.0, 2.0] for _ in p]):
            first = llm.embed(["same"])
            first[0][0] = 99.0
            second = llm.embed(["same"])
        self.assertEqual(second[0], [1.0, 2.0])


class PrefilterWindowTests(IsolatedTest):
    def test_prefilter_window_covers_configured_quota(self):
        """fetch_per_round 可配到 50，预筛窗口不能还是硬编码的 24。"""
        config.update({"fetch_per_round": 30})
        results = [{"url": f"https://w/{i}", "title": f"t{i}", "snippet": "", "kind": "web"}
                   for i in range(40)]
        engine = evolve.Engine()
        engine.state["direction"] = "ZBrush"
        with patch.object(llm, "chat_json", return_value={"pick": [30]}):
            picked = engine._pick_targets(results)
        self.assertEqual([r["url"] for r in picked], ["https://w/29"])


class ConcurrencyTests(IsolatedTest):
    def test_concurrent_ledger_writes_do_not_fail(self):
        errors: list[Exception] = []

        def writer(n):
            try:
                for i in range(20):
                    memory.record_attempts([f"https://c/{n}/{i}"], "ZBrush")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(memory.attempt_stats("ZBrush")["documents"], 80)


class SettingsConsistencyTests(IsolatedTest):
    def test_retry_setting_exists_in_both_html_and_js(self):
        """设置项只在 HTML 出现、或只在 JS 出现，都会变成「显示但存不下去」。"""
        root = Path(__file__).resolve().parent.parent
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        js = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="cfg-retry"', html)
        self.assertIn("#cfg-retry", js)
        self.assertIn("doc_retry_days", js)
