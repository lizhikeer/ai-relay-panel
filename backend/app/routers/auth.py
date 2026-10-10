"""认证路由：/panel-api/auth/*（契约 §2）"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from ..auth import (
    COOKIE_NAME,
    clear_session_cookie,
    create_session,
    destroy_session,
    get_session,
    set_session_cookie,
    verify_credentials,
)
from ..response import fail, ok

router = APIRouter(prefix="/panel-api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginBody):
    if not verify_credentials(body.username, body.password):
        return fail("用户名或密码错误")
    token, expires_at = create_session(body.username)
    resp = ok({"username": body.username, "expires_at": expires_at})
    set_session_cookie(resp, token)
    return resp


@router.post("/logout")
async def logout(request: Request):
    destroy_session(request.cookies.get(COOKIE_NAME))
    resp = ok(None)
    clear_session_cookie(resp)
    return resp


@router.get("/session")
async def session(request: Request):
    """探测登录态。契约 §2.3：本接口永不返回 401，未登录返回 200 + authenticated:false。"""
    sess = get_session(request.cookies.get(COOKIE_NAME))
    if not sess:
        return ok({"authenticated": False})
    return ok(
        {
            "authenticated": True,
            "username": sess["username"],
            "expires_at": sess["expires_at"],
        }
    )
