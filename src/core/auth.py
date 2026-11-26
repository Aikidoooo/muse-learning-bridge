"""設定変更・管理用エンドポイント向けのBearerトークン認証モジュール。"""

from __future__ import annotations

import hmac
import os

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.core.exceptions import MuseBridgeError

ADMIN_TOKEN_ENV_VAR = "MUSE_BRIDGE_ADMIN_TOKEN"
_BEARER_SCHEME = HTTPBearer(auto_error=False, description="管理用Bearerトークン")


class AdminAuthError(MuseBridgeError):
    """管理API認証失敗エラー。"""

    code = "ADMIN_AUTH_REQUIRED"
    retryable = False
    http_status = 401


def _extract_token(
    bearer: HTTPAuthorizationCredentials | None,
    x_bridge_admin_token: str | None,
) -> str:
    """Authorization Bearer または X-Bridge-Admin-Token ヘッダからトークンを抽出する。"""
    if x_bridge_admin_token and x_bridge_admin_token.strip():
        return x_bridge_admin_token.strip()
    if bearer is not None and bearer.credentials:
        return bearer.credentials.strip()
    return ""


async def require_admin_auth(
    bearer: HTTPAuthorizationCredentials | None = Depends(_BEARER_SCHEME),
    x_bridge_admin_token: str | None = Header(default=None),
) -> None:
    """設定変更・認証状態確認・接続テスト用エンドポイントの認証を検証する（Fail-Closed）。"""
    expected = os.getenv(ADMIN_TOKEN_ENV_VAR, "").strip()
    if not expected:
        raise AdminAuthError(
            f"管理認証トークン（環境変数 {ADMIN_TOKEN_ENV_VAR}）が未設定のため、"
            "セキュリティポリシーにより管理APIへのアクセスを拒否しました。",
            code="ADMIN_TOKEN_NOT_CONFIGURED",
            http_status=401,
        )

    provided = _extract_token(bearer, x_bridge_admin_token)
    if not provided or not hmac.compare_digest(provided, expected):
        raise AdminAuthError(
            "管理APIの認証に失敗しました。有効なBearerトークンを指定してください。",
            code="INVALID_ADMIN_TOKEN",
            http_status=401,
        )
