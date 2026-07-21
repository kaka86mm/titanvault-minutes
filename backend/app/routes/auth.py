"""单密码门登录路由。

token 由 security.Security 管理（进程级内存 set）。登录成功 set cookie，
后续请求由 SecurityMiddleware 校验。
"""
from __future__ import annotations

from fastapi import APIRouter, Body

from ..security import build_security

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 进程级单例（启动时读 AHAMVOICE_ACCESS_PASSWORD；空则密码门不启用）
_security = build_security()


def get_security():
    """供 main.py 的 SecurityMiddleware 取同一个单例。"""
    return _security


@router.post("/login")
def login(creds: dict = Body(...)):
    return _security.login(creds)
