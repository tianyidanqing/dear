# -*- coding: utf-8 -*-
"""任务资源：增删查改、今日排期、周期模板、速览。全部按当前用户隔离。"""
import asyncio

from fastapi import APIRouter, Depends, Request

from schedule_agent.api.deps import agent_for, get_current_user
from schedule_agent.utils import parse_dt

router = APIRouter(tags=["tasks"])


@router.get("/api/tasks")
async def list_tasks(request: Request, user_id: str = Depends(get_current_user)):
    agent = agent_for(request, user_id)
    return await asyncio.to_thread(agent.list_tasks)


@router.post("/api/tasks")
async def add_task(payload: dict, request: Request,
                   user_id: str = Depends(get_current_user)):
    """添加任务：`text`（自然语言）或结构化字段。归属恒为当前用户。"""
    payload = payload or {}
    text = payload.get("text")
    agent = agent_for(request, user_id)
    if text:
        return await asyncio.to_thread(agent.add_task, text)
    return await asyncio.to_thread(
        agent.add_task,
        text="",
        due=parse_dt(payload.get("due")),
        start=parse_dt(payload.get("start")),
        duration_min=int(payload.get("duration_min", 30)),
        title=payload.get("title"),
        recurrence=payload.get("recurrence", ""),
    )


@router.get("/api/tasks/today")
async def today(request: Request, user_id: str = Depends(get_current_user)):
    agent = agent_for(request, user_id)
    return await asyncio.to_thread(agent.show_today)


@router.post("/api/tasks/{task_id}/done")
async def mark_done(task_id: int, request: Request,
                    user_id: str = Depends(get_current_user)):
    """标记完成；对周期模板 = 停止该周期。跨用户只会得到「未找到」。"""
    agent = agent_for(request, user_id)
    return await asyncio.to_thread(agent.mark_done, task_id)


@router.delete("/api/tasks/{task_id}")
async def remove(task_id: int, request: Request,
                 user_id: str = Depends(get_current_user)):
    agent = agent_for(request, user_id)
    return await asyncio.to_thread(agent.remove, task_id)


@router.get("/api/templates")
async def templates(request: Request, user_id: str = Depends(get_current_user)):
    agent = agent_for(request, user_id)
    return await asyncio.to_thread(agent.list_templates)


@router.get("/api/summary")
async def summary(request: Request, user_id: str = Depends(get_current_user)):
    agent = agent_for(request, user_id)
    return await asyncio.to_thread(agent.summary)
