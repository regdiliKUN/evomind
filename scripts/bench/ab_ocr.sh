#!/usr/bin/env bash
# OCR 端到端 A/B：同一环境下交替跑「原始版」和「优化版」，抵消外部漂移
# （本机显卡同时被 Ollama 的 27B 模型占着，单次测量波动很大，必须交替跑）
#
#   bash scripts/bench/ab_ocr.sh [轮数] [页数]
set -u
cd "$(dirname "$0")/../.." || exit 1

ROUNDS="${1:-3}"
PAGES="${2:-1-40}"
OPT=data/bench/opt_backup
PY=.venv/Scripts/python.exe

# 首次运行时把「当前」的 backend 当作优化版存一份；之后每轮都用它还原
if [ ! -f "$OPT/ocr.py" ]; then
  mkdir -p "$OPT"
  cp backend/library.py backend/memory.py backend/ocr.py "$OPT/"
  echo "已把当前 backend/ 存为优化版快照 → $OPT"
fi

echo "轮数=$ROUNDS 页范围=$PAGES"
echo "-----"

before=()
after=()

for ((r = 1; r <= ROUNDS; r++)); do
  git checkout -- backend/
  t=$("$PY" scripts/bench/bench_ocr.py --e2e --pages "$PAGES" 2>/dev/null | grep -o '吞吐 [0-9.]*' | awk '{print $2}')
  before+=("$t"); echo "第${r}轮  原始 ${t} 页/分钟"

  cp $OPT/*.py backend/
  t=$("$PY" scripts/bench/bench_ocr.py --e2e --pages "$PAGES" 2>/dev/null | grep -o '吞吐 [0-9.]*' | awk '{print $2}')
  after+=("$t"); echo "第${r}轮  优化 ${t} 页/分钟"
done

cp $OPT/*.py backend/

echo "-----"
med() { printf '%s\n' "$@" | sort -g | awk '{a[NR]=$1} END {print (NR%2)?a[(NR+1)/2]:(a[NR/2]+a[NR/2+1])/2}'; }
mb=$(med "${before[@]}"); ma=$(med "${after[@]}")
echo "原始 中位数 ${mb} 页/分钟   样本: ${before[*]}"
echo "优化 中位数 ${ma} 页/分钟   样本: ${after[*]}"
awk -v a="$ma" -v b="$mb" 'BEGIN{printf "提升 %.2fx（%.0f%%）\n", a/b, (a/b-1)*100}'
echo "（工作区已恢复到优化版）"
