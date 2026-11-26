"""Google Gemini API プロバイダー実装。"""

from __future__ import annotations

from typing import Any

from src.core.exceptions import InvalidAIResponseError
from src.providers.base import SYSTEM_INSTRUCTION, BaseAIProvider
from src.schemas.enums import ProviderName
from src.schemas.question import MaterialInput


class GeminiProvider(BaseAIProvider):
    """Google Gemini REST API（generateContent）クライアント。"""

    provider_name = ProviderName.GEMINI

    async def _call_api(self, material: MaterialInput) -> tuple[str, int, int, int]:
        """Gemini APIを呼び出し、JSONテキストとトークン使用量を返す。"""
        api_key = self._get_api_key(required=True)
        url = f"{self.base_url}/models/{self.model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }

        parts: list[dict[str, Any]] = [{"text": self.build_user_prompt(material)}]
        for question_id, img in self.collect_approved_images(material):
            parts.append({"text": f"[利用許可画像: question_id={question_id}, image_id={img.image_id}]"})
            parts.append(
                {
                    "inlineData": {
                        "mimeType": img.mime_type,
                        "data": img.base64_data,
                    }
                }
            )

        payload: dict[str, Any] = {
            "systemInstruction": {
                "parts": [{"text": SYSTEM_INSTRUCTION}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": self.compute_max_output_tokens(material),
                "responseMimeType": "application/json",
            },
        }

        data = await self._post_json(url, headers=headers, payload=payload)

        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise InvalidAIResponseError("Gemini APIから候補レスポンスが返されませんでした。")

        content = candidates[0].get("content", {})
        resp_parts = content.get("parts", [])
        text_chunks = [p.get("text", "") for p in resp_parts if isinstance(p, dict) and p.get("text")]
        raw_text = "\n".join(text_chunks).strip()
        if not raw_text:
            raise InvalidAIResponseError("Gemini APIの応答テキストが空です。")

        usage = data.get("usageMetadata", {})
        in_tok = int(usage.get("promptTokenCount", 0) or 0)
        out_tok = int(usage.get("candidatesTokenCount", 0) or 0)
        total_tok = int(usage.get("totalTokenCount", in_tok + out_tok) or 0)
        return raw_text, in_tok, out_tok, total_tok

    async def list_remote_models(self) -> list[str]:
        """Gemini APIから利用可能なモデル一覧を取得する。"""
        api_key = self._get_api_key(required=True)
        url = f"{self.base_url}/models"
        headers = {"x-goog-api-key": api_key}
        data = await self._get_json(url, headers=headers)
        raw_models = data.get("models", [])
        if not isinstance(raw_models, list):
            return list(self.config.models)
        names: list[str] = []
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            raw_name = str(item.get("name") or "").strip()
            if raw_name.startswith("models/"):
                raw_name = raw_name[len("models/") :]
            if raw_name:
                names.append(raw_name)
        return names
