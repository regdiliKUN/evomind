"""17,698 条无引文历史知识复核流水线的验收用例。

三条红线：
  1. plan() 必须能在**没有 evidence/status 列的未迁移老库**上纯只读跑通；
  2. 本地逐字补证只能补 evidence/locator，绝不把知识标成 verified；
  3. 写操作与联网复检默认 dry_run，不显式开启就不能动库、不能发请求。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from test_regressions import IsolatedTest
from backend import config, memory, review_pipeline

OCR = ("DynaMesh 的默认分辨率为 128，它直接决定生成网格的多边形密度与拓扑结构，"
       "过低会丢失细节，过高会显著增加布尔运算耗时。")
LITERAL_CLAIM = "DynaMesh 的默认分辨率为 128，它直接决定生成网格的多边形密度"
PARAPHRASE_CLAIM = "提高 DynaMesh 分辨率会让网格更细，但布尔运算更慢"


class ReviewPipelineTests(IsolatedTest):
    def _seed(self, rows):
        c = memory.conn()
        with c:
            c.execute("CREATE TABLE IF NOT EXISTS ocr_pages("
                      "id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT, "
                      "page_no INTEGER, text TEXT)")
            c.execute("INSERT INTO ocr_pages(book_id,page_no,text) VALUES('b1',1,?)", (OCR,))
            for pid, claim, url, book_id in rows:
                c.execute(
                    "INSERT INTO knowledge(id,topic,claim,detail,source_url,source_kind,"
                    "book_id,created_at,evidence,status,revision) "
                    "VALUES(?,?,?,?,?,'book',?,?,?,'legacy',1)",
                    (pid, "ZBrush", claim, "补充说明", url, book_id, 1.0, ""),
                )
        return c

    def test_plan_is_read_only_and_splits_tiers(self):
        self._seed([
            ("p1", LITERAL_CLAIM, "file:///K:/books/a.pdf", "b1"),
            ("p2", PARAPHRASE_CLAIM, "file:///K:/books/a.pdf", "b1"),
            ("p3", "网页结论", "https://example.org/a", ""),
            ("p4", "无来源结论", "", ""),
        ])
        out = review_pipeline.plan(sample=2)
        self.assertTrue(out["available"])
        self.assertTrue(out["read_only"])
        self.assertEqual(out["no_evidence_total"], 4)
        self.assertEqual(out["tiers"]["local_quote"]["count"], 2)
        self.assertEqual(out["tiers"]["url_recheck"]["count"], 1)
        self.assertEqual(out["tiers"]["url_recheck"]["distinct_urls"], 1)
        self.assertEqual(out["tiers"]["unresolvable"]["count"], 1)
        self.assertIn("honest_note", out)

    def test_plan_works_on_unmigrated_13_column_legacy_db(self):
        """真实老库没有 evidence/status 列；plan 不能因为引用新列而崩掉。"""
        legacy = Path(self.tmp.name) / "legacy.db"
        c = sqlite3.connect(str(legacy))
        c.executescript(
            "CREATE TABLE knowledge(id TEXT PRIMARY KEY, topic TEXT, claim TEXT, "
            "detail TEXT, source_url TEXT, source_title TEXT, source_kind TEXT, "
            "tags TEXT, confidence REAL, embedding BLOB, session_id TEXT, "
            "book_id TEXT, created_at REAL);"
        )
        c.execute("INSERT INTO knowledge(id,topic,claim,detail,source_kind) "
                  "VALUES('a','T','有引文','结论 原文依据：原文片段','book')")
        c.execute("INSERT INTO knowledge(id,topic,claim,detail,source_kind) "
                  "VALUES('b','T','无引文','纯粹结论','book')")
        c.execute("INSERT INTO knowledge(id,topic,claim,detail,source_kind) "
                  "VALUES('c','T','另一种写法','说明 原文依据：另一段引文','web')")
        c.commit()
        c.close()
        with patch.object(config, "DB_FILE", legacy):
            out = review_pipeline.plan(sample=2)
        self.assertTrue(out["available"])
        self.assertFalse(out["schema_migrated"])
        self.assertEqual(out["no_evidence_total"], 1)
        self.assertEqual(out["by_source_kind"], {"book": 1})

    def test_enqueue_dry_run_writes_nothing(self):
        self._seed([("p1", LITERAL_CLAIM, "file:///K:/books/a.pdf", "b1"),
                    ("p3", "网页结论", "https://example.org/a", "")])
        out = review_pipeline.enqueue(dry_run=True)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["scanned"], 2)
        self.assertEqual(out["tiers"]["local_quote"], 1)
        self.assertEqual(memory.conn().execute(
            "SELECT COUNT(*) FROM review_queue").fetchone()[0], 0)

    def test_enqueue_then_status_reports_queue(self):
        self._seed([("p1", LITERAL_CLAIM, "file:///K:/books/a.pdf", "b1"),
                    ("p3", "网页结论", "https://example.org/a", "")])
        out = review_pipeline.enqueue(dry_run=False)
        self.assertEqual(out["queued"], 2)
        st = review_pipeline.status()
        self.assertTrue(st["available"])
        self.assertEqual(st["queue"]["local_quote"]["pending"], 1)
        self.assertEqual(st["queue"]["url_recheck"]["pending"], 1)
        self.assertEqual(st["no_evidence_remaining"], 2)

    def test_run_local_dry_run_does_not_touch_knowledge(self):
        self._seed([("p1", LITERAL_CLAIM, "file:///K:/books/a.pdf", "b1")])
        review_pipeline.enqueue(dry_run=False)
        out = review_pipeline.run_local(batch=10, dry_run=True)
        self.assertEqual(out["examined"], 1)
        self.assertEqual(out["cited"], 0)
        row = memory.conn().execute(
            "SELECT evidence,verification,status FROM knowledge WHERE id='p1'").fetchone()
        self.assertEqual((row["evidence"], row["verification"], row["status"]),
                         ("", "", "legacy"))

    def test_run_local_backfills_only_literal_quotes_and_never_marks_verified(self):
        self._seed([
            ("p1", LITERAL_CLAIM, "file:///K:/books/a.pdf", "b1"),
            ("p2", PARAPHRASE_CLAIM, "file:///K:/books/a.pdf", "b1"),
        ])
        review_pipeline.enqueue(dry_run=False)
        out = review_pipeline.run_local(batch=10, dry_run=False, max_pages=5)
        self.assertEqual(out["cited"], 1)
        self.assertEqual(out["no_literal_match"], 1)
        c = memory.conn()
        hit = c.execute("SELECT claim,detail,evidence,evidence_locator,verification,status "
                        "FROM knowledge WHERE id='p1'").fetchone()
        self.assertTrue(hit["evidence"])
        self.assertIn("ocr_pages:", hit["evidence_locator"])
        self.assertEqual(hit["verification"], "legacy_literal")
        # 补到引文 ≠ 事实已核实；状态绝不能跳到 verified
        self.assertEqual(hit["status"], "legacy")
        self.assertEqual(hit["claim"], LITERAL_CLAIM)
        miss = c.execute("SELECT evidence,verification FROM knowledge WHERE id='p2'").fetchone()
        self.assertEqual((miss["evidence"], miss["verification"]), ("", ""))
        self.assertGreaterEqual(c.execute(
            "SELECT COUNT(*) FROM knowledge_revisions WHERE kind='evidence_backfill'"
        ).fetchone()[0], 1)
        self.assertEqual(review_pipeline.status()["no_evidence_remaining"], 1)

    def test_run_urls_dry_run_makes_no_network_request(self):
        self._seed([("p3", "网页结论", "https://example.org/a", "")])
        review_pipeline.enqueue(dry_run=False)
        # _probe 已改走 httpc 共享客户端（带 SSRF hook），mock 目标随之对齐
        with patch.object(review_pipeline, "_probe") as probe:
            out = review_pipeline.run_urls(dry_run=True)
        probe.assert_not_called()
        self.assertEqual(out["distinct_urls"], 1)
        self.assertEqual(out["checked"], 0)

    def test_run_urls_records_liveness_and_closes_queue(self):
        self._seed([("p3", "网页结论", "https://example.org/a", "")])
        review_pipeline.enqueue(dry_run=False)
        with patch.object(review_pipeline, "_probe", return_value=("ok", "HTTP 200")):
            out = review_pipeline.run_urls(dry_run=False)
        self.assertEqual(out["checked"], 1)
        self.assertEqual(out["statuses"], {"ok": 1})
        c = memory.conn()
        self.assertEqual(c.execute(
            "SELECT COUNT(*) FROM source_checks WHERE url='https://example.org/a'"
        ).fetchone()[0], 1)
        self.assertEqual(c.execute(
            "SELECT state FROM review_queue WHERE pid='p3'").fetchone()[0], "url_checked")
