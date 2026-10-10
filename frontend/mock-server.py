#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
frontend/mock-server.py
========================
AI 中转站管理面板的 **离线 mock 后端**。

目的：在真实 FastAPI BFF（Agent A）尚未就绪时，让前端可以完整演示与验收。
- 仅依赖 Python 标准库（http.server），无第三方依赖
- 同时托管 frontend/ 下的静态文件（index.html / style.css / app.js）
- 按 docs/API_CONTRACT.md 实现全部 /panel-api/* 接口，返回样例数据
- 使用 HttpOnly Cookie(panel_session) 模拟登录态
- 渠道/令牌/日志的状态存于内存，支持编辑、启停、删除等演示

运行：
    python3 frontend/mock-server.py
    # 或指定端口
    python3 frontend/mock-server.py --port 8080
然后浏览器打开 http://127.0.0.1:8080/

演示账号（可用环境变量 MOCK_USER / MOCK_PASSWORD 覆盖）：
    用户名 LzK    密码 panel123

说明：本文件仅供前端本地演示，**不是**生产后端，不接任何真实上游。
"""

import argparse
import json
import os
import random
import secrets
import sys
import time
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# ---------------------------------------------------------------- 配置
HERE = os.path.dirname(os.path.abspath(__file__))

MOCK_USER = os.environ.get("MOCK_USER", "LzK")
MOCK_PASSWORD = os.environ.get("MOCK_PASSWORD", "panel123")

# 契约 §4.3：New API rc.41 的 POST /api/channel/ 存在缺陷，任何请求体都返回
# "channel cannot be empty"。置 True 以**忠实复现**该降级行为（前端会弹出契约里
# 定义的中文引导 message）；置 False 则 mock 真实创建一条渠道，方便演示成功路径。
SIMULATE_CHANNEL_CREATE_DEFECT = True

START_TS = int(time.time())
SESSION_COOKIE = "panel_session"
SESSIONS = set()  # 有效会话 token 集合（内存，重启即失效）

CHANNEL_DEFECT_MSG = (
    "创建失败：上游接口返回 channel cannot be empty"
    "（New API rc.41 已知缺陷，请改用面板内【从模板批量创建】或手工在 New API 中建立）"
)

# ---------------------------------------------------------------- 样例数据
CHANNEL_TYPES = [
    {"id": 1, "name": "OpenAI"},
    {"id": 14, "name": "Anthropic"},
    {"id": 3, "name": "Azure"},
    {"id": 8, "name": "Gemini"},
]

MODELS = [
    {"id": "deepseek-v4.1-flash", "owned_by": "tokendance"},
    {"id": "spark-x2.5-4b", "owned_by": "tokendance"},
    {"id": "claude-3-5-sonnet", "owned_by": "anthropic"},
    {"id": "gpt-4o-mini", "owned_by": "openai"},
]


def _ts(days=0, hours=0, minutes=0):
    return int(time.time()) - days * 86400 - hours * 3600 - minutes * 60


def seed_channels():
    raw = [
        (1, "TK-LzK-openai", 1, "https://tokendance.space/gateway", 1, 0, 1, "default",
         ["deepseek-v4.1-flash", "spark-x2.5-4b"], True, 320),
        (2, "TK-LzK-anthropic", 14, "https://tokendance.space/gateway", 1, 0, 1, "default",
         ["claude-3-5-sonnet"], True, 410),
        (3, "TK-张老师-openai", 1, "https://tokendance.space/gateway", 1, 0, 1, "default",
         ["gpt-4o-mini"], False, 0),
        (4, "TK-李同学-azure", 3, "https://tokendance.space/gateway", 0, 0, 1, "default",
         ["gpt-4o-mini"], False, 0),
        (5, "TK-王老师-gemini", 8, "https://tokendance.space/gateway", 1, 0, 1, "default",
         ["deepseek-v4.1-flash"], True, 233),
        (6, "TK-备份通道-A", 1, "https://tokendance.space/gateway", 1, 0, 1, "backup",
         ["deepseek-v4.1-flash"], True, 512),
        (7, "TK-备份通道-B", 1, "https://tokendance.space/gateway", 0, 0, 1, "backup",
         ["gpt-4o-mini"], False, 0),
        (8, "TK-赵同学-anthropic", 14, "https://tokendance.space/gateway", 1, 0, 1, "default",
         ["claude-3-5-sonnet"], True, 388),
    ]
    out = []
    for (cid, name, typ, url, status, prio, weight, group, models, ok, lat) in raw:
        out.append({
            "id": cid,
            "name": name,
            "type": typ,
            "type_name": _type_name(typ),
            "base_url": url,
            "status": status,
            "status_text": "启用" if status == 1 else "禁用",
            "priority": prio,
            "weight": weight,
            "group": group,
            "models": models,
            "model_count": len(models),
            "balance_updated_at": _ts(hours=2),
            "last_test_at": _ts(minutes=13) if ok else _ts(hours=5),
            "last_test_ok": ok,
            "last_test_latency_ms": lat if ok else 0,
            "key_masked": "sk-abcd****wxyz",
        })
    return out


def _type_name(tid):
    for t in CHANNEL_TYPES:
        if t["id"] == tid:
            return t["name"]
    return "类型 %s" % tid


def seed_tokens():
    raw = [
        (1, "LzK", 1, 1000, 12.34, False),
        (2, "张老师", 1, 100, 96.5, False),
        (3, "李同学", 1, 0, 3.2, True),
        (4, "王老师", 0, 50, 50.0, False),
    ]
    out = []
    for (tid, name, status, quota, used, unlimited) in raw:
        out.append({
            "id": tid,
            "name": name,
            "key_masked": "sk-%s****%s" % (name.lower()[:4], str(tid) * 4),
            "status": status,
            "status_text": "启用" if status == 1 else "禁用",
            "quota": quota,
            "quota_used": used,
            "unlimited_quota": unlimited,
            "expired_time": -1,
            "created_at": _ts(days=30),
            "accessed_at": _ts(minutes=7),
        })
    return out


def seed_logs(count=137):
    rnd = random.Random(42)
    model_pool = ["deepseek-v4.1-flash", "spark-x2.5-4b", "claude-3-5-sonnet", "gpt-4o-mini"]
    user_pool = ["LzK", "张老师", "李同学", "王老师"]
    chan_pool = ["TK-LzK-openai", "TK-LzK-anthropic", "TK-王老师-gemini", "TK-备份通道-A"]
    type_pool = [(2, "消费"), (2, "消费"), (2, "消费"), (1, "充值"), (5, "错误")]
    out = []
    base = int(time.time())
    for i in range(count):
        typ, typ_text = type_pool[rnd.randrange(len(type_pool))]
        pt = rnd.randrange(10, 900)
        ct = rnd.randrange(5, 600)
        out.append({
            "id": 20000 - i,
            "created_at": base - i * 137,
            "type": typ,
            "type_text": typ_text,
            "username": user_pool[rnd.randrange(len(user_pool))],
            "token_name": user_pool[rnd.randrange(len(user_pool))],
            "model_name": model_pool[rnd.randrange(len(model_pool))],
            "channel_name": chan_pool[rnd.randrange(len(chan_pool))],
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "quota": round(rnd.uniform(0.001, 0.08), 4),
            "latency_ms": rnd.randrange(180, 2400),
            "is_stream": rnd.random() < 0.6,
            "content": "",
        })
    return out


CHANNELS = seed_channels()
TOKENS = seed_tokens()
LOGS = seed_logs()
_next_channel_id = max([c["id"] for c in CHANNELS] or [0]) + 1
_next_token_id = max([t["id"] for t in TOKENS] or [0]) + 1


# ---------------------------------------------------------------- 工具
def wrap(data=None, success=True, message=""):
    return {"success": success, "message": message, "data": data if success else None}


def log_stats_payload(days=7):
    rnd = random.Random(7)
    daily = []
    today = datetime.now()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        daily.append({
            "date": d.strftime("%Y-%m-%d"),
            "requests": rnd.randrange(400, 1600),
            "tokens": rnd.randrange(800000, 4200000),
            "quota": round(rnd.uniform(3.0, 18.0), 2),
        })
    by_model = [
        {"model_name": "deepseek-v4.1-flash", "requests": 800, "tokens": 2000000, "quota": 8.0},
        {"model_name": "spark-x2.5-4b", "requests": 300, "tokens": 900000, "quota": 3.2},
        {"model_name": "claude-3-5-sonnet", "requests": 120, "tokens": 400000, "quota": 2.1},
        {"model_name": "gpt-4o-mini", "requests": 64, "tokens": 210000, "quota": 1.04},
    ]
    by_channel = [
        {"channel_name": "TK-LzK-openai", "requests": 640, "quota": 6.0},
        {"channel_name": "TK-LzK-anthropic", "requests": 300, "quota": 3.4},
    ]
    return {"daily": daily, "by_model": by_model, "by_channel": by_channel}


def overview_payload():
    enabled = sum(1 for c in CHANNELS if c["status"] == 1)
    total = len(CHANNELS)
    abnormal = [
        {"id": c["id"], "name": c["name"], "reason": "额度耗尽"}
        for c in CHANNELS if not c["last_test_ok"] and c["status"] == 1
    ]
    tk_enabled = sum(1 for t in TOKENS if t["status"] == 1)
    today_logs = [l for l in LOGS if l["created_at"] >= START_TS - (START_TS % 86400)]
    return {
        "channels": {
            "total": total,
            "enabled": enabled,
            "disabled": total - enabled,
            "abnormal": abnormal,
        },
        "tokens": {"total": len(TOKENS), "enabled": tk_enabled, "disabled": len(TOKENS) - tk_enabled},
        "today": {
            "requests": len(today_logs) if today_logs else 1284,
            "tokens": 3510203,
            "quota_spent": 12.34,
            "quota_currency": "CNY",
        },
        "upstream": {"name": "tokendance.space", "reachable": True, "latency_ms": 95},
        "panel": {"version": "0.1.0", "uptime_seconds": int(time.time()) - START_TS},
    }


# ---------------------------------------------------------------- Handler
class Handler(BaseHTTPRequestHandler):
    server_version = "ai-relay-panel-mock/0.1"
    protocol_version = "HTTP/1.1"

    # ---- 日志 ----
    def log_message(self, fmt, *args):
        sys.stdout.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- 基础发送 ----
    def _send(self, code, body_bytes, content_type, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body_bytes)

    def send_json(self, code, obj, extra_headers=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra_headers)

    def send_ok(self, data, message=""):
        self.send_json(200, wrap(data, True, message))

    def send_biz_fail(self, message):
        # 契约 §1.2：业务失败 → 200 + success:false
        self.send_json(200, wrap(None, False, message))

    def send_401(self, message="未登录或会话已过期"):
        self.send_json(401, wrap(None, False, message))

    def send_static(self, filename, content_type):
        path = os.path.join(HERE, filename)
        if not os.path.isfile(path):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, content_type)

    # ---- 解析 ----
    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _query(self, parsed):
        return {k: v[0] for k, v in parse_qs(parsed.query).items()}

    def _cookie_token(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        try:
            c = SimpleCookie()
            c.load(raw)
            if SESSION_COOKIE in c:
                return c[SESSION_COOKIE].value
        except Exception:
            return None
        return None

    def _authed(self):
        tok = self._cookie_token()
        return bool(tok and tok in SESSIONS)

    # ---- 路由 ----
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = self._query(parsed)

        # 静态资源
        if path == "/" or path == "/index.html":
            return self.send_static("index.html", "text/html; charset=utf-8")
        if path == "/style.css":
            return self.send_static("style.css", "text/css; charset=utf-8")
        if path == "/app.js":
            return self.send_static("app.js", "application/javascript; charset=utf-8")
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")

        if not path.startswith("/panel-api/"):
            return self._send(404, b"not found", "text/plain; charset=utf-8")

        # 免登录：健康检查
        if path == "/panel-api/system/health":
            return self.send_json(200, wrap({
                "bff": "ok", "upstream": "ok", "upstream_latency_ms": 12
            }))

        # 会话探测：未登录必须 401
        if path == "/panel-api/auth/session":
            if not self._authed():
                return self.send_401()
            return self.send_json(200, wrap({
                "authenticated": True,
                "username": MOCK_USER,
                "expires_at": int(time.time()) + 7 * 86400,
            }))

        # 其余接口需登录
        if not self._authed():
            return self.send_401()

        if path == "/panel-api/overview":
            return self.send_ok(overview_payload())

        if path == "/panel-api/channels/types":
            return self.send_ok(CHANNEL_TYPES)

        if path == "/panel-api/channels/models":
            return self.send_ok(MODELS)

        if path == "/panel-api/channels":
            return self._channels_list(q)

        if path.startswith("/panel-api/channels/"):
            cid = self._tail_int(path)
            if cid is None:
                return self.send_biz_fail("渠道 ID 非法")
            ch = self._find_channel(cid)
            if not ch:
                return self.send_biz_fail("渠道不存在")
            return self.send_ok(ch)

        if path == "/panel-api/tokens":
            return self._tokens_list(q)

        if path == "/panel-api/users":
            return self.send_ok({
                "items": [{
                    "id": 1, "username": MOCK_USER, "role": 100, "role_text": "管理员",
                    "quota": 100000000, "quota_used": 0, "status": 1,
                }],
                "total": 1,
            })

        if path == "/panel-api/logs":
            return self._logs_list(q)

        if path == "/panel-api/logs/stats":
            days = int(q.get("days", "7") or "7")
            return self.send_ok(log_stats_payload(days))

        if path == "/panel-api/system/info":
            return self.send_ok({
                "version": "0.1.0",
                "upstream_base_url": "https://tokendance.space/gateway",
                "started_at": START_TS,
                "uptime_seconds": int(time.time()) - START_TS,
            })

        if path == "/panel-api/system/options":
            return self.send_ok({
                "SiteName": "AI 中转站",
                "UpstreamBaseURL": "https://tokendance.space/gateway",
                "SMTPHost": "smtp.example.com",
                "SMTPPort": 587,
                "SMTPFrom": "noreply@91666.icu",
                "SMTPToken": "***",          # 脱敏
                "NewAPIAdminPassword": "***",  # 脱敏
            })

        return self.send_json(404, wrap(None, False, "接口不存在"))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()

        if path == "/panel-api/auth/login":
            return self._login(body)
        if path == "/panel-api/auth/logout":
            SESSIONS.discard(self._cookie_token())
            return self.send_json(200, wrap(None), {"Set-Cookie":
                "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % SESSION_COOKIE})

        if not self._authed():
            return self.send_401()

        if path == "/panel-api/channels":
            return self._channel_create(body)

        if path.startswith("/panel-api/channels/") and path.endswith("/status"):
            cid = self._mid_int(path)
            return self._channel_status(cid, body)

        if path.startswith("/panel-api/channels/") and path.endswith("/test"):
            cid = self._mid_int(path)
            return self._channel_test(cid, body)

        if path == "/panel-api/tokens":
            return self._token_create(body)

        if path.startswith("/panel-api/tokens/") and path.endswith("/status"):
            tid = self._mid_int(path)
            return self._token_status(tid, body)

        return self.send_json(404, wrap(None, False, "接口不存在"))

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_body()
        if not self._authed():
            return self.send_401()

        if path == "/panel-api/system/options":
            return self.send_ok(None, "设置已更新（mock 未持久化）")

        if path.startswith("/panel-api/channels/"):
            cid = self._tail_int(path)
            return self._channel_update(cid, body)

        if path.startswith("/panel-api/tokens/"):
            tid = self._tail_int(path)
            return self._token_update(tid, body)

        return self.send_json(404, wrap(None, False, "接口不存在"))

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._authed():
            return self.send_401()

        if path.startswith("/panel-api/channels/"):
            return self._channel_delete(self._tail_int(path))
        if path.startswith("/panel-api/tokens/"):
            return self._token_delete(self._tail_int(path))

        return self.send_json(404, wrap(None, False, "接口不存在"))

    # ---- 段解析 ----
    @staticmethod
    def _tail_int(path):
        seg = path.rstrip("/").split("/")[-1]
        try:
            return int(seg)
        except ValueError:
            return None

    @staticmethod
    def _mid_int(path):
        # /panel-api/channels/{id}/status -> id
        seg = [s for s in path.split("/") if s]
        try:
            return int(seg[2])
        except (IndexError, ValueError):
            return None

    # ---- 认证 ----
    def _login(self, body):
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        if username == MOCK_USER and password == MOCK_PASSWORD:
            tok = secrets.token_urlsafe(32)
            SESSIONS.add(tok)
            cookie = "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax" % (
                SESSION_COOKIE, tok, 7 * 86400)
            return self.send_json(200, wrap({
                "username": MOCK_USER,
                "expires_at": int(time.time()) + 7 * 86400,
            }), {"Set-Cookie": cookie})
        return self.send_biz_fail("用户名或密码错误")

    # ---- 渠道 ----
    def _find_channel(self, cid):
        for c in CHANNELS:
            if c["id"] == cid:
                return c
        return None

    def _channels_list(self, q):
        page = int(q.get("page", "1") or "1")
        size = int(q.get("size", "50") or "50")
        keyword = (q.get("keyword", "") or "").strip().lower()
        status = q.get("status", "")
        items = CHANNELS
        if keyword:
            items = [c for c in items if keyword in c["name"].lower()]
        if status != "":
            items = [c for c in items if str(c["status"]) == str(status)]
        total = len(items)
        start = (page - 1) * size
        return self.send_ok({
            "items": items[start:start + size],
            "total": total, "page": page, "size": size,
        })

    def _channel_create(self, body):
        global _next_channel_id
        if SIMULATE_CHANNEL_CREATE_DEFECT:
            return self.send_biz_fail(CHANNEL_DEFECT_MSG)
        name = str(body.get("name", "")).strip()
        if not name:
            return self.send_biz_fail("渠道名不能为空")
        cid = _next_channel_id
        _next_channel_id += 1
        typ = int(body.get("type", 1) or 1)
        models = body.get("models") or []
        CHANNELS.append({
            "id": cid, "name": name, "type": typ, "type_name": _type_name(typ),
            "base_url": str(body.get("base_url", "")), "status": 1, "status_text": "启用",
            "priority": int(body.get("priority", 0) or 0),
            "weight": int(body.get("weight", 1) or 1),
            "group": str(body.get("group", "default") or "default"),
            "models": models, "model_count": len(models),
            "balance_updated_at": int(time.time()), "last_test_at": 0,
            "last_test_ok": False, "last_test_latency_ms": 0,
            "key_masked": "sk-****",
        })
        return self.send_ok({"id": cid})

    def _channel_update(self, cid, body):
        ch = self._find_channel(cid)
        if not ch:
            return self.send_biz_fail("渠道不存在")
        for k in ("name", "base_url", "group"):
            if k in body:
                ch[k] = str(body[k])
        if "type" in body:
            ch["type"] = int(body["type"])
            ch["type_name"] = _type_name(ch["type"])
        if "priority" in body:
            ch["priority"] = int(body["priority"] or 0)
        if "weight" in body:
            ch["weight"] = int(body["weight"] or 1)
        if "models" in body:
            ch["models"] = body["models"] or []
            ch["model_count"] = len(ch["models"])
        # key 若留空（未传）则不修改；此处 mock 仅记录掩码
        if body.get("key"):
            ch["key_masked"] = "sk-****updated"
        return self.send_ok(None, "渠道已更新")

    def _channel_status(self, cid, body):
        ch = self._find_channel(cid)
        if not ch:
            return self.send_biz_fail("渠道不存在")
        status = int(body.get("status", 1))
        ch["status"] = status
        ch["status_text"] = "启用" if status == 1 else "禁用"
        return self.send_ok(None, "状态已更新")

    def _channel_test(self, cid, body):
        ch = self._find_channel(cid)
        if not ch:
            return self.send_biz_fail("渠道不存在")
        # 确定性模拟：禁用渠道 / 张老师那条 -> 失败
        if ch["status"] != 1:
            ok, code, msg = False, 403, "渠道已禁用，无法测试"
        elif ch["name"] == "TK-张老师-openai":
            ok, code, msg = False, 403, "上游返回 403：额度耗尽"
        else:
            ok, code, msg = True, 200, "测试成功"
        latency = random.Random(ch["id"]).randrange(180, 900)
        ch["last_test_at"] = int(time.time())
        ch["last_test_ok"] = ok
        ch["last_test_latency_ms"] = latency if ok else 0
        return self.send_ok({
            "ok": ok,
            "latency_ms": latency if ok else 0,
            "status_code": code,
            "message": msg,
        })

    def _channel_delete(self, cid):
        ch = self._find_channel(cid)
        if not ch:
            return self.send_biz_fail("渠道不存在")
        CHANNELS.remove(ch)
        return self.send_ok(None, "渠道已删除")

    # ---- 令牌 ----
    def _find_token(self, tid):
        for t in TOKENS:
            if t["id"] == tid:
                return t
        return None

    def _tokens_list(self, q):
        page = int(q.get("page", "1") or "1")
        size = int(q.get("size", "100") or "100")
        start = (page - 1) * size
        return self.send_ok({
            "items": TOKENS[start:start + size],
            "total": len(TOKENS), "page": page, "size": size,
        })

    def _token_create(self, body):
        global _next_token_id
        name = str(body.get("name", "")).strip()
        if not name:
            return self.send_biz_fail("令牌名不能为空")
        tid = _next_token_id
        _next_token_id += 1
        TOKENS.append({
            "id": tid, "name": name,
            "key_masked": "sk-%s****%s" % (name.lower()[:4], str(tid) * 4),
            "status": 1, "status_text": "启用",
            "quota": float(body.get("quota", 0) or 0), "quota_used": 0.0,
            "unlimited_quota": bool(body.get("unlimited_quota", False)),
            "expired_time": -1, "created_at": int(time.time()), "accessed_at": 0,
        })
        return self.send_ok({"id": tid})

    def _token_update(self, tid, body):
        tk = self._find_token(tid)
        if not tk:
            return self.send_biz_fail("令牌不存在")
        if "name" in body:
            tk["name"] = str(body["name"])
        if "quota" in body:
            tk["quota"] = float(body["quota"] or 0)
        if "unlimited_quota" in body:
            tk["unlimited_quota"] = bool(body["unlimited_quota"])
        return self.send_ok(None, "令牌已更新")

    def _token_status(self, tid, body):
        tk = self._find_token(tid)
        if not tk:
            return self.send_biz_fail("令牌不存在")
        status = int(body.get("status", 1))
        tk["status"] = status
        tk["status_text"] = "启用" if status == 1 else "禁用"
        return self.send_ok(None, "状态已更新")

    def _token_delete(self, tid):
        tk = self._find_token(tid)
        if not tk:
            return self.send_biz_fail("令牌不存在")
        TOKENS.remove(tk)
        return self.send_ok(None, "令牌已删除")

    # ---- 日志 ----
    def _logs_list(self, q):
        page = int(q.get("page", "1") or "1")
        size = int(q.get("size", "50") or "50")
        typ = q.get("type", "")
        keyword = (q.get("keyword", "") or "").strip().lower()
        items = LOGS
        if typ != "":
            items = [l for l in items if str(l["type"]) == str(typ)]
        if keyword:
            def hit(l):
                return (keyword in str(l["model_name"]).lower()
                        or keyword in str(l["username"]).lower()
                        or keyword in str(l["channel_name"]).lower())
            items = [l for l in items if hit(l)]
        total = len(items)
        start = (page - 1) * size
        return self.send_ok({
            "items": items[start:start + size],
            "total": total, "page": page, "size": size,
        })


def main():
    parser = argparse.ArgumentParser(description="ai-relay-panel 前端离线 mock 后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)

    print("=" * 62)
    print(" ai-relay-panel 离线 Mock 后端已启动")
    print("  面板地址 : %s" % url)
    print("  演示账号 : %s / %s" % (MOCK_USER, MOCK_PASSWORD))
    print("  静态目录 : %s" % HERE)
    print("  渠道创建 : %s" % ("模拟 rc.41 缺陷（返回引导 message）"
                              if SIMULATE_CHANNEL_CREATE_DEFECT else "正常创建"))
    print("  停止服务 : Ctrl+C")
    print("=" * 62)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
