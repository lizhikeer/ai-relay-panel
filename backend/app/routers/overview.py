"""概览路由：/panel-api/overview（契约 §3）"""
from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends

from ..auth import require_login
from ..config import settings
from ..newapi import get_client, map_channel_abnormal
from ..response import ok

router = APIRouter(prefix="/panel-api", tags=["overview"])


@router.get("/overview")
async def overview(_: dict = Depends(require_login)):
    client = get_client()

    # --- 渠道 ---
    ch_data = (await client.list_channels(1, 500)).get("data") or {}
    channels = ch_data.get("items") or []
    ch_total = ch_data.get("total")
    if ch_total is None:
        ch_total = len(channels)
    ch_enabled = sum(1 for c in channels if c.get("status") == 1)
    abnormal = [a for a in (map_channel_abnormal(c) for c in channels) if a]

    # --- 令牌 ---
    tk_data = (await client.list_tokens(1, 500)).get("data") or {}
    tokens = tk_data.get("items") or []
    tk_total = tk_data.get("total")
    if tk_total is None:
        tk_total = len(tokens)
    tk_enabled = sum(1 for t in tokens if t.get("status") == 1)

    # --- 今日用量（从消费日志聚合）---
    midnight = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    now = int(time.time())
    log_data = (
        await client.list_logs(
            {"p": 1, "page_size": 500, "type": 2, "start_timestamp": midnight, "end_timestamp": now}
        )
    ).get("data") or {}
    today_logs = log_data.get("items") or []
    today_requests = 0
    today_tokens = 0
    today_quota = 0.0
    for log in today_logs:
        today_requests += 1
        today_tokens += int(log.get("prompt_tokens") or 0) + int(log.get("completion_tokens") or 0)
        today_quota += float(log.get("quota") or 0)

    # --- 上游连通性 ---
    upstream_name = urlparse(settings.newapi_base_url).hostname or settings.newapi_base_url
    if channels:
        host = urlparse(channels[0].get("base_url") or "").hostname
        if host:
            upstream_name = host
    reachable = True
    latency_ms = 0
    try:
        started = time.perf_counter()
        status = await client.get_status()
        latency_ms = int(round((time.perf_counter() - started) * 1000))
        reachable = bool(status.get("success"))
        currency = (status.get("data") or {}).get("quota_display_type") or "USD"
    except Exception:  # noqa: BLE001 - 概览不应因上游抖动而整体失败
        reachable = False
        currency = "USD"

    return ok(
        {
            "channels": {
                "total": ch_total,
                "enabled": ch_enabled,
                "disabled": max(ch_total - ch_enabled, 0),
                "abnormal": abnormal,
            },
            "tokens": {
                "total": tk_total,
                "enabled": tk_enabled,
                "disabled": max(tk_total - tk_enabled, 0),
            },
            "today": {
                "requests": today_requests,
                "tokens": today_tokens,
                "quota_spent": round(today_quota, 6),
                "quota_currency": currency,
            },
            "upstream": {
                "name": upstream_name,
                "reachable": reachable,
                "latency_ms": latency_ms,
            },
            "panel": {
                "version": settings.version,
                "uptime_seconds": int(time.time()) - settings.start_ts,
            },
        }
    )
