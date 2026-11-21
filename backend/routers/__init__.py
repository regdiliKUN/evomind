"""按域拆分的 HTTP 路由（原先全部堆在 backend/main.py：117 个接口 / 1500 行）。

``ROUTERS`` 的顺序就是注册顺序。Starlette 按注册顺序匹配：同一模块内沿用原
main.py 的相对顺序（例如 knowledge 里 /api/knowledge/<字面量> 先于单段 {pid}）；
跨模块之间不允许出现「同一方法 + 可同时命中同一路径」的路由，
tests/test_main_routers.py 会逐对校验，新增接口撞上别的域时测试直接失败。

新增接口请放进对应域的模块；新增一个域时，在这里登记并加进 ``ROUTERS``。
"""
from __future__ import annotations

from typing import Any, Iterator

from . import (
    system,
    learning,
    events,
    knowledge,
    analytics,
    topics,
    resources,
    media,
    chat,
    finetune,
    backups,
    library,
    ocr,
    review,
)

ROUTERS = (
    system.router,
    learning.router,
    events.router,
    knowledge.router,
    analytics.router,
    topics.router,
    resources.router,
    media.router,
    chat.router,
    finetune.router,
    backups.router,
    library.router,
    ocr.router,
    review.router,
)


def iter_api_routes(app: Any) -> Iterator[Any]:
    """版本无关地展开 app 的路由表（每项都有 path / methods / name）。

    FastAPI ≥ 0.150 的 ``include_router`` 不再把子路由摊平进 ``app.routes``，
    而是挂一个惰性的 ``_IncludedRouter`` 节点；旧版本则直接摊平。需要按
    路由自省（测试、文档、诊断）时统一走这里，不要直接遍历 ``app.routes``。
    """
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # 旧版 FastAPI：include_router 已摊平
        yield from app.routes
        return
    yield from iter_route_contexts(app.routes)


__all__ = ["ROUTERS", "iter_api_routes"]
