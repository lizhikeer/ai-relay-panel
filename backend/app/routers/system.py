"""系统路由：/panel-api/system/*（契约 §8）"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from ..auth import require_login
from ..config import settings
from ..newapi import get_client
from ..response import ok

router = APIRouter(prefix="/panel-api/system", tags=["system"])

# 脱敏：契约 §8 要求 `*_secret` / `*_key` 返回 "***"。
# 采用「后缀」判定，避免把 PasswordLoginEnabled、oidc.token_endpoint 这类
# 开关/地址误伤；同时保守地掩码私钥与证书。
_SENSITIVE_SUFFIX = ("secret", "key", "password", "token", "cert")


def _is_sensitive(key: str) -> bool:
    low = key.lower()
    if low.endswith(_SENSITIVE_SUFFIX):
        return True
    return "privatekey" in low.replace("_", "") or "apikey" in low


@router.get("/health")
async def health():
    """免登录，供容器健康检查使用（契约 §8）。

    比 /api/status 更进一步：附带 `auth` —— 用**当前 token** 真实请求一次上游
    鉴权接口。这样「token 被服务端吊销/失效」这类 /api/status 探不出的故障
    （2026-10-24 的 409 死局期间健康检查一直绿着）能被监控看见。
    探测用的是裸请求，**绝不触发登录** —— 健康检查每 30s 一次，
    若它打登录接口，限流（20 次/20 分钟）和会话数都会被它打爆。
    """
    client = get_client()
    upstream = "ok"
    latency = 0
    try:
        started = time.perf_counter()
        status = await client.get_status()
        latency = int(round((time.perf_counter() - started) * 1000))
        if not status.get("success"):
            upstream = "down"
    except Exception:  # noqa: BLE001
        upstream = "down"
    auth_state, expires_in = client.auth_snapshot()
    if auth_state == "ok":
        # 本地没过期再花一次网络请求确认服务端认可；expired/none 直接如实上报
        server_state = await client.probe_auth()
        # 服务端拒绝但本地未过期：以服务端为准（下一笔业务请求会自动重登）
        auth_state = {"ok": "ok", "invalid": "invalid", "none": "none", "unreachable": "unknown"}[server_state]
    return ok(
        {
            "bff": "ok",
            "upstream": upstream,
            "upstream_latency_ms": latency,
            "auth": auth_state,
            "token_expires_in": expires_in,
        }
    )


@router.get("/info")
async def info(_: dict = Depends(require_login)):
    client = get_client()
    upstream_version = ""
    try:
        status = await client.get_status()
        upstream_version = (status.get("data") or {}).get("version") or ""
    except Exception:  # noqa: BLE001
        upstream_version = ""
    return ok(
        {
            "version": settings.version,
            "upstream_base_url": settings.newapi_base_url,
            "upstream_version": upstream_version,
            "started_at": settings.start_ts,
        }
    )


@router.get("/options")
async def get_options(_: dict = Depends(require_login)):
    raw = await get_client().get_options()
    masked: dict[str, str] = {}
    for item in raw:
        key = item.get("key")
        if not key:
            continue
        masked[key] = "***" if _is_sensitive(key) else item.get("value")
    return ok(masked)


@router.put("/options")
async def update_options(payload: dict, _: dict = Depends(require_login)):
    client = get_client()
    results = {}
    for key, value in payload.items():
        res = await client.update_option(key, "" if value is None else str(value))
        results[key] = bool(res.get("success"))
    return ok(results)
