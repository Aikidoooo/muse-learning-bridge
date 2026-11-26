"""FastAPI ルーターおよびエラーハンドラ定義。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.core.auth import require_admin_auth
from src.core.config import AppConfig, load_config
from src.core.config_manager import ConfigManager
from src.core.exceptions import MuseBridgeError
from src.core.logging_config import setup_logging
from src.jobs.manager import JobManager
from src.schemas.config_api import (
    ConfigPatchRequest,
    ConfigPatchResponse,
    CredentialsStatusResponse,
    ProviderModelsResponse,
    ProviderTestRequest,
    ProviderTestResult,
    PublicConfigResponse,
)
from src.schemas.job import JobCreateRequest, JobStatusResponse, ProviderInfo
from src.schemas.report import MuseReport


class ScreenshotAttachRequest(BaseModel):
    """許可済みスクリーンショット登録用リクエスト。"""

    screenshot_paths: list[str] = Field(default_factory=list)


def create_router(manager: JobManager, config_manager: ConfigManager) -> APIRouter:
    """仕様書および追加仕様に基づくFastAPIルーターを構築する。"""
    router = APIRouter()

    @router.get("/health", summary="稼働確認")
    async def health() -> dict[str, Any]:
        """サーバーと設定の稼働状況を返す。"""
        cfg = config_manager.get_snapshot()
        return {
            "status": "ok",
            "app_name": cfg.app.name,
            "version": cfg.app.version,
            "default_provider": cfg.providers.default_provider,
        }

    @router.get("/providers", response_model=list[ProviderInfo], summary="対応AI一覧取得")
    async def list_providers() -> list[ProviderInfo]:
        """利用可能な外部AIプロバイダーとモデル一覧を返す（APIキー値は含めない）。"""
        cfg = config_manager.get_snapshot()
        items: list[ProviderInfo] = []
        for name, entry in cfg.providers.all_providers().items():
            configured, source = entry.resolve_credential_status(
                name,
                secret_store=config_manager.secret_store,
            )
            p_type = entry.provider_type or (name if cfg.providers.is_builtin(name) else "openai_compatible")
            items.append(
                ProviderInfo(
                    name=name,
                    display_name=entry.display_name,
                    enabled=entry.enabled,
                    default_model=entry.default_model,
                    models=entry.models,
                    api_key_configured=configured,
                    builtin=cfg.providers.is_builtin(name),
                    provider_type=p_type,
                    credential_source=source,
                )
            )
        return items

    @router.get(
        "/config",
        response_model=PublicConfigResponse,
        dependencies=[Depends(require_admin_auth)],
        summary="現在の設定取得（管理認証必須）",
    )
    async def get_config() -> PublicConfigResponse:
        """現在の動的設定を取得する（APIキーは絶対に返却しない）。"""
        return config_manager.build_public_config()

    @router.patch(
        "/config",
        response_model=ConfigPatchResponse,
        dependencies=[Depends(require_admin_auth)],
        summary="設定変更・プロバイダー管理（管理認証必須・接続テストとロールバック付き）",
    )
    async def patch_config(
        body: ConfigPatchRequest,
        x_provider_api_key: str | None = Header(default=None),
    ) -> ConfigPatchResponse:
        """Museからの指示で動的に設定を変更し、接続テスト成功時のみ永続化する。"""
        return await config_manager.apply_patch(body, header_api_key=x_provider_api_key)

    @router.post(
        "/providers/test",
        response_model=ProviderTestResult,
        dependencies=[Depends(require_admin_auth)],
        summary="プロバイダー接続テスト（管理認証必須）",
    )
    async def test_provider(
        body: ProviderTestRequest,
        x_provider_api_key: str | None = Header(default=None),
    ) -> ProviderTestResult:
        """指定されたプロバイダー・モデル・通信先への疎通確認を実施する。"""
        return await config_manager.test_provider_connection(
            body,
            header_api_key=x_provider_api_key,
        )

    @router.get(
        "/providers/models",
        response_model=ProviderModelsResponse,
        dependencies=[Depends(require_admin_auth)],
        summary="対応モデル一覧取得（管理認証必須）",
    )
    async def get_provider_models(
        provider: str | None = Query(default=None, description="プロバイダー名（未指定時は既定）"),
        remote: bool = Query(default=True, description="プロバイダーAPIから最新モデル取得を試みるか"),
        x_provider_api_key: str | None = Header(default=None),
    ) -> ProviderModelsResponse:
        """プロバイダーが対応するモデル一覧を取得する。"""
        return await config_manager.get_provider_models(
            provider=provider,
            remote=remote,
            header_api_key=x_provider_api_key,
        )

    @router.get(
        "/credentials/status",
        response_model=CredentialsStatusResponse,
        dependencies=[Depends(require_admin_auth)],
        summary="認証情報の登録状態確認（管理認証必須・キー値非返却）",
    )
    async def get_credentials_status() -> CredentialsStatusResponse:
        """各プロバイダーの認証情報登録ステータス（有無と取得元のみ）を返す。"""
        return config_manager.get_credentials_status()

    @router.post("/jobs", response_model=JobStatusResponse, status_code=202, summary="解析ジョブ作成")
    async def create_job(
        payload: JobCreateRequest,
        wait: bool = Query(default=False, description="完了まで待機する場合はtrue"),
        x_provider_api_key: str | None = Header(default=None),
    ) -> JobStatusResponse:
        """教材データを受け取り、解析ジョブを作成する。"""
        created = manager.create_job(payload, header_api_key=x_provider_api_key)
        if wait:
            return await manager.execute_job(created.job_id)
        manager.start_background_job(created.job_id)
        return created

    @router.get("/jobs", response_model=list[JobStatusResponse], summary="ジョブ一覧取得")
    async def list_jobs(limit: int = Query(default=20, ge=1, le=100)) -> list[JobStatusResponse]:
        """最近のジョブ一覧を返す。"""
        return manager.list_jobs(limit=limit)

    @router.get("/jobs/{job_id}", response_model=JobStatusResponse, summary="実行状況取得")
    async def get_job_status(job_id: str) -> JobStatusResponse:
        """ジョブIDを指定して現在のステータスを取得する。"""
        return manager.get_status(job_id)

    @router.get("/jobs/{job_id}/events", summary="ステータス遷移履歴取得")
    async def get_job_events(job_id: str) -> list[dict[str, Any]]:
        """ジョブのステータス遷移履歴を返す。"""
        return manager.get_events(job_id)

    @router.get("/jobs/{job_id}/result", response_model=MuseReport, summary="結果取得")
    async def get_job_result(job_id: str) -> MuseReport:
        """完了したジョブのMuse向け結果レポートを取得する。"""
        return manager.get_result(job_id)

    @router.post("/jobs/{job_id}/cancel", response_model=JobStatusResponse, summary="実行中止")
    async def cancel_job(job_id: str) -> JobStatusResponse:
        """指定されたジョブを中止する。"""
        return manager.cancel_job(job_id)

    @router.post("/jobs/{job_id}/resume", response_model=JobStatusResponse, summary="保存済み部分結果から再開")
    async def resume_job(
        job_id: str,
        wait: bool = Query(default=True, description="完了まで待機する場合はtrue"),
        x_provider_api_key: str | None = Header(default=None),
    ) -> JobStatusResponse:
        """途中失敗または中止されたジョブの保存済み部分結果を再利用し、未処理分のみ再実行する。"""
        if wait:
            return await manager.resume_job(job_id, header_api_key=x_provider_api_key)
        manager.start_background_job(job_id)
        return manager.get_status(job_id)

    @router.get("/usage", summary="API使用量・トークン・モデル別処理時間・無料枠状況取得")
    async def get_usage(
        today_only: bool = Query(default=True, description="trueでUTC本日分、falseで全期間"),
    ) -> dict[str, Any]:
        """APIリクエスト回数・入力/出力/思考トークン・モデル別処理時間・キャッシュ統計を返す。"""
        return manager.get_usage_summary(today_only=today_only)

    @router.post(
        "/jobs/{job_id}/screenshots",
        response_model=MuseReport,
        summary="許可された結果画面スクリーンショット登録",
    )
    async def attach_screenshots(job_id: str, body: ScreenshotAttachRequest) -> MuseReport:
        """Muse側で撮影した許可済み結果画面のスクリーンショットパスをレポートに追加する。"""
        return manager.attach_screenshots(job_id, body.screenshot_paths)

    return router


def create_app(
    config: AppConfig | None = None,
    *,
    manager: JobManager | None = None,
    config_manager: ConfigManager | None = None,
) -> FastAPI:
    """FastAPIアプリケーションインスタンスを生成する。"""
    if config_manager is not None:
        resolved_cfg_manager = config_manager
        resolved_config = config_manager.get_snapshot()
    elif manager is not None:
        resolved_cfg_manager = manager.config_manager
        resolved_config = resolved_cfg_manager.get_snapshot()
    else:
        resolved_config = config or load_config()
        resolved_cfg_manager = ConfigManager(resolved_config)

    setup_logging(logs_dir=resolved_config.app.logs_dir, level=resolved_config.app.log_level)
    resolved_manager = manager or JobManager(resolved_cfg_manager)

    app = FastAPI(
        title=resolved_config.app.name,
        version=resolved_config.app.version,
        description="Meta MuseとPython・外部AI APIを連携させる学習支援自動化システム API",
    )
    app.state.config_manager = resolved_cfg_manager
    app.state.job_manager = resolved_manager

    @app.exception_handler(MuseBridgeError)
    async def muse_bridge_error_handler(_: Request, exc: MuseBridgeError) -> JSONResponse:
        """独自業務例外を構造化JSONエラーレスポンスへ変換する。"""
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "details": exc.details,
                }
            },
        )

    app.include_router(create_router(resolved_manager, resolved_cfg_manager))
    return app
