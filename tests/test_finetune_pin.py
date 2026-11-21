"""GGUF 导出：llama.cpp 必须按固定 tag 获取并校验提交 sha 后才执行其中脚本。

全部 mock 掉 subprocess，不触网、不真的跑 git / pip。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend import config, finetune  # noqa: E402

PIN = finetune.LLAMA_CPP_SHA
OTHER = "0" * 40


class FakeGit:
    """按参数模拟 git clone / rev-parse / status，以及 pip 与转换脚本。"""

    def __init__(self, clone_sha: str = PIN, clone_rc: int = 0, dirty: set[str] | None = None):
        self.clone_sha = clone_sha
        self.clone_rc = clone_rc
        self.heads: dict[str, str] = {}
        self.dirty = dirty or set()
        self.calls: list[list[str]] = []

    def __call__(self, cmd, *args, **kwargs):  # noqa: ANN001
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if cmd[:2] == ["git", "-C"]:
            path = str(Path(cmd[2]))
            if cmd[3] == "rev-parse":
                if path in self.heads:
                    return SimpleNamespace(returncode=0, stdout=self.heads[path] + "\n", stderr="")
                return SimpleNamespace(returncode=128, stdout="", stderr="not a git repository")
            if cmd[3] == "status":
                return SimpleNamespace(returncode=0, stdout=" M x.py\n" if path in self.dirty else "", stderr="")
        if cmd[0] == "git" and "clone" in cmd:
            if self.clone_rc:
                return SimpleNamespace(returncode=self.clone_rc, stdout="", stderr="fatal: network down")
            dest = Path(cmd[-1])
            (dest / ".git").mkdir(parents=True)
            (dest / "convert_hf_to_gguf.py").write_text("# pinned", encoding="utf-8")
            self.heads[str(dest)] = self.clone_sha
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        # pip install / convert_hf_to_gguf.py
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def clones(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "git" and "clone" in c]

    def converts(self) -> list[list[str]]:
        return [c for c in self.calls if any(a.endswith("convert_hf_to_gguf.py") for a in c)]


class LlamaCppPinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="evomind_pin_"))
        self.target = self.tmp / "llama.cpp"
        self._patches = [
            patch.object(config, "DATA", self.tmp),
            patch.dict(os.environ, {"EVOMIND_LLAMA_CPP_REF": "", "EVOMIND_LLAMA_CPP_SHA": ""}),
        ]
        for p in self._patches:
            p.start()
        # 配置里若被人加了覆盖键，测试也不受影响
        real_get = config.get
        self._patches.append(patch.object(
            finetune.config, "get",
            side_effect=lambda k, d=None: None if k.startswith("llama_cpp_") else real_get(k, d)))
        self._patches[-1].start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_old_clone(self, head: str | None, fake: FakeGit, git: bool = True) -> None:
        (self.target).mkdir()
        if git:
            (self.target / ".git").mkdir()
        (self.target / "convert_hf_to_gguf.py").write_text("# old HEAD", encoding="utf-8")
        if head:
            fake.heads[str(self.target)] = head

    def test_fresh_clone_pins_tag_and_verifies_sha(self):
        fake = FakeGit()
        with patch.object(finetune.subprocess, "run", side_effect=fake):
            path = finetune._ensure_llama_cpp()
        self.assertEqual(path, self.target)
        self.assertTrue((self.target / "convert_hf_to_gguf.py").exists())
        self.assertFalse(self.target.with_name("llama.cpp.part").exists())
        [clone] = fake.clones()
        self.assertIn("--branch", clone)
        self.assertEqual(clone[clone.index("--branch") + 1], finetune.LLAMA_CPP_REF)
        self.assertIn("--depth", clone)
        self.assertEqual(clone[-2], finetune.LLAMA_CPP_REPO)

    def test_sha_mismatch_aborts_cleans_and_never_executes(self):
        fake = FakeGit(clone_sha=OTHER)
        src = self.tmp / "merged"
        src.mkdir()
        with patch.object(finetune.subprocess, "run", side_effect=fake):
            out = finetune.export_gguf(str(src))
        self.assertFalse(out["ok"])
        self.assertIn("校验失败", out["msg"])
        self.assertFalse(self.target.exists())
        self.assertFalse(self.target.with_name("llama.cpp.part").exists())
        self.assertEqual(fake.converts(), [])
        self.assertFalse(any("pip" in c for c in fake.calls))

    def test_existing_matching_checkout_is_reused(self):
        fake = FakeGit()
        self._seed_old_clone(PIN, fake)
        with patch.object(finetune.subprocess, "run", side_effect=fake):
            finetune._ensure_llama_cpp()
        self.assertEqual(fake.clones(), [])

    def test_existing_old_head_is_refetched_before_execution(self):
        fake = FakeGit()
        self._seed_old_clone(OTHER, fake)
        src = self.tmp / "merged"
        src.mkdir()
        with patch.object(finetune.subprocess, "run", side_effect=fake):
            out = finetune.export_gguf(str(src))
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(fake.clones()), 1)
        self.assertEqual((self.target / "convert_hf_to_gguf.py").read_text(encoding="utf-8"), "# pinned")
        [convert] = fake.converts()
        self.assertEqual(Path(convert[1]), self.target / "convert_hf_to_gguf.py")
        pip = next(c for c in fake.calls if "pip" in c)
        self.assertTrue(all(any(op in spec for op in ("<", ">")) for spec in pip[pip.index("-q") + 1:]))

    def test_dirty_checkout_with_right_sha_is_not_trusted(self):
        fake = FakeGit(dirty={str(self.tmp / "llama.cpp")})
        self._seed_old_clone(PIN, fake)
        with patch.object(finetune.subprocess, "run", side_effect=fake):
            finetune._ensure_llama_cpp()
        self.assertEqual(len(fake.clones()), 1)

    def test_clone_failure_keeps_old_dir_and_does_not_run_it(self):
        fake = FakeGit(clone_rc=128)
        self._seed_old_clone(OTHER, fake)
        src = self.tmp / "merged"
        src.mkdir()
        with patch.object(finetune.subprocess, "run", side_effect=fake):
            out = finetune.export_gguf(str(src))
        self.assertFalse(out["ok"])
        self.assertEqual((self.target / "convert_hf_to_gguf.py").read_text(encoding="utf-8"), "# old HEAD")
        self.assertEqual(fake.converts(), [])

    def test_non_git_dir_is_moved_aside_not_deleted(self):
        fake = FakeGit()
        self._seed_old_clone(None, fake, git=False)
        with patch.object(finetune.subprocess, "run", side_effect=fake):
            finetune._ensure_llama_cpp()
        backups = [p for p in self.tmp.iterdir() if p.name.startswith("llama.cpp.bak-")]
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "convert_hf_to_gguf.py").read_text(encoding="utf-8"), "# old HEAD")
        self.assertEqual((self.target / "convert_hf_to_gguf.py").read_text(encoding="utf-8"), "# pinned")

    def test_override_requires_sha(self):
        with patch.dict(os.environ, {"EVOMIND_LLAMA_CPP_REF": "b99999", "EVOMIND_LLAMA_CPP_SHA": ""}):
            with self.assertRaises(ValueError):
                finetune._llama_cpp_pin()
        with patch.dict(os.environ, {"EVOMIND_LLAMA_CPP_REF": "", "EVOMIND_LLAMA_CPP_SHA": OTHER}):
            with self.assertRaises(ValueError):
                finetune._llama_cpp_pin()
        with patch.dict(os.environ, {"EVOMIND_LLAMA_CPP_REF": "b99999", "EVOMIND_LLAMA_CPP_SHA": "abc"}):
            with self.assertRaises(ValueError):
                finetune._llama_cpp_pin()
        with patch.dict(os.environ, {"EVOMIND_LLAMA_CPP_REF": "--upload-pack=x", "EVOMIND_LLAMA_CPP_SHA": OTHER}):
            with self.assertRaises(ValueError):
                finetune._llama_cpp_pin()

    def test_override_with_both_is_used(self):
        fake = FakeGit(clone_sha=OTHER)
        with patch.dict(os.environ, {"EVOMIND_LLAMA_CPP_REF": "b99999", "EVOMIND_LLAMA_CPP_SHA": OTHER.upper()}):
            self.assertEqual(finetune._llama_cpp_pin(), ("b99999", OTHER))
            with patch.object(finetune.subprocess, "run", side_effect=fake):
                finetune._ensure_llama_cpp()
        [clone] = fake.clones()
        self.assertEqual(clone[clone.index("--branch") + 1], "b99999")

    def test_override_missing_sha_rejected_by_export(self):
        fake = FakeGit()
        src = self.tmp / "merged"
        src.mkdir()
        with patch.dict(os.environ, {"EVOMIND_LLAMA_CPP_REF": "master"}), \
                patch.object(finetune.subprocess, "run", side_effect=fake):
            out = finetune.export_gguf(str(src))
        self.assertFalse(out["ok"])
        self.assertIn("llama_cpp_sha", out["msg"])
        self.assertEqual(fake.clones(), [])


if __name__ == "__main__":
    unittest.main()
