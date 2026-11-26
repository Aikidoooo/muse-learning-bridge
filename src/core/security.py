"""セキュリティ検証、シークレット・個人情報マスキング機能。"""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Any
from urllib.parse import urlparse

from src.core.exceptions import CredentialLeakageError, MuseBridgeError, PermissionDeniedError
from src.core.secret_store import GLOBAL_RUNTIME_SECRETS

# 入力JSON内で受け入れを禁止する認証・セッション関連キー（小文字で比較）
FORBIDDEN_CREDENTIAL_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "passcode",
        "cookie",
        "cookies",
        "set_cookie",
        "session_token",
        "session_id",
        "sessionid",
        "auth_token",
        "access_token",
        "refresh_token",
        "id_token",
        "classi_password",
        "classi_token",
        "authorization",
    }
)

# APIキー系の環境変数名一覧
KNOWN_SECRET_ENV_VARS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPAT_API_KEY",
    "MUSE_BRIDGE_ADMIN_TOKEN",
)

# テキスト中のシークレットパターン（Bearerトークン、代表的なAPIキー接頭辞など）
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{10,}", re.IGNORECASE),
    re.compile(r"(x-api-key[:=]\s*)[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"(x-goog-api-key[:=]\s*)[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}\b"),
)

# 個人情報（PII）マスキング用パターン
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_PATTERN = re.compile(r"(?:\+?81[-\s]?|0)\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}")
_STUDENT_ID_PATTERN = re.compile(
    r"(学籍番号|生徒番号|出席番号|受験番号|ID|UserID)\s*[:：]?\s*[A-Za-z0-9\-]{4,16}",
    re.IGNORECASE,
)
_NAME_LABEL_PATTERN = re.compile(
    r"(氏名|名前|生徒名|受験者名|ユーザー名)\s*[:：]\s*[^\s\n,、。]{1,20}"
)


def mask_secrets(text: str, extra_secrets: list[str] | None = None) -> str:
    """文字列内に含まれるAPIキーやトークンを安全なマスク文字列へ置換する。"""
    if not text:
        return text

    masked = text
    # 環境変数に現在設定されているAPIキーの値を直接マスキング
    for env_name in KNOWN_SECRET_ENV_VARS:
        secret_value = os.getenv(env_name, "").strip()
        if len(secret_value) >= 6 and secret_value in masked:
            masked = masked.replace(secret_value, f"***MASKED:{env_name}***")

    # 実行時メモリシークレットストアに保持されているキーもマスキング対象に含める
    for runtime_val in GLOBAL_RUNTIME_SECRETS.list_plaintext_values_for_masking():
        if len(runtime_val) >= 6 and runtime_val in masked:
            masked = masked.replace(runtime_val, "***MASKED_RUNTIME_SECRET***")

    if extra_secrets:
        for secret in extra_secrets:
            clean = secret.strip()
            if len(clean) >= 6 and clean in masked:
                masked = masked.replace(clean, "***MASKED_SECRET***")

    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 1:
            masked = pattern.sub(r"\1***MASKED***", masked)
        else:
            masked = pattern.sub("***MASKED_KEY***", masked)

    return masked


def mask_pii(text: str) -> str:
    """メールアドレス、電話番号、学籍番号、氏名ラベル等の個人情報をマスキングする。"""
    if not text:
        return text

    masked = _EMAIL_PATTERN.sub("[MASKED_EMAIL]", text)
    masked = _PHONE_PATTERN.sub("[MASKED_PHONE]", masked)
    masked = _STUDENT_ID_PATTERN.sub(r"\1: [MASKED_ID]", masked)
    masked = _NAME_LABEL_PATTERN.sub(r"\1: [MASKED_NAME]", masked)
    return masked


def sanitize_for_log(text: str) -> str:
    """ログ出力用にシークレットと個人情報の両方をマスキングする。"""
    return mask_pii(mask_secrets(text))


def scan_forbidden_credentials(payload: Any, path: str = "") -> None:
    """入力データ内にClassi認証情報・Cookie・セッショントークンが含まれていないか再帰検査する。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_str = str(key)
            normalized = key_str.strip().lower()
            current_path = f"{path}.{key_str}" if path else key_str
            if normalized in FORBIDDEN_CREDENTIAL_KEYS:
                # 空値（Noneや空文字）でもキー自体の送信を厳格に禁止する
                raise CredentialLeakageError(
                    f"認証情報・セッション関連フィールド '{current_path}' の受け渡しは禁止されています。",
                    details={"field": current_path},
                )
            scan_forbidden_credentials(value, current_path)
    elif isinstance(payload, list):
        for idx, item in enumerate(payload):
            scan_forbidden_credentials(item, f"{path}[{idx}]")


def verify_material_permission(
    *,
    ai_usage_approved: bool,
    material_category: str,
    auto_submit_requested: bool = False,
    auto_submit_allowed: bool = False,
) -> None:
    """学校によるAI利用許可および自動提出可否のポリシーを検証する。"""
    if not ai_usage_approved:
        raise PermissionDeniedError(
            "学校によるAI利用が許可されていない教材のため処理を中止しました。",
            details={"material_category": material_category},
        )

    if material_category == "school_assessment" and auto_submit_requested:
        raise PermissionDeniedError(
            "通常の学校評価対象テストでは自動入力・自動提出を実行できません。",
            details={"material_category": material_category},
        )

    if auto_submit_requested and not auto_submit_allowed:
        raise PermissionDeniedError(
            "自動解答・提出が明示的に許可されていない教材です。",
            details={"material_category": material_category},
        )


def validate_provider_base_url(
    url: str,
    *,
    allow_http_for_localhost: bool = True,
) -> str:
    """外部AI API通信先URLを検証し、HTTPS必須および危険な宛先（メタデータIP等）の拒否を行う。"""
    clean = url.strip().rstrip("/")
    parsed = urlparse(clean)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()

    if not scheme or not host:
        raise MuseBridgeError(
            f"不正なAPIベースURLです: {url}",
            code="INVALID_PROVIDER_URL",
            retryable=False,
            http_status=400,
        )

    # クラウドメタデータ等のSSRF標的を拒否
    if host in {"169.254.169.254", "metadata.google.internal", "metadata"}:
        raise PermissionDeniedError(
            f"セキュリティポリシーにより指定された宛先ホスト '{host}' への接続は禁止されています。",
            details={"host": host},
        )
    try:
        ip_obj = ipaddress.ip_address(host)
        if ip_obj.is_link_local:
            raise PermissionDeniedError(
                f"リンクローカルアドレス '{host}' への接続は禁止されています。",
                details={"host": host},
            )
    except ValueError:
        pass

    is_local = host in {"localhost", "127.0.0.1", "::1"}
    if scheme == "https":
        return clean
    if scheme == "http" and allow_http_for_localhost and is_local:
        return clean

    raise PermissionDeniedError(
        "外部AI APIの通信先には HTTPS を使用してください（http:// はローカル検証ホストのみ許可）。",
        details={"scheme": scheme, "host": host},
    )
