# -*- coding: utf-8 -*-
"""API 层：鉴权拦截 + 多用户隔离（经 HTTP，用 TestClient）。"""
import json

from conftest import bearer, register

PROTECTED = [
    ("GET", "/api/tasks"),
    ("GET", "/api/tasks/today"),
    ("GET", "/api/templates"),
    ("GET", "/api/summary"),
    ("GET", "/api/events"),
    ("GET", "/api/me"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream"),
    ("DELETE", "/api/tasks/1"),
]


def test_protected_endpoints_require_token(client):
    for method, path in PROTECTED:
        r = client.request(method, path, json={"message": "hi"})
        assert r.status_code == 401, f"{method} {path} 应返回 401，实际 {r.status_code}"


def test_health_is_public(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["auth_required"] is True


def test_register_login_and_duplicate(client):
    key = register(client, "carol", "pw")
    assert key

    dup = client.post("/api/auth/register", json={"user_id": "carol"})
    assert dup.status_code == 409

    bad = client.post("/api/auth/login", json={"user_id": "carol", "password": "wrong"})
    assert bad.status_code == 401

    ok = client.post("/api/auth/login", json={"user_id": "carol", "password": "pw"})
    assert ok.status_code == 200 and ok.json()["api_key"] == key


def test_forged_token_rejected(client):
    assert client.get("/api/tasks", headers=bearer("nope")).status_code == 401


def test_users_see_only_own_tasks(client, alice, bob):
    ra = client.post("/api/tasks", headers=bearer(alice),
                     json={"title": "alice-会议", "start": "2026-10-24 15:00:00",
                           "duration_min": 60})
    assert ra.json()["ok"] is True
    rb = client.post("/api/tasks", headers=bearer(bob),
                     json={"title": "bob-健身", "start": "2026-10-24 19:00:00",
                           "duration_min": 45})
    assert rb.json()["ok"] is True

    ta = [t["title"] for t in client.get("/api/tasks", headers=bearer(alice)).json()["tasks"]]
    tb = [t["title"] for t in client.get("/api/tasks", headers=bearer(bob)).json()["tasks"]]
    assert "alice-会议" in ta and "bob-健身" not in ta
    assert "bob-健身" in tb and "alice-会议" not in tb


def test_cross_user_delete_and_done_are_refused(client, alice, bob):
    client.post("/api/tasks", headers=bearer(alice),
                json={"title": "alice-会议", "start": "2026-10-24 15:00:00"})
    alice_id = client.get("/api/tasks", headers=bearer(alice)).json()["tasks"][0]["id"]

    r = client.delete(f"/api/tasks/{alice_id}", headers=bearer(bob))
    assert r.json()["ok"] is False
    assert any(t["id"] == alice_id
               for t in client.get("/api/tasks", headers=bearer(alice)).json()["tasks"])

    bob_id = None
    client.post("/api/tasks", headers=bearer(bob), json={"title": "bob-健身"})
    bob_id = [t["id"] for t in client.get("/api/tasks", headers=bearer(bob)).json()["tasks"]][0]
    r = client.post(f"/api/tasks/{bob_id}/done", headers=bearer(alice))
    assert r.json()["ok"] is False


def test_sse_stream_accepts_token_query_param(client, alice):
    """浏览器 EventSource 无法设头，SSE 必须支持 ?token=。"""
    with client.stream("POST", f"/api/chat/stream?token={alice}",
                       json={"message": "今日日程"}) as resp:
        assert resp.status_code == 200
        text = ""
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            ev = json.loads(data)
            if ev.get("type") == "token":
                text += ev.get("text", "")
    assert len(text) > 0


def test_sse_without_token_is_401(client):
    assert client.post("/api/chat/stream", json={"message": "今日日程"}).status_code == 401


def test_events_are_isolated_per_user(client, alice, bob):
    client.post("/api/tasks", headers=bearer(alice), json={"title": "alice-会议"})
    client.get("/api/summary", headers=bearer(alice))     # 触发一次速览事件

    ea = client.get("/api/events", params={"since": 0, "token": alice}).json()
    eb = client.get("/api/events", params={"since": 0, "token": bob}).json()
    assert ea and all(e["user_id"] == "alice" for e in ea)
    assert not any(e["user_id"] == "alice" for e in eb)


def test_admin_key_needs_x_user_id(client, alice, bob):
    admin = {"Authorization": "Bearer test-admin-key"}

    r = client.get("/api/admin/users", headers=admin)
    assert r.status_code == 200
    assert {u["user_id"] for u in r.json()["users"]} >= {"alice", "bob"}

    # 不带 X-User-Id -> 400
    assert client.get("/api/tasks", headers=admin).status_code == 400
    # 带上后可以扮演指定用户
    r = client.get("/api/tasks", headers={**admin, "X-User-Id": "alice"})
    assert r.status_code == 200
    # 普通用户不能访问管理接口
    assert client.get("/api/admin/users", headers=bearer(alice)).status_code == 403
