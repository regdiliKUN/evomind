"""第二十一轮（F4）核心模块正确性回归：memory / evolve / scheduler。

每条用例对应一个已逐字核对源码、能稳定复现的缺陷；修复前均失败。
"""
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

from test_regressions import IsolatedTest
from backend import cleanup, config, evolve, memory, scheduler


def _wait(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class Round21MemoryTests(IsolatedTest):
    def add(self, claim, vec, topic="甲方向"):
        return memory.add_point(topic=topic, claim=claim, detail="细节", source_url="u://" + claim,
                                source_title="t", source_kind="web", tags=[], confidence=0.6,
                                vec=vec, dedup=False)

    def rowid(self, pid):
        return memory.conn().execute("SELECT rowid FROM knowledge WHERE id=?", (pid,)).fetchone()[0]

    # ---- 1. near_duplicates 不得持全局写锁跑 O(n²) 扫描 -------------------------
    def test_near_duplicates_scan_runs_outside_global_write_lock(self):
        self.add("同一条结论的第一种说法", [1.0, 0.0, 0.0])
        self.add("同一条结论的第二种说法", [0.99, 0.02, 0.0])
        original = cleanup.preview
        observed = []

        def probe(*args, **kwargs):
            got = []

            def try_lock():
                ok = memory._lock.acquire(timeout=0.5)
                got.append(ok)
                if ok:              # RLock 只能由持有它的线程释放
                    memory._lock.release()

            t = threading.Thread(target=try_lock)
            t.start()
            t.join()
            observed.append(bool(got and got[0]))
            return original(*args, **kwargs)

        with patch.object(cleanup, "preview", side_effect=probe):
            r = memory.near_duplicates(threshold=0.9)
        self.assertEqual(len(r["pairs"]), 1)
        self.assertEqual(observed, [True], "扫描期间其他线程拿不到全局写锁，所有写入都会被阻塞")

    # ---- 2. 检索结果缓存不能被并发写入「回填」成旧结果 ---------------------------
    def test_search_cache_not_repopulated_after_concurrent_invalidation(self):
        self.add("第一条已入库的知识点结论", [1.0, 0.0, 0.0])
        original = memory.similarity

        def racing_similarity(*args, **kwargs):
            # 模拟另一个线程在本次检索计算期间完成写入并作废缓存
            memory._bump_search_cache()
            return original(*args, **kwargs)

        with patch.object(memory.llm, "embed", return_value=[[1.0, 0.0, 0.0]]), \
             patch.object(memory, "similarity", side_effect=racing_similarity):
            hits = memory.search("查询", k=8)
        self.assertEqual(len(hits), 1)
        self.assertNotIn(("查询", None, 8), memory._search_cache,
                         "作废之后写回的旧结果会一直命中，直到下一次写入")

    def test_search_cache_still_used_without_writes(self):
        self.add("第一条已入库的知识点结论", [1.0, 0.0, 0.0])
        with patch.object(memory.llm, "embed", return_value=[[1.0, 0.0, 0.0]]):
            first = memory.search("查询", k=8)
            with patch.object(memory, "similarity", side_effect=AssertionError("应命中缓存")):
                second = memory.search("查询", k=8)
        self.assertEqual([h["id"] for h in first], [h["id"] for h in second])

    # ---- 3. 向量缓存追加不能出现重复 rowid ---------------------------------------
    def test_append_vector_skips_rowid_already_loaded_by_rebuild(self):
        pid = self.add("已提交且被重建读进缓存的知识点", [1.0, 0.0, 0.0])
        rid = self.rowid(pid)
        memory._invalidate()
        ids, _ = memory._all_vectors(3)        # 重建发生在写入提交之后、追加之前
        self.assertIn(rid, ids)
        memory._append_vector(rid, [1.0, 0.0, 0.0])
        ids, mat = memory._all_vectors(3)
        self.assertEqual(ids.count(rid), 1)
        self.assertEqual(mat.shape[0], len(ids))
        # 真正的新行仍然要能追加进去
        memory._append_vector(rid + 1000, [0.0, 1.0, 0.0])
        ids, mat = memory._all_vectors(3)
        self.assertEqual(ids[-1], rid + 1000)
        self.assertEqual(mat.shape[0], len(ids))

    # ---- 4. 软删不应丢弃整份向量缓存（2.5 万条重建实测 333ms） --------------------
    def test_soft_delete_keeps_vector_cache_but_hides_point(self):
        a = self.add("将被软删除的知识点结论文本", [1.0, 0.0, 0.0])
        b = self.add("保留下来的另一条知识点结论", [0.9, 0.1, 0.0])
        with patch.object(memory.llm, "embed", return_value=[[1.0, 0.0, 0.0]]):
            self.assertEqual({h["id"] for h in memory.search("查询", k=8)}, {a, b})
            cache = memory._vec_cache
            self.assertIsNotNone(cache)
            self.assertTrue(memory.delete_point(a))
            self.assertIs(memory._vec_cache, cache, "软删只改状态，不该触发全量向量重建")
            self.assertEqual([h["id"] for h in memory.search("查询", k=8)], [b])
        self.assertNotIn(a, [p["id"] for p in memory.similar_points(b, k=5)])
        self.assertEqual(memory.count_points(), 1)

    def test_hard_delete_still_drops_vector_cache(self):
        a = self.add("将被物理删除的知识点结论", [1.0, 0.0, 0.0])
        memory._all_vectors(3)
        self.assertIsNotNone(memory._vec_cache)
        memory.delete_point(a, hard=True)
        self.assertIsNone(memory._vec_cache)

    # ---- 5. 备份恢复换库后，所有派生缓存必须作废 -----------------------------------
    def test_db_replacement_invalidates_derived_caches(self):
        # 先准备「备份里的库」：另一个文件，内容完全不同
        other = Path(self.tmp.name) / "restored.db"
        with patch.object(config, "DB_FILE", other):
            memory.bump_db_generation()
            new = self.add("恢复后库里的另一条知识点", [0.0, 1.0, 0.0])
            new2 = self.add("恢复后库里的第三条知识点", [0.0, 0.9, 0.1])
            memory.bump_db_generation()
        # 回到当前库，建立检索缓存 / 向量缓存 / stats 版本
        memory._invalidate()
        memory._bump_search_cache()
        old = self.add("旧库里的知识点结论文本内容", [1.0, 0.0, 0.0])
        with patch.object(memory.llm, "embed", return_value=[[1.0, 0.0, 0.0]]):
            self.assertEqual([h["id"] for h in memory.search("查询", k=8)], [old])
        ver_before = memory.stats()["ver"]

        with patch.object(config, "DB_FILE", other):
            memory.bump_db_generation()          # ← backup.restore 换库后唯一的通知
            try:
                self.assertGreater(memory.stats()["ver"], ver_before,
                                   "版本号不变：前端按 ver 判断，会跳过整轮刷新")
                with patch.object(memory.llm, "embed", return_value=[[0.0, 1.0, 0.0]]):
                    ids = [h["id"] for h in memory.search("查询", k=8)]
                self.assertEqual(ids, [new, new2], "检索仍返回旧库的缓存结果 / 旧向量")
            finally:
                memory.bump_db_generation()


class Round21EngineTests(IsolatedTest):
    # ---- 6. 抓取线程里的台账写失败不能中断整轮学习 -------------------------------
    def test_fetch_all_survives_bookkeeping_write_failure(self):
        eng = evolve.Engine()

        def fake_fetch(url):
            return {"ok": True, "url": url, "title": "t", "text": "正文" * 100}

        def flaky_mark(url, *a, **k):
            if url.endswith("/a"):
                raise sqlite3.OperationalError("database is locked")

        with patch.object(evolve.fetcher, "fetch", side_effect=fake_fetch), \
             patch.object(evolve.memory, "mark_doc", side_effect=flaky_mark):
            docs = eng._fetch_all([{"url": "https://x.test/a"}, {"url": "https://x.test/b"}])
        self.assertEqual([d["url"] for d in docs], ["https://x.test/a", "https://x.test/b"])
        self.assertTrue(all(d["ok"] for d in docs))


class Round21SchedulerTests(IsolatedTest):
    # ---- 7. disable() 正在 join 时再次 enable()，持续自学不能静默失效 ----------------
    def test_enable_during_pending_disable_keeps_loop_alive(self):
        gate, entered = threading.Event(), threading.Event()
        calls = []

        def fake_tick():
            calls.append(time.time())
            entered.set()
            gate.wait(5)

        fresh = {"enabled": False, "last_run": 0., "next_run": 0., "runs": 0,
                 "thread": None, "stop": threading.Event(), "waiting": ""}
        with patch.dict(scheduler._state, fresh), patch.object(scheduler, "_tick", fake_tick):
            try:
                scheduler.enable()
                self.assertTrue(entered.wait(2))
                t = threading.Thread(target=scheduler.disable)
                t.start()
                self.assertTrue(_wait(lambda: scheduler._state["stop"].is_set()))
                scheduler.enable()          # 用户在旧线程收尾期间重新打开开关
                gate.set()
                t.join(5)
                self.assertTrue(scheduler.state()["enabled"])
                th = scheduler._state["thread"]
                self.assertIsNotNone(th, "开关显示已开启，但调度线程已不存在")
                self.assertTrue(th.is_alive())
                n = len(calls)
                self.assertTrue(_wait(lambda: len(calls) > n), "调度线程不再推进")
            finally:
                gate.set()
                scheduler.disable()
                th = scheduler._state["thread"]
                if th is not None:
                    th.join(2)

    def test_disable_then_enable_restarts_cleanly(self):
        ticks = []
        fresh = {"enabled": False, "last_run": 0., "next_run": 0., "runs": 0,
                 "thread": None, "stop": threading.Event(), "waiting": ""}
        with patch.dict(scheduler._state, fresh), \
             patch.object(scheduler, "_tick", lambda: ticks.append(1)):
            try:
                scheduler.enable()
                self.assertTrue(_wait(lambda: ticks))
                scheduler.disable()
                self.assertIsNone(scheduler._state["thread"])
                n = len(ticks)
                scheduler.enable()
                self.assertTrue(_wait(lambda: len(ticks) > n))
            finally:
                scheduler.disable()


if __name__ == "__main__":
    import unittest
    unittest.main()
