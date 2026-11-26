"""動的設定管理（スナップショット分離、検証、接続テスト、ロールバック、YAML永続化）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from src.core.config import AppConfig, ProviderEntryConfig, save_config
from src.core.exceptions import MuseBridgeError, PermissionDeniedError, ProviderNotFoundError
from src.core.logging_config import get_logger
from src.core.secret_store import GLOBAL_RUNTIME_SECRETS, RuntimeSecretStore
from src.core.security import validate_provider_base_url
from src.providers.factory import create_provider
from src.schemas.config_api import (
    ConfigPatchRequest,
    ConfigPatchResponse,
    CredentialStatusItem,
    CredentialsStatusResponse,
    ProviderModelsResponse,
    ProviderTestRequest,
    ProviderTestResult,
    PublicConfigResponse,
    PublicProviderConfigView,
)


class ConfigManager:
    """再起動なしで安全に設定を変更・永続化・ロールバックする設定マネージャー。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        config_path: str | Path = "config.yaml",
        secret_store: RuntimeSecretStore | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """設定マネージャーを初期化する。"""
        self._config = config.model_copy(deep=True)
        self.config_path = Path(config_path)
        self.secret_store = secret_store or GLOBAL_RUNTIME_SECRETS
        self._http_client = http_client
        self._lock = asyncio.Lock()
        self.logger = get_logger("core.config_manager")

    def get_snapshot(self) -> AppConfig:
        """現在の設定のディープコピーを返す（実行中ジョブの隔離用）。"""
        return self._config.model_copy(deep=True)

    def build_public_config(self, cfg: AppConfig | None = None) -> PublicConfigResponse:
        """APIキーを含まない公開用設定レスポンスを構築する。"""
        target = cfg or self._config
        providers_view: dict[str, PublicProviderConfigView] = {}
        for name, entry in target.providers.all_providers().items():
            configured, source = entry.resolve_credential_status(
                name,
                secret_store=self.secret_store,
            )
            providers_view[name] = PublicProviderConfigView(
                name=name,
                builtin=target.providers.is_builtin(name),
                provider_type=(entry.provider_type or (name if target.providers.is_builtin(name) else "openai_compatible")),
                enabled=entry.enabled,
                display_name=entry.display_name,
                default_model=entry.default_model,
                base_url=entry.resolve_base_url(),
                api_key_env=entry.api_key_env,
                models=list(entry.models),
                input_cost_per_1m_usd=entry.input_cost_per_1m_usd,
                output_cost_per_1m_usd=entry.output_cost_per_1m_usd,
                api_key_configured=configured,
                credential_source=source,
            )

        return PublicConfigResponse(
            app=target.app.model_dump(mode="json"),
            security=target.security.model_dump(mode="json"),
            retry=target.retry.model_dump(mode="json"),
            default_provider=target.providers.default_provider,
            providers=providers_view,
        )

    def get_credentials_status(self) -> CredentialsStatusResponse:
        """全プロバイダーの認証情報登録状態を返す（平文APIキーは絶対に返さない）。"""
        cfg = self.get_snapshot()
        items: list[CredentialStatusItem] = []
        for name, entry in cfg.providers.all_providers().items():
            configured, source = entry.resolve_credential_status(
                name,
                secret_store=self.secret_store,
            )
            p_type = (entry.provider_type or name).strip().lower()
            required = p_type != "openai_compatible"
            items.append(
                CredentialStatusItem(
                    provider=name,
                    display_name=entry.display_name,
                    api_key_env=entry.api_key_env,
                    configured=configured,
                    source=source,
                    header_injection_supported=True,
                    required=required,
                )
            )
        return CredentialsStatusResponse(
            secure_storage_policy=(
                "APIキーはMuse Secure Credentials Storeからのリクエストヘッダ注入(X-Provider-Api-Key)、"
                "プロセス内揮発メモリ、または環境変数でのみ参照され、YAML/JSON/SQLite/ログへは保存されません。"
            ),
            recommended_headers=["Authorization: Bearer <admin_token>", "X-Provider-Api-Key: <provider_api_key>"],
            items=items,
        )

    async def test_provider_connection(
        self,
        req: ProviderTestRequest,
        *,
        cfg: AppConfig | None = None,
        header_api_key: str | None = None,
    ) -> ProviderTestResult:
        """指定プロバイダーに対して実際の接続テストを実行する。"""
        target_cfg = (cfg or self.get_snapshot()).model_copy(deep=True)
        prov_name = (req.provider or target_cfg.providers.default_provider).strip().lower()

        if req.base_url:
            validated_url = validate_provider_base_url(
                req.base_url,
                allow_http_for_localhost=target_cfg.security.allow_http_for_localhost,
            )
            entry = target_cfg.providers.get_provider_config(prov_name)
            if entry is None:
                raise ProviderNotFoundError(
                    f"未対応のプロバイダーです: {prov_name}",
                    details={"provider": prov_name},
                )
            entry.base_url = validated_url

        provider_inst = create_provider(
            target_cfg,
            provider_name=prov_name,
            model=req.model,
            api_key_override=header_api_key,
            secret_store=self.secret_store,
            http_client=self._http_client,
        )
        return await provider_inst.test_connection()

    async def get_provider_models(
        self,
        *,
        provider: str | None = None,
        remote: bool = True,
        header_api_key: str | None = None,
    ) -> ProviderModelsResponse:
        """プロバイダーの対応モデル一覧を取得する（失敗時または非対応時は設定モデルへフォールバック）。"""
        cfg = self.get_snapshot()
        prov_name = (provider or cfg.providers.default_provider).strip().lower()
        entry = cfg.providers.get_provider_config(prov_name)
        if entry is None:
            raise ProviderNotFoundError(
                f"未対応のプロバイダーです: {prov_name}",
                details={"provider": prov_name},
            )

        if remote:
            try:
                provider_inst = create_provider(
                    cfg,
                    provider_name=prov_name,
                    api_key_override=header_api_key,
                    secret_store=self.secret_store,
                    http_client=self._http_client,
                )
                remote_models = await provider_inst.list_remote_models()
                if remote_models:
                    return ProviderModelsResponse(
                        provider=prov_name,
                        default_model=entry.default_model,
                        models=remote_models,
                        source="remote",
                    )
            except MuseBridgeError as exc:
                self.logger.warning(
                    "リモートモデル一覧取得に失敗したため設定済み一覧へフォールバックします (provider=%s, code=%s)",
                    prov_name,
                    exc.code,
                )
                return ProviderModelsResponse(
                    provider=prov_name,
                    default_model=entry.default_model,
                    models=list(entry.models),
                    source="configured",
                    note=f"リモート取得不可のため設定値を返却しました ({exc.code})",
                )

        return ProviderModelsResponse(
            provider=prov_name,
            default_model=entry.default_model,
            models=list(entry.models),
            source="configured",
        )

    def _set_provider_entry(
        self,
        cfg: AppConfig,
        name: str,
        entry: ProviderEntryConfig,
    ) -> None:
        """AppConfig内の該当プロバイダー定義を更新または追加する。"""
        normalized = name.strip().lower()
        if normalized == "gemini":
            cfg.providers.gemini = entry
        elif normalized == "deepseek":
            cfg.providers.deepseek = entry
        elif normalized == "openai":
            cfg.providers.openai = entry
        elif normalized == "anthropic":
            cfg.providers.anthropic = entry
        elif normalized == "openai_compatible":
            cfg.providers.openai_compatible = entry
        else:
            cfg.providers.custom_providers[normalized] = entry

    async def apply_patch(
        self,
        patch: ConfigPatchRequest,
        *,
        header_api_key: str | None = None,
    ) -> ConfigPatchResponse:
        """接続テスト成功時のみ設定を確定・永続化し、失敗時は自動ロールバックする。"""
        async with self._lock:
            prev_cfg = self._config.model_copy(deep=True)
            prev_secrets = self.secret_store.snapshot()
            candidate = prev_cfg.model_copy(deep=True)
            needs_connection_test = False
            test_target_provider = (
                patch.target_provider
                or patch.default_provider
                or candidate.providers.default_provider
            ).strip().lower()
            test_target_model: str | None = patch.default_model

            try:
                # 1. プロバイダー登録・更新
                if patch.register_or_update_provider is not None:
                    spec = patch.register_or_update_provider
                    p_name = spec.name.strip().lower()
                    existing = candidate.providers.get_provider_config(p_name)

                    if existing is None:
                        if not spec.default_model or not spec.base_url:
                            raise MuseBridgeError(
                                "新規プロバイダー登録には default_model と base_url が必須です。",
                                code="INVALID_PROVIDER_SPEC",
                                retryable=False,
                                http_status=400,
                            )
                        validated_url = validate_provider_base_url(
                            spec.base_url,
                            allow_http_for_localhost=candidate.security.allow_http_for_localhost,
                        )
                        new_entry = ProviderEntryConfig(
                            enabled=True if spec.enabled is None else spec.enabled,
                            display_name=spec.display_name or p_name,
                            default_model=spec.default_model,
                            base_url=validated_url,
                            api_key_env=spec.api_key_env or f"{p_name.upper().replace('-', '_')}_API_KEY",
                            models=spec.models or [spec.default_model],
                            input_cost_per_1m_usd=spec.input_cost_per_1m_usd or 0.0,
                            output_cost_per_1m_usd=spec.output_cost_per_1m_usd or 0.0,
                            provider_type=spec.provider_type or "openai_compatible",
                        )
                        self._set_provider_entry(candidate, p_name, new_entry)
                    else:
                        updated_entry = existing.model_copy(deep=True)
                        if spec.display_name is not None:
                            updated_entry.display_name = spec.display_name
                        if spec.enabled is not None:
                            updated_entry.enabled = spec.enabled
                        if spec.default_model is not None:
                            updated_entry.default_model = spec.default_model
                            if spec.default_model not in updated_entry.models:
                                updated_entry.models.append(spec.default_model)
                        if spec.base_url is not None:
                            updated_entry.base_url = validate_provider_base_url(
                                spec.base_url,
                                allow_http_for_localhost=candidate.security.allow_http_for_localhost,
                            )
                        if spec.api_key_env is not None:
                            updated_entry.api_key_env = spec.api_key_env
                        if spec.models is not None:
                            updated_entry.models = list(spec.models)
                        if spec.input_cost_per_1m_usd is not None:
                            updated_entry.input_cost_per_1m_usd = spec.input_cost_per_1m_usd
                        if spec.output_cost_per_1m_usd is not None:
                            updated_entry.output_cost_per_1m_usd = spec.output_cost_per_1m_usd
                        if spec.provider_type is not None and not candidate.providers.is_builtin(p_name):
                            updated_entry.provider_type = spec.provider_type
                        self._set_provider_entry(candidate, p_name, updated_entry)

                    test_target_provider = p_name
                    test_target_model = (
                        spec.default_model
                        or candidate.providers.get_provider_config(p_name).default_model  # type: ignore[union-attr]
                    )
                    needs_connection_test = True

                # 2. プロバイダー削除・無効化
                if patch.delete_provider is not None:
                    del_name = patch.delete_provider.strip().lower()
                    if del_name == (patch.default_provider or candidate.providers.default_provider):
                        raise PermissionDeniedError(
                            f"現在デフォルトに設定されているプロバイダー '{del_name}' は削除できません。",
                            details={"provider": del_name},
                        )
                    if candidate.providers.is_builtin(del_name):
                        builtin_entry = candidate.providers.get_provider_config(del_name)
                        assert builtin_entry is not None
                        builtin_entry.enabled = False
                        self._set_provider_entry(candidate, del_name, builtin_entry)
                    elif del_name in candidate.providers.custom_providers:
                        del candidate.providers.custom_providers[del_name]
                    else:
                        raise ProviderNotFoundError(
                            f"削除対象のプロバイダーが見つかりません: {del_name}",
                            details={"provider": del_name},
                        )
                    self.secret_store.delete_secret(del_name)

                # 3. デフォルトプロバイダー変更
                if patch.default_provider is not None:
                    new_def = patch.default_provider.strip().lower()
                    entry = candidate.providers.get_provider_config(new_def)
                    if entry is None or not entry.enabled:
                        raise ProviderNotFoundError(
                            f"デフォルトに設定しようとしたプロバイダー '{new_def}' が存在しないか無効です。",
                            details={"provider": new_def},
                        )
                    candidate.providers.default_provider = new_def
                    test_target_provider = new_def
                    needs_connection_test = True

                # 4. デフォルトモデル変更
                if patch.default_model is not None:
                    target_p = (
                        patch.target_provider or candidate.providers.default_provider
                    ).strip().lower()
                    entry = candidate.providers.get_provider_config(target_p)
                    if entry is None or not entry.enabled:
                        raise ProviderNotFoundError(
                            f"対象プロバイダー '{target_p}' が存在しないか無効です。",
                            details={"provider": target_p},
                        )
                    entry.default_model = patch.default_model.strip()
                    if entry.default_model not in entry.models:
                        entry.models.append(entry.default_model)
                    self._set_provider_entry(candidate, target_p, entry)
                    test_target_provider = target_p
                    test_target_model = entry.default_model
                    needs_connection_test = True

                # 5. 料金上限・タイムアウト・リトライ設定
                if patch.max_cost_per_job_usd is not None:
                    candidate.security.max_cost_per_job_usd = patch.max_cost_per_job_usd
                if patch.max_daily_cost_usd is not None:
                    candidate.security.max_daily_cost_usd = patch.max_daily_cost_usd
                if patch.max_questions_per_job is not None:
                    candidate.security.max_questions_per_job = patch.max_questions_per_job
                if patch.timeout_seconds is not None:
                    candidate.retry.timeout_seconds = patch.timeout_seconds
                if patch.max_attempts is not None:
                    candidate.retry.max_attempts = patch.max_attempts

                # 6. ヘッダ注入APIキーの揮発メモリ一時適用（ディスクには絶対書かない）
                if header_api_key and header_api_key.strip() and patch.save_header_api_key_in_memory:
                    self.secret_store.set_secret(test_target_provider, header_api_key.strip())
                    needs_connection_test = True

                # 7. 接続テストの実行（verify_connection=Trueが既定、削除のみ時は任意）
                test_result: ProviderTestResult | None = None
                if patch.verify_connection and (
                    needs_connection_test or patch.delete_provider is None
                ):
                    target_entry = candidate.providers.get_provider_config(test_target_provider)
                    if target_entry is not None and target_entry.enabled:
                        test_result = await self.test_provider_connection(
                            ProviderTestRequest(
                                provider=test_target_provider,
                                model=test_target_model or target_entry.default_model,
                            ),
                            cfg=candidate,
                            header_api_key=header_api_key,
                        )

                # 8. 接続テスト成功後のみ永続化・反映
                save_config(candidate, self.config_path)
                self._config = candidate
                self.logger.info(
                    "設定を動的に更新・永続化しました (default_provider=%s, tested=%s)",
                    candidate.providers.default_provider,
                    test_result is not None,
                )
                return ConfigPatchResponse(
                    updated=True,
                    rolled_back=False,
                    message="設定を検証し、再起動なしで反映・保存しました（進行中ジョブには影響しません）。",
                    connection_test=test_result,
                    config=self.build_public_config(candidate),
                )

            except Exception:
                # 失敗時は完全に以前の設定とシークレット状態を復元（ロールバック）
                self._config = prev_cfg
                self.secret_store.restore(prev_secrets)
                self.logger.warning("設定変更の検証または接続テストに失敗したためロールバックしました。")
                raise
