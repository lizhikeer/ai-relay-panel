"""用户路由：/panel-api/users（契约 §6）"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import require_login
from ..newapi import get_client, map_user
from ..response import ok

router = APIRouter(prefix="/panel-api/users", tags=["users"])


@router.get("")
async def list_users(_: dict = Depends(require_login)):
    body = await get_client().list_users()
    data = body.get("data") or {}
    items = [map_user(u) for u in (data.get("items") or [])]
    total = data.get("total")
    if total is None:
        total = len(items)
    return ok({"items": items, "total": total})
