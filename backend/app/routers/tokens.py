"""令牌路由：/panel-api/tokens/*（契约 §5）"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..auth import require_login
from ..newapi import get_client, map_token
from ..response import fail, ok, paged

router = APIRouter(prefix="/panel-api/tokens", tags=["tokens"])


class TokenIn(BaseModel):
    name: str | None = None
    quota: int | None = None
    unlimited_quota: bool | None = None
    expired_days: int | None = None
    models: list[str] | str | None = None
    group: str | None = None


class StatusIn(BaseModel):
    status: int


@router.get("")
async def list_tokens(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    _: dict = Depends(require_login),
):
    body = await get_client().list_tokens(page, size)
    data = body.get("data") or {}
    items = [map_token(t) for t in (data.get("items") or [])]
    total = data.get("total")
    if total is None:
        total = len(items)
    return ok(paged(items, total, page, size))


@router.post("")
async def create_token(payload: TokenIn, _: dict = Depends(require_login)):
    if not payload.name:
        return fail("令牌名不能为空")
    result = await get_client().create_token(payload.model_dump(exclude_none=True))
    # data = { "id": 12, "key": "sk-...." }
    # key 为**一次性**明文回显（上游仅能在创建后取到明文），此后所有查询接口只回掩码。
    return ok(result)


@router.get("/{tid}/key")
async def get_token_key(tid: int, _: dict = Depends(require_login)):
    """按需取回令牌**明文 key**（契约 §5.1，供「快速开始」自动填入真实密钥）。

    实测（rc.41）：上游 `POST /api/token/{id}/key` 对**任意已有令牌**都返回明文
    （`token.GetFullKey()`），并不限于刚创建的那一次；列表/详情接口则恒为掩码。
    上游存的 key 不带 `sk-` 前缀，这里统一补上。
    """
    key = await get_client().get_token_key(tid)
    if not key:
        return fail("无法取回该令牌的明文 key（上游接口未返回）")
    if not key.startswith("sk-"):
        key = "sk-" + key
    return ok({"id": tid, "key": key})


@router.put("/{tid}")
async def update_token(tid: int, payload: TokenIn, _: dict = Depends(require_login)):
    data = await get_client().update_token(tid, payload.model_dump(exclude_none=True))
    return ok(map_token(data) if data else None)


@router.delete("/{tid}")
async def delete_token(tid: int, _: dict = Depends(require_login)):
    await get_client().delete_token(tid)
    return ok(None)


@router.post("/{tid}/status")
async def set_status(tid: int, payload: StatusIn, _: dict = Depends(require_login)):
    if payload.status not in (0, 1):
        return fail("status 只能为 0 或 1")
    await get_client().set_token_status(tid, payload.status)
    return ok(None)
