"""New API 客户端封装 —— 全项目**唯一**直接访问 New API 的出口。

职责：
- 用配置的管理员账号登录，缓存 access_token，过期自动重登（契约 §2 鉴权要点）
- 统一加 `Authorization` + `New-Api-User` 两个头
- 把 New API 的响应结构映射成面板契约字段
- key 一律脱敏（`sk-abcd****wxyz` 形态），绝不泄露明文
- 上游不可达/异常统一抛 UpstreamError（→502）

其他模块**禁止**直接使用 httpx。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from .config import settings
from .response import BusinessError, UpstreamError

logger = logging.getLogger("bff.newapi")

# rc.41 渠道创建接口的请求体是包装结构 {mode, channel}；扁平体会被判为 "channel cannot be empty"。
# 契约 §4.3 给出的降级文案（接口确实不可用时使用）。
CHANNEL_CREATE_DEGRADE_MSG = (
    "创建失败：上游接口返回 channel cannot be empty"
    "（New API rc.41 已知缺陷，请改用面板内【从模板批量创建】或手工在 New API 中建立）"
)

# New API 渠道类型常量（静态映射；实测 1=OpenAI、14=Anthropic，其余为常见值，未知回落为「类型 N」）
CHANNEL_TYPE_NAMES: dict[int, str] = {
    1: "OpenAI",
    2: "Azure",
    3: "Azure OpenAI",
    4: "Claude",
    8: "Claude",
    14: "Anthropic",
    15: "Gemini",
    16: "Zhipu",
    17: "DeepSeek",
    18: "Mistral",
    19: "Cohere",
    20: "XAI",
    21: "Moonshot",
    22: "Baichuan",
    24: "Ollama",
    25: "Vertex AI",
    33: "AWS Bedrock",
}

LOG_TYPE_NAMES: dict[int, str] = {
    1: "充值",
    2: "消费",
    3: "管理",
    4: "系统",
    5: "错误",
    6: "退款",
}

# New API 角色常量：1=普通用户，10=管理员，100=超级管理员
ROLE_NAMES: dict[int, str] = {
    1: "普通用户",
    10: "管理员",
    100: "超级管理员",
}

_STATUS_CODE_RE = re.compile(r"status code\s+(\d+)", re.IGNORECASE)
# 上游 New API 已把 token key 掩码成形如 `Qqmq**********OYZJ`
_MASKED_RE = re.compile(r"^(.{2,8})\*+(.{2,6})$")


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
def mask_key(raw: str | None) -> str:
    """把明文 key 脱敏为 `前6****后4`；已含 `*` 的（上游掩码）做规范化。"""
    if not raw:
        return "****"
    s = str(raw).strip()
    if "*" in s:
        m = _MASKED_RE.match(s)
        if m:
            return "sk-" + m.group(1) + "****" + m.group(2)
        return "sk-" + s.replace("*", "****")[:24]
    if len(s) <= 10:
        return s[:2] + "****" if len(s) > 2 else "****"
    head, tail = s[:6], s[-4:]
    return f"{head}****{tail}"


def _collapse_stars(s: str) -> str:
    return re.sub(r"\*{2,}", "****", s)


# --------------------------------------------------------------------------- #
# 字段映射：New API -> 面板契约
# --------------------------------------------------------------------------- #
def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    if isinstance(value, str):
        return [p for p in (x.strip() for x in value.split(",")) if p]
    return [str(value)]


def _models_to_str(models: Any) -> str:
    if models is None:
        return ""
    if isinstance(models, str):
        return models
    return ",".join(str(m) for m in models if str(m))


def map_channel(ch: dict, key_cache: dict | None = None) -> dict:
    """New API 渠道对象 -> 面板渠道对象（契约 §4.1）。"""
    models = _as_list(ch.get("models"))
    raw_status = ch.get("status")
    panel_status = 1 if raw_status == 1 else 0
    if raw_status == 1:
        status_text = "启用"
    elif raw_status == 3:
        status_text = "自动禁用"
    elif raw_status == 2:
        status_text = "禁用"
    else:
        status_text = "未知"
    response_time = int(ch.get("response_time") or 0)
    test_time = int(ch.get("test_time") or 0)
    cid = ch.get("id")
    cached_key = (key_cache or {}).get(cid) if cid is not None else None
    return {
        "id": cid,
        "name": ch.get("name") or "",
        "type": ch.get("type"),
        "type_name": CHANNEL_TYPE_NAMES.get(ch.get("type"), f"类型 {ch.get('type')}"),
        "base_url": ch.get("base_url") or "",
        "status": panel_status,
        "status_text": status_text,
        "priority": ch.get("priority") or 0,
        "weight": ch.get("weight") or 0,
        # auto_ban=1 表示上游会在该渠道调用失败（含额度耗尽）时自动禁用它
        "auto_ban": bool(ch.get("auto_ban")),
        "group": ch.get("group") or "",
        "models": models,
        "model_count": len(models),
        "balance_updated_at": int(ch.get("balance_updated_time") or 0),
        "last_test_at": test_time,
        "last_test_ok": bool(test_time and response_time > 0),
        "last_test_latency_ms": response_time,
        "key_masked": mask_key(cached_key) if cached_key else "****",
    }


def map_channel_abnormal(ch: dict) -> dict | None:
    """异常渠道（契约 §3 的 abnormal）——状态非启用即异常。"""
    if ch.get("status") == 1:
        return None
    if ch.get("status") == 3:
        reason = "自动禁用（上游失败或额度耗尽）"
    elif ch.get("status") == 2:
        reason = "手动禁用"
    else:
        reason = "状态异常"
    return {"id": ch.get("id"), "name": ch.get("name") or "", "reason": reason}


def map_token(tok: dict) -> dict:
    """New API 令牌对象 -> 面板令牌对象（契约 §5）。"""
    raw_status = tok.get("status")
    panel_status = 1 if raw_status == 1 else 0
    if raw_status == 1:
        status_text = "启用"
    elif raw_status == 3:
        status_text = "已过期"
    elif raw_status == 4:
        status_text = "已用尽"
    else:
        status_text = "禁用"
    return {
        "id": tok.get("id"),
        "name": tok.get("name") or "",
        "key_masked": mask_key(tok.get("key")),
        "status": panel_status,
        "status_text": status_text,
        "quota": tok.get("remain_quota") or 0,
        "quota_used": tok.get("used_quota") or 0,
        "unlimited_quota": bool(tok.get("unlimited_quota")),
        "expired_time": tok.get("expired_time", -1),
        "created_at": int(tok.get("created_time") or 0),
        "accessed_at": int(tok.get("accessed_time") or 0),
    }


def map_user(user: dict) -> dict:
    role = user.get("role")
    return {
        "id": user.get("id"),
        "username": user.get("username") or "",
        "role": role,
        "role_text": ROLE_NAMES.get(role, f"角色 {role}"),
        "quota": user.get("quota") or 0,
        "quota_used": user.get("used_quota") or 0,
        "status": 1 if user.get("status") == 1 else 0,
    }


def map_log(log: dict) -> dict:
    ltype = log.get("type")
    channel_name = log.get("channel_name")
    if channel_name is None:
        channel_name = log.get("channel") or ""
    return {
        "id": log.get("id"),
        "created_at": int(log.get("created_at") or log.get("created_time") or 0),
        "type": ltype,
        "type_text": LOG_TYPE_NAMES.get(ltype, "其他"),
        "username": log.get("username") or "",
        "token_name": log.get("token_name") or "",
        "model_name": log.get("model_name") or "",
        "channel_name": channel_name or "",
        "prompt_tokens": log.get("prompt_tokens") or 0,
        "completion_tokens": log.get("completion_tokens") or 0,
        "quota": log.get("quota") or 0,
        "latency_ms": int(log.get("use_time") or log.get("response_time") or 0),
        "is_stream": bool(log.get("is_stream")),
        "content": log.get("content") or "",
    }


def map_test_result(data: dict, elapsed_ms: float) -> dict:
    """New API 渠道测试响应 -> 契约 §4.5。真实 status_code 从 message 中解析。"""
    ok = bool(data.get("success"))
    message = str(data.get("message") or "")
    status_code: int | None = 200 if ok else None
    m = _STATUS_CODE_RE.search(message)
    if m:
        status_code = int(m.group(1))
    elif not ok:
        status_code = 0
    latency = int(round(elapsed_ms))
    return {
        "ok": ok,
        "latency_ms": latency,
        "status_code": status_code,
        "message": message or ("测试成功" if ok else "测试失败"),
    }


# --------------------------------------------------------------------------- #
# 请求体映射：面板 -> New API
# --------------------------------------------------------------------------- #
def _channel_payload_to_upstream(payload: dict, *, partial: bool) -> dict:
    """把面板渠道请求体（扁平、models 为列表）转成 New API 的渠道字段。

    partial=True（更新）时仅包含请求中出现的字段；key 为空则不下发（契约 §9.4 留空=不修改）。
    """
    out: dict = {}
    mapping = {
        "name": "name",
        "type": "type",
        "base_url": "base_url",
        "priority": "priority",
        "weight": "weight",
        "group": "group",
    }
    for src, dst in mapping.items():
        if src in payload and payload[src] is not None:
            out[dst] = payload[src]
    if "models" in payload and payload["models"] is not None:
        out["models"] = _models_to_str(payload["models"])
    # key：仅在提供且非空时下发
    key = payload.get("key")
    if key:
        out["key"] = key
    if not partial:
        out.setdefault("models", "")
        out.setdefault("group", "default")
        out.setdefault("priority", 0)
        out.setdefault("weight", 1)
    return out


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class NewAPIClient:
    def __init__(self) -> None:
        self.base_url = settings.newapi_base_url
        self._token: str | None = None
        self._uid: int | None = None
        self._expires_at: int = 0
        self._lock = asyncio.Lock()
        # trust_env=False：忽略环境变量里的 HTTP(S)_PROXY，始终直连 New API。
        # 否则在带有全局代理的环境下，BFF 会误把局域网请求交给代理（曾导致死地址探测
        # 返回代理的 502 而非真实连接错误）。
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=settings.request_timeout, trust_env=False
        )
        # 渠道创建能力（三态）：None=未探测，True=可用，False=不可用
        self.channel_create_supported: bool | None = None
        self.channel_create_note: str = ""
        # 进程内缓存我们创建/更新过的渠道明文 key，仅用于生成脱敏展示，绝不下发
        self._channel_key_cache: dict[int, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def key_cache(self) -> dict[int, str]:
        """进程内缓存的渠道明文 key（仅用于生成脱敏展示，绝不下发）。"""
        return self._channel_key_cache

    # --- 鉴权 ------------------------------------------------------------- #
    async def login(self) -> None:
        try:
            resp = await self._client.post(
                "/api/user/login",
                json={
                    "username": settings.newapi_admin_user,
                    "password": settings.newapi_admin_password,
                },
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"无法连接 New API 登录接口：{exc}") from exc
        # New API 对 /api/user/login 施加 CriticalRateLimit（默认 20 次/20 分钟/IP）。
        # 被拒请求不计入窗口（见 common/rate-limit.go），因此这里快速失败、不做重试，
        # 避免无谓的等待；生产环境靠 token 缓存几乎不会触发。
        if resp.status_code == 429:
            raise UpstreamError("New API 登录被限流（HTTP 429，20 次/20 分钟/IP），请稍后重试")
        if resp.status_code != 200:
            raise UpstreamError(f"New API 登录返回 HTTP {resp.status_code}")
        body = _safe_json(resp)
        if not body.get("success"):
            raise UpstreamError(f"New API 登录失败：{body.get('message') or 'unknown'}")
        data = body.get("data") or {}
        user = data.get("user") or {}
        token = data.get("access_token")
        uid = user.get("id")
        if not token or uid is None:
            raise UpstreamError("New API 登录响应缺少 access_token / user.id")
        self._token = token
        self._uid = int(uid)
        expires = data.get("access_expires_at")
        self._expires_at = int(expires) if expires else int(time.time()) + 3600
        logger.info("New API 登录成功：user=%s uid=%s", user.get("username"), uid)

    async def _ensure_token(self) -> None:
        async with self._lock:
            if not self._token or time.time() >= self._expires_at - 60:
                await self.login()

    # --- 通用请求 --------------------------------------------------------- #
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        auth: bool = True,
        _retry: bool = True,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if auth:
            await self._ensure_token()
            headers = {
                "Authorization": f"Bearer {self._token}",
                "New-Api-User": str(self._uid),
            }
        try:
            resp = await self._client.request(method, path, json=json, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"New API 不可达：{exc}") from exc
        # access_token 失效：强制重登并重试一次（仅 401，避免权限类 403 消耗登录配额）
        if auth and resp.status_code == 401 and _retry:
            logger.warning("New API 返回 401，尝试重新登录后重试 %s %s", method, path)
            async with self._lock:
                self._token = None
                await self.login()
            return await self._request(
                method, path, json=json, params=params, auth=auth, _retry=False
            )
        if resp.status_code >= 500:
            raise UpstreamError(f"New API 返回 HTTP {resp.status_code}")
        return resp

    async def _json_request(
        self, method: str, path: str, *, json: Any = None, params: dict | None = None, auth: bool = True
    ) -> dict:
        resp = await self._request(method, path, json=json, params=params, auth=auth)
        return _safe_json(resp)

    # --- 系统 ------------------------------------------------------------- #
    async def get_status(self) -> dict:
        """公开状态接口，无需登录。"""
        return await self._json_request("GET", "/api/status", auth=False)

    async def get_options(self) -> list[dict]:
        body = await self._json_request("GET", "/api/option/")
        return body.get("data") or []

    async def update_option(self, key: str, value: str) -> dict:
        return await self._json_request("PUT", "/api/option/", json={"key": key, "value": value})

    # --- 渠道 ------------------------------------------------------------- #
    async def list_channels(self, page: int, size: int, keyword: str = "") -> dict:
        params = {"p": page, "page_size": size}
        if keyword:
            params["keyword"] = keyword
            return await self._json_request("GET", "/api/channel/search", params=params)
        return await self._json_request("GET", "/api/channel/", params=params)

    async def get_channel(self, cid: int) -> dict:
        return await self._json_request("GET", f"/api/channel/{cid}")

    async def list_models(self) -> list[dict]:
        body = await self._json_request("GET", "/api/channel/models")
        return body.get("data") or []

    async def all_channels(self, max_pages: int = 20) -> list[dict]:
        """拉取**全部**渠道（翻页直到拿完）。

        用于需要全量视图的场景：优先级排序、按协议聚合模型。
        单页失败时向上抛 UpstreamError，由调用方决定降级。
        """
        out: list[dict] = []
        page = 1
        while page <= max_pages:
            body = await self.list_channels(page, 100)
            data = body.get("data") or {}
            items = data.get("items") or []
            out.extend(items)
            total = data.get("total")
            if not items or (total is not None and len(out) >= int(total)):
                break
            page += 1
        return out

    async def probe_channel_create(self) -> tuple[bool | None, str]:
        """非破坏性探测渠道创建接口是否可用（不会真正创建渠道）。

        原理：rc.41 的 POST /api/channel/ 期望 `{mode, channel}` 包装体。
        发一个「含有效 key + 非法 mode」的包装体：
          - 返回「不支持的添加模式」→ 说明服务器认包装体（接口可用）
          - 返回「channel cannot be empty」→ 说明服务器把 channel 判空（仅接受扁平体/或缺陷版本）
        """
        probe_body = {
            "mode": "__capability_probe__",
            "channel": {
                "name": "__capability_probe__",
                "type": 1,
                "base_url": "https://example.invalid",
                "key": "sk-__capability_probe__000000000000",
                "models": "__probe__",
                "group": "default",
            },
        }
        try:
            body = await self._json_request("POST", "/api/channel/", json=probe_body)
        except UpstreamError as exc:
            return None, f"渠道创建接口探测失败：{exc}"
        message = str(body.get("message") or "")
        if body.get("success") is True:
            return True, "探测：服务器接受了未知 mode（接口似乎直接忽略 mode，创建可用）"
        if "不支持的添加模式" in message or "unsupported" in message.lower():
            return True, "探测：POST /api/channel/ 需要 {mode, channel} 包装体，渠道创建可用"
        if "channel cannot be empty" in message:
            return False, (
                "探测：POST /api/channel/ 将 {mode, channel} 包装体判为 channel cannot be empty，"
                "该版本接口不可用"
            )
        return None, f"探测：未知响应 message={message!r}"

    async def create_channel(self, payload: dict) -> int | None:
        if self.channel_create_supported is None:
            supported, note = await self.probe_channel_create()
            self.channel_create_supported = supported
            self.channel_create_note = note
        if self.channel_create_supported is False:
            raise BusinessError(CHANNEL_CREATE_DEGRADE_MSG)
        upstream_channel = _channel_payload_to_upstream(payload, partial=False)
        body = {
            "mode": "single",
            "channel": upstream_channel,
            "batch_add_set_key_prefix2name": False,
        }
        res = await self._json_request("POST", "/api/channel/", json=body)
        if not res.get("success"):
            message = str(res.get("message") or "")
            if "channel cannot be empty" in message:
                # 运行期才发现包装体不被接受 → 记下并走降级文案
                self.channel_create_supported = False
                self.channel_create_note = "运行期创建返回 channel cannot be empty，已切换为降级提示"
                raise BusinessError(CHANNEL_CREATE_DEGRADE_MSG)
            raise BusinessError(f"创建失败：{message or '未知错误'}")
        new_id = await self._find_channel_id_by_name(upstream_channel.get("name", ""))
        key = upstream_channel.get("key")
        if new_id is not None and key:
            self._channel_key_cache[new_id] = key
        return new_id

    async def _find_channel_id_by_name(self, name: str) -> int | None:
        try:
            body = await self.list_channels(1, 100, keyword=name)
        except UpstreamError:
            return None
        items = (body.get("data") or {}).get("items") or []
        matches = [c for c in items if c.get("name") == name]
        if not matches:
            return None
        return max(matches, key=lambda c: c.get("id") or 0).get("id")

    async def update_channel(self, cid: int, payload: dict) -> dict:
        upstream = _channel_payload_to_upstream(payload, partial=True)
        upstream["id"] = cid
        res = await self._json_request("PUT", "/api/channel/", json=upstream)
        if not res.get("success"):
            raise BusinessError(f"更新失败：{res.get('message') or '未知错误'}")
        key = upstream.get("key")
        if key:
            self._channel_key_cache[cid] = key
        return res.get("data") or {}

    async def delete_channel(self, cid: int) -> None:
        res = await self._json_request("DELETE", f"/api/channel/{cid}")
        if not res.get("success"):
            raise BusinessError(f"删除失败：{res.get('message') or '未知错误'}")
        self._channel_key_cache.pop(cid, None)

    async def set_channel_priority(self, cid: int, priority: int) -> None:
        """只更新渠道优先级。

        实测（rc.41）：`PUT /api/channel/` 用最小体 `{"id", "priority"}` 是**安全**的 ——
        name/models/group/weight/auto_ban/status 均保持原值不变（已在临时渠道上验证）；
        但**绝不能带 `status` 字段**，否则直接 `Invalid parameters`（见契约 §10.3）。
        """
        res = await self._json_request(
            "PUT", "/api/channel/", json={"id": cid, "priority": int(priority)}
        )
        if not res.get("success"):
            raise BusinessError(f"更新渠道 {cid} 优先级失败：{res.get('message') or '未知错误'}")

    async def set_channel_status(self, cid: int, status: int) -> None:
        # 面板 1=启用/0=禁用 -> New API 1=启用/2=禁用
        upstream_status = 1 if int(status) == 1 else 2
        res = await self._json_request(
            "POST", f"/api/channel/{cid}/status", json={"channel_id": cid, "status": upstream_status}
        )
        if not res.get("success"):
            raise BusinessError(f"启停失败：{res.get('message') or '未知错误'}")

    async def test_channel(self, cid: int, model: str | None = None) -> dict:
        params = {"model": model} if model else None
        started = time.perf_counter()
        body = await self._json_request("GET", f"/api/channel/test/{cid}", params=params)
        elapsed_ms = (time.perf_counter() - started) * 1000
        return map_test_result(body, elapsed_ms)

    # --- 令牌 ------------------------------------------------------------- #
    async def list_tokens(self, page: int, size: int) -> dict:
        return await self._json_request("GET", "/api/token/", params={"p": page, "page_size": size})

    async def create_token(self, payload: dict) -> dict:
        """创建令牌，并**一次性**取回明文 key。

        New API 的 `POST /api/token/` 既不返回令牌 id 也不返回明文 key，
        且列表/详情接口一律只回掩码（`MaskTokenKey`）。唯一能拿到明文的途径是
        `POST /api/token/{id}/key`（返回 `token.GetFullKey()`）。
        因此这里：创建 → 按 name 定位最新令牌 → 取回明文 key → 随创建响应返回一次。
        之后任何查询接口仍只会拿到掩码（契约 §5「一次性回显」）。
        """
        body = {
            "name": payload.get("name") or "",
            "remain_quota": payload.get("quota", 0),
            "expired_time": _expired_time(payload),
            "unlimited_quota": bool(payload.get("unlimited_quota")),
            "model_limits_enabled": bool(payload.get("models")),
            "model_limits": _models_to_str(payload.get("models")),
            "allow_ips": "",
            "group": payload.get("group") or "",
        }
        res = await self._json_request("POST", "/api/token/", json=body)
        if not res.get("success"):
            raise BusinessError(f"创建令牌失败：{res.get('message') or '未知错误'}")

        new_id = await self._find_newest_token_id_by_name(body["name"])
        key: str | None = None
        if new_id is not None:
            raw = await self.get_token_key(new_id)
            if raw:
                # 上游存的 key 不带 sk- 前缀；对外统一暴露带前缀的完整密钥
                key = raw if raw.startswith("sk-") else f"sk-{raw}"
        return {"id": new_id, "key": key}

    async def get_token_key(self, tid: int) -> str | None:
        """取回令牌明文 key（New API `POST /api/token/{id}/key`）。失败返回 None。"""
        try:
            res = await self._json_request("POST", f"/api/token/{tid}/key", json={})
        except UpstreamError:
            return None
        if not res.get("success"):
            return None
        return (res.get("data") or {}).get("key") or None

    async def _find_newest_token_id_by_name(self, name: str) -> int | None:
        """按 name 定位刚创建的令牌（取 id 最大者，即最新）。"""
        body = await self.list_tokens(1, 100)
        items = (body.get("data") or {}).get("items") or []
        matches = [t for t in items if (t.get("name") or "") == name]
        if not matches:
            return None
        return max(matches, key=lambda t: t.get("id") or 0).get("id")

    async def update_token(self, tid: int, payload: dict) -> dict:
        # New API 的 PUT /api/token/ 是「全量覆盖」，缺字段会被清零；先取回原值再合并。
        current = await self._find_token(tid)
        if current is None:
            raise BusinessError("令牌不存在")
        body = {
            "id": tid,
            "name": payload.get("name", current.get("name") or ""),
            "remain_quota": payload.get("quota", current.get("remain_quota") or 0),
            "expired_time": payload.get("expired_time", current.get("expired_time", -1)),
            "unlimited_quota": payload.get("unlimited_quota", current.get("unlimited_quota")),
            "model_limits_enabled": current.get("model_limits_enabled", False),
            "model_limits": current.get("model_limits") or "",
            "allow_ips": current.get("allow_ips") or "",
            "group": payload.get("group", current.get("group") or ""),
            "status": current.get("status", 1),
        }
        if "expired_days" in payload and "expired_time" not in payload:
            body["expired_time"] = _expired_time(payload)
        if "models" in payload and payload["models"] is not None:
            body["model_limits"] = _models_to_str(payload["models"])
            body["model_limits_enabled"] = bool(payload["models"])
        res = await self._json_request("PUT", "/api/token/", json=body)
        if not res.get("success"):
            raise BusinessError(f"更新令牌失败：{res.get('message') or '未知错误'}")
        return res.get("data") or {}

    async def _find_token(self, tid: int) -> dict | None:
        body = await self.list_tokens(1, 100)
        for tok in (body.get("data") or {}).get("items") or []:
            if tok.get("id") == tid:
                return tok
        return None

    async def delete_token(self, tid: int) -> None:
        res = await self._json_request("DELETE", f"/api/token/{tid}")
        if not res.get("success"):
            raise BusinessError(f"删除令牌失败：{res.get('message') or '未知错误'}")

    async def set_token_status(self, tid: int, status: int) -> None:
        upstream_status = 1 if int(status) == 1 else 2
        res = await self._json_request(
            "PUT", "/api/token/", params={"status_only": "true"}, json={"id": tid, "status": upstream_status}
        )
        if not res.get("success"):
            raise BusinessError(f"令牌启停失败：{res.get('message') or '未知错误'}")

    # --- 用户 ------------------------------------------------------------- #
    async def list_users(self) -> dict:
        return await self._json_request("GET", "/api/user/", params={"p": 1, "page_size": 100})

    # --- 日志 ------------------------------------------------------------- #
    async def list_logs(self, params: dict) -> dict:
        return await self._json_request("GET", "/api/log/", params=params)

    async def log_stat(self, log_type: int = 0) -> dict:
        body = await self._json_request("GET", "/api/log/stat", params={"type": log_type})
        return body.get("data") or {}

    # --- 分组 ------------------------------------------------------------- #
    async def list_groups(self) -> list[str]:
        body = await self._json_request("GET", "/api/group/")
        return body.get("data") or []


def _expired_time(payload: dict) -> int:
    if "expired_time" in payload and payload["expired_time"] is not None:
        return int(payload["expired_time"])
    days = payload.get("expired_days", -1)
    if days is None or int(days) < 0:
        return -1
    return int(time.time()) + int(days) * 86400


def _safe_json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(f"New API 响应非 JSON（HTTP {resp.status_code}）") from exc
    if isinstance(data, dict):
        return data
    return {"success": True, "data": data}


_client: NewAPIClient | None = None


def get_client() -> NewAPIClient:
    global _client
    if _client is None:
        _client = NewAPIClient()
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
