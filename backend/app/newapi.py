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
import json
import logging
import re
import time
from datetime import datetime
from typing import Any

import httpx

from . import pricing
from .config import settings
from .response import BusinessError, UpstreamError
from .state_store import StateStore

logger = logging.getLogger("bff.newapi")

# --------------------------------------------------------------------------- #
# 会话治理（P0-1：user_sessions 上限 50 → 409 自锁死局的根治）
# --------------------------------------------------------------------------- #
# BFF / 调试脚本登录留下的 UA。**只回收能精确匹配这些前缀的会话**，
# 浏览器会话（Mozilla/Chrome/...）一律不碰 —— 2026-10-24 实测 50 条堆满的
# 会话全是 python-httpx / Python-urllib / curl，没有一条浏览器。
_SCRIPT_UA_PREFIXES = ("python-httpx", "python-requests", "python-urllib", "curl/", "go-http-client", "wget/")
# 两次登录之间至少间隔这么久才做一次例行回收（409 自救不受此限制）。
_REAP_MIN_INTERVAL = 600.0


def _is_script_ua(user_agent: str | None) -> bool:
    ua = (user_agent or "").strip().lower()
    return bool(ua) and ua.startswith(_SCRIPT_UA_PREFIXES)

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


def _log_cache_tokens(log: dict) -> int:
    """从日志的 `other` JSON 里取缓存命中 token 数（缓存按 1/30 计价，影响金额）。"""
    other = log.get("other")
    if isinstance(other, dict):
        return int(other.get("cache_tokens") or 0)
    if isinstance(other, str) and other:
        try:
            parsed = json.loads(other)
        except ValueError:
            return 0
        if isinstance(parsed, dict):
            return int(parsed.get("cache_tokens") or 0)
    return 0


def map_log(log: dict) -> dict:
    ltype = log.get("type")
    channel_name = log.get("channel_name")
    if channel_name is None:
        channel_name = log.get("channel") or ""
    prompt_tokens = int(log.get("prompt_tokens") or 0)
    completion_tokens = int(log.get("completion_tokens") or 0)
    cache_tokens = _log_cache_tokens(log)
    return {
        "id": log.get("id"),
        "created_at": int(log.get("created_at") or log.get("created_time") or 0),
        "type": ltype,
        "type_text": LOG_TYPE_NAMES.get(ltype, "其他"),
        "username": log.get("username") or "",
        "token_name": log.get("token_name") or "",
        "model_name": log.get("model_name") or "",
        "channel_name": channel_name or "",
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_tokens": cache_tokens,
        "quota": log.get("quota") or 0,
        # 按 DeepSeek 均价现算（契约 §3.1）；`quota` 是历史记账，旧倍率下会虚高
        "cost_cny": round(
            pricing.cost_cny(prompt_tokens, completion_tokens, cache_tokens, log.get("model_name")), 6
        ),
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
        # token 持久化：重启复用，减少登录次数（登录 = 新会话，是 409 的直接推手）
        self._state = StateStore()
        self._last_reap: float = 0.0
        saved = self._state.load()
        saved_token = str(saved.get("access_token") or "")
        saved_uid = saved.get("uid")
        saved_exp = int(saved.get("expires_at") or 0)
        if saved_token and saved_uid is not None and time.time() < saved_exp - 60:
            self._token = saved_token
            self._uid = int(saved_uid)
            self._expires_at = saved_exp
            logger.info(
                "已从 %s 恢复 New API token（剩余 %d 分钟），启动无需登录",
                self._state.path, int((saved_exp - time.time()) / 60),
            )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def key_cache(self) -> dict[int, str]:
        """进程内缓存的渠道明文 key（仅用于生成脱敏展示，绝不下发）。"""
        return self._channel_key_cache

    # --- 鉴权 ------------------------------------------------------------- #
    async def startup(self) -> None:
        """启动时尽量就绪 token：优先复用持久化的有效 token，否则登录一次。

        **绝不抛异常** —— 上游暂时不可达不能挡住 BFF 启动（首次真实请求时
        `_ensure_token` 会再试）。
        """
        if self._token and time.time() < self._expires_at - 60:
            return
        try:
            await self.login()
        except Exception as exc:  # noqa: BLE001 - 启动失败不影响进程
            logger.warning("启动时 New API 登录未成功（推迟到首个请求时再试）：%s", exc)

    async def login(self) -> None:
        resp = await self._post_login()
        # New API 对 /api/user/login 施加 CriticalRateLimit（默认 20 次/20 分钟/IP）。
        # 被拒请求不计入窗口（见 common/rate-limit.go），因此这里快速失败、不做重试，
        # 避免无谓的等待；生产环境靠 token 缓存几乎不会触发。
        if resp.status_code == 429:
            raise UpstreamError("New API 登录被限流（HTTP 429，20 次/20 分钟/IP），请稍后重试")
        # 409 = 每用户会话数达上限（AUTH_SESSION_LIMIT，实测上限 50，会话 30 天才过期
        # 且无自动清理）。死局在于：登录失败拿不到 token，没 token 就调不了清理接口。
        # 破局点：**旧的 access_token 通常仍然有效**（上限只挡「新建」不废「已有」），
        # 直接用它回收自己的脚本会话腾出名额，再重登一次。
        if resp.status_code == 409:
            logger.warning("New API 登录返回 409（会话数达上限），尝试用现有凭证自救清理…")
            reaped = await self._reap_own_sessions_quietly(force=True)
            if reaped > 0:
                resp = await self._post_login()
            if resp.status_code == 409:
                raise UpstreamError(
                    "New API 会话数已达上限（HTTP 409 AUTH_SESSION_LIMIT，每用户 50 个），"
                    "自动清理未腾出名额。请在 New API 原生面板（:18083）登录后手动清理会话，"
                    "或参考交接文档执行数据库级应急清理"
                )
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
        # 立刻落盘：重启/重建容器后可复用，少一次登录就少一条 30 天寿命的会话
        self._state.save(
            {
                "access_token": token,
                "uid": self._uid,
                "expires_at": self._expires_at,
                "saved_at": int(time.time()),
                "base_url": self.base_url,
            }
        )
        # 登录成功就回收「自己」的旧脚本会话，把 user_sessions 摁在个位数（根治 409）
        await self._reap_own_sessions_quietly()

    async def _post_login(self) -> httpx.Response:
        try:
            return await self._client.post(
                "/api/user/login",
                json={
                    "username": settings.newapi_admin_user,
                    "password": settings.newapi_admin_password,
                },
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"无法连接 New API 登录接口：{exc}") from exc

    async def _authed_raw(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        uid: int | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """带当前（或指定）凭证的裸请求。

        与 `_request` 的区别：**不做** `_ensure_token`、**不做** 401 自动重登 ——
        供「会话回收」「健康探测」使用，绝不能反过来再触发登录（登录 = 新会话，
        会话回收过程中再登录只会火上浇油；健康检查每 30s 一次更不能打登录接口）。
        """
        use_token = token if token is not None else self._token
        use_uid = uid if uid is not None else self._uid
        headers = {}
        if use_token:
            headers = {"Authorization": f"Bearer {use_token}", "New-Api-User": str(use_uid or "")}
        try:
            return await self._client.request(method, path, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"New API 不可达：{exc}") from exc

    async def _list_sessions(self) -> list[dict]:
        """GET /api/user/sessions（2026-10-24 实测）→ data[]：
        {sid, current, login_method, ip, user_agent, created_at, last_active_at, expires_at}"""
        resp = await self._authed_raw("GET", "/api/user/sessions")
        if resp.status_code != 200:
            raise UpstreamError(f"获取会话列表返回 HTTP {resp.status_code}")
        body = _safe_json(resp)
        data = body.get("data")
        return [s for s in data if isinstance(s, dict)] if isinstance(data, list) else []

    async def _reap_own_sessions(self) -> int:
        """回收「自己」的历史脚本会话，返回成功回收的条数。

        规则（保守优先）：`current=true` 跳过；user_agent 不是已知脚本前缀的跳过
        （浏览器会话绝不能误杀）。逐条 `DELETE /api/user/sessions/{sid}`。
        """
        sessions = await self._list_sessions()
        reaped = 0
        for s in sessions:
            if s.get("current"):
                continue
            if not _is_script_ua(s.get("user_agent")):
                continue
            sid = s.get("sid")
            if not sid:
                continue
            resp = await self._authed_raw("DELETE", f"/api/user/sessions/{sid}")
            if resp.status_code == 200:
                reaped += 1
            else:
                logger.warning("回收会话 %s 返回 HTTP %s", sid[:8], resp.status_code)
        return reaped

    async def _reap_own_sessions_quietly(self, *, force: bool = False) -> int:
        """回收失败只记日志、绝不抛 —— 会话治理是保险，不能反过来弄挂登录。"""
        if not self._token:
            return 0
        if not force and (time.time() - self._last_reap) < _REAP_MIN_INTERVAL:
            return 0
        self._last_reap = time.time()
        try:
            reaped = await self._reap_own_sessions()
            if reaped:
                logger.info("已回收 %d 条历史脚本会话（user_sessions 限额保险）", reaped)
            return reaped
        except Exception as exc:  # noqa: BLE001
            logger.warning("历史脚本会话回收失败（不影响主流程）：%s", exc)
            return 0

    def auth_snapshot(self) -> tuple[str, int]:
        """本地视角的 token 状态（无网络请求）：("ok"|"expired"|"none", 剩余秒数)。"""
        if not self._token:
            return "none", 0
        remaining = self._expires_at - int(time.time())
        return ("ok" if remaining > 60 else "expired"), max(remaining, 0)

    async def probe_auth(self) -> str:
        """探测当前 token 在服务端是否仍有效（一次真实鉴权请求）。

        返回 "ok"（200）/"invalid"（401/403）/"none"（无 token）/"unreachable"。
        **绝不触发登录** —— 供健康检查高频调用；3 秒短超时（容器健康检查整体
        只给 5 秒，探测不能占满）。
        """
        if not self._token:
            return "none"
        try:
            resp = await self._authed_raw("GET", "/api/user/sessions", timeout=3.0)
        except UpstreamError:
            return "unreachable"
        if resp.status_code == 200:
            return "ok"
        if resp.status_code in (401, 403):
            return "invalid"
        return "unreachable"

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

        ⚠️ 它只改 `channels.priority`，**不会**同步 `abilities` 表 —— 见 `fix_abilities()`。
        """
        res = await self._json_request(
            "PUT", "/api/channel/", json={"id": cid, "priority": int(priority)}
        )
        if not res.get("success"):
            raise BusinessError(f"更新渠道 {cid} 优先级失败：{res.get('message') or '未知错误'}")

    async def fix_abilities(self) -> dict:
        """重建 `abilities` 表（渠道-模型路由表）。

        **为什么必须调**：New API 的路由读的是 `abilities`，不是 `channels`。
        `PUT /api/channel/` 只写 `channels.priority`，`abilities` 里那些早先建的
        行会保留旧值 —— 于是出现「面板显示优先级 3，实际按旧值随机」的假象。
        2026-10-18 实测：6 个渠道在 `abilities` 里对 `deepseek-v4.1-flash-1017`
        全是 `priority=1`，导致优先级形同虚设、请求被随机打到 Anthropic 渠道。

        该接口 = 清空并重建 `abilities`（逐渠道按 `channels` 当前值写入），幂等。
        """
        res = await self._json_request("POST", "/api/channel/fix", json={})
        if not res.get("success"):
            raise BusinessError(f"重建 abilities 失败：{res.get('message') or '未知错误'}")
        return res.get("data") or {}

    async def sync_model_ratios(self) -> dict:
        """把我们渠道上每个模型按 DeepSeek 均价写入三个倍率 option（契约 §3.1）。

        未配价的模型会被 New API 按硬编码 37.5 计费（≈$75/M，高出两个数量级），
        所以新增模型后必须重新同步一次（BFF 启动时会自动做一次）。

        合并写入：只覆盖我们渠道里出现的模型名，上游内置的 200+ 条价格原样保留。
        """
        channels = await self.all_channels()
        models = sorted(
            {
                m.strip()
                for c in channels
                for m in (c.get("models") or "").split(",")
                if m.strip()
            }
        )
        changed: dict[str, int] = {}
        if not models:
            return {"models": 0, "changed": changed}

        opts = {o.get("key"): o.get("value") for o in await self.get_options()}
        built = pricing.build_ratio_options(models)
        for key, mapping in zip(("ModelRatio", "CompletionRatio", "CacheRatio"), built):
            try:
                current = json.loads(opts.get(key) or "{}")
            except ValueError:
                current = {}
            if not isinstance(current, dict):
                current = {}
            merged = dict(current)
            delta = 0
            for name, value in mapping.items():
                if merged.get(name) != value:
                    merged[name] = value
                    delta += 1
            if delta:
                await self.update_option(key, json.dumps(merged, ensure_ascii=False))
            changed[key] = delta
        return {"models": len(models), "changed": changed}

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

    async def today_consumption_logs(self) -> list[dict]:
        """今日（本地时间 00:00 起）的消费日志，按令牌/模型聚合 token 用量用。

        ⚠️ 上游单页上限 500 条；日志量更大时结果会低估（本项目规模足够）。
        """
        midnight = int(
            datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        )
        data = (
            await self.list_logs(
                {
                    "p": 1,
                    "page_size": 500,
                    "type": 2,
                    "start_timestamp": midnight,
                    "end_timestamp": int(time.time()),
                }
            )
        ).get("data") or {}
        return data.get("items") or []

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
