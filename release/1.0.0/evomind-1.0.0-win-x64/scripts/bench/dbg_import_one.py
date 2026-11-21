"""调试：用单本书导入，把接受的 claim 集合写到文件，便于比对两条路径差异。"""
import sys, hashlib, time, json
from pathlib import Path
sys.path.insert(0, "scripts/bench")
import _env
_env.isolate(); _env.fake_llm()
from backend import library, memory

name = sys.argv[1] if len(sys.argv) > 1 else "plain.txt"
out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/claims.json"

p = _env.FIXTURES / name
library.init_db()
c = memory.conn()
# 清掉所有知识点，保证从干净库开始
c.execute("DELETE FROM knowledge"); c.commit(); memory._invalidate()
bid = hashlib.md5(str(p).encode("utf-8")).hexdigest()[:16]
c.execute(
    "INSERT OR REPLACE INTO books(id,dir_id,path,source_type,format,title,"
    "size_bytes,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
    (bid, "", str(p), "local", p.suffix.lstrip("."), p.stem,
     p.stat().st_size, "pending", time.time()),
)
c.commit()
library.parse_book(bid)
imp = library.import_book(bid, topic="等价性核对", max_chunks=12)
rows = c.execute("SELECT claim FROM knowledge ORDER BY claim").fetchall()
claims = [r["claim"] for r in rows]
Path(out).write_text(json.dumps(claims, ensure_ascii=False, indent=0), encoding="utf-8")
print(f"{name}: 接受 {len(claims)} 条, points={imp.get('points')}")
