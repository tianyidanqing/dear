# -*- coding: utf-8 -*-
"""管理员端点：需 config 中的 admin_api_key（为空则能力关闭）。"""
from fastapi import APIRouter, Depends

from schedule_agent.api.deps import require_admin
from schedule_agent.storage import auth as auth_store

router = APIRouter(tags=["admin"])


@router.get("/api/admin/users")
async def admin_users(_: str = Depends(require_admin)):
    return {"ok": True, "users": auth_store.list_users()}
