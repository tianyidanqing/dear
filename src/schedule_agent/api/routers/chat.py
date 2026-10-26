# -*- coding: utf-8 -*-
"""聊天式入口：一句话完成添加 / 完成 / 查询，支持非流式与 SSE 流式。"""
import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from schedule_agent.api.deps import agent_for, get_current_user

router = APIRouter(tags=["chat"])


@router.post("/api/chat")
async def chat(payload: dict, request: Request,
               user_id: str = Depends(get_current_user)):
    """非流式：一次返回完整结构化结果。"""
    message = (payload or {}).get("message", "")
    agent = agent_for(request, user_id)
    return await asyncio.to_thread(agent.chat, message)


@router.post("/api/chat/stream")
async def chat_stream(payload: dict, request: Request,
                      user_id: str = Depends(get_current_user)):
    """流式（SSE）：逐块返回 status / result / token / done 事件，实现打字机效果。

    鉴权可用 Authorization 头，也可用查询参数 ?token=<api_key>（浏览器 EventSource）。
    """
    message = (payload or {}).get("message", "")
    agent = agent_for(request, user_id)

    async def event_gen():
        async for ev in agent.chat_stream(message):
            yield ("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n").encode("utf-8")
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
