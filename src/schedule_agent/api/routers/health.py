# -*- coding: utf-8 -*-
"""健康检查与当前身份（探活用，健康检查不鉴权）。"""
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request

from schedule_agent import __version__
from schedule_agent.api.deps import get_current_user
from schedule_agent.i18n import t

router = APIRouter(tags=["health"])


def _locales() -> list[str]:
    """磁盘上可用的语言（新增 JSON 即自动出现，无需改代码）。"""
    from schedule_agent.i18n import available_locales
    return available_locales()


def llm_status() -> dict:
    """大模型配置概览（**不返回 Key**，只用来确认「到底配没配上」）。"""
    from schedule_agent.config import get_settings
    from schedule_agent.llm import client as llm
    s = get_settings()
    host = urlparse(s.llm_base_url or "").netloc or s.llm_base_url
    return {
        "enabled": llm.llm_enabled(),
        "model": s.llm_model,
        "base_host": host,
        "last_error": llm.last_error(),
        # 默认 false：意图只由模型判断，模型不可用就直接报错（不会用规则猜）
        "use_rule_fallback": s.use_rule_fallback,
        "language": s.language,
        "locales": _locales(),
        "note": t("health.note"),
    }


@router.get("/api/health")
async def health(request: Request):
    channels = getattr(request.app.state, "channels", {}) or {}
    return {
        "status": "ok",
        "service": "schedule-agent",
        "version": __version__,
        "auth_required": True,
        "users_cached": len(request.app.state.registry.known_users()),
        "llm": llm_status(),
        # 已启用的第三方渠道（只暴露名字与回调地址，不含任何凭据）
        "channels": [
            {"name": ch.name, "display_name": ch.display_name,
             "callback": ch.callback_path}
            for ch in channels.values()
        ],
    }


@router.get("/api/me")
async def me(user_id: str = Depends(get_current_user)):
    return {"ok": True, "user_id": user_id}
