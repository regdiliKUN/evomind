"""Exercise the repair command against a real Windows venv redirector."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPAIR = "② 修复环境.cmd"


@unittest.skipUnless(os.name == "nt", "Windows batch launcher")
class WindowsSetupTests(unittest.TestCase):
    def test_repair_reconnects_existing_environment(self):
        with tempfile.TemporaryDirectory(prefix="evomind setup ") as tmp:
            project = Path(tmp)
            (project / "scripts").mkdir()
            for name in (REPAIR, "scripts/setup.cmd", "scripts/install.py"):
                shutil.copyfile(ROOT / name, project / name)

            base = Path(sys._base_executable)
            venv = project / ".venv"
            subprocess.run([str(base), "-m", "venv", "--without-pip", str(venv)], check=True)
            cfg = venv / "pyvenv.cfg"
            old = project / "missing-python"
            cfg.write_text(
                re.sub(r"(?m)^home = .*", lambda _: f"home = {old}", cfg.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
            python = venv / "Scripts" / "python.exe"
            self.assertNotEqual(subprocess.run([str(python), "--version"], capture_output=True).returncode, 0)

            # Stand in for pip to keep the batch regression test offline.
            pip = venv / "Lib" / "site-packages" / "pip.py"
            pip.write_text("from pathlib import Path\nPath('pip-ran').touch()\n", encoding="utf-8")
            (project / "requirements.txt").touch()
            env = dict(os.environ, PATH=os.pathsep.join([str(base.parent), os.path.dirname(os.environ["ComSpec"])]))
            result = subprocess.run(
                f'cmd.exe /d /c call "{REPAIR}"',
                cwd=project, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                encoding="utf-8", errors="replace", timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((project / "pip-ran").exists())
            self.assertTrue(pip.exists())
            self.assertFalse(list(project.glob(".venv.backup-*")))
            subprocess.run([str(python), "--version"], check=True, capture_output=True)
            subprocess.run(
                [str(python.with_name("pythonw.exe")), "-c", "import sys; sys.exit(0)"],
                check=True, capture_output=True, timeout=10,
            )
