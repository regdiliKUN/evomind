"""备份 / 恢复 API 端到端验证（临时数据目录，不碰用户真实数据）。

覆盖：导出、列表、下载、非法文件名、恢复（含未确认 / 坏 zip 拒绝）、删除。

注意两点（都是为了不把临时库锁住，让临时目录能干净删除）：
  - 不进入 TestClient 生命周期（不跑 lifespan / startup），启动期不会建库连接；
  - 把 memory.log_event 打成空操作，请求链路同样不碰库。

用法：
    .venv\\Scripts\\python.exe tests\\test_backup_api_e2e.py
"""
from __future__ import annotations

import gc
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend import config, main, memory  # noqa: E402


def build_db(db: Path, n: int, tag: str) -> None:
    c = sqlite3.connect(str(db))
    c.executescript(
        "CREATE TABLE IF NOT EXISTS knowledge(id TEXT PRIMARY KEY, topic TEXT, claim TEXT);"
        "CREATE TABLE IF NOT EXISTS books(id TEXT PRIMARY KEY, title TEXT);"
    )
    c.execute("DELETE FROM knowledge")
    for i in range(n):
        c.execute("INSERT INTO knowledge VALUES(?,?,?)",
                  (f"{tag}-{i}", "t", f"{tag} 端到端测试知识点第 {i} 条"))
    c.commit()
    c.close()


def run() -> int:
    fails: list[str] = []

    def check(name: str, cond: bool, extra: object = "") -> None:
        print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{extra}]" if extra != "" else ""))
        if not cond:
            fails.append(name)

    tmp = Path(tempfile.mkdtemp(prefix="evo_bak_e2e_"))
    try:
        kdir = tmp / "knowledge"
        kdir.mkdir(parents=True, exist_ok=True)
        db = kdir / "knowledge.db"
        build_db(db, 6, "e2e")

        with patch.multiple(config, DATA=tmp, LOGS=tmp / "logs", MODELS=tmp / "models",
                            DATASETS=tmp / "datasets", KNOWLEDGE=kdir,
                            CONFIG_FILE=tmp / "config.json", DB_FILE=db, _cache=None), \
                patch.object(memory, "log_event", lambda *a, **k: None):
            client = TestClient(main.app)

            r = client.post("/api/backups/export", json={"label": "端到端"})
            check("导出接口 200", r.status_code == 200, r.text[:120])
            name = (r.json() or {}).get("name", "")
            check("返回备份文件名", bool(name), name)
            check("返回下载地址", "/api/backups/download/" in (r.json() or {}).get("download", ""))

            items = client.get("/api/backups").json()["items"]
            check("列表含新备份", any(x["name"] == name for x in items))

            r = client.get(f"/api/backups/download/{name}")
            check("下载是 zip", r.status_code == 200 and r.content[:2] == b"PK", r.status_code)

            r = client.get("/api/backups/download/evil.zip")
            check("单段非法名下载 400", r.status_code == 400, r.status_code)

            import urllib.parse

            trav = urllib.parse.quote("../knowledge.db", safe="")
            r = client.get(f"/api/backups/download/{trav}")
            check("路径逃逸被拒（非 200）", r.status_code in (400, 404), r.status_code)

            bak = kdir.parent / "backups" / name
            with open(bak, "rb") as f:
                payload = f.read()

            # 改库 → 从备份恢复 → 行数应回到 6
            build_db(db, 2, "after")
            r = client.post("/api/backups/restore",
                            files={"file": (name, payload, "application/zip")},
                            data={"confirm": "true"})
            check("恢复接口 200", r.status_code == 200, r.text[:160])
            c = sqlite3.connect(str(db))
            n = c.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
            c.close()
            check("恢复后行数回到 6", n == 6, n)

            names = [x["name"] for x in client.get("/api/backups").json()["items"]]
            check("自动生成 pre-restore 兜底", any("pre-restore" in x for x in names), names)

            r = client.post("/api/backups/restore",
                            files={"file": (name, payload, "application/zip")},
                            data={"confirm": "false"})
            check("未确认恢复 400", r.status_code == 400, r.status_code)

            bad = tmp / "not-a-backup.zip"
            with zipfile.ZipFile(bad, "w") as zf:
                zf.writestr("note.txt", "没有知识库")
            with open(bad, "rb") as f:
                bad_payload = f.read()
            r = client.post("/api/backups/restore",
                            files={"file": ("bad.zip", bad_payload, "application/zip")},
                            data={"confirm": "true"})
            check("坏 zip 恢复 400", r.status_code == 400, r.status_code)

            r = client.delete(f"/api/backups/{name}")
            check("删除 200", r.status_code == 200, r.status_code)
            r = client.delete(f"/api/backups/{name}")
            check("重复删除 404", r.status_code == 404, r.status_code)
    finally:
        # 兜底：清掉本线程可能残留的连接再回收临时目录
        c = getattr(memory._local, "c", None)
        if c is not None:
            c.close()
            memory._local.c = None
        gc.collect()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)

    print("\n全部通过 ✔" if not fails else "\n失败：")
    for f in fails:
        print("  - " + f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
