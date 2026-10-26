# -*- coding: utf-8 -*-
"""国际化（i18n）核心：文案查找、语言解析与回退链。

为什么不用 gettext / babel：
  1. gettext 要先编译 .mo，改一句文案就得重新编译，部署多一道工序；
     本项目是自托管的小服务，JSON 词典更直观，改完重启即生效。
  2. 零第三方依赖，与项目其余部分（纯标准库）保持一致。
  3. 词典可运行时热加载（`reload()`），调试和测试都方便。

语言优先级（从高到低）：
  显式参数（`t(..., locale=)` / `use_locale()`）
  > 请求的 Accept-Language
  > `?lang=` 查询参数（由调用方合并进 explicit）
  > 环境变量 / 配置文件 `language`
  > `DEFAULT_LOCALE`

并发安全：
  当前语言放在 `contextvars` 里，asyncio 每个任务各有一份，因此多个用户
  同时用不同语言互不干扰；`asyncio.to_thread` 会复制上下文，所以线程池里
  执行的动作（DB 写入、排期）仍然拿到正确的语言。

回退链：
  目标语言缺 key -> 回退到 `DEFAULT_LOCALE` -> 仍缺则返回 key 本身并告警一次。
  **绝不因为缺一条文案就抛异常**：i18n 不该成为业务可用性的单点。
"""
from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Mapping

DEFAULT_LOCALE = "zh-CN"
LOCALES_DIR = Path(__file__).with_name("locales")

# 语言别名 -> 规范 locale。只做「同一语言的不同写法」归一，不在运行时翻译
# 未提供词典的语言，避免出现「显示成半截英文」的尴尬。
_ALIASES = {
    "zh": "zh-CN", "zh-cn": "zh-CN", "zh-hans": "zh-CN", "zh-sg": "zh-CN",
    "zh-chs": "zh-CN", "cn": "zh-CN", "chinese": "zh-CN",
    "en": "en-US", "en-us": "en-US", "en-gb": "en-US", "en-au": "en-US",
    "english": "en-US",
}

# 语言切换后需要让 UI / 前端知道用哪种日期格式，这里给一个简单的约定。
DATE_FORMATS = {"zh-CN": "%Y-%m-%d %H:%M", "en-US": "%Y-%m-%d %H:%M"}

_current: ContextVar[str] = ContextVar("schedule_agent_locale", default="")
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
_missing_warned: set[str] = set()

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


# ---------------------------------------------------------------------------
# 词典加载
# ---------------------------------------------------------------------------
def locales_dir() -> Path:
    """词典目录（测试可用 `SCHEDULE_LOCALES_DIR` 指向临时目录）。"""
    env = os.environ.get("SCHEDULE_LOCALES_DIR")
    return Path(env).expanduser() if env else LOCALES_DIR


def _search_dirs() -> list[Path]:
    """词典查找路径：自定义目录优先，**内置目录永远作为兜底**。

    为什么要两级：`SCHEDULE_LOCALES_DIR` 的常见用法是「只放我要覆盖的几条」，
    如果它一旦设置就把内置词典整个顶掉，默认语言会凭空消失，所有文案退化成
    key——这种故障很难查。两级查找让自定义目录可以只写增量。
    """
    dirs = [locales_dir()]
    if LOCALES_DIR not in dirs:
        dirs.append(LOCALES_DIR)
    return dirs


def _read_locale_file(locale: str) -> dict:
    for base in _search_dirs():
        path = base / f"{locale}.json"
        if not path.is_file():
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:  # noqa: BLE001 - 词典损坏不应让服务起不来
            print(f"[i18n] 读取词典失败（{path}）：{e}")
            return {}
    return {}


def catalog(locale: str | None = None) -> dict:
    """取某个语言的词典（带缓存）。"""
    loc = normalize_locale(locale) or DEFAULT_LOCALE
    with _cache_lock:
        if loc not in _cache:
            _cache[loc] = _read_locale_file(loc)
        return _cache[loc]


def reload() -> None:
    """清空缓存（改完 JSON 后热加载；测试也用它隔离）。"""
    with _cache_lock:
        _cache.clear()
        _missing_warned.clear()


def available_locales() -> list[str]:
    """所有可用的语言（自定义目录与内置目录取并集，默认语言永远排第一）。"""
    names: set[str] = set()
    for base in _search_dirs():
        if base.is_dir():
            names |= {p.stem for p in base.glob("*.json")}
    ordered = sorted(names)
    if DEFAULT_LOCALE in ordered:
        ordered.remove(DEFAULT_LOCALE)
        ordered.insert(0, DEFAULT_LOCALE)
    return ordered


def locale_name(locale: str) -> str:
    """语言的显示名（用于 UI 下拉框）。"""
    key = f"language.{normalize_locale(locale) or locale}"
    name = t(key)
    return locale if name == key else name


# ---------------------------------------------------------------------------
# 语言解析
# ---------------------------------------------------------------------------
def normalize_locale(tag: str | None) -> str:
    """把 `en`、`en_GB`、`zh-Hans-CN` 之类归一成 `en-US` / `zh-CN`。

    未提供词典的语言返回空串（由调用方决定回退），不做「假装支持」。
    """
    if not tag:
        return ""
    t = str(tag).strip().replace("_", "-").lower()
    if not t:
        return ""
    if t in _ALIASES:
        return _ALIASES[t]
    base = t.split("-")[0]
    return _ALIASES.get(base, "")


def _settings_locale() -> str:
    """配置文件 / 环境变量里的默认语言（惰性导入，避免循环依赖）。"""
    try:
        from schedule_agent.config import get_settings
        return getattr(get_settings(), "language", "") or ""
    except Exception:  # noqa: BLE001 - 配置不可用时就当没设
        return ""


def parse_accept_language(header: str | None) -> list[str]:
    """按 q 值从高到低解析 `Accept-Language`，返回候选 tag 列表。"""
    if not header:
        return []
    items = []
    for idx, part in enumerate(header.split(",")):
        piece = part.strip()
        if not piece:
            continue
        tag, _, params = piece.partition(";")
        q = 1.0
        for kv in params.split(";"):
            kv = kv.strip()
            if kv.startswith("q="):
                try:
                    q = float(kv[2:])
                except ValueError:
                    q = 0.0
        items.append((-q, idx, tag.strip()))
    items.sort()
    return [tag for _, _, tag in items]


def resolve_locale(explicit: str | None = None,
                   accept_language: str | None = None) -> str:
    """按优先级挑一个「确定有词典」的语言；全都不匹配就用默认语言。"""
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    candidates.extend(parse_accept_language(accept_language))
    candidates.append(_settings_locale())

    known = set(available_locales()) or {DEFAULT_LOCALE}
    for cand in candidates:
        loc = normalize_locale(cand)
        if loc and loc in known:
            return loc
    return DEFAULT_LOCALE


def get_locale() -> str:
    """当前生效的语言（上下文优先，其次配置，最后默认）。"""
    loc = _current.get()
    if loc:
        return loc
    return normalize_locale(_settings_locale()) or DEFAULT_LOCALE


def set_locale(locale: str | None) -> Token:
    """设置当前上下文的语言，返回 token 供 `reset_locale` 还原。"""
    return _current.set(normalize_locale(locale) or DEFAULT_LOCALE)


def reset_locale(token: Token | None) -> None:
    if token is not None:
        try:
            _current.reset(token)
        except ValueError:      # 跨上下文 reset 会报错，忽略即可
            pass


@contextmanager
def use_locale(locale: str | None):
    """`with use_locale("en-US"): ...` —— 块内使用指定语言。"""
    tok = set_locale(locale)
    try:
        yield get_locale()
    finally:
        reset_locale(tok)


# ---------------------------------------------------------------------------
# 翻译
# ---------------------------------------------------------------------------
def _pick_plural(forms: Mapping[str, str], n: Any) -> str:
    """英文单复数：n == 1 取 one，其余取 other；没有 other 就取第一个。"""
    try:
        num = float(n)
    except (TypeError, ValueError):
        num = 1
    if num == 1 and "one" in forms:
        return forms["one"]
    return forms.get("other") or forms.get("one") or next(iter(forms.values()), "")


def _format(template: str, params: Mapping[str, Any]) -> str:
    """安全插值：只替换已知占位符，缺参数时保留 `{name}` 原样。

    不用 `str.format` 是因为用户标题里可能带 `{}`，那会让格式化直接抛
    KeyError / ValueError，把一条普通文案变成 500。
    """
    if not params:
        return template

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key in params and params[key] is not None:
            return str(params[key])
        return m.group(0)

    return _PLACEHOLDER.sub(repl, template)


def _lookup(locale: str, key: str) -> Any:
    return catalog(locale).get(key)


def _warn_missing(key: str) -> None:
    if key in _missing_warned:
        return
    _missing_warned.add(key)
    print(f"[i18n] 缺少文案 key：{key}（请检查 locales/*.json）")


def t(key: str, locale: str | None = None, **params: Any) -> str:
    """取一条文案并插值。

    >>> t("agent.added_today", title="开会")
    '已添加《开会》并重新规划了今日日程。'

    支持复数：词典里把值写成 `{"one": "...", "other": "..."}`，
    传 `n=` 即可自动选择；中文词典通常只写字符串。
    """
    loc = normalize_locale(locale) or get_locale()
    value = _lookup(loc, key)
    if value is None and loc != DEFAULT_LOCALE:
        value = _lookup(DEFAULT_LOCALE, key)
    if value is None:
        _warn_missing(key)
        return key
    if isinstance(value, dict):
        value = _pick_plural(value, params.get("n", params.get("count", 1)))
    if not isinstance(value, str):
        return str(value)
    return _format(value, params)


def has(key: str, locale: str | None = None) -> bool:
    """某个语言是否有这条文案（词典完整性测试用）。"""
    return _lookup(normalize_locale(locale) or DEFAULT_LOCALE, key) is not None


def keys(locale: str | None = None) -> set[str]:
    return set(catalog(locale).keys())


def missing_keys(locale: str | None = None) -> set[str]:
    """相对默认语言缺失的 key（用于测试与自检）。"""
    return keys(DEFAULT_LOCALE) - keys(locale or DEFAULT_LOCALE)
