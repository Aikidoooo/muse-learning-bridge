"""Anthropic Claude API プロバイダー実装。"""

from __future__ import annotations

from typing import Any

from src.core.exceptions import InvalidAIResponseError
from src.providers.base import SYSTEM_INSTRUCTION, BaseAIProvider
from src.schemas.enums import ProviderName
from src.schemas.question import MaterialInput


class AnthropicProvider(BaseAIProvider):
    """Anthropic Messages API クライアント。"""

    provider_name = ProviderName.ANTHROPIC

    async def _call_api(self, material: MaterialInput) -> tuple[str, int, int, int, int]:
        """Anthropic Messages APIを呼び出し、JSONテキストとトークン使用量を返す。"""
        api_key = self._get_api_key(required=True)
        url = f"{self.base_url}/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }

        content_blocks: list[dict[str, Any]] = []
        for question_id, img in self.collect_approved_images(material):
            content_blocks.append(
                {
                    "type": "text",
                    "text": f"[img:{question_id}/{img.image_id}]",
                }
            )
            content_blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.mime_type,
                        "data": img.base64_data,
                    },
                }
            )
        content_blocks.append({"type": "text", "text": self.build_user_prompt(material)})

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.compute_max_output_tokens(material),
            "temperature": 0.1,
            "system": SYSTEM_INSTRUCTION,
            "messages": [
                {
                    "role": "user",
                    "content": content_blocks,
                }
            ],
        }

        data = await self._post_json(url, headers=headers, payload=payload)

        blocks = data.get("content")
        if not isinstance(blocks, list) or not blocks:
            raise InvalidAIResponseError("Anthropic APIからコンテンツが返されませんでした。")

        text_chunks = [
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ]
        raw_text = "\n".join(text_chunks).strip()
        if not raw_text:
            raise InvalidAIResponseError("Anthropic APIの応答テキストが空です。")

        usage = data.get("usage", {})
        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        think_tok = int(usage.get("thinking_tokens", 0) or 0)
        return raw_text, in_tok, out_tok, in_tok + out_tok, think_tok

    async def list_remote_models(self) -> list[str]:
        """Anthropic Models APIから利用可能なモデル一覧を取得する。"""
        api_key = self._get_api_key(required=True)
        url = f"{self.base_url}/models"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        data = await self._get_json(url, headers=headers)
        raw_items = data.get("data", [])
        if not isinstance(raw_items, list):
            return list(self.config.models)
        return [
            str(item.get("id")).strip()
            for item in raw_items
            if isinstance(item, dict) and item.get("id")
        ]
