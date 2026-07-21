"""单密码门 security.py 的单元测试（TDD）。

测试用 TestClient 驱动 middleware，覆盖：
- 无密码 → 不拦截
- 有密码 → 未登录 401 / 白名单放行 / 登录 set cookie / 登录后通过 / 错误密码
"""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import security as sec_mod


def make_app(password):
    """构造带密码门中间件的测试 app。"""
    app = FastAPI()
    sec = sec_mod.Security(password=password if password else None)
    app.add_middleware(sec_mod.SecurityMiddleware, security=sec)

    @app.get("/api/health")
    def _health():
        return {"ok": True}

    @app.get("/api/me")
    def _me():
        return {"id": "local-admin"}

    @app.get("/")
    def _index():
        return {"page": "index"}

    @app.post("/api/auth/login")
    def _login(creds: dict):
        return sec.login(creds)

    return app, sec


def test_no_password_no_gate(tmp_home):
    """密码为空 → 不启用密码门，所有请求放行。"""
    app, _ = make_app(password=None)
    with TestClient(app) as c:
        assert c.get("/api/me").status_code == 200


def test_password_gate_blocks_unauthenticated(tmp_home):
    """启用密码门 → 无 cookie 的 /api/me 返回 401。"""
    app, _ = make_app(password="secret")
    with TestClient(app) as c:
        assert c.get("/api/me").status_code == 401


def test_health_is_whitelisted(tmp_home):
    """启用密码门 → /api/health 仍可访问（healthcheck 用）。"""
    app, _ = make_app(password="secret")
    with TestClient(app) as c:
        assert c.get("/api/health").status_code == 200


def test_root_whitelisted(tmp_home):
    """启用密码门 → / 静态入口放行（否则登录页打不开）。"""
    app, _ = make_app(password="secret")
    with TestClient(app) as c:
        assert c.get("/").status_code != 401


def test_login_sets_cookie_then_api_works(tmp_home):
    """正确密码登录 → set cookie → 带 cookie 访问 /api/me 成功。"""
    app, _ = make_app(password="secret")
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"password": "secret"})
        assert r.status_code == 200
        # TestClient 自动带 cookie
        assert c.get("/api/me").status_code == 200


def test_login_wrong_password(tmp_home):
    """错误密码 → 401，不 set cookie。"""
    app, _ = make_app(password="secret")
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"password": "wrong"})
        assert r.status_code == 401
        assert c.get("/api/me").status_code == 401


def test_wrong_password_repeated_no_lockout(tmp_home):
    """密码门不实现锁定（单密码门，不需要防爆破）。"""
    app, _ = make_app(password="secret")
    with TestClient(app) as c:
        for _ in range(10):
            assert c.post("/api/auth/login", json={"password": "wrong"}).status_code == 401


# -------- API Token（Bearer / query）通道 --------

def make_app_with_api_token(password, api_token):
    """构造带密码门 + 固定 API Token 的测试 app。"""
    app = FastAPI()
    sec = sec_mod.Security(password=password, api_token=api_token)
    app.add_middleware(sec_mod.SecurityMiddleware, security=sec)

    @app.get("/api/health")
    def _health():
        return {"ok": True}

    @app.get("/api/me")
    def _me():
        return {"id": "local-admin"}

    return app, sec


def test_bearer_token_authorizes(tmp_home):
    """固定 API Token + Authorization: Bearer → 通过。"""
    app, _ = make_app_with_api_token(password="secret", api_token="sk-hermes-xyz")
    with TestClient(app) as c:
        r = c.get("/api/me", headers={"Authorization": "Bearer sk-hermes-xyz"})
        assert r.status_code == 200


def test_bearer_token_wrong_rejected(tmp_home):
    """错误的 Bearer token → 401。"""
    app, _ = make_app_with_api_token(password="secret", api_token="sk-hermes-xyz")
    with TestClient(app) as c:
        r = c.get("/api/me", headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401


def test_query_token_authorizes(tmp_home):
    """固定 API Token + ?token= → 通过（前端媒体 URL 用）。"""
    app, _ = make_app_with_api_token(password="secret", api_token="sk-hermes-xyz")
    with TestClient(app) as c:
        r = c.get("/api/me?token=sk-hermes-xyz")
        assert r.status_code == 200


def test_api_token_and_cookie_coexist(tmp_home):
    """API Token 和登录 cookie 共存：两种方式都能访问。"""
    app, sec = make_app_with_api_token(password="secret", api_token="sk-hermes-xyz")

    @app.post("/api/auth/login")
    def _login(creds: dict):
        return sec.login(creds)

    with TestClient(app) as c:
        # Bearer 方式
        assert c.get("/api/me", headers={"Authorization": "Bearer sk-hermes-xyz"}).status_code == 200
        # 登录拿 cookie 方式
        c.post("/api/auth/login", json={"password": "secret"})
        assert c.get("/api/me").status_code == 200


def test_bearer_prefix_case_insensitive(tmp_home):
    """Bearer 前缀大小写不敏感（bearer/BEARER 都行）。"""
    app, _ = make_app_with_api_token(password="secret", api_token="sk-hermes-xyz")
    with TestClient(app) as c:
        assert c.get("/api/me", headers={"Authorization": "bearer sk-hermes-xyz"}).status_code == 200
        assert c.get("/api/me", headers={"Authorization": "BEARER sk-hermes-xyz"}).status_code == 200


def test_build_security_reads_api_token_env(tmp_home, monkeypatch):
    """build_security 从 AHAMVOICE_API_TOKEN 环境变量读固定 token。"""
    monkeypatch.setenv("AHAMVOICE_ACCESS_PASSWORD", "secret")
    monkeypatch.setenv("AHAMVOICE_API_TOKEN", "sk-from-env-123")
    sec = sec_mod.build_security()
    assert sec.enabled
    assert "sk-from-env-123" in sec._tokens


def test_build_security_no_api_token(tmp_home, monkeypatch):
    """没配 AHAMVOICE_API_TOKEN → 只有登录能拿 token。"""
    monkeypatch.setenv("AHAMVOICE_ACCESS_PASSWORD", "secret")
    monkeypatch.delenv("AHAMVOICE_API_TOKEN", raising=False)
    sec = sec_mod.build_security()
    assert sec.enabled
    assert len(sec._tokens) == 0  # 没有预置 token，只能靠登录生成
