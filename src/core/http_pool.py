"""HTTP接続プールの管理（TCP/TLSハンドシェイク削減による高速化）。"""

from __future__ import annotations

import httpx


class HttpClientPool:
    """外部AI API通信向けの共有非同期HTTPクライアントプール。"""

    def __init__(
        self,
        *,
        timeout_seconds: float = 45.0,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
    ) -> None:
        """接続プールを初期化する。"""
        self._timeout_seconds = timeout_seconds
        self._max_connections = max_connections
        self._max_keepalive_connections = max_keepalive_connections
        self._client: httpx.AsyncClient | None = None

    def get_client(self) -> httpx.AsyncClient:
        """再利用可能な共有 httpx.AsyncClient を返す。"""
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(
                max_connections=self._max_connections,
                max_keepalive_connections=self._max_keepalive_connections,
                keepalive_expiry=30.0,
            )
            self._client = httpx.AsyncClient(
                timeout=self._timeout_seconds,
                limits=limits,
            )
        return self._client

    async def close(self) -> None:
        """共有クライアントを安全にクローズする。"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
