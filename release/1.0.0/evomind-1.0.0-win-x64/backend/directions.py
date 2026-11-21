"""Local randomized direction decks, available even when the model is offline."""
import random
from . import config

ZBRUSH = [
    "DynaMesh 分辨率、模型尺寸与细节保留", "ZRemesher 引导线与目标面数设置",
    "细分层级与雕刻细节的分配", "Subdivision 与 DynaMesh 的切换时机",
    "角色头部比例与主要骨点塑造", "眼睑、眼窝和眼球的分层雕刻",
    "鼻翼、鼻梁和口周的体块关系", "手部骨点、关节与指节雕刻",
    "头发大体块与发束分组方法", "衣褶的受力点与主次关系",
    "ClayBuildup、DamStandard 与 Move 笔刷分工", "笔刷强度、焦点与笔触稳定设置",
    "遮罩、PolyGroups 与可见性管理", "SubTool 拆分、合并与命名管理",
    "布尔切割与硬表面边缘控制", "ZModeler 的面挤出与支撑线工作流",
    "投射细节前的距离设置与伪影排查", "多级细分模型的重拓扑与细节投射",
    "UV 展开、接缝与贴图导出", "Polypaint 分辨率与颜色分层",
    "置换贴图与法线贴图的烘焙区别", "Alpha 笔刷的灰度、深度与接缝控制",
    "对称失效、局部对称与姿态恢复", "Transpose 与 Gizmo 的轴心设置",
    "Decimation Master 减面与细节检查", "导出 STL 的尺寸、单位与网格检查",
    "手办分件切割与定位桩设计流程", "薄壁、穿插和非流形网格的检查方法",
    "参考图对齐与轮廓校验", "雕刻阶段的文件版本与恢复策略",
]
GENERAL = [
    "核心概念及最容易混淆的术语", "入门工作流与最小实践步骤", "关键参数对结果的影响",
    "常见错误的原因与修复步骤", "典型案例的逐步拆解", "质量检查清单与判断标准",
    "基础方法和进阶方法的选择条件", "性能瓶颈与效率优化", "数据或文件的导入导出",
    "可复用的操作流程与快捷方式", "边界条件与不适用场景", "官方文档中的核心机制",
    "不同方案的取舍与对比", "失败案例与排错顺序", "从练习到完整作品的工作流",
    "结果验证与重复实验方法", "与相关工具协作的接口", "项目组织与版本管理",
]


def deck(seed: str) -> list[str]:
    """该领域对应的本地候选池（只读，不改配置、不依赖模型）。

    方向轮换在模型不可用或拆解数量不够时用它兜底：拼出来的方向一定带着
    母方向前缀，天然落在语义范围内。
    """
    return ZBRUSH if "zbrush" in seed.lower() else GENERAL


def compose(seed: str, parts: list[str], exclude: list[str] | None = None, count: int = 6) -> list[str]:
    """把母方向与候选侧面拼成具体方向，跳过已用过的。"""
    seed = " ".join(seed.split())
    used = {s.casefold() for s in (exclude or [])}
    fresh = [f"{seed}：{part}" for part in parts if f"{seed}：{part}".casefold() not in used]
    if len(fresh) < count:
        fresh = [f"{seed}：{part}" for part in parts]
    return random.SystemRandom().sample(fresh, min(count, len(fresh)))


def refresh(seed: str, exclude: list[str] | None = None, count: int = 6) -> dict:
    seed = " ".join(seed.split())
    if not seed or len(seed) > 200:
        raise ValueError("预设方向需为1到200个字符")
    config.update({"learning_seed": seed})
    return {"seed": seed, "items": compose(seed, deck(seed), exclude, count)}
