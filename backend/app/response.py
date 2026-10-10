"""统一响应包装与业务异常。

契约 §1.1：所有接口（含错误）都返回 {success, message, data}。
契约 §1.2：业务失败 → 200 + success:false；未登录 → 401；上游故障 → 502。
"""
from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


def envelope(success: bool, message: str = "", data: Any = None) -> dict:
    return {"success": success, "message": message, "data": data}


def ok(data: Any = None, message: str = "") -> JSONResponse:
    """业务成功：200 + success:true。"""
    return JSONResponse(status_code=200, content=envelope(True, message, data))


def fail(message: str, data: Any = None) -> JSONResponse:
    """业务失败：200 + success:false（契约 §1.2）。"""
    return JSONResponse(status_code=200, content=envelope(False, message, data))


def paged(items: list, total: int, page: int, size: int) -> dict:
    """契约 §1.4 分页结构。"""
    return {"items": items, "total": total, "page": page, "size": size}


class Unauthorized(Exception):
    """未登录 / session 失效 → 401。"""

    def __init__(self, message: str = "未登录或登录已过期"):
        super().__init__(message)
        self.message = message


class UpstreamError(Exception):
    """New API 不可达或返回异常 → 502。"""

    def __init__(self, message: str = "上游 New API 不可达"):
        super().__init__(message)
        self.message = message


class BusinessError(Exception):
    """业务失败（可预期的上游拒绝等）→ 200 + success:false。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
