#!/usr/bin/env bash
# 向量写入 A/B：库内已有 N 条时写入 M 条，交替跑「原始版」和「优化版」
#   bash scripts/bench/ab_vec.sh [轮数] [库内已有] [每轮写入]
set -u
cd "$(dirname "$0")/../.." || exit 1

ROUNDS="${1:-20}"
EXISTING="${2:-4000}"
ADD="${3:-100}"
PY=.venv/Scripts/python.exe
SNAP=/tmp/ab_vec_memory_opt.py

cp backend/memory.py "$SNAP"

run() {
  "$PY" - "$EXISTING" "$ADD" "$ROUNDS" <<'EOF'
import sys, time, statistics
sys.path.insert(0, 'scripts/bench')
import _env
db = _env.DB_DIR / 'bench.db'
if db.exists():
    db.unlink()
_env.isolate(); _env.fake_llm()
from backend import memory
c = memory.conn(); c.executescript(memory.SCHEMA); c.commit()
existing, add, rounds = (int(x) for x in sys.argv[1:4])


def one():
    c.execute('DELETE FROM knowledge'); c.commit(); memory._invalidate()
    for i in range(existing):
        memory.add_point(topic='t', claim=f'既有知识点 {i} 这是一条足够长的句子用于压测。',
                         detail='填充' * 100, dedup=False)
    memory._invalidate()
    t0 = time.perf_counter()
    for i in range(add):
        memory.add_point(topic='t', claim=f'新增知识点 {i} 内容各不相同互不重复的句子压测用。',
                         detail='内容' * 100, dedup=True)
    return (time.perf_counter() - t0) * 1000


ts = [one() for _ in range(rounds)]
print(f'{statistics.median(ts):.1f} {statistics.mean(ts):.1f} {min(ts):.1f} {max(ts):.1f}')
EOF
}

echo "轮数=$ROUNDS 库内已有=$EXISTING 每轮写入=$ADD"
NEW=$(run)
git checkout -- backend/memory.py
OLD=$(run)
cp "$SNAP" backend/memory.py

echo "  优化版 中位 $(echo $NEW | cut -d' ' -f1) ms  均值 $(echo $NEW | cut -d' ' -f2) ms  [min $(echo $NEW | cut -d' ' -f3) / max $(echo $NEW | cut -d' ' -f4)]"
echo "  原始版 中位 $(echo $OLD | cut -d' ' -f1) ms  均值 $(echo $OLD | cut -d' ' -f2) ms  [min $(echo $OLD | cut -d' ' -f3) / max $(echo $OLD | cut -d' ' -f4)]"
awk -v o="$(echo $OLD | cut -d' ' -f1)" -v n="$(echo $NEW | cut -d' ' -f1)" \
  'BEGIN{printf "  中位数口径 提升 %.2fx（%.0f%%）\n", o/n, (o/n-1)*100}'
echo "（工作区已恢复）"
