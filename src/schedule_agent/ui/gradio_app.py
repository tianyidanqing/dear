#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""基于 Gradio 的简易前端：登录 / 对话 / 日程三个独立页面（中英文可切换）。

它只是一个**轻量前端**，所有逻辑都在后端的 FastAPI 微服务上完成：
这里只负责登录拿 Token、把用户输入发聊天接口、展示今日排期、拉取提醒事件。

页面（Gradio Tabs，各自独立）：
  1. 🔐 登录  —— 注册/登录拿 Bearer Token，退出；顶部常驻显示当前身份
  2. 💬 对话  —— 流式聊天（打字机），后端提醒以气泡注入
  3. 📅 日程  —— 今日排期表 + 全部任务表 + 按 ID 完成/删除

国际化说明（重要）：
  界面文案在**构建时**由 `i18n.t()` 生成，因此初始语言取决于
  `SCHEDULE_LANGUAGE` / 配置里的 `language` / `?lang=` 参数。
  Gradio 的 Tab 标题无法在运行时更新，所以顶部语言下拉切换后：
    - 按钮 / 输入框 / 表格列头 等**可更新组件立即变**；
    - Tab 标题等静态部分需要重新打开页面（或重启前端）才生效。
  后端回复的语言则是**按请求**决定的（请求会带 `?lang=`），所以对话内容
  会立刻跟着变，不受前端静态文案限制。

后端已开启 **API 鉴权 + 多用户隔离**，因此本前端：
  1. Token 存于 Gradio 会话 State（每个浏览器会话各自隔离），切换用户会清空本地内容。
  2. 所有 REST / SSE 请求都带 `Authorization: Bearer <token>`
     （流式接口同时把 token 放查询参数，便于浏览器原生 EventSource 对接）。
  3. 提醒事件由后端按 user_id 过滤，只会看到自己的日程提醒。

运行方式：
  schedule-agent-ui                       # console script
  python -m schedule_agent.ui.gradio_app  # 或直接跑模块
  SCHEDULE_API_URL=http://1.2.3.4:8000 schedule-agent-ui
"""
import json
import os

import gradio as gr
import requests

from schedule_agent.config import get_settings
from schedule_agent.i18n import (
    DEFAULT_LOCALE,
    available_locales,
    locale_name,
    normalize_locale,
    set_locale,
    t,
)

# 后端地址：环境变量优先，其次用配置里的 api_url
API_URL = (os.environ.get("SCHEDULE_API_URL")
           or get_settings().api_url).rstrip("/")
POLL_INTERVAL = int(os.environ.get("SCHEDULE_POLL_INTERVAL", "3"))

_session = requests.Session()

# 界面语言。Gradio 的事件在不同线程执行，用模块级变量保存（进程级）。
# 同时写回 settings.language，让没有显式 set_locale 的调用也能拿到正确语言。
_UI_LOCALE = (normalize_locale(os.environ.get("SCHEDULE_UI_LANGUAGE"))
              or normalize_locale(get_settings().language)
              or DEFAULT_LOCALE)


def _init_locale():
    """把启动语言同步到 i18n 与 settings（供 t() 的兜底路径使用）。"""
    global _UI_LOCALE
    if _UI_LOCALE not in available_locales():
        _UI_LOCALE = DEFAULT_LOCALE
    set_locale(_UI_LOCALE)
    try:
        get_settings().language = _UI_LOCALE
    except Exception:      # noqa: BLE001 - 配置只读时不影响界面
        pass


_init_locale()


def _lang():
    """当前界面语言（请求参数用）。"""
    return _UI_LOCALE


# ---------------------------------------------------------------------------
# HTTP 小工具
# ---------------------------------------------------------------------------
def _headers(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


def _with_lang(params):
    return {**(params or {}), "lang": _lang()}


def _post(path, payload=None, token=None, timeout=20):
    try:
        r = _session.post(API_URL + path, json=payload, params=_with_lang(None),
                          headers=_headers(token), timeout=timeout)
        if r.status_code >= 400:
            try:
                return {"ok": False, "message": r.json().get("detail", r.text[:200])}
            except Exception:  # noqa
                return {"ok": False, "message": r.text[:200]}
        return r.json()
    except requests.exceptions.ConnectionError:
        return {"ok": False, "message": t("ui.backend_unreachable", url=API_URL)}
    except Exception as e:  # noqa
        return {"ok": False, "message": t("ui.request_error", err=e)}


def _get(path, params=None, token=None, default=None):
    try:
        r = _session.get(API_URL + path, params=_with_lang(params),
                         headers=_headers(token), timeout=10)
        if r.status_code >= 400:
            return default
        return r.json()
    except requests.exceptions.ConnectionError:
        return default
    except Exception:  # noqa
        return default


def _del(path, token=None):
    try:
        r = _session.delete(API_URL + path, params=_with_lang(None),
                            headers=_headers(token), timeout=10)
        if r.status_code >= 400:
            return {"ok": False, "message": r.json().get("detail", r.text[:200])}
        return r.json()
    except requests.exceptions.ConnectionError:
        return {"ok": False, "message": t("ui.backend_unreachable", url=API_URL)}
    except Exception as e:  # noqa
        return {"ok": False, "message": t("ui.request_error", err=e)}


# ---------------------------------------------------------------------------
# 状态文案
# ---------------------------------------------------------------------------
def _status_md(auth):
    auth = auth or {}
    if not auth.get("token"):
        return t("ui.status_not_logged_in")
    masked = (auth.get("token") or "")[:8]
    return t("ui.status_logged_in", user=auth["user_id"], token=masked)


def _login_hint():
    return t("ui.login_hint_chat")


# ---------------------------------------------------------------------------
# 登录 / 注册（返回登录页所需的一整套更新）
# ---------------------------------------------------------------------------
def do_auth(user_id, password):
    """注册（新用户）或登录（已存在）。

    成功后**自动跳到「💬 对话」页**；同时清空本地聊天记录与事件游标，
    避免同一浏览器会话里看到上一位用户的对话内容（前端层的隔离）。
    """
    user_id = (user_id or "").strip()
    if not user_id:
        return ({"user_id": None, "token": None}, t("ui.enter_username"),
                _status_md(None), [], 0, gr.Tabs(selected="login"))

    reg = _post("/api/auth/register", {"user_id": user_id, "password": password or None})
    if reg.get("ok"):
        auth = {"user_id": user_id, "token": reg.get("api_key")}
        tip = t("ui.registered", user=user_id)
        return auth, tip, _status_md(auth), [], 0, gr.Tabs(selected="chat")

    # 已存在 -> 走登录
    login = _post("/api/auth/login", {"user_id": user_id, "password": password or None})
    if login.get("ok"):
        auth = {"user_id": user_id, "token": login.get("api_key")}
        tip = t("ui.logged_in", user=user_id)
        return auth, tip, _status_md(auth), [], 0, gr.Tabs(selected="chat")

    err = login.get("detail") or login.get("message") or reg.get("detail") \
        or reg.get("message") or t("ui.unknown_error")
    return ({"user_id": None, "token": None}, t("ui.login_failed", err=err),
            _status_md(None), [], 0, gr.Tabs(selected="login"))


def do_logout():
    """退出登录：清空会话状态与聊天记录，回到登录页。"""
    return ({"user_id": None, "token": None}, t("ui.logged_out"),
            _status_md(None), [], 0, gr.Tabs(selected="login"))


# ---------------------------------------------------------------------------
# 💬 对话页（流式）
# ---------------------------------------------------------------------------
def _format_schedule(items):
    if not items:
        return t("ui.schedule_empty")
    lines = [t("ui.schedule_header")]
    for it in items:
        ss = (it.get("scheduled_start") or it.get("start") or "")[-8:-3] or "?"
        se = (it.get("scheduled_end") or "")[-8:-3] or "?"
        flag = t("ui.risk_attention") if it.get("risk") else ""
        lines.append(t("ui.schedule_line", start=ss, end=se, title=it["title"],
                       priority=it["priority"], flag=flag))
    return "\n".join(lines)


def chat_msg(history, role, text):
    """追加一条消息（Gradio 6 的聊天记录格式）。"""
    return (history or []) + [{"role": role, "content": text}]


def respond(user_msg, history, last_id, auth_state):
    """处理一次用户发言（流式），产出 (history, chatbot, last_id, msg)。

    调用后端 /api/chat/stream，把 SSE 事件逐块转成聊天框增量更新以实现打字机效果；
    流式结束后若返回了排期，则把「今日排期」附在回复后面。
    """
    token = (auth_state or {}).get("token")
    if not token:
        hist = chat_msg(history, "user", user_msg) if (user_msg or "").strip() else history
        hist = chat_msg(hist, "assistant", _login_hint())
        yield hist, hist, last_id, ""
        return
    if not (user_msg or "").strip():
        yield history, history, last_id, ""
        return

    hist = chat_msg(history, "user", user_msg.strip())
    hist = chat_msg(hist, "assistant", "")
    resp = None
    try:
        # 独立连接读 SSE（不复用全局 Session 的 keep-alive，避免上次未消费完的流污染连接池）
        resp = requests.post(
            API_URL + "/api/chat/stream",
            json={"message": user_msg.strip()},
            headers=_headers(token),
            params={"token": token, "lang": _lang()},
            stream=True, timeout=90,
        )
        reply = ""
        schedule_md = ""
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                ev = json.loads(data)
            except Exception:  # noqa
                continue
            etype = ev.get("type")
            if etype == "token":
                reply += ev.get("text", "")
                hist[-1]["content"] = reply
                yield hist, hist, last_id, ""
            elif etype == "result":
                sched = (ev.get("data") or {}).get("schedule")
                if sched:
                    schedule_md = "\n\n" + _format_schedule(sched)
            elif etype == "error":
                hist[-1]["content"] = ev.get("message", t("ui.something_wrong"))
                yield hist, hist, last_id, ""
            elif etype == "done":
                if schedule_md and not reply.endswith(schedule_md):
                    hist[-1]["content"] = reply + schedule_md
                    yield hist, hist, last_id, ""
    except requests.exceptions.ConnectionError:
        hist[-1]["content"] = t("ui.backend_unreachable", url=API_URL)
        yield hist, hist, last_id, ""
    except Exception as e:  # noqa
        hist[-1]["content"] = t("ui.error_generic", err=e)
        yield hist, hist, last_id, ""
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa
                pass


def clear_chat():
    return [], []


def tick(history, last_id, auth_state):
    """定时器：拉取属于当前用户的提醒事件并注入对话页。"""
    token = (auth_state or {}).get("token")
    if not token:
        return history, history, last_id
    events = _get("/api/events", params={"since": last_id, "token": token},
                  token=token, default=[])
    if not events:
        return history, history, last_id
    hist = list(history)
    for e in events:
        hist = chat_msg(hist, "assistant",
                        t("ui.reminder_bubble", title=e["title"], message=e["message"]))
    return hist, hist, events[-1]["id"]


# ---------------------------------------------------------------------------
# 📅 日程页
# ---------------------------------------------------------------------------
def _rows_today(items):
    rows = []
    for it in items:
        risk = it.get("risk")
        rows.append([
            it["id"], it["title"],
            (it.get("scheduled_start") or it.get("start") or "")[:16],
            (it.get("scheduled_end") or "")[:16],
            it.get("duration_min"), it.get("priority"), it.get("status_cn"),
            t("ui.risk_passed") if risk == 2 else (t("ui.risk_overdue") if risk else ""),
        ])
    return rows


def _rows_all(tasks):
    rows = []
    for task in tasks:
        when = task.get("scheduled_start") or task.get("start") or task.get("due") or ""
        rows.append([
            task["id"], task["title"], when[:16], task.get("duration_min"),
            task.get("priority"), task.get("status_cn"),
            task.get("recurrence") or "—",
            "⚠️" if task.get("risk") else "",
        ])
    return rows


def refresh_schedule(auth_state):
    """拉取今日排期 + 全部任务，返回 (today_df, summary, all_df)。"""
    token = (auth_state or {}).get("token")
    if not token:
        return [], _login_hint(), []
    today = _get("/api/tasks/today", params={"token": token}, token=token, default={})
    all_t = _get("/api/tasks", params={"token": token}, token=token, default={"tasks": []})
    return (_rows_today(today.get("schedule") or []),
            today.get("message") or t("ui.schedule_empty"),
            _rows_all(all_t.get("tasks") or []))


def mark_done(task_id, auth_state):
    token = (auth_state or {}).get("token")
    if not token:
        return _login_hint(), [], _login_hint(), []
    if task_id is None:
        return t("ui.enter_task_id"), *refresh_schedule(auth_state)
    res = _post(f"/api/tasks/{int(task_id)}/done", token=token)
    msg = res.get("message") or res.get("detail") or t("ui.handled")
    return msg, *refresh_schedule(auth_state)


def remove_task(task_id, auth_state):
    token = (auth_state or {}).get("token")
    if not token:
        return _login_hint(), [], _login_hint(), []
    if task_id is None:
        return t("ui.enter_task_id"), *refresh_schedule(auth_state)
    res = _del(f"/api/tasks/{int(task_id)}", token=token)
    msg = res.get("message") or res.get("detail") or t("ui.handled")
    return msg, *refresh_schedule(auth_state)


def on_tab_select(auth_state, evt: gr.SelectData):
    """切到「📅 日程」页时自动刷新一次数据。"""
    if getattr(evt, "selected", None) is True or getattr(evt, "value", None) == "schedule":
        return refresh_schedule(auth_state)
    return gr.skip()


def _cols_today():
    return [t("ui.col_id"), t("ui.col_task"), t("ui.col_start"), t("ui.col_end"),
            t("ui.col_duration"), t("ui.col_priority"), t("ui.col_status"),
            t("ui.col_risk")]


def _cols_all():
    return [t("ui.col_id"), t("ui.col_task"), t("ui.col_due_or_start"),
            t("ui.col_duration"), t("ui.col_priority"), t("ui.col_status"),
            t("ui.col_recurrence"), t("ui.col_risk")]


def _examples():
    """输入示例也跟着语言走（英文示例才能被英文解析器理解）。"""
    if _UI_LOCALE.lower().startswith("en"):
        return [["meeting tomorrow 3pm for 1 hour high"],
                ["daily standup 9am 20 min high"],
                ["brunch at 10am today"],
                ["move study to 11am"],
                ["today's schedule"], ["done 1"], ["list tasks"]]
    return [["明天下午3点开会 1小时 高"],
            ["每天 上午9点 晨会 20分钟 高"],
            ["今天早上10点吃早茶"],
            ["自习时间改成11点"],
            ["今日日程"], ["完成 1"], ["列出任务"]]


# ---------------------------------------------------------------------------
# 语言切换
# ---------------------------------------------------------------------------
def switch_language(locale, auth_state):
    """切换界面语言。

    可更新的组件（按钮、输入框、表格列头、提示文字）立即变；
    Tab 标题属于 Gradio 的静态结构，无法运行时改写，所以会提示刷新页面。
    """
    global _UI_LOCALE
    loc = normalize_locale(locale) or DEFAULT_LOCALE
    if loc not in available_locales():
        loc = DEFAULT_LOCALE
    _UI_LOCALE = loc
    set_locale(loc)
    try:
        get_settings().language = loc
    except Exception:      # noqa: BLE001
        pass

    name = locale_name(loc)
    return (
        t("ui.lang_switched", language=name),          # lang_msg
        t("ui.login_intro", url=API_URL),              # login_intro
        t("ui.login_footer"),                          # login_footer
        gr.update(label=t("ui.username"),
                  placeholder=t("ui.username_placeholder")),
        gr.update(label=t("ui.password"),
                  placeholder=t("ui.password_placeholder")),
        gr.update(value=t("ui.btn_login")),
        gr.update(value=t("ui.btn_logout")),
        t("ui.not_logged_in") if not (auth_state or {}).get("token")
        else _status_md(auth_state),                   # login_status
        _status_md(auth_state),                        # top_status
        gr.update(placeholder=t("ui.chat_placeholder")),
        gr.update(value=t("ui.btn_send")),
        gr.update(value=t("ui.btn_clear")),
        gr.update(value=t("ui.btn_refresh")),
        t("ui.schedule_click_refresh"),                # today_summary
        gr.update(headers=_cols_today(), label=t("ui.label_today")),
        t("ui.label_all_section"),
        gr.update(headers=_cols_all(), label=t("ui.label_all")),
        gr.update(label=t("ui.label_task_id")),
        gr.update(value=t("ui.btn_done")),
        gr.update(value=t("ui.btn_delete")),
    )


# ---------------------------------------------------------------------------
# 界面组装
# ---------------------------------------------------------------------------
def build_app():
    with gr.Blocks(title=t("ui.title")) as demo:
        with gr.Row():
            lang_dd = gr.Dropdown(
                choices=[(locale_name(loc), loc) for loc in available_locales()],
                value=_UI_LOCALE, label=t("ui.language"), scale=1, min_width=120,
            )
            top_status = gr.Markdown(_status_md(None))
        lang_msg = gr.Markdown()

        # 会话级状态：登录信息 / 聊天记录 / 事件游标
        auth_state = gr.State({"user_id": None, "token": None})
        history = gr.State([])
        last_id = gr.State(0)

        tabs = gr.Tabs()
        with tabs:
            # ---------------- 🔐 登录 ----------------
            with gr.Tab(t("ui.tab.login"), id="login"):
                login_intro = gr.Markdown(t("ui.login_intro", url=API_URL))
                uid_box = gr.Textbox(label=t("ui.username"),
                                     placeholder=t("ui.username_placeholder"))
                pw_box = gr.Textbox(label=t("ui.password"), type="password",
                                    placeholder=t("ui.password_placeholder"))
                with gr.Row():
                    login_btn = gr.Button(t("ui.btn_login"), variant="primary")
                    logout_btn = gr.Button(t("ui.btn_logout"))
                login_status = gr.Markdown(t("ui.not_logged_in"))
                login_footer = gr.Markdown(t("ui.login_footer"))

            # ---------------- 💬 对话 ----------------
            with gr.Tab(t("ui.tab.chat"), id="chat"):
                chatbot = gr.Chatbot(label=t("ui.tab.chat"), height=460, show_label=False)
                with gr.Row():
                    msg = gr.Textbox(placeholder=t("ui.chat_placeholder"),
                                     show_label=False, scale=5, submit_btn=False)
                    send = gr.Button(t("ui.btn_send"), variant="primary", scale=1)
                with gr.Row():
                    clear_btn = gr.Button(t("ui.btn_clear"))
                gr.Examples(examples=_examples(), inputs=[msg])

            # ---------------- 📅 日程 ----------------
            with gr.Tab(t("ui.tab.schedule"), id="schedule"):
                refresh_btn = gr.Button(t("ui.btn_refresh"), variant="primary")
                today_summary = gr.Markdown(t("ui.schedule_click_refresh"))
                today_df = gr.Dataframe(
                    headers=_cols_today(),
                    datatype=["number", "str", "str", "str",
                              "number", "str", "str", "str"],
                    interactive=False, wrap=True, label=t("ui.label_today"),
                )
                all_section = gr.Markdown(t("ui.label_all_section"))
                all_df = gr.Dataframe(
                    headers=_cols_all(),
                    datatype=["number", "str", "str", "number",
                              "str", "str", "str", "str"],
                    interactive=False, wrap=True, label=t("ui.label_all"),
                )
                with gr.Row():
                    task_id = gr.Number(label=t("ui.label_task_id"), precision=0, scale=1)
                    done_btn = gr.Button(t("ui.btn_done"), scale=1)
                    del_btn = gr.Button(t("ui.btn_delete"), scale=1)
                action_msg = gr.Markdown()

        # ---------------- 事件绑定 ----------------
        auth_outputs = [auth_state, login_status, top_status, history, last_id, tabs]
        login_btn.click(do_auth, [uid_box, pw_box], auth_outputs)
        logout_btn.click(do_logout, None,
                         [auth_state, login_status, top_status, history, last_id, tabs])

        send.click(respond, [msg, history, last_id, auth_state], [history, chatbot, last_id, msg])
        msg.submit(respond, [msg, history, last_id, auth_state], [history, chatbot, last_id, msg])
        clear_btn.click(clear_chat, None, [history, chatbot])

        sched_outputs = [today_df, today_summary, all_df]
        refresh_btn.click(refresh_schedule, [auth_state], sched_outputs)
        done_btn.click(mark_done, [task_id, auth_state], [action_msg, *sched_outputs])
        del_btn.click(remove_task, [task_id, auth_state], [action_msg, *sched_outputs])
        tabs.select(on_tab_select, [auth_state], sched_outputs)

        # 语种切换：更新所有可更新的文案（Tab 标题是静态的，需刷新页面）
        lang_dd.change(
            switch_language, [lang_dd, auth_state],
            [lang_msg, login_intro, login_footer, uid_box, pw_box, login_btn,
             logout_btn, login_status, top_status, msg, send, clear_btn,
             refresh_btn, today_summary, today_df, all_section, all_df,
             task_id, done_btn, del_btn],
        )

        # 提醒轮询：定时把「属于我的」提醒事件注入对话页
        if hasattr(gr, "Timer"):
            timer = gr.Timer(POLL_INTERVAL)
            timer.tick(tick, [history, last_id, auth_state], [history, chatbot, last_id])

    return demo


def launch(port=None, share=False, host="0.0.0.0", open_browser=False, language=None):
    """启动前端。端口默认取配置里的 ui_port，语言默认取配置 / 环境变量。"""
    global _UI_LOCALE
    if language:
        _UI_LOCALE = normalize_locale(language) or DEFAULT_LOCALE
    _init_locale()
    port = int(port or get_settings().ui_port)
    print(f"[ui] backend={API_URL}  port={port}  language={_UI_LOCALE}")
    demo = build_app()
    # Gradio 6 起 theme 等外观参数改由 launch() 传入
    demo.launch(server_name=host, server_port=port, share=share,
                inbrowser=open_browser, theme=gr.themes.Default())


def main():
    """console script 入口：schedule-agent-ui"""
    import argparse
    ap = argparse.ArgumentParser(description=t("cli.ui_desc"))
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--share", action="store_true", help=t("cli.help_share"))
    ap.add_argument("--open", action="store_true", help=t("cli.help_open"))
    ap.add_argument("--language", default=None, help=t("cli.help_lang"))
    args = ap.parse_args()
    launch(port=args.port, share=args.share, host=args.host,
           open_browser=args.open, language=args.language)


if __name__ == "__main__":
    main()
