# -*- coding: utf-8 -*-
"""FastAPI 依赖：鉴权解析 + 共享资源获取。

鉴权：
  - 常规请求：`Authorization: Bearer <api_key>`
  - SSE / WebSocket（客户端不便设头）：URL 查询参数 `?token=<api_key>`
  - 管理员：config 里的 `admin_api_key` 作 Bearer，再用 `X-User-Id` 指定目标用户
"""
from __future__ import annotations

from fastapi import Header, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse

from schedule_agent.config import get_settings
from schedule_agent.i18n import t
from schedule_agent.storage import auth as auth_store


# ---------------------------------------------------------------------------
# 共享资源
# ---------------------------------------------------------------------------
def get_registry(request: Request):
    return request.app.state.registry


def get_bus(request: Request):
    return request.app.state.bus


def agent_for(request: Request, user_id: str):
    """取该用户专属的 Agent。"""
    return request.app.state.registry.get(user_id)


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------
def _bearer(authorization: str | None, token: str | None) -> str:
    """从请求头或查询参数里取出 token。"""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return (token or "").strip()


def get_current_user(
    request: Request,
    authorization: str = Header(None),
    token: str = Query(None),
    x_user_id: str = Header(None, alias="X-User-Id"),
) -> str:
    """解析当前请求的用户身份，失败直接 401。"""
    tok = _bearer(authorization, token)
    if not tok:
        raise HTTPException(status_code=401, detail=t("api.missing_token"))

    admin_key = get_settings().admin_api_key
    if admin_key and tok == admin_key:
        if not x_user_id:
            raise HTTPException(status_code=400,
                                detail=t("api.admin_header_required"))
        return x_user_id

    user_id = auth_store.verify_token(tok)
    if user_id is None:
        raise HTTPException(status_code=401, detail=t("api.invalid_token"))
    return user_id


def require_admin(
    request: Request,
    authorization: str = Header(None),
    token: str = Query(None),
) -> str:
    """管理员接口：校验 admin_api_key；未配置则能力关闭（403）。"""
    admin_key = get_settings().admin_api_key
    if not admin_key:
        raise HTTPException(status_code=403, detail=t("api.admin_disabled"))
    if _bearer(authorization, token) != admin_key:
        raise HTTPException(status_code=403, detail=t("api.admin_wrong"))
    return "__admin__"


def ws_current_user(websocket: WebSocket) -> str | None:
    """WebSocket 鉴权：浏览器 WS 不能自定义头，故 token 走查询参数。"""
    tok = (websocket.query_params.get("token") or "").strip()
    if not tok:
        return None
    if get_settings().admin_api_key and tok == get_settings().admin_api_key:
        return websocket.query_params.get("user_id") or None
    return auth_store.verify_token(tok)
