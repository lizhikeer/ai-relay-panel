"""契约冒烟测试（验收 A3–A8 / A10）。

运行：在 backend/ 目录执行 `pytest -q`（或 `python -m pytest tests/test_contract.py`）。
依赖真实可达的 New API（.env 中的 NEWAPI_BASE_URL）。A6/A8 需要上游可达，
不可达时自动 skip，其余用例（A3/A4/A5/A7）纯本地即可通过。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import pytest
from fastapi.testclient import TestClient

# 让 `import app` 可用（backend/ 在 sys.path）
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PANEL_USER = settings.panel_user
PANEL_PASSWORD = settings.panel_password

PLAINTEXT_KEY_RE = re.compile(r"sk-[A-Za-z0-9]{20,}")


def _assert_no_plaintext_key(payload) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    hits = PLAINTEXT_KEY_RE.findall(text)
    assert hits == [], f"响应中出现疑似明文 key：{hits[:3]}"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def session_client(client):
    """已登录的 TestClient（整个模块复用同一登录态，减少上游登录次数）。"""
    resp = client.post(
        "/panel-api/auth/login", json={"username": PANEL_USER, "password": PANEL_PASSWORD}
    )
    assert resp.status_code == 200 and resp.json()["success"] is True
    return client


@pytest.fixture()
def anon_client():
    """裸 TestClient：不进入 lifespan（不触发上游探测登录），且无 cookie。"""
    return TestClient(app)


def _upstream_reachable(client) -> bool:
    try:
        resp = client.get("/panel-api/system/health")
        return resp.status_code == 200 and resp.json()["data"].get("upstream") == "ok"
    except Exception:  # noqa: BLE001
        return False


# --------------------------- A3 未登录拦截 --------------------------- #
def test_a3_overview_requires_login(anon_client):
    resp = anon_client.get("/panel-api/overview")
    assert resp.status_code == 401
    body = resp.json()
    assert body["success"] is False
    assert body["data"] is None


# --------------------------- A4 登录流程 --------------------------- #
def test_a4_login_wrong_password(anon_client):
    resp = anon_client.post(
        "/panel-api/auth/login", json={"username": PANEL_USER, "password": "definitely-wrong"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert "用户名或密码错误" in body["message"]


def test_a4_login_success_sets_cookie(anon_client):
    resp = anon_client.post(
        "/panel-api/auth/login", json={"username": PANEL_USER, "password": PANEL_PASSWORD}
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert "panel_session" in resp.cookies


# --------------------------- A5 会话探测 --------------------------- #
def test_a5_session_authenticated(session_client):
    resp = session_client.get("/panel-api/auth/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["authenticated"] is True
    assert body["data"]["username"] == PANEL_USER


def test_a5_session_unauthorized(anon_client):
    """契约 §2.3：session 探测接口不返回 401，未登录时 200 + authenticated:false。"""
    resp = anon_client.get("/panel-api/auth/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["authenticated"] is False


def test_a3_protected_endpoint_still_401(anon_client):
    """受保护接口在未登录时仍必须是 401（与 session 探测区分开）。"""
    for path in ("/panel-api/overview", "/panel-api/channels", "/panel-api/tokens"):
        assert anon_client.get(path).status_code == 401, path


# --------------------------- A6 key 脱敏 --------------------------- #
def test_a6_channels_no_plaintext_key(session_client):
    if not _upstream_reachable(session_client):
        pytest.skip("New API 不可达，跳过 key 脱敏集成校验")
    resp = session_client.get("/panel-api/channels?page=1&size=50")
    assert resp.status_code == 200
    _assert_no_plaintext_key(resp.json())


# --------------------------- A8 渠道测试接口 --------------------------- #
def test_a8_channel_test_returns_latency_and_status(session_client):
    if not _upstream_reachable(session_client):
        pytest.skip("New API 不可达，跳过渠道测试集成校验")

    created = session_client.post(
        "/panel-api/channels",
        json={
            "name": "__contract_test__",
            "type": 1,
            "base_url": "https://example.invalid",
            "key": "sk-__contract_test__0000000000000000",
            "models": ["gpt-4o"],
            "group": "default",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["success"] is True, body.get("message")
    cid = body["data"]["id"]
    try:
        resp = session_client.post(f"/panel-api/channels/{cid}/test", json={"model": "gpt-4o"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "latency_ms" in data and isinstance(data["latency_ms"], int)
        assert "status_code" in data and data["status_code"] is not None
    finally:
        session_client.delete(f"/panel-api/channels/{cid}")


# --------------------------- A7 上游故障 → 502 --------------------------- #
def test_a7_upstream_failure_returns_502(client):
    from app import newapi

    # 先本地登录拿 session
    login = client.post(
        "/panel-api/auth/login", json={"username": PANEL_USER, "password": PANEL_PASSWORD}
    )
    assert login.json()["success"] is True

    # 把 New API 客户端指向死地址（不改配置文件），触发真实的连接失败
    upstream = newapi.get_client()
    saved_base = upstream._client.base_url
    saved = (upstream._token, upstream._uid, upstream._expires_at)
    upstream._client.base_url = "http://127.0.0.1:59999"
    upstream._token = None
    upstream._uid = None
    upstream._expires_at = 0
    try:
        resp = client.get("/panel-api/overview")
        assert resp.status_code == 502
        body = resp.json()
        assert body["success"] is False
    finally:
        upstream._client.base_url = saved_base
        upstream._token, upstream._uid, upstream._expires_at = saved
