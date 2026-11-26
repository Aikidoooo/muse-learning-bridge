"""外部AI APIプロバイダーのファクトリ関数。"""

from __future__ import annotations

import httpx

from src.core.cache import OptimizationCacheStore
from src.core.config import AppConfig
from src.core.exceptions import ProviderNotFoundError
from src.core.http_pool import HttpClientPool
from src.core.rate_limit import AsyncRateLimiter
from src.providers.anthropic import AnthropicProvider
from src.providers.base import BaseAIProvider
from src.providers.deepseek import DeepSeekProvider
from src.providers.gemini import GeminiProvider
from src.providers.openai import OpenAICompatibleProvider, OpenAIProvider
from src.schemas.enums import ProviderName

_PROVIDER_CLASSES: dict[ProviderName, type[BaseAIProvider]] = {
    ProviderName.GEMINI: GeminiProvider,
    ProviderName.DEEPSEEK: DeepSeekProvider,
    ProviderName.OPENAI: OpenAIProvider,
    ProviderName.ANTHROPIC: AnthropicProvider,
    ProviderName.OPENAI_COMPATIBLE: OpenAICompatibleProvider,
}


def create_provider(
    config: AppConfig,
    *,
    provider_name: str | ProviderName | None = None,
    model: str | None = None,
    api_key_override: str | None = None,
    secret_store: object | None = None,
    http_client: httpx.AsyncClient | None = None,
    cache_store: OptimizationCacheStore | None = None,
    rate_limiter: AsyncRateLimiter | None = None,
    http_pool: HttpClientPool | None = None,
    job_id: str | None = None,
) -> BaseAIProvider:
    """設定と指定プロバイダー名から対応するAIプロバイダーインスタンスを生成する。"""
    raw_name = (
        provider_name.value
        if isinstance(provider_name, ProviderName)
        else (provider_name or config.providers.default_provider)
    ).strip().lower()

    entry_cfg = config.providers.get_provider_config(raw_name)
    if entry_cfg is None:
        raise ProviderNotFoundError(
            f"未対応のAIプロバイダーが指定されました: {raw_name}",
            details={"provider": raw_name},
        )
    if not entry_cfg.enabled:
        raise ProviderNotFoundError(
            f"AIプロバイダー '{raw_name}' は現在無効化されています。",
            details={"provider": raw_name},
        )

    raw_type = (entry_cfg.provider_type or raw_name).strip().lower()
    try:
        enum_type = ProviderName(raw_type)
    except ValueError:
        enum_type = ProviderName.OPENAI_COMPATIBLE

    provider_cls = _PROVIDER_CLASSES[enum_type]
    return provider_cls(
        config=entry_cfg,
        retry_config=config.retry,
        provider_id=raw_name,
        model=model,
        api_key_override=api_key_override,
        secret_store=secret_store,
        allow_http_for_localhost=config.security.allow_http_for_localhost,
        mask_pii_before_send=config.security.mask_pii_in_logs_and_reports,
        http_client=http_client,
        optimization_config=config.optimization,
        security_config=config.security,
        cache_store=cache_store,
        rate_limiter=rate_limiter,
        http_pool=http_pool,
        job_id=job_id,
    )
