"""出口模型清单增强：`GET /v1/models`（契约 §11）。

由 Caddy 把 `/v1/models` **单独**分流到本 BFF（其余 `/v1*`、`/v1beta*` 仍直连 New API）。

设计要点：
1. **鉴权完全透传** —— 用请求原样的 `Authorization` / `x-api-key` 去调 New API 的
   `/v1/models`，状态码与响应体原样返回。无效 key 依然是 401/403，不泄露任何信息，
   也不会因为换了实现就绕过计费/限流。
2. **补全** —— 200 时把上游的 `context_length` / `supported_protocols` / `description` /
   `name` 合并到每个模型对象上（**不覆盖** New API 已有的字段）。
3. **优雅降级** —— 上游元数据拿不到时原样返回 New API 的响应，客户端完全无感。
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import model_meta
from ..config import settings

logger = logging.getLogger("bff.v1models")

router = APIRouter(tags=["v1"])

# 只有这些字段会从上游元数据补进来；New API 已有的同名字段优先
_ENRICH_FIELDS = ("context_length", "supported_protocols", "description", "name")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """访问 New API 的客户端。trust_env=False 见契约 §10.6。"""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=settings.newapi_base_url,
            timeout=settings.request_timeout,
            trust_env=False,
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _passthrough_headers(request: Request) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name in ("authorization", "x-api-key"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


@router.get("/v1/models")
async def list_models(request: Request):
    try:
        resp = await _get_client().get("/v1/models", headers=_passthrough_headers(request))
    except httpx.HTTPError as exc:
        logger.warning("/v1/models 透传 New API 失败：%s", exc)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": f"上游不可达：{exc}", "type": "api_error"}},
        )

    if resp.status_code != 200:
        # 鉴权失败 / 限流等：原样透传，保持行为一致
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001 - 上游返回非 JSON 时不做补全
        return Response(content=resp.content, status_code=200, media_type="application/json")

    meta = await model_meta.model_meta()
    data = payload.get("data") if isinstance(payload, dict) else None
    enriched = 0
    if isinstance(data, list) and meta:
        for item in data:
            if not isinstance(item, dict):
                continue
            extra = meta.get(str(item.get("id")))
            if not extra:
                continue
            hit = False
            for field in _ENRICH_FIELDS:
                value = extra.get(field)
                if value in (None, "", [], {}):
                    continue
                if item.get(field) in (None, ""):
                    item[field] = value
                    hit = True
            if hit:
                enriched += 1
    logger.info("/v1/models 返回 %s 个模型（补全 %d 条元数据）",
                len(data) if isinstance(data, list) else "?", enriched)

    return JSONResponse(content=payload)
