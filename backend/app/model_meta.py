"""上游业务网关（tokendance）的模型能力元数据。

**本模块是项目中除 `newapi.py` 之外唯一使用 httpx 的地方。**
原因：它访问的是「上游业务网关」而非 New API 管理面，两者的鉴权与语义完全不同，
不应该混进 New API 客户端里。

背景（2026-10-12 实测）：
New API 自己生成的 `/v1/models` 只回 `id/object/created/owned_by/supported_endpoint_types`，
把上游的 `context_length` / `description` / `supported_protocols` **全丢了** ——
客户端（Cherry Studio / LibreChat / 各类 agent）因此无法自动识别上下文长度。
本模块负责取回并缓存这份元数据，供 `/v1/models` 补全使用。
"""
from __future__ import annotations

import logging
import time

import httpx

from .config import settings

logger = logging.getLogger("bff.modelmeta")

# {model_id: 上游原始模型对象}
_cache: dict[str, dict] = {}
_cache_at: float = 0.0
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # trust_env=False：忽略环境变量里的 HTTP(S)_PROXY（同 newapi.py，见契约 §10.6）
        _client = httpx.AsyncClient(timeout=settings.request_timeout, trust_env=False)
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _extract_items(payload: object) -> list[dict]:
    """兼容 `{data:[...]}` / `{models:[...]}` / 裸数组三种形状。"""
    if isinstance(payload, dict):
        for key in ("data", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                return [m for m in value if isinstance(m, dict)]
        return []
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    return []


async def model_meta(force: bool = False) -> dict[str, dict]:
    """返回 `{model_id: 上游模型对象}`（含 context_length / supported_protocols / description）。

    **绝不抛异常**：拉取失败时沿用上一次的成功结果（可能为空 dict）。
    `/v1/models` 的可用性优先于「补全」这个锦上添花的能力。
    """
    global _cache, _cache_at

    now = time.time()
    if not force and _cache and (now - _cache_at) < settings.models_meta_ttl:
        return _cache

    try:
        resp = await _get_client().get(settings.upstream_models_url)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        items = _extract_items(resp.json())
        fresh = {str(m["id"]): m for m in items if m.get("id")}
        if not fresh:
            raise RuntimeError("响应里没有可用的模型条目")
        _cache = fresh
        _cache_at = now
        logger.info("上游模型元数据已刷新：%d 条", len(fresh))
    except Exception as exc:  # noqa: BLE001 - 元数据非关键路径
        if _cache:
            logger.warning("刷新上游模型元数据失败（沿用缓存 %d 条）：%s", len(_cache), exc)
        else:
            logger.warning("拉取上游模型元数据失败（本次不做补全）：%s", exc)

    return _cache
