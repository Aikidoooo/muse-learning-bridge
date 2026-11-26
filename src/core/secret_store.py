"""プロセス内揮発性シークレット管理（APIキーをディスクや平文ログに残さないための隔離ストア）。"""

from __future__ import annotations

import base64
import os
import secrets
import threading
from typing import Literal


CredentialSource = Literal["request_header", "runtime_secret", "environment", "none"]


class RuntimeSecretStore:
    """実行中のみメモリ上にAPIキーを難読化保持するスレッドセーフなストア。

    SQLite・YAML・平文JSON・ログには一切書き出さない。
    """

    def __init__(self) -> None:
        """ストアを初期化する。"""
        self._lock = threading.Lock()
        # 単純な平文メモリダンプを避けるため、プロセス起動ごとのランダムマスクキーでXOR難読化
        self._ephemeral_pad: bytes = secrets.token_bytes(32)
        self._secrets: dict[str, str] = {}

    def _encode(self, plaintext: str) -> str:
        """プロセス固有パッドでXOR難読化しBase64文字列を返す。"""
        raw = plaintext.encode("utf-8")
        pad = self._ephemeral_pad
        xored = bytes(b ^ pad[i % len(pad)] for i, b in enumerate(raw))
        return base64.b64encode(xored).decode("ascii")

    def _decode(self, encoded: str) -> str:
        """XOR難読化された文字列を復号する。"""
        xored = base64.b64decode(encoded.encode("ascii"))
        pad = self._ephemeral_pad
        raw = bytes(b ^ pad[i % len(pad)] for i, b in enumerate(xored))
        return raw.decode("utf-8")

    def set_secret(self, provider_name: str, api_key: str) -> None:
        """プロバイダー別のAPIキーをメモリ内ストアへ登録・更新する。"""
        key_id = provider_name.strip().lower()
        clean = api_key.strip()
        with self._lock:
            if not clean:
                self._secrets.pop(key_id, None)
            else:
                self._secrets[key_id] = self._encode(clean)

    def delete_secret(self, provider_name: str) -> None:
        """プロバイダー別のメモリ内APIキーを削除する。"""
        key_id = provider_name.strip().lower()
        with self._lock:
            self._secrets.pop(key_id, None)

    def get_secret(self, provider_name: str) -> str:
        """プロバイダー別のAPIキーを取得する。未登録時は空文字を返す。"""
        key_id = provider_name.strip().lower()
        with self._lock:
            encoded = self._secrets.get(key_id)
            if not encoded:
                return ""
            return self._decode(encoded)

    def resolve_with_source(
        self,
        provider_name: str,
        *,
        env_var: str,
        header_api_key: str | None = None,
    ) -> tuple[str, CredentialSource]:
        """優先順位（リクエストヘッダ -> 実行時メモリ -> 環境変数）でAPIキーと取得元を返す。"""
        if header_api_key and header_api_key.strip():
            return header_api_key.strip(), "request_header"

        runtime_val = self.get_secret(provider_name)
        if runtime_val:
            return runtime_val, "runtime_secret"

        env_val = os.getenv(env_var, "").strip()
        if env_val:
            return env_val, "environment"

        return "", "none"

    def snapshot(self) -> dict[str, str]:
        """ロールバック用に暗号化済み状態のスナップショットを取得する。"""
        with self._lock:
            return dict(self._secrets)

    def restore(self, snap: dict[str, str]) -> None:
        """スナップショットからメモリ内シークレット状態を復元する。"""
        with self._lock:
            self._secrets = dict(snap)

    def list_plaintext_values_for_masking(self) -> list[str]:
        """ログサニタイズフィルタ用に保持中のシークレット値一覧を返す。"""
        with self._lock:
            return [self._decode(v) for v in self._secrets.values() if v]


# アプリケーション全体で共有するデフォルトのランタイムシークレットストア
GLOBAL_RUNTIME_SECRETS = RuntimeSecretStore()
