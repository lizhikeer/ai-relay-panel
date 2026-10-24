"""会话治理单元测试（P0-1：409 自锁死局的根治逻辑）。

全部用 httpx.MockTransport 假上游，**不触网、不消耗登录限流**：
- UA 匹配规则：只回收脚本会话，浏览器会话绝不误杀
- 登录成功 → 回收旧脚本会话（跳过 current 与浏览器）→ token 落盘
- 重启复用：NewAPIClient 重建后直接用落盘 token，不发登录请求
- 409 自救：登录被拒时用旧 token 清理腾位后重登
- 健康探测 probe_auth 不触发登录

注：为避免引入 pytest-asyncio 依赖，用 asyncio.run() 包装异步用例。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import time

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import newapi as nw  # noqa: E402
from app.state_store import StateStore  # noqa: E402


# --------------------------------------------------------------------------- #
# UA 匹配
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "ua,want",
    [
        ("python-httpx/0.27.0", True),
        ("Python-urllib/3.11", True),  # lower() 后匹配 python-urllib
        ("curl/7.88.1", True),
        ("Go-http-client/2.0", True),
        ("Mozilla/5.0 (Windows NT 10.0) Chrome/126.0", False),
        ("Mozilla/5.0 python-httpx/0.27", False),  # 前缀不符（Mozilla 开头）→ 不回收
        ("", False),
        (None, False),
    ],
)
def test_is_script_ua(ua, want):
    assert nw._is_script_ua(ua) is want


# --------------------------------------------------------------------------- #
# StateStore
# --------------------------------------------------------------------------- #
def test_state_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_STATE_FILE", str(tmp_path / "state.json"))
    st = StateStore()
    assert st.path == tmp_path / "state.json"
    assert st.load() == {}
    assert st.save({"access_token": "abc", "uid": 1}) is True
    loaded = st.load()
    assert loaded["access_token"] == "abc"
    # 权限 600（POSIX；Windows 上跳过该断言）
    import os

    if os.name == "posix":
        assert (oct((tmp_path / "state.json").stat().st_mode)[-3:]) == "600"


def test_state_store_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_STATE_FILE", str(tmp_path / "state.json"))
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    st = StateStore()
    assert st.load() == {}


def test_state_store_unwritable_dir(monkeypatch):
    monkeypatch.setenv("PANEL_STATE_FILE", "/proc/definitely/not/writable/state.json")
    st = StateStore()
    # 目录不可写 → path 降级为 None，save/load 全部安全失效
    if st.path is not None:  # 以 root 跑测试时 /proc 下也可能建出来，此时跳过
        pytest.skip("当前进程有权限写入该路径")


# --------------------------------------------------------------------------- #
# 假上游：完整走一遍 登录 → 回收 → 落盘
# --------------------------------------------------------------------------- #
class FakeUpstream:
    """按 (method, path) 分发的假 New API。记录收到的请求供断言。"""

    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.login_status = 200
        # >0 时接下来 N 次登录返回 fail_status（模拟「409 一次、自救后成功」），
        # 与恒定生效的 login_status（如 429 恒定限流）分开。
        self.login_failures_remaining = 0
        self.fail_status = 409
        self.sessions: list[dict] = []
        self.deleted: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if request.method == "POST" and path == "/api/user/login":
            if self.login_failures_remaining > 0:
                self.login_failures_remaining -= 1
                return httpx.Response(self.fail_status, json={})
            if self.login_status != 200:
                return httpx.Response(self.login_status, json={})
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "access_token": "tok-fresh",
                        "access_expires_at": int(time.time()) + 900,
                        "user": {"id": 1, "username": "LzK"},
                    },
                },
            )
        if request.method == "GET" and path == "/api/user/sessions":
            return httpx.Response(200, json={"success": True, "data": self.sessions})
        if request.method == "DELETE" and path.startswith("/api/user/sessions/"):
            sid = path.rsplit("/", 1)[-1]
            self.deleted.append(sid)
            return httpx.Response(200, json={"success": True, "data": {"revoked_sid": sid}})
        return httpx.Response(404, json={"success": False, "message": "not found"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="http://fake", transport=httpx.MockTransport(self.handler), trust_env=False
        )


def _preset_sessions(fu: FakeUpstream) -> None:
    """4 条会话：2 条旧脚本会话（应回收）、1 条浏览器会话（绝不回收）、1 条 current（跳过）"""
    fu.sessions = [
        {"sid": "s-old-1", "current": False, "user_agent": "python-httpx/0.27.0"},
        {"sid": "s-old-2", "current": False, "user_agent": "curl/7.88.1"},
        {"sid": "s-browser", "current": False, "user_agent": "Mozilla/5.0 Chrome/126.0"},
        {"sid": "s-current", "current": True, "user_agent": "python-httpx/0.27.0"},
    ]


def _make_client(fake_upstream: FakeUpstream) -> nw.NewAPIClient:
    c = nw.NewAPIClient()
    c._client = fake_upstream.client()  # 替换底层 httpx 客户端为假上游
    return c


def test_login_reaps_script_sessions_and_persists_token(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_STATE_FILE", str(tmp_path / "state.json"))
    fu = FakeUpstream()
    _preset_sessions(fu)

    async def body():
        client = _make_client(fu)
        await client.login()
        await client.aclose()

    asyncio.run(body())

    # 只删了 2 条脚本旧会话；浏览器与 current 原样保留
    assert sorted(fu.deleted) == ["s-old-1", "s-old-2"]
    # token 已落盘
    saved = StateStore().load()
    assert saved["access_token"] == "tok-fresh"
    assert saved["uid"] == 1


def test_restart_reuses_persisted_token_without_login(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_STATE_FILE", str(tmp_path / "state.json"))
    fu = FakeUpstream()
    _preset_sessions(fu)

    async def body():
        client = _make_client(fu)
        await client.login()
        await client.aclose()

        # 模拟重启：全新 client，同一个状态文件
        fu.requests.clear()
        client2 = _make_client(fu)
        assert client2._token == "tok-fresh"
        # startup() 应直接复用，一个登录请求都不发
        await client2.startup()
        assert ("POST", "/api/user/login") not in fu.requests
        await client2.aclose()

    asyncio.run(body())


def test_409_self_rescue_uses_old_token_then_relogins(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_STATE_FILE", str(tmp_path / "state.json"))
    fu = FakeUpstream()
    _preset_sessions(fu)

    async def body():
        client = _make_client(fu)
        await client.login()  # 拿到 tok-fresh 并落盘
        await client.aclose()

        # 模拟会话打满：下一次登录返回 409，之后自救重登恢复 200
        fu.login_failures_remaining = 1
        fu.requests.clear()
        fu.deleted.clear()

        client2 = _make_client(fu)
        assert client2._token == "tok-fresh"
        await client2.login()  # 第一次 409 → 用 tok-fresh 清理 → 重登成功

        # 自救过程清理了旧脚本会话，并最终重新登录成功
        assert sorted(fu.deleted) == ["s-old-1", "s-old-2"]
        login_posts = [r for r in fu.requests if r == ("POST", "/api/user/login")]
        assert len(login_posts) >= 2  # 失败一次 + 自救后成功
        assert client2._token == "tok-fresh"
        await client2.aclose()

    asyncio.run(body())


def test_409_without_saved_token_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_STATE_FILE", str(tmp_path / "no-state.json"))
    fu = FakeUpstream()
    fu.login_status = 409

    async def body():
        client = _make_client(fu)
        client._token = None  # 模拟冷启动且无持久化 token
        with pytest.raises(nw.UpstreamError) as ei:
            await client.login()
        assert "409" in str(ei.value)
        await client.aclose()

    asyncio.run(body())


def test_429_fails_fast_without_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_STATE_FILE", str(tmp_path / "state.json"))
    fu = FakeUpstream()
    fu.login_status = 429

    async def body():
        client = _make_client(fu)
        client._token = None
        with pytest.raises(nw.UpstreamError) as ei:
            await client.login()
        assert "429" in str(ei.value)
        assert fu.deleted == []  # 限流时绝不做清理动作
        await client.aclose()

    asyncio.run(body())


def test_probe_auth_does_not_login(tmp_path, monkeypatch):
    monkeypatch.setenv("PANEL_STATE_FILE", str(tmp_path / "state.json"))
    fu = FakeUpstream()

    async def body():
        client = _make_client(fu)
        await client.login()
        fu.requests.clear()

        assert await client.probe_auth() == "ok"
        assert ("POST", "/api/user/login") not in fu.requests

        client._token = None
        assert await client.probe_auth() == "none"
        assert ("POST", "/api/user/login") not in fu.requests
        await client.aclose()

    asyncio.run(body())


def test_reap_failure_never_breaks_login(tmp_path, monkeypatch):
    """回收接口炸掉时登录照常成功（治理是保险，不能反噬主流程）。"""
    monkeypatch.setenv("PANEL_STATE_FILE", str(tmp_path / "state.json"))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/user/login":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "access_token": "tok-x",
                        "access_expires_at": int(time.time()) + 900,
                        "user": {"id": 1, "username": "LzK"},
                    },
                },
            )
        if path == "/api/user/sessions":
            return httpx.Response(500, json={"success": False})
        return httpx.Response(404, json={})

    async def body():
        client = nw.NewAPIClient()
        client._client = httpx.AsyncClient(
            base_url="http://fake", transport=httpx.MockTransport(handler), trust_env=False
        )
        await client.login()  # 不应抛异常
        assert client._token == "tok-x"
        await client.aclose()

    asyncio.run(body())
