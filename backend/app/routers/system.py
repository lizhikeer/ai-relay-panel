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
    """免登录，供容器健康检查使用（契约 §8）。"""
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
    return ok({"bff": "ok", "upstream": upstream, "upstream_latency_ms": latency})


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
