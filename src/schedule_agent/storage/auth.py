# -*- coding: utf-8 -*-
"""轻量 API 鉴权：基于 Bearer Token 的用户身份体系。

设计取舍：
  - 每个用户一个 **随机 API Key**（`secrets.token_urlsafe`），请求时放在
    `Authorization: Bearer <key>` 头里（SSE / WebSocket 也支持 `?token=` 查询参数）。
  - 密码可选：适合本项目的本地/小团队场景。设过密码的用户登录时必须带密码。
  - 依赖为零：用标准库 sqlite3 + sha256 + secrets，不引入 pyjwt / passlib。
  - 用户与调度数据分库（`auth.db` vs `schedule.db`），职责清晰。

对外接口：
    register(user_id, password=None)  -> (api_key, err)   注册，已存在则 err
    authenticate(user_id, password=None) -> (api_key, err)  登录校验
    ensure_user(user_id, password=None) -> (api_key, err)   不存在则注册，存在则登录
    verify_token(api_key) -> user_id | None                 请求鉴权的核心
"""
import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime

from schedule_agent.config import get_settings

_conn = None
# 注意：_db() 自身会上锁，而它又常被包在 `with _lock:` 内部调用，
# 因此这里必须是**可重入**锁，否则同一线程二次获取会直接死锁。
_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    api_key    TEXT NOT NULL UNIQUE,
    pwd_hash   TEXT,
    created_at TEXT
);
"""


def _db():
    """懒初始化连接（避免 CLI 等不需要鉴权的路径凭空建库）。

    库路径来自配置（默认 <data_dir>/auth.db），部署时可指向代码之外的位置。
    """
    global _conn
    with _lock:
        if _conn is None:
            from pathlib import Path
            path = Path(get_settings().auth_db_path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(path), check_same_thread=False)
            _conn.execute(_SCHEMA)
            _conn.commit()
        return _conn


def reset_conn():
    """关闭并重置连接（测试换库时用）。"""
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa
                pass
        _conn = None


def _hash(pwd):
    return hashlib.sha256((pwd or "").encode("utf-8")).hexdigest()


def _new_key():
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# 注册 / 登录 / 校验
# ---------------------------------------------------------------------------
def register(user_id, password=None):
    """注册新用户并返回其 API Key。

    错误用**稳定的英文错误码**返回（`empty user_id` / `already exists`），
    由接口层翻译成用户语言——存储层不应该知道界面用什么语言，
    这样同一份错误码既能给 JSON 客户端，也能给中文 / 英文界面。
    """
    user_id = (user_id or "").strip()
    if not user_id:
        return None, "empty user_id"
    api_key = _new_key()
    pwd_hash = _hash(password) if password else None
    with _lock:
        try:
            _db().execute(
                "INSERT INTO users(user_id, api_key, pwd_hash, created_at) "
                "VALUES (?,?,?,?)",
                (user_id, api_key, pwd_hash, datetime.now().isoformat(timespec="seconds")),
            )
            _db().commit()
        except sqlite3.IntegrityError:
            return None, "already exists"
    return api_key, None


def authenticate(user_id, password=None):
    """校验用户身份（若该用户设过密码则必须匹配），成功返回其 API Key。"""
    user_id = (user_id or "").strip()
    with _lock:
        row = _db().execute(
            "SELECT api_key, pwd_hash FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None, "no such user"
    api_key, pwd_hash = row
    if pwd_hash is not None and pwd_hash != _hash(password):
        return None, "wrong password"
    return api_key, None


def ensure_user(user_id, password=None):
    """不存在则注册，存在则按密码登录。幂等地拿到一个可用的 API Key。"""
    key, err = register(user_id, password)
    if err is None:
        return key, None
    return authenticate(user_id, password)


def verify_token(api_key):
    """核心鉴权：把 API Key 换成 user_id。无效返回 None。"""
    if not api_key:
        return None
    with _lock:
        row = _db().execute(
            "SELECT user_id FROM users WHERE api_key=?", (api_key.strip(),)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# 管理用途
# ---------------------------------------------------------------------------
def list_users():
    with _lock:
        rows = _db().execute(
            "SELECT user_id, created_at FROM users ORDER BY user_id").fetchall()
    return [{"user_id": r[0], "created_at": r[1]} for r in rows]


def reset_key(user_id, password=None):
    """重新签发某用户的 API Key（旧 Key 立即失效）。"""
    user_id = (user_id or "").strip()
    key, err = authenticate(user_id, password)
    if err:
        return None, err
    with _lock:
        new_key = _new_key()
        _db().execute("UPDATE users SET api_key=? WHERE user_id=?", (new_key, user_id))
        _db().commit()
    return new_key, None
