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


class ReorderGroupIn(BaseModel):
    type: int
    ids: list[int]


class ReorderIn(BaseModel):
    groups: list[ReorderGroupIn]


# 注意：/types、/models 必须声明在 /{cid} 之前，避免被路径参数吞掉
@router.get("/types")
async def channel_types(_: dict = Depends(require_login)):
    items = [{"id": tid, "name": name} for tid, name in sorted(CHANNEL_TYPE_NAMES.items())]
    return ok(items)


@router.get("/models")
async def channel_models(type: int | None = None, _: dict = Depends(require_login)):
    """模型清单。

    - **不带 `type`**：上游全量模型（原行为，`/api/channel/models`）
    - **带 `type`**：只聚合**该类型、且启用中**的渠道实际挂载的模型，
      用于「快速开始」按协议给出真正可用的候选（契约 §4.6）
    """
    client = get_client()
    if type is None:
        raw = await client.list_models()
        items = [{"id": m.get("id"), "owned_by": m.get("owned_by") or ""} for m in raw]
        return ok(items)

    wanted = int(type)
    models: list[str] = []
    seen: set = set()
    for ch in await client.all_channels():
        if int(ch.get("type") or -1) != wanted or ch.get("status") != 1:
            continue
        for m in (ch.get("models") or "").split(","):
            name = m.strip()
            if name and name not in seen:
                seen.add(name)
                models.append(name)
    return ok([{"id": m, "owned_by": ""} for m in models])


@router.post("/reorder")
async def reorder_channels(payload: ReorderIn, _: dict = Depends(require_login)):
    """按「组内顺序」批量写回渠道优先级（契约 §4.7）。

    - 组内**第 1 个 → 优先级最高**；第 n 个 → 优先级 1。
    - 语义（New API 原生）：`priority` 越大越优先；**同一协议内，高优先级渠道
      未失败/未被禁用前不会使用低优先级渠道**。渠道因额度耗尽被上游拒绝时，
      `auto_ban=1` 会让 New API 自动禁用该渠道（status=3），流量随即落到下一优先级。
    - 校验：id 必须存在、类型必须与所在组一致、不得重复出现 —— 防止误把
      OpenAI 渠道排进 Anthropic 组。
    """
    if not payload.groups:
        return fail("groups 不能为空")

    client = get_client()
    by_id = {c.get("id"): c for c in await client.all_channels()}

    plan: list[tuple[int, int]] = []
    seen = set()
    for group in payload.groups:
        total = len(group.ids)
        for idx, cid in enumerate(group.ids):
            if cid in seen:
                return fail(f"渠道 {cid} 在请求中重复出现")
            ch = by_id.get(cid)
            if ch is None:
                return fail(f"渠道 {cid} 不存在")
            if int(ch.get("type") or -1) != int(group.type):
                return fail(
                    f"渠道 {cid} 的类型（{ch.get('type')}）与所在分组（{group.type}）不一致"
                )
            seen.add(cid)
            plan.append((cid, total - idx))

    updated = []
    for cid, priority in plan:
        await client.set_channel_priority(cid, priority)
        updated.append({"id": cid, "priority": priority})
    return ok({"updated": updated})


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
