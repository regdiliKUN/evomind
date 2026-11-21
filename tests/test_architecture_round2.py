"""第二轮四项架构重构的验收用例：A3 / A6 / B2 / D8。

对应原评审报告的「可验证验收标准」：

* A3  keyword_search 的 score 不再恒为 0、精确术语可召回、混合召回有归一化融合；
      active 状态过滤下推到回表查询，archived 不会从索引漏进结果。
* A6  同一类 warn 连续出现 N 次触发**一条**聚合告警；台账归档无损、dry_run 只读；
      过期行搬走后 events 主表行数有上界。
* B2  三本主题不同的书归到同一学科主题后跨书近重复可被 cleanup 检出；
      重分类不改 claim/detail/embedding；词表与别名有 CRUD。
* D8  产出率只认实际入库；三段漏斗（candidates/verified/ingested）分别落列；
      「只有重复候选」的资料不再被标成已学。

刻意不覆盖 A3 的 ANN / 100k 压测 / recall@10：本轮没有引入 ANN 索引，
向量侧仍是 O(N·D) 全量余弦，这部分验收标准仍未达成，不能写成通过。
"""
from __future__ import annotations

import json
import time
from unittest.mock import patch

from test_regressions import IsolatedTest
from backend import cleanup, config, evolve, event_ops, governance, llm, memory, search_index, topics


DOC = {"ok": True, "url": "https://example.org/funnel", "title": "Funnel",
       "text": "", "error": "", "kind": "web"}
EXACT = "ZBrush 4R6 DynaMesh 分辨率 128 控制多边形密度"


class _Round2Test(IsolatedTest):
    def setUp(self):
        super().setUp()
        # 每个用例换一个临时目录，索引库的可用性缓存也要跟着重置
        search_index._available = None

    def _add(self, topic, claim, *, detail="", vec=None):
        return memory.add_point(topic=topic, claim=claim, detail=detail,
                                vec=vec or [1.0, 0.0], dedup=False)


# --------------------------------------------------------------------------
# A3 · FTS5 / BM25 / RRF
# --------------------------------------------------------------------------
class A3LexicalIndexTests(_Round2Test):
    def test_rebuild_counts_all_rows_and_status_is_fresh(self):
        for i in range(3):
            self._add("ZBrush", f"DynaMesh 结论 {i}")
        out = search_index.rebuild()
        self.assertTrue(out["available"], out)
        self.assertEqual(out["indexed"], 3)
        self.assertEqual(out["tokenizer"], "trigram")
        st = search_index.status()
        self.assertEqual(st["indexed"], 3)
        self.assertEqual(st["knowledge_total"], 3)
        self.assertFalse(st["stale"])

    def test_bm25_scores_exact_terms_instead_of_zero(self):
        self._add("ZBrush", EXACT, detail="Cura 切片参数与 ZBrush 4R6 无关")
        self._add("Cooking", "红烧肉需要冰糖与老抽，和 DynaMesh 完全无关")
        search_index.rebuild()
        hits = search_index.bm25_search("ZBrush 4R6", k=5)
        self.assertTrue(hits, "精确术语/型号应能由 trigram 索引召回")
        self.assertIn("ZBrush 4R6", hits[0]["claim"])
        self.assertGreater(hits[0]["score"], 0.0)
        self.assertGreater(hits[0]["bm25"], 0.0)

    def test_keyword_search_returns_real_scores(self):
        self._add("ZBrush", EXACT)
        search_index.rebuild()
        hits = memory.keyword_search("ZBrush 4R6", k=5)
        self.assertTrue(hits)
        self.assertGreater(hits[0]["score"], 0.0)

    def test_hybrid_rrf_marks_both_hits_and_ranks_them_first(self):
        a = {"id": "a", "claim": "both", "score": 0.9}
        b = {"id": "b", "claim": "vector only", "score": 0.8}
        d = {"id": "d", "claim": "lexical only", "score": 0.7}
        out = search_index.hybrid_search(
            "ZBrush 4R6", k=3, alpha=0.6,
            vector_hits=[a, b], lexical_hits=[a, d],
        )
        self.assertEqual(out["method"], "hybrid_rrf")
        self.assertEqual(out["rrf_k"], search_index.RRF_K)
        self.assertEqual(out["both"], 1)
        by_id = {x["id"]: x for x in out["items"]}
        self.assertEqual(by_id["a"]["match"], "both")
        self.assertEqual(by_id["a"]["vector_rank"], 1)
        self.assertEqual(by_id["a"]["lexical_rank"], 1)
        self.assertEqual(by_id["b"]["match"], "vector")
        self.assertEqual(by_id["d"]["match"], "lexical")
        self.assertGreater(by_id["a"]["rrf_score"], by_id["b"]["rrf_score"])
        self.assertGreater(by_id["a"]["rrf_score"], by_id["d"]["rrf_score"])

    @staticmethod
    def _disjoint_hits(n: int = 12):
        vec = [{"id": f"v{i}", "claim": f"向量命中 {i}", "score": 1.0 - i * 0.01}
               for i in range(1, n + 1)]
        lex = [{"id": f"l{i}", "claim": f"词法命中 {i}", "score": 1.0 - i * 0.01}
               for i in range(1, n + 1)]
        return vec, lex

    def test_default_alpha_lets_lexical_hits_into_topk(self):
        """alpha=0.6 会让向量前 31 名全部压过词法第 1 名，纯词法整路被挤出。"""
        vec, lex = self._disjoint_hits()
        out = search_index.hybrid_search("ZBrush 4R6", k=10,
                                         vector_hits=vec, lexical_hits=lex)
        self.assertEqual(out["alpha"], 0.5)
        lex_in_topk = sum(1 for x in out["items"] if str(x["id"]).startswith("l"))
        self.assertGreaterEqual(lex_in_topk, 4,
                                f"默认等权下词法命中仍被压制：{out['items']}")
        self.assertGreater(lex_in_topk, 0)

    def test_explicit_alpha_still_expresses_vector_preference(self):
        """alpha 仍是有效旋钮；显式强偏向量时词法席位会如预期减少。"""
        vec, lex = self._disjoint_hits()
        out = search_index.hybrid_search("ZBrush 4R6", k=10, alpha=0.9,
                                         vector_hits=vec, lexical_hits=lex)
        lex_in_topk = sum(1 for x in out["items"] if str(x["id"]).startswith("l"))
        self.assertLessEqual(lex_in_topk, 1)
        self.assertEqual(out["alpha"], 0.9)

    def test_archived_rows_never_leak_back_through_the_index(self):
        pid = self._add("ZBrush", EXACT)
        search_index.rebuild()
        self.assertTrue(search_index.bm25_search("ZBrush 4R6", k=5))
        governance.set_status(pid, "archived", reason="A3 过滤验收")
        # 索引里仍留着旧行，但回表查询用 ACTIVE_FILTER 二次过滤
        hits = search_index.bm25_search("ZBrush 4R6", k=5)
        self.assertNotIn(pid, [h["id"] for h in hits])

    def test_short_query_does_not_crash_fts(self):
        self._add("ZBrush", EXACT)
        search_index.rebuild()
        self.assertEqual(search_index.bm25_search("ab", k=5), [])


# --------------------------------------------------------------------------
# A6 · 事件聚合 / 无损归档
# --------------------------------------------------------------------------
class A6EventOpsTests(_Round2Test):
    def test_repeated_warn_yields_one_aggregated_alert(self):
        n = event_ops.ALERT_THRESHOLD
        for i in range(n):
            memory.log_event("warn", f"重复告警 {i}")
        alerts = event_ops.alerts()
        self.assertEqual(len(alerts), 1, f"{n} 条 warn 应只产出 1 条聚合告警")
        self.assertEqual(alerts[0]["n"], n)
        self.assertEqual(alerts[0]["kind"], "warn")
        # 聚合告警本身写进 events，但只有一条，而不是 20 条可见噪音
        rows = memory.conn().execute(
            "SELECT COUNT(*) n FROM events WHERE kind='alert'").fetchone()
        self.assertEqual(rows["n"], 1)
        self.assertGreaterEqual(event_ops.rollups()["total"], n)

    def test_info_events_do_not_create_alerts(self):
        for i in range(event_ops.ALERT_THRESHOLD + 5):
            memory.log_event("info", f"普通事件 {i}")
        self.assertEqual(event_ops.alerts(), [])

    def _old_events(self, n=3):
        c = memory.conn()
        old = time.time() - 100 * 86400
        with c:
            for i in range(n):
                c.execute("INSERT INTO events(ts,kind,message,payload) VALUES(?,?,?,?)",
                          (old, "warn", f"old-{i}", json.dumps({"i": i})))
        return c

    def test_archive_dry_run_is_read_only(self):
        c = self._old_events(3)
        before = c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
        out = event_ops.archive(c=c, dry_run=True, keep_days=30, batch=100)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["moved"]["events"], 3)
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM events").fetchone()["n"], before)
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM ops_archive").fetchone()["n"], 0)

    def test_archive_moves_rows_without_losing_content(self):
        c = self._old_events(3)
        event_ops.archive(c=c, dry_run=False, keep_days=30, batch=100)
        c.commit()
        self.assertEqual(c.execute(
            "SELECT COUNT(*) n FROM events WHERE message LIKE 'old-%'").fetchone()["n"], 0)
        rows = c.execute("SELECT origin,row_json FROM ops_archive WHERE origin='events'").fetchall()
        self.assertEqual(len(rows), 3)
        saved = {json.loads(r["row_json"])["message"] for r in rows}
        self.assertEqual(saved, {"old-0", "old-1", "old-2"})

    def test_ops_status_exposes_bounded_main_tables(self):
        self._old_events(2)
        s = event_ops.ops_status()
        for key in ("events", "rollup_rows", "alerts", "archived", "keep_days"):
            self.assertIn(key, s)


# --------------------------------------------------------------------------
# B2 · 受控主题词表 / 别名 / 重分类
# --------------------------------------------------------------------------
BOOK_TOPICS = (
    "书库 · ZBrush 入门 (张三) (Z-Library)",
    "书库 · ZBrush 进阶 (李四) (人民邮电出版社)",
    "书库 · ZBrush 实战 (王五)",
)


class B2TopicsTests(_Round2Test):
    def test_normalize_strips_book_decorations_deterministically(self):
        self.assertEqual(topics.normalize(BOOK_TOPICS[0]), "ZBrush 入门")
        self.assertEqual(topics.normalize("  书库 · 主题  "), "主题")
        self.assertEqual(topics.normalize("普通主题"), "普通主题")

    def test_vocabulary_and_alias_crud(self):
        self.assertEqual(topics.vocabulary(), [])
        topics.add_term("ZBrush 雕刻", parent="3D 建模", note="学科主题")
        self.assertEqual(len(topics.vocabulary()), 1)
        topics.add_alias(BOOK_TOPICS[0], "ZBrush 雕刻")
        self.assertEqual(topics.canonical(BOOK_TOPICS[0]), "ZBrush 雕刻")
        self.assertEqual(len(topics.aliases()), 1)
        topics.remove_alias(BOOK_TOPICS[0])
        self.assertEqual(topics.aliases(), [])
        topics.remove_term("ZBrush 雕刻")
        self.assertEqual(topics.vocabulary(), [])

    def test_group_key_only_changes_for_registered_aliases(self):
        # 没登记别名时行为完全不变，避免悄悄改变既有去重语义
        self.assertEqual(topics.group_key(BOOK_TOPICS[0]), BOOK_TOPICS[0])
        topics.add_alias(BOOK_TOPICS[0], "ZBrush 雕刻")
        self.assertEqual(topics.group_key(BOOK_TOPICS[0]), "ZBrush 雕刻")
        self.assertEqual(topics.group_key(BOOK_TOPICS[1]), BOOK_TOPICS[1])

    def test_reclassify_dry_run_then_preserves_claim_detail_embedding(self):
        ids = [self._add(t, "DynaMesh 默认分辨率 128", detail="补充资料") for t in BOOK_TOPICS]
        c = memory.conn()
        before = {pid: tuple(c.execute(
            "SELECT claim,detail,embedding,topic FROM knowledge WHERE id=?", (pid,)
        ).fetchone()) for pid in ids}
        dry = topics.reclassify(BOOK_TOPICS, "ZBrush 雕刻", dry_run=True)
        self.assertEqual(dry["matched"], 3)
        self.assertEqual(dry["changed"], 0)
        self.assertEqual(topics.canonical(BOOK_TOPICS[0]), "ZBrush 入门")

        real = topics.reclassify(BOOK_TOPICS, "ZBrush 雕刻", dry_run=False,
                                 actor="tester", reason="B2 验收")
        self.assertEqual(real["changed"], 3)
        for pid, (claim, detail, emb, _) in before.items():
            row = c.execute(
                "SELECT claim,detail,embedding,topic,revision FROM knowledge WHERE id=?",
                (pid,)).fetchone()
            self.assertEqual(row["claim"], claim)
            self.assertEqual(row["detail"], detail)
            self.assertEqual(row["embedding"], emb)
            self.assertEqual(row["topic"], "ZBrush 雕刻")
        snaps = c.execute(
            "SELECT COUNT(*) n FROM knowledge_revisions WHERE kind='reclassify'"
        ).fetchone()["n"]
        self.assertGreaterEqual(snaps, 3)

    def test_three_books_merged_detects_cross_book_duplicates(self):
        for t in BOOK_TOPICS:
            self._add(t, "DynaMesh 默认分辨率 128", detail="跨书重复", vec=[1.0, 0.0])
        # 未登记别名：三个 topic 是三个互不相通的分组，跨书重复检不出
        self.assertEqual(len(cleanup.preview(threshold=.9)["items"]), 0)
        for t in BOOK_TOPICS:
            topics.add_alias(t, "ZBrush 雕刻")
        # 归并后落在同一分组，2 条近重复应被检出
        items = cleanup.preview(threshold=.9)["items"]
        self.assertEqual(len(items), 2)
        self.assertTrue(all(i["sim"] >= .9 for i in items))
        self.assertEqual(topics.clusters()["raw_topics"], 3)

    def test_clusters_reports_dual_basis_and_warns_on_normalize_only_merges(self):
        # F3：两个不同书名 normalize 后显示成同一规范主题，但都没登记别名，
        # 视图口径说「1 组」、去重口径仍是「2 组」。必须显式报出，不能静默。
        a = "书库 · 野外生存之旅--百法结绳(基础篇)"
        b = "书库 · 野外生存之旅--百法结绳(完结篇)"
        self.assertEqual(topics.normalize(a), topics.normalize(b))
        # claim 必须 >= 8 字，否则 add_point 会静默拒收（既有行为）
        self._add(a, "结绳基础手法与绳结选择")
        self._add(b, "结绳进阶手法与承重应用")

        out = topics.clusters()
        self.assertEqual(out["raw_topics"], 2)
        self.assertEqual(out["canonical_topics"], 1)   # 视图口径：已合并
        self.assertEqual(out["dedup_groups"], 2)       # 去重口径：仍彼此独立
        self.assertFalse(out["grouping_consistent"])
        self.assertEqual(len(out["normalize_only_merges"]), 1)
        merge = out["normalize_only_merges"][0]
        self.assertEqual(set(merge["members"]), {a, b})
        self.assertEqual(merge["dedup_groups"], 2)
        self.assertIn("登记别名", out["warning"])
        self.assertEqual(out["alias_registered_topics"], 0)

        # 显式登记别名后两个口径重新一致，警示消失
        for t in (a, b):
            topics.add_alias(t, "野外生存技能")
        out2 = topics.clusters()
        self.assertEqual(out2["dedup_groups"], 1)
        self.assertTrue(out2["grouping_consistent"])
        self.assertEqual(out2["normalize_only_merges"], [])
        self.assertEqual(out2["warning"], "")
        self.assertEqual(out2["alias_registered_topics"], 2)

    def test_clusters_is_consistent_when_no_normalize_only_merge(self):
        # 没有「视图合并但去重未合并」时不许误报警示
        self._add("ZBrush", "DynaMesh 基础分辨率")
        self._add("Blender", "Blender 重拓扑基础流程")
        out = topics.clusters()
        self.assertTrue(out["grouping_consistent"])
        self.assertEqual(out["normalize_only_merges"], [])
        self.assertEqual(out["warning"], "")
        self.assertEqual(out["dedup_groups"], 2)


# --------------------------------------------------------------------------
# D8 · 三段漏斗与产出率口径
# --------------------------------------------------------------------------
class D8YieldRateTests(_Round2Test):
    def test_three_stage_funnel_and_primary_yield_rate(self):
        u1, u2 = "https://e/1", "https://e/2"
        memory.record_attempts([u1, u2], "ZBrush",
                               candidates=[u1, u1, u2], verified=[u1], ingested=[u1])
        s = memory.attempt_stats("ZBrush")
        self.assertEqual(s["candidates"], 3)
        self.assertEqual(s["verified"], 1)
        self.assertEqual(s["ingested"], 1)
        out = memory.acquisition_report()
        self.assertEqual(out["metric_basis"], "actual_ingested")
        self.assertEqual(out["produced_documents"], 1)
        self.assertEqual(out["yield_rate"], 0.5)
        self.assertEqual(out["funnel_rate"], round(1 / 3, 3))
        self.assertEqual(out["funnel"], {"attempted": 2, "candidates": 3,
                                         "verified": 1, "ingested": 1})

    def test_legacy_callers_fall_back_explicitly(self):
        memory.record_attempts(["https://e/1", "https://e/2"], "ZBrush",
                               yielded=["https://e/1"], learned=["https://e/1"])
        out = memory.acquisition_report()
        # 老库/旧调用方没有漏斗数据，不能让产出率一夜掉成 0%，但要标明口径
        self.assertEqual(out["metric_basis"], "legacy_yielded")
        self.assertEqual(out["yield_rate"], 0.5)

    def test_evolve_does_not_mark_duplicate_only_document_as_learned(self):
        doc = dict(DOC, text="DynaMesh resolution controls polygon density. " * 8)
        point = {"claim": "DynaMesh 分辨率控制多边形密度", "detail": "提高分辨率增加面数。",
                 "evidence": "DynaMesh resolution controls polygon density.",
                 "relevance": .9, "confidence": .8}
        fake = {"accepted_points": [], "ids": [], "dup": 1, "added": 0, "rejected": 0}
        with patch.object(llm, "chat_json", side_effect=[{"points": [point]},
                                                         {"verified": [0]}]), \
             patch.object(memory, "add_points_batch", return_value=fake):
            got = evolve.Engine()._extract_all([doc], "ZBrush", "")
        self.assertEqual(got, [])
        # 旧实现会把这个 URL 同时写进 yielded 与 learned_documents
        self.assertFalse(memory.doc_seen(doc["url"], "ZBrush"))
        s = memory.attempt_stats("ZBrush")
        self.assertEqual((s["candidates"], s["verified"], s["ingested"]), (1, 1, 0))
        self.assertEqual(s["yielded"], 0)
        out = memory.acquisition_report()
        self.assertEqual(out["metric_basis"], "actual_ingested")
        self.assertEqual(out["yield_rate"], 0.0)

    def test_evolve_counts_only_real_ingested_rows(self):
        doc = dict(DOC, text="DynaMesh resolution controls polygon density. " * 8)
        point = {"claim": "DynaMesh 分辨率控制多边形密度", "detail": "提高分辨率增加面数。",
                 "evidence": "DynaMesh resolution controls polygon density.",
                 "relevance": .9, "confidence": .8}
        accepted = {"id": "p1", "claim": point["claim"], "source_url": doc["url"]}
        fake = {"accepted_points": [accepted], "ids": ["p1"], "dup": 0,
                "added": 1, "rejected": 0}
        with patch.object(llm, "chat_json", side_effect=[{"points": [point]},
                                                         {"verified": [0]}]), \
             patch.object(memory, "add_points_batch", return_value=fake):
            evolve.Engine()._extract_all([doc], "ZBrush", "")
        s = memory.attempt_stats("ZBrush")
        self.assertEqual((s["candidates"], s["verified"], s["ingested"]), (1, 1, 1))
        self.assertEqual(s["yielded"], 1)
        self.assertTrue(memory.doc_seen(doc["url"], "ZBrush"))
        self.assertEqual(memory.acquisition_report()["yield_rate"], 1.0)
