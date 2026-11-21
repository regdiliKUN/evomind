"""测一轮学习里「对话模型 ↔ 向量模型」来回切换的真实代价。

背景：每轮学习的 ① 诊断缺口要调 memory.search() → llm.embed()，
④ 提炼要调 llm.chat()。Ollama 在同一张卡上只驻留有限个模型时，
embed 会把 27B 挤出去，下一次 chat 要重新加载（16.5GB 读盘 + 上卡）。

本脚本按真实顺序打一遍，并在每步后查 /api/ps 看谁还在显存里：

    chat(冷) → chat(热) → embed(冷) → chat(embed 之后)

用法：
    .venv\\Scripts\\python.exe tests\\bench_model_swap.py [--repeat 2]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from backend import config, llm  # noqa: E402


def loaded() -> str:
    try:
        with httpx.Client(base_url=config.get("ollama_url").rstrip("/"), timeout=5) as c:
            models = c.get("/api/ps").json().get("models") or []
    except Exception as e:  # noqa: BLE001
        return f"(ps 失败 {e})"
    if not models:
        return "(空)"
    return " + ".join(f"{m.get('name') or m.get('model')}({round(m.get('size', 0) / 2**30, 1)}G)"
                      for m in models)


def timed(fn, label: str) -> float:
    t0 = time.perf_counter()
    out = fn()
    el = time.perf_counter() - t0
    print(f"  {label:<26} {el * 1000:8.0f} ms   显存：{loaded()}")
    return el


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=2)
    args = ap.parse_args()

    main_model = config.get("model")
    emb_model = config.get("embed_model")
    print(f"对话模型 {main_model} / 向量模型 {emb_model}")
    print(f"num_ctx {config.get('context_num')}，think={not config.get('disable_thinking', True)}")

    ask = [{"role": "user", "content": "用一句话说明什么是重拓扑。"}]
    rows: list[dict] = []
    for i in range(args.repeat):
        print(f"\n第 {i + 1} 轮")
        cold = timed(lambda: llm.chat(ask, timeout=600), "chat 冷启动")
        warm = timed(lambda: llm.chat(ask, timeout=600), "chat 热")
        emb = timed(lambda: llm.embed(["重拓扑"]), "embed（计划阶段）")
        back = timed(lambda: llm.chat(ask, timeout=600), "chat（embed 之后）")
        rows.append({"cold_chat_ms": round(cold * 1000), "warm_chat_ms": round(warm * 1000),
                     "embed_ms": round(emb * 1000), "chat_after_embed_ms": round(back * 1000)})
        # 再把 embed 打一次：如果两边都驻留，这次应该和上面差不多
        timed(lambda: llm.embed(["重拓扑"]), "embed（再打一次）")

    print("\n结果：")
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    if rows:
        swap = sum(r["chat_after_embed_ms"] - r["warm_chat_ms"] for r in rows) / len(rows)
        print(f"\n每轮因 embed 造成的额外等待（chat_after_embed − chat 热）≈ {swap:.0f} ms")


if __name__ == "__main__":
    main()
