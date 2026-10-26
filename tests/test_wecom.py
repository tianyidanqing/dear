# -*- coding: utf-8 -*-
"""企业微信渠道的回归测试。

覆盖四层：
  1. 加解密（AES-CBC + PKCS#7 + receiveid 校验）——最容易出错，逐字节验证；
  2. 签名校验（Token 不匹配必须拒绝）；
  3. 身份映射与文本适配（手机上的纯文本、2048 字节限制）；
  4. 回调路由（未配置 503、签名错 403、正常消息建任务并推送、重发不重复处理）。
"""
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from schedule_agent.api.app import create_app                    # noqa: E402
from schedule_agent.channels.wecom import messages as wc        # noqa: E402
from schedule_agent.channels.wecom.client import WeComClient     # noqa: E402
from schedule_agent.channels.wecom.crypto import WeComCrypto     # noqa: E402
from schedule_agent.config import Settings, set_settings          # noqa: E402
from schedule_agent.storage import auth as auth_store             # noqa: E402

TOKEN = "test-token-123"
AES_KEY = "a" * 43          # 43 字符 -> base64 补 '=' 后正好 32 字节
CORP_ID = "ww_test_corp"
AGENT_ID = "1000002"


def _wecom_cfg(**kw):
    cfg = dict(corp_id=CORP_ID, agent_id=AGENT_ID, secret="secret",
               token=TOKEN, aes_key=AES_KEY)
    cfg.update(kw)
    return cfg


# ---------------------------------------------------------------------------
# 1. 加解密
# ---------------------------------------------------------------------------
def test_encrypt_decrypt_roundtrip():
    c = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
    original = "<xml><Content><![CDATA[明天上午10点开会，优先级高]]></Content></xml>"
    assert c.decrypt(c.encrypt(original)) == original


def test_decrypt_rejects_wrong_corp_id():
    """receiveid 不匹配说明密钥对不上，必须拒绝而不是静默接受。"""
    a = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
    b = WeComCrypto(TOKEN, AES_KEY, "another_corp")
    with pytest.raises(ValueError, match="receiveid"):
        b.decrypt(a.encrypt("<xml>hi</xml>"))


def test_decrypt_rejects_garbage():
    c = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
    with pytest.raises(ValueError):
        c.decrypt("not-base64-@@@")


def test_bad_aes_key_rejected():
    with pytest.raises(ValueError, match="43"):
        WeComCrypto(TOKEN, "too-short", CORP_ID)


# ---------------------------------------------------------------------------
# 2. 签名
# ---------------------------------------------------------------------------
def test_signature_matches_official_formula():
    """sha1(''.join(sorted([token, ts, nonce, encrypt])))。"""
    import hashlib
    c = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
    ts, nonce, enc = "1700000000", "nonce123", "ENCRYPTED"
    expect = hashlib.sha1(
        "".join(sorted([TOKEN, ts, nonce, enc])).encode()).hexdigest()
    assert c.signature(ts, nonce, enc) == expect


def test_verify_rejects_wrong_signature():
    c = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
    assert c.verify("deadbeef", "1", "2", "x") is False


def test_decrypt_echo_requires_valid_signature():
    c = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
    echo = c.encrypt("echo-plain")
    ts, nonce = "1700000000", "n1"
    sig = c.signature(ts, nonce, echo)
    assert c.decrypt_echo(sig, ts, nonce, echo) == "echo-plain"
    with pytest.raises(ValueError, match="签名"):
        c.decrypt_echo("bad-signature", ts, nonce, echo)


# ---------------------------------------------------------------------------
# 3. 消息解析 / 身份映射 / 文本适配
# ---------------------------------------------------------------------------
def test_parse_message():
    xml = ("<xml><ToUserName><![CDATA[ww]]></ToUserName>"
           "<FromUserName><![CDATA[zhangsan]]></FromUserName>"
           "<CreateTime>1700000000</CreateTime>"
           "<MsgType><![CDATA[text]]></MsgType>"
           "<Content><![CDATA[明天10点开会]]></Content>"
           "<MsgId>1001</MsgId><AgentID>1000002</AgentID></xml>")
    m = wc.parse_message(xml)
    assert m["from_user"] == "zhangsan"
    assert m["msg_type"] == "text"
    assert m["content"] == "明天10点开会"
    assert m["msg_id"] == "1001"


def test_resolve_user_id_default_isolated():
    assert wc.resolve_user_id("zhangsan") == "wecom:zhangsan"


def test_resolve_user_id_with_bind():
    assert wc.resolve_user_id("zhangsan", {"zhangsan": "jiyu"}) == "jiyu"


def test_format_reply_strips_markdown_and_adds_schedule():
    text = "已添加**《开会》**，并重新规划。"
    sched = [{"title": "开会", "scheduled_start": "2026-10-25 15:00:00",
              "scheduled_end": "2026-10-25 15:30:00", "risk": 0}]
    out = wc.format_reply(text, sched)
    assert "**" not in out
    assert "15:00-15:30 开会" in out
    assert "今日排期" in out


def test_truncate_by_bytes_not_chars():
    """企业微信按字节限流，中文一个字 3 字节——不能按字符数截断。"""
    out = wc.format_reply("汉" * 1000, limit=100)
    assert len(out.encode("utf-8")) <= 100
    assert out.endswith("…")


# ---------------------------------------------------------------------------
# 4. 回调路由
# ---------------------------------------------------------------------------
def _settings(tmp_path, **kw):
    base = dict(
        data_dir=str(tmp_path / "data"),
        log_level="WARNING",
        use_rule_fallback=True,       # 测试不连真模型
        channels={"wecom": _wecom_cfg(**kw)},
    )
    s = Settings(**base)
    s.ensure_dirs()
    set_settings(s)
    auth_store.reset_conn()
    return s


def _app(tmp_path, sent, monkeypatch, **kw):
    s = _settings(tmp_path, **kw)
    app = create_app(settings=s)
    # 截住真实网络调用，把要发的消息记下来（monkeypatch 会自动还原）
    monkeypatch.setattr(
        WeComClient, "send_text",
        lambda self, touser, content: (
            sent.append({"touser": touser, "content": content}) or {"errcode": 0}))
    return app


def _payload(content="明天上午10点开会", msg_id="1001", msg_type="text",
             from_user="zhangsan", agent_id=AGENT_ID):
    xml = (f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
           f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
           f"<CreateTime>1700000000</CreateTime>"
           f"<MsgType><![CDATA[{msg_type}]]></MsgType>"
           f"<Content><![CDATA[{content}]]></Content>"
           f"<MsgId>{msg_id}</MsgId><AgentID>{agent_id}</AgentID></xml>")
    c = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
    enc = c.encrypt(xml)
    ts, nonce = "1700000000", "nonce1"
    sig = c.signature(ts, nonce, enc)
    body = (f"<xml><ToUserName><![CDATA[{CORP_ID}]]></ToUserName>"
            f"<Encrypt><![CDATA[{enc}]]></Encrypt><AgentID>{AGENT_ID}</AgentID></xml>")
    return body, {"msg_signature": sig, "timestamp": ts, "nonce": nonce}


def test_callback_503_when_not_configured(tmp_path):
    s = Settings(data_dir=str(tmp_path / "d"), log_level="WARNING")
    s.ensure_dirs()
    set_settings(s)
    auth_store.reset_conn()
    with TestClient(create_app(settings=s)) as c:
        r = c.post("/api/wecom/callback?msg_signature=x&timestamp=1&nonce=2",
                   content=b"<xml></xml>")
        assert r.status_code == 503
        assert "未配置" in r.text


def test_callback_rejects_bad_signature(tmp_path, monkeypatch):
    sent = []
    with TestClient(_app(tmp_path, sent, monkeypatch)) as c:
        body, params = _payload()
        params["msg_signature"] = "0" * 40
        r = c.post("/api/wecom/callback", params=params, content=body.encode())
        assert r.status_code == 403
        assert sent == [], "签名不正确时绝不能执行业务逻辑"


def test_callback_creates_task_and_pushes_reply(tmp_path, monkeypatch):
    sent = []
    with TestClient(_app(tmp_path, sent, monkeypatch)) as c:
        body, params = _payload()
        r = c.post("/api/wecom/callback", params=params, content=body.encode())
        assert r.status_code == 200
        assert r.text == "", "必须返回空串，否则企业微信会当成已回复"

        assert len(sent) == 1, sent
        assert sent[0]["touser"] == "zhangsan"
        assert sent[0]["content"]

        # 任务落在独立的 wecom: 空间里
        reg = c.app.state.registry
        tasks = reg.get("wecom:zhangsan").store.all("wecom:zhangsan")
        assert len(tasks) == 1
        assert tasks[0].title == "开会"
        assert tasks[0].start.hour == 10


def test_callback_dedupes_retried_message(tmp_path, monkeypatch):
    """企业微信超时会重发同一个 MsgId，只能处理一次。"""
    sent = []
    with TestClient(_app(tmp_path, sent, monkeypatch)) as c:
        body, params = _payload(msg_id="2002")
        c.post("/api/wecom/callback", params=params, content=body.encode())
        c.post("/api/wecom/callback", params=params, content=body.encode())

        reg = c.app.state.registry
        assert len(reg.get("wecom:zhangsan").store.all("wecom:zhangsan")) == 1
        assert len(sent) == 1


def test_callback_ignores_other_agent(tmp_path, monkeypatch):
    """同一台服务器挂了多个应用时，靠 AgentID 区分，别串消息。"""
    sent = []
    with TestClient(_app(tmp_path, sent, monkeypatch)) as c:
        body, params = _payload(msg_id="3003", agent_id="9999999")
        c.post("/api/wecom/callback", params=params, content=body.encode())
        assert sent == []


def test_callback_non_text_gets_hint(tmp_path, monkeypatch):
    sent = []
    with TestClient(_app(tmp_path, sent, monkeypatch)) as c:
        body, params = _payload(msg_id="4004", msg_type="image", content="")
        c.post("/api/wecom/callback", params=params, content=body.encode())
        assert len(sent) == 1
        assert "文字" in sent[0]["content"]


def test_get_verifies_url(tmp_path, monkeypatch):
    """URL 配置时企业微信会 GET echostr，必须原样返回解密后的明文。"""
    c = WeComCrypto(TOKEN, AES_KEY, CORP_ID)
    echo = c.encrypt("hello-wecom")
    ts, nonce = "1700000000", "n2"
    sig = c.signature(ts, nonce, echo)

    sent = []
    with TestClient(_app(tmp_path, sent, monkeypatch)) as cli:
        r = cli.get("/api/wecom/callback",
                    params={"msg_signature": sig, "timestamp": ts,
                            "nonce": nonce, "echostr": echo})
        assert r.status_code == 200
        assert r.text == "hello-wecom"


def test_generic_path_works_too(tmp_path, monkeypatch):
    """统一路径 /api/channels/<name>/callback 与旧路径等价。"""
    sent = []
    with TestClient(_app(tmp_path, sent, monkeypatch)) as c:
        body, params = _payload(msg_id="5005")
        r = c.post("/api/channels/wecom/callback", params=params, content=body.encode())
        assert r.status_code == 200
        assert len(sent) == 1
