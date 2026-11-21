"""证据策略灰度白名单的通路匹配与静默失效防护。

背景：``config.py`` 的注释曾把书库通路写成 ``library``，但代码里真实的
``_ingest_path`` 是 ``library:book`` / ``library:url``。旧实现做精确字符串
相等比较，用户照注释配置后白名单**永远不生效，且没有任何报错或提示**——
以为已经收紧了，实际上一条候选都没被拦。

本组测试锁定三件事：
* 组名（library / evolve）能覆盖其子通路，精确值仍然生效；
* 配错的名称由 ``policy_diagnostics()`` 显式报出，不再静默；
* ``KNOWN_INGEST_PATHS`` 与源码里实际写入的 ``_ingest_path`` 值保持同步，
  以后新增入库通路却忘了登记会在这里失败。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import config, evidence, main

BACKEND = Path(config.__file__).resolve().parent
REAL_EXPECTED = {"evolve:web", "library:book", "library:url", "refinement"}


class EvidencePolicyPathTests(unittest.TestCase):
    def setUp(self):
        evidence.reset_observed_paths()
        evidence.reset_reject_stats()

    def tearDown(self):
        evidence.reset_observed_paths()
        evidence.reset_reject_stats()

    def _policy(self, **over):
        base = {"require_evidence": False, "require_evidence_paths": [],
                "require_evidence_dry_run": False}
        base.update(over)
        return patch.object(config, "get",
                            side_effect=lambda key, default=None: base.get(key, default))

    def test_group_name_library_covers_both_subpaths(self):
        with self._policy(require_evidence_paths=["library"]):
            self.assertEqual(evidence.evidence_policy("library:book"), (True, False))
            self.assertEqual(evidence.evidence_policy("library:url"), (True, False))
            self.assertEqual(evidence.evidence_policy("evolve:web"), (False, False))
            self.assertEqual(evidence.evidence_policy("refinement"), (False, False))

    def test_group_name_evolve_covers_web(self):
        with self._policy(require_evidence_paths=["evolve"]):
            self.assertTrue(evidence.evidence_policy("evolve:web")[0])
            self.assertFalse(evidence.evidence_policy("library:book")[0])

    def test_exact_values_still_match_and_stay_narrow(self):
        with self._policy(require_evidence_paths=["library:book"]):
            self.assertTrue(evidence.evidence_policy("library:book")[0])
            self.assertFalse(evidence.evidence_policy("library:url")[0])

    def test_prefix_does_not_overmatch_across_groups(self):
        """library 不能误伤 library_extra 之外的无关路径，也不能靠裸前缀乱匹配。"""
        with self._policy(require_evidence_paths=["library"]):
            self.assertFalse(evidence.evidence_policy("libraryx:other")[0])
            self.assertFalse(evidence.evidence_policy("")[0])
            self.assertFalse(evidence.evidence_policy("other:library")[0])

    def test_global_true_overrides_empty_whitelist(self):
        with self._policy(require_evidence=True, require_evidence_paths=[]):
            for path in REAL_EXPECTED:
                with self.subTest(path=path):
                    self.assertTrue(evidence.evidence_policy(path)[0])

    def test_typo_is_diagnosed_instead_of_failing_silently(self):
        with self._policy(require_evidence_paths=["libary", "evolve:web"]):
            diag = evidence.policy_diagnostics()
            self.assertEqual(diag["unmatched_configured"], ["libary"])
            self.assertFalse(diag["ok"])
            self.assertIn("library", diag["hint"])
            self.assertEqual(diag["configured_paths"], ["libary", "evolve:web"])
            # 如实反映：拼错的项确实不会让任何候选被拦
            self.assertFalse(evidence.evidence_policy("library:book")[0])

    def test_valid_configuration_diagnostics_are_ok(self):
        with self._policy(require_evidence_paths=["library", "refinement"]):
            diag = evidence.policy_diagnostics()
            self.assertTrue(diag["ok"])
            self.assertEqual(diag["unmatched_configured"], [])
            self.assertEqual(diag["hint"], "")

    def test_known_paths_match_every_ingest_path_in_source(self):
        """源码里写入的 _ingest_path 值必须全部登记进 KNOWN_INGEST_PATHS。"""
        found = set()
        for f in BACKEND.glob("*.py"):
            text = f.read_text(encoding="utf-8")
            found |= set(re.findall(r'"_ingest_path"\s*:\s*"([^"]+)"', text))
        self.assertEqual(found, REAL_EXPECTED,
                         f"源码通路与常量不同步：只在源码={sorted(found - REAL_EXPECTED)} "
                         f"只在常量={sorted(REAL_EXPECTED - found)}")
        self.assertEqual(set(evidence.KNOWN_INGEST_PATHS), REAL_EXPECTED)

    def test_observed_paths_are_recorded_for_diagnostics(self):
        with self._policy():
            evidence.prepare_candidate({"claim": "无引文候选", "detail": "",
                                        "_ingest_path": "future:new"})
        diag = evidence.policy_diagnostics()
        self.assertIn("future:new", diag["observed_paths"])
        # 运行时新出现的通路也算「可解析」，不该被误报成拼错
        with self._policy(require_evidence_paths=["future:new"]):
            self.assertTrue(evidence.policy_diagnostics()["ok"])

    def test_api_exposes_path_diagnostics(self):
        with self._policy(require_evidence_paths=["libary"]):
            out = main.api_evidence_policy()
        self.assertIn("path_diagnostics", out)
        self.assertFalse(out["path_diagnostics"]["ok"])
        self.assertIn("rejects", out)
