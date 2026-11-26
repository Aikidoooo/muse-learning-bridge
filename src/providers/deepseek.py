"""DeepSeek API プロバイダー実装。"""

from __future__ import annotations

from typing import Any

from src.providers.openai import OpenAIProvider
from src.schemas.enums import ProviderName
from src.schemas.question import MaterialInput


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek API クライアント（deepseek-chat / deepseek-reasoner 対応）。"""

    provider_name = ProviderName.DEEPSEEK
    require_api_key = True

    def _build_request_payload(self, material: MaterialInput) -> dict[str, Any]:
        """モデル種別に応じてDeepSeek向けのリクエストペイロードを構築する。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(material),
        }
        # deepseek-reasoner系はtemperatureやresponse_formatの制約がある場合に備えて分岐
        if "reasoner" not in self.model.lower():
            payload["temperature"] = 0.1
            payload["response_format"] = {"type": "json_object"}
        return payload
