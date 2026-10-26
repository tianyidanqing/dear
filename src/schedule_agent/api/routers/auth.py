# -*- coding: utf-8 -*-
"""注册 / 登录：公开端点，用于换取 Bearer Token。

错误信息按请求语言返回：存储层给的是**稳定错误码**（`already exists` 等），
这里翻译成用户能看懂的话；未知错误码原样透出，避免把真实原因吞掉。
"""
from fastapi import APIRouter, HTTPException

from schedule_agent.i18n import t
from schedule_agent.storage import auth as auth_store

router = APIRouter(tags=["auth"])

# 存储层错误码 -> 文案 key
_ERROR_KEYS = {
    "empty user_id": "api.auth_empty_user",
    "already exists": "api.auth_user_exists",
    "no such user": "api.auth_no_such_user",
    "wrong password": "api.auth_bad_password",
}


def _translate(err: str) -> str:
    key = _ERROR_KEYS.get((err or "").strip())
    return t(key) if key else err


@router.post("/api/auth/register")
async def register(payload: dict):
    """注册用户并签发 API Key；已存在返回 409（此时应改用 /api/auth/login）。"""
    payload = payload or {}
    user_id = (payload.get("user_id") or "").strip()
    password = payload.get("password") or None
    api_key, err = auth_store.register(user_id, password)
    if err:
        raise HTTPException(status_code=409,
                            detail=t("api.user_exists", err=_translate(err)))
    return {"ok": True, "user_id": user_id, "api_key": api_key}


@router.post("/api/auth/login")
async def login(payload: dict):
    """登录并取回 API Key；设过密码的用户必须携带正确密码。"""
    payload = payload or {}
    user_id = (payload.get("user_id") or "").strip()
    password = payload.get("password") or None
    api_key, err = auth_store.authenticate(user_id, password)
    if err:
        raise HTTPException(status_code=401,
                            detail=t("api.login_failed", err=_translate(err)))
    return {"ok": True, "user_id": user_id, "api_key": api_key}
