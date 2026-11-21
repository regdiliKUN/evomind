"""知识治理层验收测试（对应评审报告 C / D 层面的整改闭环）。

覆盖：单一入库网关的证据判定、事实质量指标、健康度不再被自报分刷满、
纠错/状态机/反馈/独立核查 API、训练集溯源、备份 manifest 自证。

全部继承 IsolatedTest，用临时库；不触碰真实 data/knowledge/knowledge.db。
运行：.venv/Scripts/python -m unittest discover -s tests -p test_*.py
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_regressions import IsolatedTest

from backend import analytics, backup, config, evidence, finetune, governance, memory

SOURCE_TEXT = "ZBrush 面雕时，泪腺位于眶内侧壁前缘，下眼睑弧度应沿眶下缘自然过渡。"
# 必须 >= 12 字：evidence_supported() 刻意拒绝过短引文，否则随手几个字就能冒充证据
LITERAL_CITATION = "泪腺位于眶内侧壁前缘，下眼睑弧度"


class EvidenceGatewayTests(IsolatedTest):
    """所有入库路径最终都过 memory.add_points_batch → evidence.prepare_candidate。"""

    def test_fabricated_citation_is_rejected(self):
        res = memory.add_points_batch([{
            "topic": "CAD", "claim": "泪腺位置需要沿眶内侧壁定位",
            "detail": "细节", "vec": [1., 0.],
            "evidence": "泪腺位于颧骨上缘并且需要向外翻转三十度",
            "_source_text": SOURCE_TEXT, "_ingest_path": "library:book",
        }])
        self.assertEqual(res["rejected"], 1)
        self.assertEqual(res["added"], 0)
        self.assertEqual(memory.count_points(), 0)

    def test_literal_citation_records_locator_hash_status_and_provenance(self):
        res = memory.add_points_batch([{
            "topic": "CAD", "claim": "泪腺位置需要沿眶内侧壁定位",
            "detail": "细节", "vec": [1., 0.],
            "evidence": LITERAL_CITATION,
            "_source_text": SOURCE_TEXT, "_ingest_path": "library:book",
        }])
        self.assertEqual(res["added"], 1)
        p = memory.list_points("CAD")[0]
        self.assertEqual(p["verification"], "literal_pass")
        self.assertEqual(p["status"], "verified")
        self.assertEqual(p["created_by"], "library:book")
        self.assertIn("#char=", p["evidence_locator"])
        self.assertEqual(p["evidence_hash"], evidence.sha256(LITERAL_CITATION))
        self.assertTrue(p["model_version"])

    def test_require_evidence_switch_rejects_candidates_without_citation(self):
        base = {"topic": "CAD", "claim": "泪腺位置需要沿眶内侧壁定位", "vec": [1., 0.]}
        self.assertIsNone(evidence.prepare_candidate(dict(base), enforce=True))
        self.assertIsNotNone(evidence.prepare_candidate(dict(base), enforce=False))
        self.assertTrue(evidence.evidence_supported(LITERAL_CITATION, SOURCE_TEXT))
        self.assertFalse(evidence.evidence_supported("泪腺大概在颧骨附近的位置", SOURCE_TEXT))

    def test_evidence_policy_can_be_rolled_out_per_ingest_path(self):
        """别一刀切：先只对一条通路强制，看清楚拒了多少再扩大范围。"""
        base = {"topic": "CAD", "claim": "泪腺位置需要沿眶内侧壁定位", "vec": [1., 0.]}
        evidence.reset_reject_stats()
        cfg = {"require_evidence": False,
               "require_evidence_paths": ["library:book"],
               "require_evidence_dry_run": False}
        with patch.object(config, "_cache", cfg):
            self.assertEqual(evidence.evidence_policy("library:book"), (True, False))
            self.assertEqual(evidence.evidence_policy("evolve:web"), (False, False))
            self.assertIsNone(evidence.prepare_candidate(
                dict(base, _ingest_path="library:book")))
            self.assertIsNotNone(evidence.prepare_candidate(
                dict(base, _ingest_path="evolve:web")))
        stats = evidence.reject_stats()
        self.assertEqual(stats["no_evidence"], 1)
        self.assertEqual(stats["by_path"]["library:book"]["no_evidence"], 1)

    def test_dry_run_tags_instead_of_dropping_candidates(self):
        base = {"topic": "CAD", "claim": "泪腺位置需要沿眶内侧壁定位", "vec": [1., 0.]}
        cfg = {"require_evidence": False,
               "require_evidence_paths": ["library:book"],
               "require_evidence_dry_run": True}
        with patch.object(config, "_cache", cfg):
            out = evidence.prepare_candidate(dict(base, _ingest_path="library:book"))
        self.assertIsNotNone(out)
        self.assertEqual(out["verification"], "policy_would_reject")
        self.assertEqual(out["status"], "unverified")

    def test_global_switch_overrides_path_whitelist(self):
        cfg = {"require_evidence": True,
               "require_evidence_paths": ["library:book"],
               "require_evidence_dry_run": False}
        with patch.object(config, "_cache", cfg):
            self.assertEqual(evidence.evidence_policy("evolve:web"), (True, False))


class QualityMetricsTests(IsolatedTest):
    def test_health_confidence_dim_no_longer_saturates_on_self_reported_scores(self):
        for i in range(5):
            self.point(i)
        c = memory.conn()
        c.execute("UPDATE knowledge SET confidence=0.99")
        c.commit()
        analytics._reset_caches()
        h = analytics.health()
        dim = next(d for d in h["dims"] if d["key"] == "confidence")
        # 旧算法 (0.99-0.3)/0.5*100 = 138 → clamp 100，无论知识对错都满分
        self.assertEqual(dim["label"], "事实依据")
        self.assertLess(dim["score"], 20)
        self.assertIn("不计分", dim["detail"])
        self.assertEqual(len(h["dims"]), 4)  # 前端 / 既有契约不变
        self.assertTrue(h["quality"]["available"])
        self.assertEqual(h["quality"]["evidence_coverage"], 0.0)

    def test_quality_metrics_reflect_evidence_feedback_and_quarantine(self):
        pid = self.point()
        c = memory.conn()
        c.execute(
            "UPDATE knowledge SET evidence=?,verification='literal_pass',status='verified' WHERE id=?",
            (LITERAL_CITATION, pid),
        )
        c.commit()
        q = governance.quality_metrics()
        self.assertEqual(q["evidence_coverage"], 1.0)
        self.assertEqual(q["literal_pass_rate"], 1.0)
        governance.add_feedback(pid, "wrong")
        q2 = analytics.quality_snapshot(fresh=True)
        self.assertEqual(q2["negative_feedback_rate"], 1.0)
        self.assertEqual(q2["status_breakdown"]["disputed"], 1)
        self.assertEqual(q2["active"], 0)

    def test_quality_snapshot_cache_is_invalidated_by_writes(self):
        self.point(0)
        a = analytics.quality_snapshot()
        self.assertIs(analytics.quality_snapshot(), a)
        self.point(1)
        self.assertIsNot(analytics.quality_snapshot(), a)

    def test_training_pairs_carry_provenance_and_exclude_inactive_points(self):
        pid = self.point()
        pairs = memory.export_training_pairs()
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["provenance"]["point_id"], pid)
        self.assertEqual(pairs[0]["provenance"]["schema_version"], evidence.SCHEMA_VERSION)
        governance.set_status(pid, "disputed")
        self.assertEqual(memory.export_training_pairs(), [])

    def test_dataset_jsonl_writes_provenance_per_line(self):
        pid = self.point()
        out = Path(self.tmp.name) / "finetune"
        with patch.object(finetune, "OUTPUTS", out):
            res = finetune.build_dataset(min_samples=1, rewrite=False)
        self.assertTrue(res["ok"])
        lines = Path(res["path"]).read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["provenance"]["point_id"], pid)


class GovernanceApiTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        from backend.main import app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def test_evidence_policy_endpoint_reports_config_and_rejects(self):
        r = self.client.get("/api/analytics/evidence-policy")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("require_evidence", d)
        self.assertIn("require_evidence_paths", d)
        self.assertIn("require_evidence_dry_run", d)
        self.assertIn("rejects", d)
        self.assertIn("independent_verification", d)
        self.assertEqual(d["independent_verification"],
                         bool(d["verify_model"] and d["verify_model"] != d["primary_model"]))

    def test_patch_corrects_text_and_preserves_previous_revision(self):
        pid = self.point()
        r = self.client.patch(f"/api/knowledge/{pid}", json={
            "claim": "修正后的结论：壁厚必须大于喷嘴直径两倍",
            "detail": "补充说明", "evidence": LITERAL_CITATION,
            "reason": "原结论写反了",
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["revision"], 2)
        p = memory.get_point(pid)
        self.assertEqual(p["status"], "unverified")  # 编辑不继承旧核查结论
        self.assertEqual(p["revision"], 2)
        self.assertTrue(p["evidence_hash"])
        # 入库时有一条 create 快照，编辑再留一条 update 快照；两者都不可修改。
        revs = governance.revisions_for(pid)
        updates = [r for r in revs if r["kind"] == "update"]
        self.assertEqual(len(updates), 1)
        self.assertIn("Regression knowledge point number 0", updates[0]["row"]["claim"])
        self.assertEqual(updates[0]["reason"], "原结论写反了")
        self.assertGreaterEqual(
            len(self.client.get(f"/api/knowledge/{pid}/revisions").json()["items"]), 2
        )

    def test_invalid_status_transition_is_rejected_without_changing_state(self):
        pid = self.point()
        self.assertEqual(self.client.patch(f"/api/knowledge/{pid}",
                                           json={"status": "archived"}).status_code, 200)
        # archived 只能回到 verified / unverified
        self.assertEqual(self.client.patch(f"/api/knowledge/{pid}",
                                           json={"status": "superseded"}).status_code, 422)
        self.assertEqual(memory.get_point(pid)["status"], "archived")
        self.assertEqual(self.client.patch(f"/api/knowledge/{pid}",
                                           json={"status": "不存在的状态"}).status_code, 422)

    def test_negative_feedback_quarantines_point_and_is_listed(self):
        pid = self.point()
        r = self.client.post(f"/api/knowledge/{pid}/feedback",
                             json={"verdict": "wrong", "note": "与出处不符"})
        self.assertTrue(r.json()["quarantined"])
        self.assertNotIn(pid, [p["id"] for p in memory.list_points()])
        self.assertEqual(self.client.get(f"/api/knowledge/{pid}/feedback").json()["items"][0]["verdict"], "wrong")
        self.assertEqual(self.client.post(f"/api/knowledge/{pid}/feedback",
                                         json={"verdict": "瞎写的"}).status_code, 422)

    def test_verify_endpoint_refuses_to_pretend_self_check_is_independent(self):
        pid = self.point()
        r = self.client.post(f"/api/knowledge/{pid}/verify")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["independent"])
        rows = memory.conn().execute(
            "SELECT method,verdict FROM knowledge_verifications WHERE point_id=?", (pid,)
        ).fetchall()
        self.assertEqual([tuple(x) for x in rows], [("self_verify", "skipped")])
        self.assertEqual(governance.quality_metrics()["independent_verify_count"], 0)

    def test_verify_endpoint_uses_separate_model_and_records_pass(self):
        pid = self.point()
        c = memory.conn()
        c.execute("UPDATE knowledge SET evidence=?,status='verified' WHERE id=?",
                  (LITERAL_CITATION, pid))
        c.commit()
        real_get = config.get

        def fake_get(key, default=None):
            if key == "verify_model":
                return "checker:7b"
            if key == "model":
                return "primary:7b"
            return real_get(key, default)

        with patch.object(config, "get", side_effect=fake_get), \
             patch("backend.llm.chat_json", return_value={"verified": [0]}):
            r = self.client.post(f"/api/knowledge/{pid}/verify")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["independent"])
        self.assertEqual(r.json()["verdict"], "pass")
        self.assertEqual(governance.quality_metrics()["independent_verify_pass_rate"], 1.0)

    def test_source_check_and_soft_delete_default(self):
        pid = self.point()
        self.assertEqual(self.client.post(
            "/api/sources/check", json={"url": "https://example.com/a", "status": "dead"}
        ).status_code, 200)
        self.assertEqual(governance.quality_metrics()["source_failure_rate"], 1.0)
        self.assertEqual(self.client.post(
            "/api/sources/check", json={"url": "https://example.com/a", "status": "瞎写"}
        ).status_code, 422)
        self.assertEqual(self.client.delete(f"/api/knowledge/{pid}").json()["mode"], "archived")
        self.assertIsNotNone(memory.get_point(pid))
        self.assertEqual(memory.get_point(pid)["status"], "archived")
        self.client.delete(f"/api/knowledge/{pid}?hard=true")
        self.assertIsNone(memory.get_point(pid))

    def test_quality_endpoint_is_readable(self):
        self.point()
        r = self.client.get("/api/analytics/quality")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["available"])
        self.assertEqual(r.json()["schema_version"], evidence.SCHEMA_VERSION)


class BackupManifestTests(IsolatedTest):
    def test_manifest_records_versions_and_member_hashes(self):
        self.point()
        info = backup.export_backup("governance")
        path = backup._dir() / info["name"]
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        self.assertEqual(manifest["manifest_version"], 2)
        self.assertEqual(manifest["schema_version"], evidence.SCHEMA_VERSION)
        self.assertTrue(manifest["embedding_model"])
        self.assertTrue(manifest["code_fingerprint"])
        self.assertEqual(set(manifest["members"]), {"knowledge.db", "config.json"})
        self.assertTrue(all(len(v["sha256"]) == 64 for v in manifest["members"].values()))
        self.assertEqual(backup.validate_backup(path)["manifest"]["schema_version"],
                         evidence.SCHEMA_VERSION)

    def test_tampered_member_fails_validation(self):
        self.point()
        info = backup.export_backup("governance")
        path = backup._dir() / info["name"]
        tampered = backup._dir() / "tampered.zip"
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(tampered, "w") as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "config.json":
                    data = data.replace(b"model", b"modl")  # 内容变了，manifest 没变
                dst.writestr(item, data)
        with self.assertRaises(ValueError) as ctx:
            backup.validate_backup(tampered)
        self.assertIn("SHA256", str(ctx.exception))
