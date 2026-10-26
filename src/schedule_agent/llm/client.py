# -*- coding: utf-8 -*-
"""大模型自然语言理解与回复生成（可选增强）。

- understand():      一句话的「意图识别 + 字段抽取」（NLU），返回结构化 dict。
- respond_stream():   把系统执行结果转成自然语言回复，逐字流式产出（生成器）。
- respond():         同上，但一次性返回完整文本（用于测试 / 非流式兜底）。
- parse_with_llm():  旧版字段级解析，仅作兜底保留。

提示词全部集中在 prompts.py，便于调整后复用，本文件只负责调用与解析。

配置方式（二选一）：
  1) 环境变量：SCHEDULE_LLM_API_KEY / SCHEDULE_LLM_BASE_URL / SCHEDULE_LLM_MODEL
  2) 同目录 config.json 的 llm_api_key / llm_base_url / llm_model
（base_url 兼容 OpenAI Chat Completions，可换成 DeepSeek / 通义 / 本地 vLLM）
"""
import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime

from schedule_agent.llm import prompts
from schedule_agent.domain.models import Priority
from schedule_agent.i18n import t

try:
    import httpx
except ImportError:  # 仅异步路径需要；缺失时同步接口仍可工作
    httpx = None

_CONFIG_CACHE = None
LAST_ERROR = None   # 最近一次调用失败的原因（供 /api/health 展示，排查用）

# 重试配置
MAX_RETRIES = 3
RETRY_DELAY = 1  # 初始延迟（秒）
RETRY_MAX_DELAY = 10  # 最大延迟（秒）


def _note_error(e):
    """记录失败原因，便于在健康检查里一眼看出「Key 配了但用不了」。

    文案按当前语言生成（日志里也一致），原因要可排查而不是只给一个状态码。
    """
    global LAST_ERROR
    msg = str(e)
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", "ignore")[:200]
        except Exception:  # noqa
            body = ""
        msg = f"HTTP {e.code} {body}".strip()
        if e.code == 402:
            msg = t("agent.err_402")
        elif e.code == 401:
            msg = t("agent.err_401")
        elif e.code == 429:
            msg = t("agent.err_429")
    LAST_ERROR = msg
    return msg


def load_config():
    """返回当前生效的配置字典（来自集中配置模块，已合并 文件 < 环境变量）。

    历史注意：早期本模块直接读同目录的 config.json；包结构重构后这个相对路径
    指向 `schedule_agent/llm/config.json`（并不存在），会让配置文件里的 Key
    永远失效（表现为“明明配了 Key 却仍走规则兜底”）。现在统一走
    `schedule_agent.config.get_settings()`。
    """
    global _CONFIG_CACHE
    from schedule_agent.config import get_settings
    _CONFIG_CACHE = get_settings().as_dict()
    return _CONFIG_CACHE


def llm_enabled():
    return bool(os.environ.get("SCHEDULE_LLM_API_KEY") or load_config().get("llm_api_key"))


def _creds():
    cfg = load_config()
    api_key = os.environ.get("SCHEDULE_LLM_API_KEY") or cfg.get("llm_api_key")
    base = (os.environ.get("SCHEDULE_LLM_BASE_URL")
            or cfg.get("llm_base_url") or "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("SCHEDULE_LLM_MODEL") or cfg.get("llm_model") or "gpt-4o-mini"
    return api_key, base, model


def _parse_dt(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _coerce(obj):
    """把大模型返回的 NLU 原始 dict 规范化为代码可用的类型。"""
    prio_map = {"high": Priority.HIGH, "medium": Priority.MEDIUM, "low": Priority.LOW}
    obj["priority"] = prio_map.get(str(obj.get("priority") or "medium").lower(),
                                  Priority.MEDIUM)
    obj["due"] = _parse_dt(obj.get("due"))
    obj["start"] = _parse_dt(obj.get("start"))
    obj["duration_min"] = int(obj.get("duration_min") or 30)
    obj["recurrence"] = (obj.get("recurrence") or "").strip()
    obj["task_ids"] = [int(x) for x in (obj.get("task_ids") or []) if str(x).isdigit()]
    # 不要给 intent 兜默认值：模型漏填时默认成 add，等于「代码替模型决定意图」，
    # 历史上多次把查询/修改误建成新任务。留空串，交给调用方显式处理。
    obj["intent"] = str(obj.get("intent") or "").strip().lower()
    return obj


def last_error():
    """最近一次大模型调用失败的原因（成功时为 None）。"""
    return LAST_ERROR


def _extract_json(content):
    content = (content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    return json.loads(content)


# ---------------------------------------------------------------------------
# 字段级解析（兜底）
# ---------------------------------------------------------------------------
def parse_with_llm(text):
    """调用大模型解析；失败返回 None（由调用方回退规则解析）。"""
    api_key, base, model = _creds()
    if not api_key:
        return None
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompts.parse_prompt(now)},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"].strip()
        obj = _extract_json(content)
        is_fixed = bool(obj.get("is_fixed"))
        due = _parse_dt(obj.get("due"))
        start = _parse_dt(obj.get("start"))
        if is_fixed and start is not None:
            due = None
        prio_map = {"high": Priority.HIGH, "medium": Priority.MEDIUM, "low": Priority.LOW}
        return {
            "title": (obj.get("title") or text).strip(),
            "due": due,
            "start": start,
            "duration_min": int(obj.get("duration_min") or 30),
            "priority": prio_map.get((obj.get("priority") or "medium").lower(),
                                    Priority.MEDIUM),
            "recurrence": (obj.get("recurrence") or "").strip(),
        }
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            KeyError, IndexError, Exception) as e:
        print(f"[LLM解析] 失败: {_note_error(e)}")
        return None


# ---------------------------------------------------------------------------
# 统一自然语言理解（NLU）：一句话判断「意图 + 抽取结构化字段」
# ---------------------------------------------------------------------------
def understand(message):
    """用大模型做一次完整 NLU：返回 {intent, title, due, start, duration_min,
    priority(Priority), recurrence, task_ids, title_hint}；无 Key 或失败返回 None。
    
    带重试机制（指数退避），提升网络波动时的稳定性。
    """
    api_key, base, model = _creds()
    if not api_key:
        return None
    system = prompts.understand_prompt()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ],
        "temperature": 0,
    }
    
    last_exception = None
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                base + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {api_key}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"].strip()
            obj = _extract_json(content)
            global LAST_ERROR
            LAST_ERROR = None
            return _coerce(obj)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
                KeyError, IndexError, Exception) as e:
            last_exception = e
            _note_error(e)
            # 如果不是最后一次重试，则等待后重试
            if attempt < MAX_RETRIES - 1:
                delay = min(RETRY_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                print(f"[LLM理解] 第 {attempt + 1} 次失败，{delay}秒后重试...")
                time.sleep(delay)
            else:
                print(f"[LLM理解] 已达最大重试次数 ({MAX_RETRIES})，放弃。")
    return None


# ---------------------------------------------------------------------------
# 流式补全（OpenAI 兼容 SSE）
# ---------------------------------------------------------------------------
def _stream_completion(messages, temperature=0.4):
    """向大模型发起 stream=True 的对话补全，逐块 yield 文本增量（delta）。

    兼容 OpenAI Chat Completions 的 SSE 协议：每行形如 `data: {...}`，结束为
    `data: [DONE]`。任意异常都会静默结束（调用方据此回退到非流式文本）。
    """
    api_key, base, model = _creds()
    if not api_key:
        return
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue
                try:
                    delta = obj["choices"][0]["delta"].get("content")
                except (KeyError, IndexError):
                    continue
                if delta:
                    yield delta
    except Exception as e:  # noqa
        print(f"[LLM流式] 出错: {_note_error(e)}")


def _result_to_context(user_message, action_result):
    """把一次执行结果压成一小段上下文，喂给回复生成模型。

    这段上下文**也要跟随语言**：英文 prompt + 中文上下文会让模型在两种语言
    之间来回翻译，回复质量明显下降。
    """
    if not action_result:
        return t("llm.ctx_none", text=user_message)
    action = action_result.get("action")
    msg = action_result.get("message", "")
    sched = action_result.get("schedule") or []
    tasks = action_result.get("tasks") or []
    parts = [
        t("llm.ctx_user", text=user_message),
        t("llm.ctx_action", action=action),
        t("llm.ctx_result", msg=msg),
    ]
    if sched:
        lines = []
        for it in sched:
            ss = (it.get("scheduled_start") or it.get("start") or "")[:16] or "?"
            se = (it.get("scheduled_end") or "")[:16] or "?"
            lines.append(t("llm.ctx_schedule_line", start=ss, end=se,
                           title=it["title"], priority=it["priority"]))
        parts.append(t("llm.ctx_schedule_header")
                     + t("llm.ctx_join").join(lines))
    if tasks:
        tlines = [t("llm.ctx_task_line", id=x["id"], title=x["title"],
                    status=x["status_cn"]) for x in tasks[:20]]
        parts.append(t("llm.ctx_tasks_header") + t("llm.ctx_join").join(tlines))
    parts.append(t("llm.ctx_ask"))
    return "\n".join(parts)


def _build_respond_messages(user_message, action_result, history=None):
    messages = [{"role": "system", "content": prompts.respond_prompt()}]
    # 多轮上下文（可选，最近若干轮）
    if history:
        for h in history[-10:]:
            if isinstance(h, dict) and h.get("role") in ("user", "assistant"):
                messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": _result_to_context(user_message, action_result)})
    return messages


def respond_stream(user_message, action_result, history=None):
    """生成自然语言回复，逐字流式产出（生成器）。失败则产出空（调用方兜底）。"""
    messages = _build_respond_messages(user_message, action_result, history)
    for delta in _stream_completion(messages, temperature=0.4):
        yield delta


def respond(user_message, action_result, history=None):
    """一次性返回完整自然语言回复（非流式，用于测试 / 兜底）。"""
    return "".join(respond_stream(user_message, action_result, history))


# ---------------------------------------------------------------------------
# 异步版本（基于 httpx，真正的非阻塞 I/O，用于 FastAPI 并发场景）
# ---------------------------------------------------------------------------
def _headers(api_key):
    return {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}


async def understand_async(message):
    """understand 的异步版：用 httpx 异步请求大模型做 NLU，无 Key/失败返回 None。
    
    带重试机制（指数退避），提升网络波动时的稳定性。
    """
    if httpx is None:
        return None
    api_key, base, model = _creds()
    if not api_key:
        return None
    system = prompts.understand_prompt()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message},
        ],
        "temperature": 0,
    }
    
    last_exception = None
    for attempt in range(MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    base + "/chat/completions", json=payload, headers=_headers(api_key))
                r.raise_for_status()
                data = r.json()
            content = data["choices"][0]["message"]["content"].strip()
            obj = _extract_json(content)
            global LAST_ERROR
            LAST_ERROR = None
            return _coerce(obj)
        except Exception as e:  # noqa
            last_exception = e
            _note_error(e)
            # 如果不是最后一次重试，则等待后重试
            if attempt < MAX_RETRIES - 1:
                delay = min(RETRY_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
                print(f"[LLM理解-async] 第 {attempt + 1} 次失败，{delay}秒后重试...")
                await asyncio.sleep(delay)
            else:
                print(f"[LLM理解-async] 已达最大重试次数 ({MAX_RETRIES})，放弃。")
    return None


async def _stream_completion_async(messages, temperature=0.4):
    """_stream_completion 的异步版：用 httpx 异步流式读取 SSE delta。"""
    api_key, base, model = _creds()
    if not api_key or httpx is None:
        return
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            async with client.stream(
                "POST", base + "/chat/completions",
                json=payload, headers=_headers(api_key)) as resp:
                async for raw in resp.aiter_lines():
                    line = (raw or "").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        obj = json.loads(chunk)
                    except ValueError:
                        continue
                    try:
                        delta = obj["choices"][0]["delta"].get("content")
                    except (KeyError, IndexError):
                        continue
                    if delta:
                        yield delta
    except Exception as e:  # noqa
        print(f"[LLM流式-async] 出错: {_note_error(e)}")


async def respond_stream_async(user_message, action_result, history=None):
    """respond_stream 的异步版：逐字异步产出自然语言回复。"""
    messages = _build_respond_messages(user_message, action_result, history)
    async for delta in _stream_completion_async(messages, temperature=0.4):
        yield delta


async def respond_async(user_message, action_result, history=None):
    """一次性异步返回完整自然语言回复（非流式兜底）。"""
    parts = []
    async for d in respond_stream_async(user_message, action_result, history):
        parts.append(d)
    return "".join(parts)
