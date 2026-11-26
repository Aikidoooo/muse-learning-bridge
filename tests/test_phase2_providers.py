"""Phase 2: 外部AI APIプロバイダー連携の自動テスト（httpx.MockTransport使用）。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from src.core.config import AppConfig, load_config
from src.core.exceptions import InvalidJsonFormatError, ProviderAuthError, ProviderRateLimitError
from src.providers.factory import create_provider
from src.schemas import MaterialInput, QuestionFormat, QuestionImage, QuestionItem, UncertaintyLevel


def _sample_material(*, with_images: bool = False) -> MaterialInput:
    """テスト用の教材入力を生成する。"""
    images = []
    if with_images:
        images = [
            QuestionImage(
                image_id="img-approved",
                base64_data="AQID",
                mime_type="image/png",
                alt_text="許可された図表",
                ai_usage_approved=True,
            ),
            QuestionImage(
                image_id="img-blocked",
                base64_data="BAUG",
                mime_type="image/png",
                alt_text="未許可の生徒写真",
                ai_usage_approved=False,
            ),
        ]

    return MaterialInput(
        material_id="mat-eng-01",
        material_name="英語文法復習ドリル",
        ai_usage_approved=True,
        questions=[
            QuestionItem(
                question_id="q1",
                question_number="問1",
                prompt="He ( ) to school every day. 連絡先 student01@example.ac.jp",
                choices=["go", "goes", "going", "gone"],
                question_format=QuestionFormat.SINGLE_CHOICE,
                images=images,
            ),
            QuestionItem(
                question_id="q2",
                question_number="問2",
                prompt="Select all prime numbers.",
                choices=["2", "3", "4", "5"],
                question_format=QuestionFormat.MULTIPLE_CHOICE,
            ),
        ],
    )


def _fast_config() -> AppConfig:
    """テスト用に再試行待機を短縮したAppConfigを返す。"""
    cfg = load_config(config_path="config.yaml", env_path=None)
    cfg.retry.base_delay_seconds = 0.001
    cfg.retry.max_delay_seconds = 0.002
    return cfg


def _valid_ai_json() -> str:
    """正常なAI解析JSON文字列を返す。"""
    return json.dumps(
        {
            "results": [
                {
                    "question_id": "q1",
                    "answer_candidates": ["goes"],
                    "explanation": "三人称単数現在の主語Heに続く現在形はgoes。",
                    "uncertainty": "low",
                    "confidence": 0.98,
                    "needs_review": False,
                    "review_reason": None,
                },
                {
                    "question_id": "q2",
                    "answer_candidates": ["2", "3", "5"],
                    "explanation": "2, 3, 5が素数であり、4は合成数。",
                    "uncertainty": "low",
                    "confidence": 0.99,
                    "needs_review": False,
                    "review_reason": None,
                },
            ]
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_gemini_provider_success_and_sanitization(monkeypatch: pytest.MonkeyPatch) -> None:
    """GeminiプロバイダーがPIIマスキングと未許可画像除外を行いつつ正常解析できることを確認。"""
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-gemini-test-key-12345678")
    captured_payload: dict[str, Any] = {}
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        captured_payload.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": _valid_ai_json()}]}}],
                "usageMetadata": {
                    "promptTokenCount": 120,
                    "candidatesTokenCount": 80,
                    "totalTokenCount": 200,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = create_provider(_fast_config(), provider_name="gemini", http_client=client)
        result = await provider.analyze(_sample_material(with_images=True))

    assert result.provider.value == "gemini"
    assert result.total_tokens == 200
    assert len(result.results) == 2
    assert result.results[0].answer_candidates == ["goes"]
    # ヘッダ認証でAPIキーを送信し、URLに含まない
    assert captured_headers.get("x-goog-api-key") == "dummy-gemini-test-key-12345678"
    # PIIが送信データに含まれないこと
    dumped = json.dumps(captured_payload, ensure_ascii=False)
    assert "student01@example.ac.jp" not in dumped
    assert "[MASKED_EMAIL]" in dumped
    # 許可画像(AQID)は送信され、未許可画像(BAUG)は除外されていること
    assert "AQID" in dumped
    assert "BAUG" not in dumped


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_name", ["openai", "deepseek", "openai_compatible"])
async def test_openai_family_providers_success(
    provider_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI / DeepSeek / OpenAI互換プロバイダーが正常動作し切り替えられることを確認。"""
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-test-key-123456")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "dummy-deepseek-test-key-123456")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://127.0.0.1:11434/v1")

    seen_url: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_url.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": f"```json\n{_valid_ai_json()}\n```"}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = create_provider(_fast_config(), provider_name=provider_name, http_client=client)
        result = await provider.analyze(_sample_material())

    assert result.provider.value == provider_name
    assert result.total_tokens == 150
    assert len(result.results) == 2
    if provider_name == "openai_compatible":
        assert seen_url[0].startswith("http://127.0.0.1:11434/v1/chat/completions")


@pytest.mark.asyncio
async def test_anthropic_provider_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anthropicプロバイダーが正常解析できることを確認。"""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic-test-key-123456")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-api-key") == "dummy-anthropic-test-key-123456"
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": _valid_ai_json()}],
                "usage": {"input_tokens": 90, "output_tokens": 60},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = create_provider(_fast_config(), provider_name="anthropic", http_client=client)
        result = await provider.analyze(_sample_material())

    assert result.provider.value == "anthropic"
    assert result.total_tokens == 150
    assert result.results[1].answer_candidates == ["2", "3", "5"]


@pytest.mark.asyncio
async def test_auth_error_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """401認証エラー時は再試行せず即座にProviderAuthErrorで停止することを確認。"""
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-invalid-openai-key")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "invalid api key"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = create_provider(_fast_config(), provider_name="openai", http_client=client)
        with pytest.raises(ProviderAuthError):
            await provider.analyze(_sample_material())

    assert calls == 1


@pytest.mark.asyncio
async def test_rate_limit_retries_up_to_three_times(monkeypatch: pytest.MonkeyPatch) -> None:
    """429エラー時は最大3回まで再試行し、3回目で成功または上限超過停止することを確認。"""
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-gemini-test-key-12345678")
    calls = 0

    def recover_on_third(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": _valid_ai_json()}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 10, "totalTokenCount": 20},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(recover_on_third)) as client:
        provider = create_provider(_fast_config(), provider_name="gemini", http_client=client)
        result = await provider.analyze(_sample_material())

    assert calls == 3
    assert len(result.results) == 2

    # 3回連続429で停止するケース
    always_fail_calls = 0

    def always_429(request: httpx.Request) -> httpx.Response:
        nonlocal always_fail_calls
        always_fail_calls += 1
        return httpx.Response(429, json={"error": "rate limit"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(always_429)) as client:
        provider = create_provider(_fast_config(), provider_name="gemini", http_client=client)
        with pytest.raises(ProviderRateLimitError):
            await provider.analyze(_sample_material())

    assert always_fail_calls == 3


@pytest.mark.asyncio
async def test_missing_question_marked_for_review_and_invalid_json_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """問題欠落時は確認対象になり、JSON破損時は再試行後にInvalidJsonFormatErrorとなることを確認。"""
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai-test-key-123456")
    partial_json = json.dumps(
        {
            "results": [
                {
                    "question_id": "q1",
                    "answer_candidates": ["goes"],
                    "explanation": "解説1",
                    "uncertainty": "low",
                    "confidence": 0.9,
                    "needs_review": False,
                }
            ]
        }
    )

    def partial_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": partial_json}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(partial_handler)) as client:
        provider = create_provider(_fast_config(), provider_name="openai", http_client=client)
        res = await provider.analyze(_sample_material())

    assert res.raw_valid is False
    assert res.results[1].question_id == "q2"
    assert res.results[1].needs_review is True
    assert res.results[1].uncertainty == UncertaintyLevel.HIGH

    # 完全破損JSONのケース
    def broken_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "これはJSONではありません"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken_handler)) as client:
        provider = create_provider(_fast_config(), provider_name="openai", http_client=client)
        with pytest.raises(InvalidJsonFormatError):
            await provider.analyze(_sample_material())
