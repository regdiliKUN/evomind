"""冒烟服务器：隔离数据目录 + 种子语料，供可视化验证新 UI。

用法：.venv/Scripts/python.exe scripts/bench/smoke_server.py [端口]
"""
from __future__ import annotations

import sys
import time

sys.path.insert(0, "scripts/bench")
sys.path.insert(0, ".")

import _env  # noqa: E402

_env.isolate()

import numpy as np  # noqa: E402

from backend import memory  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8766

if memory.count_points() == 0:
    rng = np.random.default_rng(3)
    topics = ["光固化 3D 打印 · 收缩补偿", "ZBrush 硬表面雕刻", "Blender 几何节点", "大模型 Agent 规划机制"]
    claims = [
        "光敏树脂在紫外固化过程中的体积收缩率与曝光能量正相关，需要补偿模型",
        "ZBrush 的 DynaMesh 会在细分时重算拓扑，适合大形阶段的自由塑造",
        "Blender 几何节点用场运算驱动实例分布，可程序化生成机械结构",
        "Agent 的规划-反思循环让长任务可分解，每轮子目标都可验证",
        "收缩补偿通常采用反向缩放变换，在切片层面按层高线性调整",
        "硬表面雕刻强调硬边控制，先用遮罩再抛光，倒角均匀取决于曲率",
    ]
    # 主题向量带聚类：每个主题一个基向量 + 噪声，主题内相似度高、
    # 主题间有可分辨的差距，语义网络才会既有边、又能看出「哪两条最像」
    bases = [rng.standard_normal(64).astype(np.float32) for _ in range(len(topics))]
    for i in range(46):
        t = topics[i % len(topics)]
        v = bases[i % len(topics)] + rng.standard_normal(64).astype(np.float32) * 0.32
        v /= np.linalg.norm(v)
        memory.add_point(
            topic=t, claim=claims[i % len(claims)] + f"（变体 {i}）",
            detail="细节正文，说明这条结论的来源与适用范围。" * 4,
            source_url=f"https://example.com/doc/{i}",
            source_title=f"资料《{t.split(' · ')[0]}研究 {i}》",
            source_kind=["web", "paper", "repo", "qa"][i % 4],
            tags=["标签甲", "标签乙"], confidence=0.45 + (i % 10) / 20,
            vec=v.tolist(), dedup=False,
        )
    # 造几天的学习记录，让增长曲线/热力图/新鲜度有东西可画
    c = memory.conn()
    with c:
        for d in range(1, 8):
            c.execute(
                "UPDATE knowledge SET created_at=? WHERE rowid IN "
                "(SELECT rowid FROM knowledge ORDER BY rowid LIMIT 1 OFFSET ?)",
                (time.time() - d * 86400, d * 5),
            )
    print(f"seeded {memory.count_points()} points")

import uvicorn  # noqa: E402

from backend.main import app  # noqa: E402

print(f"smoke server on http://127.0.0.1:{PORT}")
uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
