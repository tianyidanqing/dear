# -*- coding: utf-8 -*-
"""企业微信消息解析与文本适配（纯函数，方便单测）。"""
from __future__ import annotations

from xml.etree import ElementTree as ET

from schedule_agent.channels.base import strip_markdown, truncate_bytes
from schedule_agent.i18n import t


# ---------------------------------------------------------------------------
# 报文解析
# ---------------------------------------------------------------------------
def parse_xml(xml_text: str) -> dict:
    """把回调 XML 解析成扁平 dict（只取一层，够用且不用处理命名空间）。"""
    if not xml_text or not xml_text.strip():
        raise ValueError("回调 body 为空")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"回调 XML 解析失败：{e}") from e
    return {child.tag: (child.text or "").strip() for child in root}


def extract_encrypt(xml_text: str) -> dict:
    """从外层信封里取 Encrypt / ToUserName / AgentID。"""
    data = parse_xml(xml_text)
    if not data.get("Encrypt"):
        raise ValueError("回调 XML 里没有 Encrypt 节点（后台可能未开启加密模式）")
    return data


def parse_message(xml_text: str) -> dict:
    """解析**已解密**的消息 XML，返回统一结构。

    常见 MsgType：text / image / voice / event（subscribe 等）。
    """
    d = parse_xml(xml_text)
    return {
        "from_user": d.get("FromUserName", ""),
        "to_user": d.get("ToUserName", ""),
        "msg_type": (d.get("MsgType") or "").lower(),
        "content": d.get("Content", ""),
        "msg_id": d.get("MsgId", ""),
        "create_time": d.get("CreateTime", ""),
        "agent_id": d.get("AgentID", ""),
        "event": (d.get("Event") or "").lower(),
    }


# ---------------------------------------------------------------------------
# 身份映射：企业微信 UserID -> 本系统 user_id
# ---------------------------------------------------------------------------
def resolve_user_id(wecom_userid: str, bind_map: dict | None = None) -> str:
    """把企业微信成员账号映射成本项目的 user_id。

    默认给每个企微用户开独立空间（`wecom:<UserID>`），互不干扰；
    若想让某人共用网页端的已有数据，在配置 `channels.wecom.bind_map` 里显式绑定：
        {"zhangsan": "jiyu"}
    """
    uid = (wecom_userid or "").strip()
    if not uid:
        raise ValueError("消息里没有 FromUserName")
    return (bind_map or {}).get(uid) or f"wecom:{uid}"


# ---------------------------------------------------------------------------
# 文本适配：把结构化结果变成手机上好读的纯文本
# ---------------------------------------------------------------------------
def format_reply(text: str, schedule: list | None = None, limit: int = 2048) -> str:
    """企业微信只显示纯文本，这里去掉 Markdown 标记并补排期。

    `schedule` 存在时（add/modify/today 等动作），把当天排期压成一行一条，
    比直接塞 summarize() 的宽表格更适合手机阅读。
    """
    body = strip_markdown((text or "").strip())

    if schedule:
        lines = []
        for item in schedule[:12]:
            st = (item.get("scheduled_start") or item.get("start") or "")[11:16]
            en = (item.get("scheduled_end") or "")[11:16]
            span = (f"{st}-{en}" if st and en
                    else (st or t("channel.not_scheduled")))
            risk = " ⚠️" if item.get("risk") else ""
            lines.append(f"· {span} {item.get('title', '')}{risk}")
        if lines:
            body = (body + "\n\n" + t("channel.schedule_header") + "\n"
                    + "\n".join(lines)).strip()

    return truncate_bytes(body, limit)
