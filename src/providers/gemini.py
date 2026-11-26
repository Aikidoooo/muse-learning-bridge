"""Google Gemini API プロバイダー実装（thinkingConfig動的調整・thoughtsTokenCount計測対応）。"""

from __future__ import annotations

from typing import Any

from src.core.exceptions import InvalidAIResponseError
from src.providers.base import SYSTEM_INSTRUCTION, BaseAIProvider
from src.schemas.enums import ProviderName
from src.schemas.question import MaterialInput


class GeminiProvider(BaseAIProvider):
    """Google Gemini REST API（generateContent）クライアント。"""

    provider_name = ProviderName.GEMINI

    def _supports_thinking_escalation(self) -> bool:
        """Gemini 2.5 / 3系モデルは思考予算の段階引き上げに対応する。"""
        m = self.model.lower()
        return ("gemini-2.5" in m and "gemini-2.5-pro" not in m) or "gemini-3" in m

    def _build_thinking_config(self, profile: str) -> dict[str, Any] | None:
        """モデル世代に応じた最適な thinkingConfig を返す。"""
        m = self.model.lower()
        if "gemini-3" in m:
            level_map = {"minimal": "minimal", "balanced": "low", "high": "medium"}
            return {"thinkingLevel": level_map.get(profile, "low")}
        if "gemini-2.5-pro" in m:
            budget_map = {"minimal": 128, "balanced": 512, "high": 2048}
            return {"thinkingBudget": budget_map.get(profile, 512)}
        if "gemini-2.5" in m:
            budget_map = {"minimal": 0, "balanced": 256, "high": 2048}
            return {"thinkingBudget": budget_map.get(profile, 0)}
        return None

    async def _call_api(self, material: MaterialInput) -> tuple[str, int, int, int, int]:
        """Gemini APIを呼び出し、JSONテキストと思考トークンを含む使用量を返す。"""
        api_key = self._get_api_key(required=True)
        url = f"{self.base_url}/models/{self.model}:generateContent"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }

        parts: list[dict[str, Any]] = [{"text": self.build_user_prompt(material)}]
        for question_id, img in self.collect_approved_images(material):
            parts.append({"text": f"[img:{question_id}/{img.image_id}]"})
            parts.append(
                {
                    "inlineData": {
                        "mimeType": img.mime_type,
                        "data": img.base64_data,
                    }
                }
            )

        gen_cfg: dict[str, Any] = {
            "temperature": 0.1,
            "maxOutputTokens": self.compute_max_output_tokens(material),
            "responseMimeType": "application/json",
        }
        thinking_cfg = self._build_thinking_config(self._resolve_thinking_profile(material))
        if thinking_cfg is not None:
            gen_cfg["thinkingConfig"] = thinking_cfg

        payload: dict[str, Any] = {
            "systemInstruction": {
                "parts": [{"text": self.build_system_instruction(material)}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
            "generationConfig": gen_cfg,
        }

        data = await self._post_json(url, headers=headers, payload=payload)

        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise InvalidAIResponseError("Gemini APIから候補レスポンスが返されませんでした。")

        content = candidates[0].get("content", {})
        resp_parts = content.get("parts", [])
        text_chunks = [
            p.get("text", "")
            for p in resp_parts
            if isinstance(p, dict) and p.get("text") and not p.get("thought")
        ]
        raw_text = "\n".join(text_chunks).strip()
        if not raw_text:
            raise InvalidAIResponseError("Gemini APIの応答テキストが空です。")

        usage = data.get("usageMetadata", {})
        in_tok = int(usage.get("promptTokenCount", 0) or 0)
        out_tok = int(usage.get("candidatesTokenCount", 0) or 0)
        think_tok = int(usage.get("thoughtsTokenCount", 0) or 0)
        total_tok = int(usage.get("totalTokenCount", in_tok + out_tok + think_tok) or 0)
        return raw_text, in_tok, out_tok, total_tok, think_tok

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
