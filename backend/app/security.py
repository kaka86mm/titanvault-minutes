"""单密码门：cookie token + middleware 统一拦截。

启用条件：AHAMVOICE_ACCESS_PASSWORD 非空。
token 存内存（进程级 set），重启失效，不设过期。

API Token（供 Hermes/脚本等非浏览器客户端）：
- AHAMVOICE_API_TOKEN 环境变量配置固定 token，启动时注入 self._tokens
- 客户端用 Authorization: Bearer <token> 或 ?token=<token> 调用
- 与登录 cookie token 同集合，is_authorized 统一校验，重启不失效

设计取舍：
- Cookie 而非 Authorization Header：浏览器原生支持，前端 fetch 加
  credentials:'include' 即可，手机浏览器兼容好。
- API Token 走 Bearer/query：curl/脚本无 cookie 容器，给固定 token 最省事。
- token 存内存不存 DB：重启重新登录可接受；存 DB 要加表（与删多用户表冲突）。
  固定 API Token 是 env 配置，重启自动恢复，不依赖 DB。
- 密码明文比对（hmac.compare_digest 防时序攻击）：单密码门不是用户密码库，
  密码在 .env 也是明文，哈希只是表演。
- 不设过期：单机自用，登录一次一直有效。重置靠重启或改密码。
- middleware 统一拦截而非每路由 Depends：原版每路由 Depends(current_user)，
  改 100 个路由签名不如一个 middleware 检查白名单 + cookie。
"""
from __future__ import annotations

import hmac
import os
import secrets
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

COOKIE_NAME = "tv_token"

# 不需要 token 的路径：登录本身 + 健康检查 + 静态资源（否则登录页打不开）
WHITELIST_PREFIXES = ("/api/auth/login", "/api/health", "/assets/")
WHITELIST_EXACT = ("/", "/favicon.svg", "/favicon.ico", "/index.html", "/login")


class Security:
    """密码门状态：密码 + 已发放的 token 集合（进程级内存）。

    token 来源有两种，都进同一个 self._tokens 集合：
    - 登录动态生成（cookie 用，重启失效）
    - AHAMVOICE_API_TOKEN 配置的固定 token（API/程序调用用，重启不失效）
    """

    def __init__(self, password: str | None, api_token: str | None = None) -> None:
        self.enabled = bool(password)
        self._password = password or ""
        self._tokens: set[str] = set()
        # 固定 API Token：供 Hermes/脚本等非浏览器客户端用 Bearer 调用。
        # 与登录 token 同集合，is_authorized 统一校验。
        if api_token:
            self._tokens.add(api_token)

    def login(self, creds: dict[str, Any]) -> JSONResponse:
        if not self.enabled:
            return JSONResponse({"ok": True})
        password = (creds.get("password") or "").strip()
        if not hmac.compare_digest(password, self._password):
            return JSONResponse({"detail": "密码错误"}, status_code=401)
        token = secrets.token_urlsafe(32)
        self._tokens.add(token)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax")
        return resp

    def is_authorized(self, request: Request) -> bool:
        if not self.enabled:
            return True
        path = request.url.path
        if path in WHITELIST_EXACT or any(path.startswith(p) for p in WHITELIST_PREFIXES):
            return True
        # 通道 1：浏览器 cookie（原有逻辑，浏览器用户继续用）
        token = request.cookies.get(COOKIE_NAME)
        if token in self._tokens:
            return True
        # 通道 2：Authorization: Bearer <token>（Hermes/curl 等非浏览器客户端）
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            if auth_header[7:].strip() in self._tokens:
                return True
        # 通道 3：?token=<token> query（前端媒体 URL withToken 已在发）
        query_token = request.query_params.get("token")
        if query_token and query_token in self._tokens:
            return True
        return False


class SecurityMiddleware(BaseHTTPMiddleware):
    """拦截所有非白名单 /api/* 请求，校验 cookie token。"""

    def __init__(self, app: ASGIApp, security: Security) -> None:
        super().__init__(app)
        self._security = security

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api") and not self._security.is_authorized(request):
            return JSONResponse({"detail": "未登录"}, status_code=401)
        return await call_next(request)


def build_security() -> Security:
    """从 env 读密码和可选的固定 API Token 构造 Security。

    - TITANVAULT_ACCESS_PASSWORD / AHAMVOICE_ACCESS_PASSWORD：空 → 密码门不启用
    - TITANVAULT_API_TOKEN / AHAMVOICE_API_TOKEN：非空则作为固定 long-lived token
    """
    from .config import env_compat
    return Security(
        password=env_compat("TITANVAULT_ACCESS_PASSWORD") or None,
        api_token=env_compat("TITANVAULT_API_TOKEN") or None,
    )
