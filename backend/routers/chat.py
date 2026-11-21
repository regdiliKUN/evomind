"""对话（基于知识库的 RAG 问答，含流式输出）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse

from .. import evolve

router = APIRouter()


# --------------------------------------------------------------------------
# 对话（基于知识库）
# --------------------------------------------------------------------------
@router.post("/api/chat")
def api_chat(body: dict = Body(...)) -> dict:
    question = (body.get("question") or "").strip()
    images = body.get("images") or []
    if not question and not images:
        raise HTTPException(400, "缺少问题")
    return evolve.answer_with_knowledge(
        question or "请描述这些图片的内容", body.get("topic") or None, images=images
    )


@router.post("/api/chat/stream")
def api_chat_stream(body: dict = Body(...)) -> StreamingResponse:
    question = (body.get("question") or "").strip()
    topic = body.get("topic") or None
    images = body.get("images") or []
    if not question and not images:
        raise HTTPException(400, "缺少问题")

    def gen():
        try:
            res = evolve.answer_with_knowledge(question or "请描述这些图片的内容", topic, images=images)
            for i in range(0, len(res["answer"]), 24):
                yield f"data: {json.dumps({'delta': res['answer'][i:i+24]}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True, 'sources': res['sources'], 'hits': res['hits']}, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
