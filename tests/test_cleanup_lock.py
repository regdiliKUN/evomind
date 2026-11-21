"""cleanup.clean() 的重计算不再持有 memory._lock，写库前用内容水位复验。

覆盖三条路径（预览确认 / 等价 / 精确）：
  (a) 计算期间其他线程能拿到 memory._lock —— 证明 O(n²) 在锁外；
  (b) 计算之后、写库之前库被改 —— 预览路径返回 stale 且一行不删，另两条路径按最新快照重算；
  (c) 正常路径的删除集合与旧实现（锁内算）的参考结果逐条一致。
"""
import random
import sqlite3
import threading
from collections import defaultdict
from unittest.mock import patch

from test_regressions import IsolatedTest
from backend import cleanup, config, memory, topics
from backend.knowledge_dedup import equivalent, normal, preference


def _other_thread_can_lock(timeout: float = 2.0) -> bool:
    got = {}

    def worker():
        ok = memory._lock.acquire(timeout=timeout)
        got["ok"] = ok
        if ok:
            memory._lock.release()

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout + 3)
    return got.get("ok", False)


def _in_other_thread(fn):
    """在另一线程里跑一次写入（它要拿 memory._lock）；拿不到锁就会超时失败。"""
    box = {}

    def worker():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join(10)
    if t.is_alive():
        raise AssertionError("另一线程的写入被阻塞：memory._lock 仍被持有")
    if "error" in box:
        raise box["error"]
    return box.get("value")


class _Base(IsolatedTest):
    def setUp(self):
        super().setUp()
        cleanup._forget_plans()

    def tearDown(self):
        cleanup._forget_plans()
        super().tearDown()

    def add(self, claim="DynaMesh controls polygon density.", detail="Resolution controls the density.",
            confidence=.6, topic="ZBrush", vec=(1., 0., 0., 0.)):
        return memory.add_point(topic=topic, claim=claim, detail=detail, confidence=confidence,
                                source_url="file:///s.txt", source_title="S", vec=list(vec), dedup=False)

    def ids(self):
        return {r[0] for r in memory.conn().execute("SELECT id FROM knowledge")}

    def raw_update(self, pid, detail):
        """模拟不拿 memory._lock 的直写（library / governance 等模块的写法）。"""
        c = sqlite3.connect(config.DB_FILE, timeout=30)
        try:
            c.execute("UPDATE knowledge SET detail=? WHERE id=?", (detail, pid))
            c.commit()
        finally:
            c.close()

    def seed_library(self, n=90, seed=7):
        rng = random.Random(seed)
        claims = ["DynaMesh controls polygon density.", "ZRemesher rebuilds topology cleanly.",
                  "Subdivision levels store detail layers.", "Polygroups split meshes into regions."]
        details = ["Resolution controls the density.", "Resolution  controls\n the density.",
                   "Target polygon count drives it.", "", "Requires 8 GB RAM", "Requires 16 GB RAM"]
        bases = [(1., 0., 0., 0.), (0., 1., 0., 0.), (0., 0., 1., 0.), (.7, .7, 0., 0.)]
        for _ in range(n):
            base = rng.choice(bases)
            jitter = rng.choice([0., 0., .01, .2])
            vec = [x + rng.uniform(-jitter, jitter) for x in base]
            self.add(claim=rng.choice(claims), detail=rng.choice(details),
                     confidence=rng.choice([.5, .6, .9]), topic=rng.choice(["ZBrush", "Blender"]), vec=vec)


class LockFreePlanningTests(_Base):
    def test_previewed_plan_is_computed_outside_lock_and_reused_on_confirm(self):
        self.add()
        self.add(confidence=.9)
        plan = cleanup.preview(.9)
        cleanup._forget_plans()  # 模拟重启 / 缓存未命中：确认时需要重算 O(n²)
        probes = []
        real = cleanup._plan

        def spy(*args, **kwargs):
            probes.append(_other_thread_can_lock())
            return real(*args, **kwargs)

        with patch.object(cleanup, "_plan", side_effect=spy):
            result = cleanup.clean(threshold=.9, token=plan["token"])
        self.assertEqual(probes, [True])
        self.assertEqual(result["removed"], 1)

    def test_confirm_after_preview_skips_second_quadratic_pass(self):
        self.add()
        self.add(confidence=.9)
        plan = cleanup.preview(.9)
        with patch.object(cleanup, "_plan", side_effect=AssertionError("不应再算一遍")):
            result = cleanup.clean(threshold=.9, token=plan["token"])
        self.assertEqual((result["ok"], result["removed"], result["groups"]), (True, 1, 1))

    def test_exact_and_equivalent_paths_compute_outside_lock(self):
        for name, kwargs in (("_exact_drops", {}), ("_equivalent_drops", {"equivalent_only": True})):
            with self.subTest(path=name):
                self.add()
                self.add()
                before = memory.count_points()
                probes = []
                real = getattr(cleanup, name)

                def spy(rows, _real=real):
                    probes.append(_other_thread_can_lock())
                    return _real(rows)

                with patch.object(cleanup, name, side_effect=spy):
                    result = cleanup.clean(**kwargs)
                # 只算一次（锁外），且水位未变时锁内不重算
                self.assertEqual(probes, [True])
                self.assertEqual(result["removed"], before - 1)
                self.assertEqual(memory.count_points(), 1)


class WatermarkTests(_Base):
    def test_direct_sql_write_between_plan_and_commit_returns_stale(self):
        first = self.add()
        self.add(confidence=.9)
        plan = cleanup.preview(.9)
        before = self.ids()
        real_begin = cleanup._begin_immediate

        def begin(c):
            self.raw_update(first, "Edited by another connection after the preview.")
            real_begin(c)

        with patch.object(cleanup, "_begin_immediate", side_effect=begin):
            result = cleanup.clean(threshold=.9, token=plan["token"])
        self.assertEqual(result, {"ok": False, "stale": True, "msg": "知识库内容或阈值已变化，请重新预览后确认清理"})
        self.assertEqual(self.ids(), before)
        self.assertEqual(memory.conn().execute("SELECT count(*) FROM cleanup_runs").fetchone()[0], 0)
        self.assertEqual(memory.conn().execute("SELECT count(*) FROM cleanup_archive").fetchone()[0], 0)

    def test_locked_writer_during_uncached_plan_makes_confirm_stale(self):
        self.add()
        self.add(confidence=.9)
        plan = cleanup.preview(.9)
        cleanup._forget_plans()
        real = cleanup._plan

        def spy(*args, **kwargs):
            out = real(*args, **kwargs)
            # 计划已算完、尚未进锁：另一线程照常拿锁写入
            _in_other_thread(lambda: self.add(claim="A brand new fact about sculpting brushes."))
            return out

        before = len(self.ids())
        with patch.object(cleanup, "_plan", side_effect=spy):
            result = cleanup.clean(threshold=.9, token=plan["token"])
        self.assertTrue(result.get("stale"))
        self.assertEqual(len(self.ids()), before + 1)

    def test_alias_change_after_preview_is_detected(self):
        # 旧实现的 token 不含别名表：改别名后 token 不变，执行的计划却与预览不同。
        self.add(topic="ZB")
        self.add(topic="ZBrush", confidence=.9)
        plan = cleanup.preview(.9)
        self.assertEqual(plan["removed"], 0)
        topics.add_alias("ZB", "ZBrush")
        result = cleanup.clean(threshold=.9, token=plan["token"])
        self.assertTrue(result.get("stale"))
        self.assertEqual(memory.count_points(), 2)
        fresh = cleanup.preview(.9)
        self.assertEqual(fresh["removed"], 1)
        self.assertEqual(cleanup.clean(threshold=.9, token=fresh["token"])["removed"], 1)

    def test_token_without_aliases_is_unchanged_format(self):
        self.add()
        rows = [dict(r) for r in memory.conn().execute("SELECT * FROM knowledge ORDER BY id")]
        import hashlib
        import json
        legacy = hashlib.sha256(json.dumps([.9, None], ensure_ascii=False).encode())
        for row in rows:
            legacy.update(json.dumps(cleanup._public(row), sort_keys=True, ensure_ascii=False).encode())
            legacy.update(row["embedding"] or b'')
        self.assertEqual(cleanup.preview(.9)["token"], legacy.hexdigest())

    def test_exact_path_recomputes_when_library_changes_after_planning(self):
        self.add()
        self.add()
        real = cleanup._exact_drops
        calls = []

        def spy(rows):
            calls.append(len(rows))
            out = real(rows)
            if len(calls) == 1:
                _in_other_thread(lambda: self.add())  # 第三条精确重复
            return out

        with patch.object(cleanup, "_exact_drops", side_effect=spy):
            result = cleanup.clean()
        self.assertEqual(calls, [2, 3])
        self.assertEqual((result["removed"], result["scanned"], result["remaining"]), (2, 3, 1))
        self.assertEqual(memory.count_points(), 1)

    def test_exact_path_never_archives_against_a_vanished_keeper(self):
        a = self.add(confidence=.9)
        self.add()
        real = cleanup._exact_drops
        calls = []

        def spy(rows):
            calls.append(1)
            out = real(rows)
            if len(calls) == 1:
                c = sqlite3.connect(config.DB_FILE, timeout=30)
                try:
                    c.execute("DELETE FROM knowledge WHERE id=?", (a,))
                    c.commit()
                finally:
                    c.close()
            return out

        with patch.object(cleanup, "_exact_drops", side_effect=spy):
            result = cleanup.clean()
        self.assertEqual(result["removed"], 0)
        self.assertEqual(memory.count_points(), 1)


class ReferenceEquivalenceTests(_Base):
    """与旧实现（整段锁内计算）的参考结果逐条比对。"""

    def test_exact_path_matches_reference(self):
        self.seed_library()
        rows = [dict(r) for r in memory.conn().execute(
            "SELECT id,topic,claim,detail,confidence,created_at,length(embedding) b FROM knowledge")]
        groups = defaultdict(list)
        for r in rows:
            groups[(r["topic"], cleanup._normal(r["claim"]), cleanup._normal(r["detail"]), r["b"])].append(r)
        expected = set()
        expected_groups = 0
        for members in groups.values():
            if len(members) > 1:
                members.sort(key=lambda p: (-(p["confidence"] or 0.), p["created_at"] or 0., p["id"]))
                expected |= {p["id"] for p in members[1:]}
                expected_groups += 1
        self.assertTrue(expected)
        before = self.ids()
        result = cleanup.clean()
        self.assertEqual(before - self.ids(), expected)
        self.assertEqual((result["removed"], result["groups"]), (len(expected), expected_groups))

    def test_equivalent_path_matches_reference(self):
        self.seed_library(seed=11)
        rows = [dict(r) for r in memory.conn().execute("SELECT * FROM knowledge")]
        groups = defaultdict(list)
        for r in rows:
            groups[normal(r["claim"])].append(r)
        expected = {}
        for candidates in groups.values():
            retained = []
            for r in sorted(candidates, key=preference):
                keep = next((p for p in retained if equivalent(p, r)), None)
                if keep is None:
                    retained.append(r)
                else:
                    expected[r["id"]] = keep["id"]
        self.assertTrue(expected)
        before = self.ids()
        result = cleanup.clean(equivalent_only=True)
        self.assertEqual(before - self.ids(), set(expected))
        self.assertEqual((result["removed"], result["groups"]), (len(expected), len(set(expected.values()))))

    def test_threshold_path_deletes_exactly_the_previewed_pairs(self):
        self.seed_library(seed=3)
        plan = cleanup.preview(.95)
        expected = {item["drop"]["id"] for item in plan["items"]}
        self.assertTrue(expected)
        before = self.ids()
        # 缓存命中与未命中两种确认方式结果一致
        cleanup._forget_plans()
        result = cleanup.clean(threshold=.95, token=plan["token"])
        self.assertEqual(before - self.ids(), expected)
        self.assertEqual((result["removed"], result["groups"], result["scanned"]),
                         (plan["removed"], plan["groups"], plan["scanned"]))
        rows = memory.conn().execute("SELECT point_id,kept_id FROM cleanup_archive").fetchall()
        self.assertEqual({r[0]: r[1] for r in rows}, {i["drop"]["id"]: i["keep"]["id"] for i in plan["items"]})

    def test_missing_token_and_bad_threshold_keep_old_contract(self):
        self.add()
        self.add()
        self.assertTrue(cleanup.clean(threshold=.9)["stale"])
        self.assertTrue(cleanup.clean(threshold=.9, token="nope")["stale"])
        with self.assertRaises(ValueError):
            cleanup.clean(threshold=.2, token="x")
        self.assertEqual(memory.count_points(), 2)


if __name__ == "__main__":
    import unittest
    unittest.main()
