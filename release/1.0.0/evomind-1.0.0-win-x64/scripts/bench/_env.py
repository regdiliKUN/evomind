"""基准测试公共环境：把数据库、数据目录隔离到临时目录，避免污染用户真实数据。

所有 bench 脚本都先 import 本模块，再 import backend.*。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

#: 基准工作区（可用 EVOMIND_BENCH_DIR 覆盖）
BENCH_DIR = Path(
    os.environ.get("EVOMIND_BENCH_DIR")
    or (ROOT / "data" / "bench")
).resolve()
FIXTURES = BENCH_DIR / "fixtures"
CACHE = BENCH_DIR / "cache"
DB_DIR = BENCH_DIR / "db"


def isolate(seed_db: Path | None = None) -> Path:
    """把 config.DB_FILE 指向基准专用库，返回该库路径。

    默认每次都从空库开始：基准前后跑的是同一套语料，库里若残留上次写入的
    知识点，去重会把新知识点全挡掉，测出来的就不是同一件事了。

    必须在任何 backend 模块建立连接之前调用（memory.conn 是线程局部缓存）。
    """
    from backend import config

    DB_DIR.mkdir(parents=True, exist_ok=True)
    # 正文缓存等副产物也落进基准目录，不弄脏用户真实的 data/
    config.DATA = BENCH_DIR / "data"
    config.DATA.mkdir(parents=True, exist_ok=True)
    db = DB_DIR / "bench.db"
    if seed_db is None:
        for suffix in ("", "-wal", "-shm", "-journal"):
            s = Path(str(db) + suffix)
            if s.exists():
                s.unlink()
    else:
        import shutil

        if db.exists():
            db.unlink()
        shutil.copy2(seed_db, db)
        for suffix in ("-wal", "-shm"):
            s = Path(str(db) + suffix)
            if s.exists():
                s.unlink()
    config.DB_FILE = db
    config.KNOWLEDGE = DB_DIR
    return db


def fake_llm(dim: int = 1024, points_per_chunk: int = 5,
             keep_real_embed: bool = False) -> None:
    """用确定性假实现替换 LLM：让基准可重复、且不依赖 Ollama。

    向量维度保持 1024（与 bge-m3 一致），这样向量矩阵的重建开销是真实的。
    """
    import hashlib
    import random

    from backend import llm

    def _vec(s: str) -> list[float]:
        # 用文本哈希做种子：同一句话永远得到同一向量，去重行为可复现
        h = int(hashlib.md5(s.encode("utf-8")).hexdigest()[:8], 16)
        rnd = random.Random(h)
        return [rnd.gauss(0, 1) for _ in range(dim)]

    def _embed(texts, *, batch: int = 16):
        return [_vec(str(t)) for t in texts]

    def _chat_json(system: str, user: str, **kw):
        # 从正文里挑几句现成的话当知识点，保证与真实流程一样要经过去重/入库
        body = user.split("## 正文")[-1]
        lines = [x.strip() for x in body.split("\n") if len(x.strip()) > 20]
        picked = lines[:points_per_chunk] or ["这是一条用于基准测试的知识点占位内容。"]
        return {
            "points": [
                {
                    "claim": (x[:60] if len(x) > 60 else x),
                    "detail": x[:200],
                    "tags": ["bench"],
                    "relevance": 0.9 - i * 0.05,
                    "confidence": 0.8,
                }
                for i, x in enumerate(picked)
            ]
        }

    if not keep_real_embed:
        llm.embed = _embed
    llm.chat_json = _chat_json
    # chat / chat_stream 在导入链路里用不到，兜个底避免误调真实模型
    llm.chat = lambda messages, **kw: ""
