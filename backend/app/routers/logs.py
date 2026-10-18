"""日志路由：/panel-api/logs/*（契约 §7）"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from ..auth import require_login
from ..newapi import get_client, map_log
from ..response import ok, paged

router = APIRouter(prefix="/panel-api/logs", tags=["logs"])


@router.get("/stats")
async def log_stats(days: int = Query(7, ge=1, le=90), _: dict = Depends(require_login)):
    now = int(time.time())
    start = now - days * 86400
    body = await get_client().list_logs(
        {"p": 1, "page_size": 500, "type": 2, "start_timestamp": start, "end_timestamp": now}
    )
    items = (body.get("data") or {}).get("items") or []

    daily: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    by_channel: dict[str, dict] = {}

    def _bucket(store: dict, key_name: str, key: str) -> dict:
        if key not in store:
            store[key] = {key_name: key, "requests": 0, "tokens": 0, "cost_cny": 0.0}
        return store[key]

    for raw in items:
        log = map_log(raw)
        date = datetime.fromtimestamp(log["created_at"]).strftime("%Y-%m-%d") if log["created_at"] else ""
        tokens = (log["prompt_tokens"] or 0) + (log["completion_tokens"] or 0)
        # 金额一律用 DeepSeek 均价现算（契约 §3.1），不用日志里的 quota ——
        # 那是历史记账，旧倍率(37.5)下会虚高近三个数量级
        cost = log["cost_cny"]
        d = _bucket(daily, "date", date)
        d["requests"] += 1
        d["tokens"] += tokens
        d["cost_cny"] = round(d["cost_cny"] + cost, 6)
        m = _bucket(by_model, "model_name", log["model_name"])
        m["requests"] += 1
        m["tokens"] += tokens
        m["cost_cny"] = round(m["cost_cny"] + cost, 6)
        c = _bucket(by_channel, "channel_name", log["channel_name"])
        c["requests"] += 1
        c["cost_cny"] = round(c["cost_cny"] + cost, 6)

    return ok(
        {
            "daily": sorted(daily.values(), key=lambda x: x["date"]),
            "by_model": sorted(by_model.values(), key=lambda x: x["requests"], reverse=True),
            "by_channel": sorted(by_channel.values(), key=lambda x: x["requests"], reverse=True),
            "total_cost_cny": round(sum(x["cost_cny"] for x in daily.values()), 6),
            "currency": "CNY",
        }
    )


@router.get("")
async def list_logs(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    type: str = "",
    keyword: str = "",
    start_ts: str = "",
    end_ts: str = "",
    _: dict = Depends(require_login),
):
    params: dict = {"p": page, "page_size": size}
    if type != "":
        params["type"] = type
    if keyword:
        params["keyword"] = keyword
    if start_ts != "":
        params["start_timestamp"] = start_ts
    if end_ts != "":
        params["end_timestamp"] = end_ts
    body = await get_client().list_logs(params)
    data = body.get("data") or {}
    items = [map_log(x) for x in (data.get("items") or [])]
    total = data.get("total")
    if total is None:
        total = len(items)
    return ok(paged(items, total, page, size))
