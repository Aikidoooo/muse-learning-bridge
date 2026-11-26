"""OpenAI API および OpenAI互換 API プロバイダー実装（reasoning_effort最適化・思考トークン記録対応）。"""

from __future__ import annotations

from typing import Any

from src.core.exceptions import InvalidAIResponseError
from src.providers.base import SYSTEM_INSTRUCTION, BaseAIProvider
from src.schemas.enums import ProviderName
from src.schemas.question import MaterialInput


class OpenAIProvider(BaseAIProvider):
    """OpenAI Chat Completions API クライアント。"""

    provider_name = ProviderName.OPENAI
    require_api_key: bool = True

    def _is_reasoning_model(self) -> bool:
        """OpenAI推論系モデル（o1/o3/o4/gpt-5等）かを判定する。"""
        m = self.model.lower()
        return m.startswith(("o1", "o3", "o4", "gpt-5"))

    def _supports_thinking_escalation(self) -> bool:
        """OpenAI推論系モデルのみ reasoning_effort 段階引き上げに対応する。"""
        return self._is_reasoning_model()

    def _build_messages(self, material: MaterialInput) -> list[dict[str, Any]]:
        """Chat Completions用のメッセージ配列を構築する。"""
        user_content: list[dict[str, Any]] = [
            {"type": "text", "text": self.build_user_prompt(material)}
        ]
        for question_id, img in self.collect_approved_images(material):
            user_content.append(
                {
                    "type": "text",
                    "text": f"[img:{question_id}/{img.image_id}]",
                }
            )
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{img.mime_type};base64,{img.base64_data}",
                    },
                }
            )

        # 画像が無い場合はシンプルに文字列形式で送信（互換サーバーとの親和性向上）
        user_payload: str | list[dict[str, Any]] = (
            user_content[0]["text"] if len(user_content) == 1 else user_content
        )

        return [
            {"role": "system", "content": self.build_system_instruction(material)},
            {"role": "user", "content": user_payload},
        ]

    def _build_request_payload(self, material: MaterialInput) -> dict[str, Any]:
        """送信JSONペイロードを構築する。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(material),
            "response_format": {"type": "json_object"},
        }
        if self._is_reasoning_model():
            profile = self._resolve_thinking_profile(material)
            effort_map = {"minimal": "low", "balanced": "medium", "high": "high"}
            payload["reasoning_effort"] = effort_map.get(profile, "low")
            payload["max_completion_tokens"] = self.compute_max_output_tokens(material)
        else:
            payload["temperature"] = 0.1
            payload["max_tokens"] = self.compute_max_output_tokens(material)
        return payload

    async def _call_api(self, material: MaterialInput) -> tuple[str, int, int, int, int]:
        """Chat Completions APIを呼び出す。"""
        api_key = self._get_api_key(required=self.require_api_key)
        url = f"{self.base_url}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = self._build_request_payload(material)
        data = await self._post_json(url, headers=headers, payload=payload)

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise InvalidAIResponseError(f"{self.config.display_name} からchoicesが返されませんでした。")

        message = choices[0].get("message", {})
        raw_text = str(message.get("content") or "").strip()
        if not raw_text:
            raise InvalidAIResponseError(f"{self.config.display_name} の応答テキストが空です。")

        usage = data.get("usage", {})
        in_tok = int(usage.get("prompt_tokens", 0) or 0)
        out_tok = int(usage.get("completion_tokens", 0) or 0)
        comp_details = usage.get("completion_tokens_details") or {}
        think_tok = int(comp_details.get("reasoning_tokens", 0) or 0) if isinstance(comp_details, dict) else 0
        total_tok = int(usage.get("total_tokens", in_tok + out_tok) or 0)
        return raw_text, in_tok, out_tok, total_tok, think_tok

    async def list_remote_models(self) -> list[str]:
        """OpenAI互換 /models エンドポイントから利用可能なモデル一覧を取得する。"""
        api_key = self._get_api_key(required=self.require_api_key)
        url = f"{self.base_url}/models"
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        data = await self._get_json(url, headers=headers)
        raw_items = data.get("data", [])
        if not isinstance(raw_items, list):
            return list(self.config.models)
        models = [
            str(item.get("id")).strip()
            for item in raw_items
            if isinstance(item, dict) and item.get("id")
        ]
        return models


class OpenAICompatibleProvider(OpenAIProvider):
    """任意のベースURLに接続可能なOpenAI互換APIクライアント（ローカルLLM等対応）。"""

    provider_name = ProviderName.OPENAI_COMPATIBLE
    require_api_key = False

    def _build_request_payload(self, material: MaterialInput) -> dict[str, Any]:
        """互換サーバー差異を避けるため response_format 必須に依存しないペイロードを構築する。"""
        return {
            "model": self.model,
            "messages": self._build_messages(material),
            "temperature": 0.1,
            "max_tokens": self.compute_max_output_tokens(material),
        }
