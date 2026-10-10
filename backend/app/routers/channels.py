"""渠道路由：/panel-api/channels/*（契约 §4）"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from ..auth import require_login
from ..newapi import CHANNEL_TYPE_NAMES, get_client, map_channel
from ..response import BusinessError, fail, ok, paged

router = APIRouter(prefix="/panel-api/channels", tags=["channels"])


class ChannelIn(BaseModel):
    name: str | None = None
    type: int | None = None
    base_url: str | None = None
    key: str | None = None
    models: list[str] | str | None = None
    group: str | None = None
    priority: int | None = None
    weight: int | None = None


class StatusIn(BaseModel):
    status: int


class TestIn(BaseModel):
    model: str | None = None


# 注意：/types、/models 必须声明在 /{cid} 之前，避免被路径参数吞掉
@router.get("/types")
async def channel_types(_: dict = Depends(require_login)):
    items = [{"id": tid, "name": name} for tid, name in sorted(CHANNEL_TYPE_NAMES.items())]
    return ok(items)


@router.get("/models")
async def channel_models(_: dict = Depends(require_login)):
    raw = await get_client().list_models()
    items = [{"id": m.get("id"), "owned_by": m.get("owned_by") or ""} for m in raw]
    return ok(items)


@router.get("")
async def list_channels(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=500),
    keyword: str = "",
    status: str = "",
    _: dict = Depends(require_login),
):
    client = get_client()
    body = await client.list_channels(page, size, keyword=keyword)
    data = body.get("data") or {}
    items = [map_channel(c, client.key_cache) for c in (data.get("items") or [])]
    if status != "":
        want = 1 if str(status) in ("1", "true", "enabled") else 0
        items = [c for c in items if c["status"] == want]
    total = data.get("total")
    if total is None:
        total = len(items)
    return ok(paged(items, total, page, size))


@router.get("/{cid}")
async def get_channel(cid: int, _: dict = Depends(require_login)):
    client = get_client()
    body = await client.get_channel(cid)
    if not body.get("success"):
        return fail(body.get("message") or "渠道不存在")
    return ok(map_channel(body.get("data") or {}, client.key_cache))


@router.post("")
async def create_channel(payload: ChannelIn, _: dict = Depends(require_login)):
    if not payload.name:
        return fail("渠道名不能为空")
    client = get_client()
    new_id = await client.create_channel(payload.model_dump(exclude_none=True))
    return ok({"id": new_id})


@router.put("/{cid}")
async def update_channel(cid: int, payload: ChannelIn, _: dict = Depends(require_login)):
    client = get_client()
    data = await client.update_channel(cid, payload.model_dump(exclude_none=True))
    return ok(map_channel(data, client.key_cache) if data else None)


@router.delete("/{cid}")
async def delete_channel(cid: int, _: dict = Depends(require_login)):
    await get_client().delete_channel(cid)
    return ok(None)


@router.post("/{cid}/status")
async def set_status(cid: int, payload: StatusIn, _: dict = Depends(require_login)):
    if payload.status not in (0, 1):
        return fail("status 只能为 0 或 1")
    await get_client().set_channel_status(cid, payload.status)
    return ok(None)


@router.post("/{cid}/test")
async def test_channel(cid: int, payload: TestIn | None = None, _: dict = Depends(require_login)):
    model = payload.model if payload else None
    result = await get_client().test_channel(cid, model=model)
    return ok(result)
