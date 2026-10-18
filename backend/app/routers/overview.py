"""概览路由：/panel-api/overview（契约 §3）"""
from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends

from .. import pricing
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
    # 口径：token 原样统计；金额按 `pricing` 里的 DeepSeek 均价**从 token 现算**，
    # 不用日志里的 quota 字段 —— 历史日志是按旧倍率(37.5)记的，直接加总会虚高。
    midnight = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    now = int(time.time())
    log_data = (
        await client.list_logs(
            {"p": 1, "page_size": 500, "type": 2, "start_timestamp": midnight, "end_timestamp": now}
        )
    ).get("data") or {}
    today_logs = log_data.get("items") or []
    today_requests = 0
    today_input = 0
    today_output = 0
    today_cost = 0.0
    for log in today_logs:
        today_requests += 1
        today_input += int(log.get("prompt_tokens") or 0)
        today_output += int(log.get("completion_tokens") or 0)
        today_cost += pricing.cost_cny(
            log.get("prompt_tokens"),
            log.get("completion_tokens"),
            log.get("cache_tokens"),
            log.get("model_name"),
        )
    today_tokens = today_input + today_output
    today_avg = int(round(today_tokens / today_requests)) if today_requests else 0

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
                "input_tokens": today_input,
                "output_tokens": today_output,
                "avg_tokens_per_request": today_avg,
                # 按 DeepSeek 均价从 token 现算（契约 §3.1），不读日志里的 quota 字段
                "cost_cny": round(today_cost, 6),
                "cost_currency": "CNY",
            },
            "pricing": pricing.price_table(),
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
