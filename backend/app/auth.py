"""面板自身登录态：内存 session + HttpOnly Cookie。

- 会话存内存（4 人自用，重启丢登录可接受，契约 §5「不持久化」）
- Cookie 名固定 `panel_session`（契约 §1.3）
- 有效期 7 天，滑动续期
"""
from __future__ import annotations

import secrets
import time

from fastapi import Request

from .config import settings
from .response import Unauthorized

COOKIE_NAME = "panel_session"

# token -> {"username": str, "expires_at": int}
_sessions: dict[str, dict] = {}


def _now() -> int:
    return int(time.time())


def verify_credentials(username: str | None, password: str | None) -> bool:
    """常量时间比较，避免时序侧信道。"""
    ok_user = secrets.compare_digest(username or "", settings.panel_user)
    ok_pass = secrets.compare_digest(password or "", settings.panel_password)
    return bool(ok_user and ok_pass)


def create_session(username: str) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    expires_at = _now() + settings.session_ttl
    _sessions[token] = {"username": username, "expires_at": expires_at}
    return token, expires_at


def get_session(token: str | None) -> dict | None:
    """校验并滑动续期。"""
    if not token:
        return None
    sess = _sessions.get(token)
    if not sess:
        return None
    if sess["expires_at"] < _now():
        _sessions.pop(token, None)
        return None
    sess["expires_at"] = _now() + settings.session_ttl
    return sess


def destroy_session(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=settings.session_ttl,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


async def require_login(request: Request) -> dict:
    """依赖注入：校验 session，未登录抛 Unauthorized(→401)。"""
    token = request.cookies.get(COOKIE_NAME)
    sess = get_session(token)
    if not sess:
        raise Unauthorized()
    request.state.session = sess
    return sess
