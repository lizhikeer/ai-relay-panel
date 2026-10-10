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
    await get_client().create_token(payload.model_dump(exclude_none=True))
    return ok(None)


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
