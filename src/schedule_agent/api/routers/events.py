# -*- coding: utf-8 -*-
"""提醒事件：轮询接口与 WebSocket 推送，均只返回当前用户自己的事件。"""
import asyncio

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect

from schedule_agent.api.deps import get_current_user, ws_current_user

router = APIRouter(tags=["events"])


@router.get("/api/events")
async def get_events(request: Request, since: int = 0,
                     user_id: str = Depends(get_current_user)):
    """返回 since 之后、属于当前用户的提醒/速览事件。"""
    return request.app.state.bus.since(since, user_id)


@router.websocket("/api/ws")
async def ws_events(websocket: WebSocket):
    """WebSocket 实时推送。浏览器 WS 无法自定义头，token 走查询参数：
    ws://host/api/ws?token=<api_key>
    """
    user_id = ws_current_user(websocket)
    if user_id is None:
        # 必须先 accept 才能回一个明确的关闭码
        await websocket.accept()
        await websocket.close(code=1008, reason="invalid token")
        return

    await websocket.accept()
    bus = websocket.app.state.bus
    last = 0
    try:
        while True:
            new = bus.since(last, user_id)
            for e in new:
                try:
                    await websocket.send_json(e)
                    last = e["id"]
                except Exception:  # noqa: 连接已断开
                    break
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
