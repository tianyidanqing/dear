# -*- coding: utf-8 -*-
"""国际化（i18n）测试。

覆盖四类：
  1. 词典本身：中英 key 必须**完全对齐**（少一条就会在运行时回退成中文，
     英文界面里冒出一句中文是最典型的 i18n 事故）。
  2. 核心机制：插值、复数、缺 key 回退、语言归一、Accept-Language 协商。
  3. 并发隔离：不同上下文同时使用不同语言互不影响。
  4. 端到端：同一份数据在中英环境下产出不同文案；HTTP 层按请求头切换语言。
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from schedule_agent import i18n                                        # noqa: E402
from schedule_agent.domain import nlp                                  # noqa: E402
from schedule_agent.domain.models import Priority                      # noqa: E402
from schedule_agent.domain.planner import summarize                    # noqa: E402
from schedule_agent.domain.models import Task                          # noqa: E402
from schedule_agent.i18n import (                                      # noqa: E402
    DEFAULT_LOCALE, available_locales, get_locale, keys, missing_keys,
    normalize_locale, parse_accept_language, resolve_locale, t, use_locale,
)
from schedule_agent.service.agent import ScheduleAgent                 # noqa: E402
from schedule_agent.storage.db import Store                            # noqa: E402

from conftest import bearer                                            # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_catalog():
    """每个用例都从磁盘重新读词典，避免上一个用例的缓存影响。"""
    i18n.reload()
    yield
    i18n.reload()


# ---------------------------------------------------------------------------
# 1. 词典完整性
# ---------------------------------------------------------------------------
def test_default_locale_exists():
    assert DEFAULT_LOCALE in available_locales()


def test_english_has_every_key():
    """英文词典不能缺任何 key（缺了会在英文界面露出中文）。"""
    assert missing_keys("en-US") == set()


def test_no_orphan_keys_in_english():
    """英文也不能有多余 key（拼错的 key 永远不会被用到，属于静默垃圾）。"""
    assert keys("en-US") - keys(DEFAULT_LOCALE) == set()


def test_all_catalogs_are_flat_strings_or_plural_dicts():
    """词典值只能是字符串，或 {one, other} 复数表——防止把嵌套结构写进来。"""
    for locale in available_locales():
        for key, value in i18n.catalog(locale).items():
            if isinstance(value, dict):
                assert set(value) <= {"one", "other"}, f"{locale}:{key} 复数表字段异常"
                assert all(isinstance(v, str) for v in value.values())
            else:
                assert isinstance(value, str), f"{locale}:{key} 不是字符串"


# ---------------------------------------------------------------------------
# 2. 核心机制
# ---------------------------------------------------------------------------
def test_translate_interpolation():
    with use_locale("zh-CN"):
        assert t("agent.added_today", title="开会") == "已添加《开会》并重新规划了今日日程。"
    with use_locale("en-US"):
        assert t("agent.added_today", title="meeting") == \
            'Added "meeting" and re-planned today\'s schedule.'


def test_missing_placeholder_is_kept_not_crashed():
    """少传参数时保留 `{name}`，绝不抛异常——文案问题不该变成 500。"""
    with use_locale("en-US"):
        out = t("agent.added_today")
    assert "{title}" in out


def test_braces_in_user_content_do_not_break_format():
    """用户标题里带花括号时不能被当成占位符（str.format 会炸）。"""
    with use_locale("zh-CN"):
        out = t("agent.added_today", title="学 {python} 字典")
    assert "{python}" in out


def test_english_plural():
    with use_locale("en-US"):
        assert t("agent.count_tasks", n=1) == "1 task in total."
        assert t("agent.count_tasks", n=3) == "3 tasks in total."


def test_chinese_has_no_plural_forms():
    with use_locale("zh-CN"):
        assert t("agent.count_tasks", n=1) == "共 1 条记录。"
        assert t("agent.count_tasks", n=3) == "共 3 条记录。"


def test_unknown_key_returns_key_and_does_not_raise():
    assert t("no.such.key") == "no.such.key"


def test_missing_key_falls_back_to_default_locale(monkeypatch):
    """英文词典缺某条时回退中文，而不是显示 key。"""
    en = dict(i18n.catalog("en-US"))
    en.pop("agent.demo_done", None)
    zh_val = i18n.catalog("zh-CN")["agent.demo_done"]
    real = i18n.core.catalog
    monkeypatch.setattr(i18n.core, "catalog",
                        lambda loc=None: en if loc == "en-US" else real(loc))
    with use_locale("en-US"):
        assert t("agent.demo_done") == zh_val


# ---------------------------------------------------------------------------
# 3. 语言解析
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("tag,expected", [
    ("en", "en-US"), ("en-GB", "en-US"), ("EN_us", "en-US"),
    # 繁体归到简体（目前只提供 zh-CN 词典；要繁体请加 zh-TW.json）
    ("zh", "zh-CN"), ("zh-Hans-CN", "zh-CN"), ("zh_TW", "zh-CN"),
    ("fr", ""), ("", ""), (None, ""),
])
def test_normalize_locale(tag, expected):
    assert normalize_locale(tag) == expected


def test_parse_accept_language_orders_by_q():
    got = parse_accept_language("fr;q=1.0, en;q=0.8, zh;q=0.5")
    assert got == ["fr", "en", "zh"]


def test_resolve_prefers_explicit_then_header():
    assert resolve_locale(explicit="en-US", accept_language="zh-CN") == "en-US"
    assert resolve_locale(accept_language="en-US,zh;q=0.8") == "en-US"


def test_resolve_skips_unsupported_languages():
    """不支持的语言（fr）要跳过，不能把界面变成一半法文。"""
    assert resolve_locale(accept_language="fr-FR,en;q=0.5") == "en-US"


def test_resolve_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(i18n.core, "_settings_locale", lambda: "")
    assert resolve_locale(explicit="de-DE", accept_language="de") == DEFAULT_LOCALE


def test_settings_language_is_used(monkeypatch):
    monkeypatch.setattr(i18n.core, "_settings_locale", lambda: "en-US")
    assert resolve_locale() == "en-US"


# ---------------------------------------------------------------------------
# 4. 上下文隔离
# ---------------------------------------------------------------------------
def test_use_locale_restores_previous():
    with use_locale("zh-CN"):
        assert get_locale() == "zh-CN"
        with use_locale("en-US"):
            assert get_locale() == "en-US"
        assert get_locale() == "zh-CN"


def test_locale_is_isolated_between_threads():
    """两个线程各用各的语言，互不串味（asyncio.to_thread 依赖这个语义）。"""
    import threading

    results = {}

    def work(loc):
        with use_locale(loc):
            results[loc] = t("priority.high")

    ts = [threading.Thread(target=work, args=(loc,))
          for loc in ("zh-CN", "en-US")]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    assert results["zh-CN"] == "高"
    assert results["en-US"] == "High"


# ---------------------------------------------------------------------------
# 5. 领域层：同一份数据，两种语言
# ---------------------------------------------------------------------------
def _one_task():
    """一个已排好期的任务（summarize 需要 scheduled_start/end）。"""
    from datetime import datetime, timedelta
    today = datetime.now().date()
    start = datetime(today.year, today.month, today.day, 15, 0)
    task = Task(id=1, title="开会", start=start, duration_min=30,
                priority=Priority.HIGH)
    task.scheduled_start = start
    task.scheduled_end = start + timedelta(minutes=30)
    task.risk = 0
    return [task]


def test_planner_summary_switches_language():
    with use_locale("zh-CN"):
        zh = summarize(_one_task())
        assert "今日排期" in zh and "优先:高" in zh
    with use_locale("en-US"):
        en = summarize(_one_task())
        assert "Today's schedule" in en and "priority: High" in en


def test_priority_and_status_labels():
    with use_locale("en-US"):
        assert Priority.HIGH.label() == "High"
        assert Task(title="x").status_label() == "Pending"
    with use_locale("zh-CN"):
        assert Priority.HIGH.label() == "高"
        assert Task(title="x").status_label() == "待办"


# ---------------------------------------------------------------------------
# 6. 服务层：agent 的回复跟着语言走
# ---------------------------------------------------------------------------
@pytest.fixture()
def agent():
    d = tempfile.mkdtemp(prefix="i18n_agent_")
    a = ScheduleAgent(user_id="tester", store=Store(":memory:"),
                      cfg={"use_rule_fallback": True})
    yield a
    a.stop_engine()


def test_agent_replies_in_current_language(agent):
    with use_locale("zh-CN"):
        zh = agent.list_tasks()
        assert zh["message"].startswith("共 ")
    with use_locale("en-US"):
        en = agent.list_tasks()
        assert "task" in en["message"] and "in total" in en["message"]


def test_agent_add_message_english(agent):
    with use_locale("en-US"):
        res = agent.add_task(text="", title="Standup", start=None)
    assert "Standup" in res["message"]
    assert "Added" in res["message"]


# ---------------------------------------------------------------------------
# 7. 英文输入可被规则层理解
# ---------------------------------------------------------------------------
def test_parse_natural_english():
    with use_locale("en-US"):
        r = nlp.parse_natural("meeting tomorrow 3pm for 1 hour high priority")
    assert r["title"] == "meeting"
    assert r["duration_min"] == 60
    assert r["priority"] == Priority.HIGH
    assert r["start"].hour == 15
    assert r["start"].date() != __import__("datetime").datetime.now().date()


def test_parse_natural_english_deadline():
    r = nlp.parse_natural("submit report by 6pm 30 min")
    assert r["due"] is not None and r["due"].hour == 18
    assert r["start"] is None


def test_parse_modify_english():
    r = nlp.parse_modify("move study to 11am")
    assert r["title_hint"] == "study"
    assert r["start"].hour == 11


def test_parse_priority_english():
    r = nlp.parse_priority("set ai-dev to high priority")
    assert r["title_hint"] == "ai-dev"
    assert r["priority"] == Priority.HIGH


def test_chinese_still_works_after_bilingual_rules():
    """加了英文规则不能把中文解析带偏（历史行为必须保持）。"""
    r = nlp.parse_natural("今天早上10点吃早茶")
    assert r["title"] == "吃早茶"
    assert r["start"].hour == 10
    assert r["due"] is None


# ---------------------------------------------------------------------------
# 8. HTTP 层：语言按请求协商
# ---------------------------------------------------------------------------
def test_api_accept_language(client, alice):
    r = client.get("/api/tasks", headers={**bearer(alice),
                                          "Accept-Language": "en-US"})
    assert r.status_code == 200
    assert "in total" in r.json()["message"]
    r = client.get("/api/tasks", headers={**bearer(alice),
                                          "Accept-Language": "zh-CN"})
    assert r.json()["message"].startswith("共 ")


def test_api_lang_query_param_wins(client, alice):
    """`?lang=` 优先于请求头（便于分享固定语言的链接）。"""
    r = client.get("/api/tasks", params={"lang": "en-US"},
                   headers={**bearer(alice), "Accept-Language": "zh-CN"})
    assert "in total" in r.json()["message"]


def test_api_returns_content_language_header(client):
    r = client.get("/api/health", headers={"Accept-Language": "en-US"})
    assert r.headers.get("content-language") == "en-US"


def test_auth_error_message_is_translated(client):
    """注册重复用户的 409 文案要跟着语言走。"""
    client.post("/api/auth/register", json={"user_id": "dup_user"})
    r = client.post("/api/auth/register", json={"user_id": "dup_user"},
                    headers={"Accept-Language": "en-US"})
    assert r.status_code == 409
    assert "already" in r.json()["detail"].lower()


def test_health_exposes_supported_locales(client):
    data = client.get("/api/health").json()
    assert "en-US" in data["llm"]["locales"]
    assert data["llm"]["language"] in data["llm"]["locales"]


def test_unauthorized_message_is_translated(client):
    r = client.get("/api/tasks", headers={"Accept-Language": "en-US"})
    assert r.status_code == 401
    assert "Missing API token" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 9. 扩展性：丢一个 JSON 就是一门新语言
# ---------------------------------------------------------------------------
def test_new_locale_is_auto_discovered(tmp_path, monkeypatch):
    """新增语言不需要改代码：放一个 <locale>.json 就能被识别。"""
    src = i18n.catalog("en-US")
    (tmp_path / "ja-JP.json").write_text(
        '{"priority.high": "高"}', encoding="utf-8")
    monkeypatch.setenv("SCHEDULE_LOCALES_DIR", str(tmp_path))
    i18n.reload()
    assert "ja-JP" in available_locales()
    # 词典没写全时，缺的部分回退默认语言（不会显示成 key）
    with use_locale("ja-JP"):
        assert t("priority.high") == "高"
        assert t("priority.low") == src["priority.low"] or \
            t("priority.low") == i18n.catalog("zh-CN")["priority.low"]


# ---------------------------------------------------------------------------
# 10. 防回潮：文档元数据 / CLI 帮助也必须来自词典，不能有第二份字面量
# ---------------------------------------------------------------------------
def test_openapi_metadata_comes_from_catalog(client):
    """`/docs`、`/openapi.json` 的标题、描述、路由摘要都取自词典。

    这条测试防的是「代码里还留着一份中文字面量」——只要字面量和词典不一致
    （改词典时忘了改代码，或反过来），断言立刻失败。
    """
    zh = i18n.catalog("zh-CN")
    assert client.app.title == zh["api.title"]
    assert client.app.description == zh["api.description"]

    paths = client.get("/openapi.json").json()["paths"]
    callback = paths["/api/channels/{name}/callback"]
    assert callback["get"]["summary"] == zh["api.op_verify"]
    assert callback["post"]["summary"] == zh["api.op_receive"]


def test_openapi_metadata_follows_configured_language(settings):
    """配置成英文时，新建的应用元数据也应该是英文（不是固定中文）。"""
    from schedule_agent.api.app import create_app
    from schedule_agent.config import set_settings

    settings.language = "en-US"
    set_settings(settings)
    try:
        app = create_app(settings=settings)
        assert app.title == i18n.catalog("en-US")["api.title"]
        assert app.title != i18n.catalog("zh-CN")["api.title"]
    finally:
        # 配置是全局单例，改完必须还原，否则会影响后续用例的默认语言
        settings.language = "zh-CN"
        set_settings(settings)


def test_cli_help_uses_catalog(capsys):
    """`schedule-agent --help` 的说明文字来自词典，可随语言切换。"""
    from schedule_agent.cli.main import main as cli_main

    with pytest.raises(SystemExit):
        cli_main(["--help"])
    out = capsys.readouterr().out
    assert i18n.catalog("zh-CN")["cli.desc"] in out
    assert i18n.catalog("zh-CN")["cli.help_user"] in out

    with use_locale("en-US"):
        with pytest.raises(SystemExit):
            cli_main(["--help"])
        out_en = capsys.readouterr().out
    assert i18n.catalog("en-US")["cli.desc"] in out_en
    assert i18n.catalog("en-US")["cli.help_user"] in out_en


def test_every_key_referenced_in_code_exists():
    """代码里 `t("xxx")` 引用的 key 必须真的在词典里。

    拼错 key 不会报错，只会把 `agent.added_todya` 原样显示给用户——这种 bug
    线上很难被发现，所以用静态检查兜住。动态拼接的 key（f-string / 变量）
    跳过，它们由各自的逻辑测试覆盖。
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    used: dict[str, str] = {}
    for path in src.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "t" and node.args):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                used.setdefault(arg.value, f"{path.name}:{node.lineno}")

    assert used, "没扫到任何 t() 调用，检查逻辑可能失效了"
    missing = {k: v for k, v in used.items() if not i18n.has(k, "zh-CN")}
    assert not missing, f"这些 key 在词典里不存在：{missing}"
