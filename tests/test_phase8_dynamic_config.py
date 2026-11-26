"""Phase 8: MuseによるAI API動的設定・認証・接続テスト・ロールバック・実行中ジョブ隔離テスト。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.config import AppConfig, load_config
from src.core.config_manager import ConfigManager
from src.core.secret_store import RuntimeSecretStore
from src.jobs.manager import JobManager


def _make_isolated_env(
    tmp_path: Path,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[Path, ConfigManager, JobManager, TestClient]:
    """独立したconfig.yaml・SQLite・RuntimeSecretStoreを持つテスト環境を作成する。"""
    cfg_path = tmp_path / "config.yaml"
    raw = Path("config.yaml").read_text(encoding="utf-8")
    raw = (
        raw.replace('data_dir: "data"', f'data_dir: "{(tmp_path / "data").as_posix()}"')
        .replace('logs_dir: "logs"', f'logs_dir: "{(tmp_path / "logs").as_posix()}"')
        .replace(
            'db_path: "data/muse_bridge.db"',
            f'db_path: "{(tmp_path / "data" / "dyn.db").as_posix()}"',
        )
        .replace("base_delay_seconds: 1.0", "base_delay_seconds: 0.001")
    )
    cfg_path.write_text(raw, encoding="utf-8")

    cfg = load_config(config_path=cfg_path, env_path=None)
    secret_store = RuntimeSecretStore()
    cfg_manager = ConfigManager(
        cfg,
        config_path=cfg_path,
        secret_store=secret_store,
        http_client=http_client,
    )
    job_manager = JobManager(cfg_manager)
    app = create_app(manager=job_manager, config_manager=cfg_manager)
    return cfg_path, cfg_manager, job_manager, TestClient(app)


def test_admin_endpoints_require_auth_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理トークン未設定および誤トークン時に全管理エンドポイントが401拒否されることを確認。"""
    monkeypatch.delenv("MUSE_BRIDGE_ADMIN_TOKEN", raising=False)
    _, _, _, client = _make_isolated_env(tmp_path)

    for method, path, kwargs in [
        ("GET", "/config", {}),
        ("PATCH", "/config", {"json": {"max_cost_per_job_usd": 0.3}}),
        ("POST", "/providers/test", {"json": {"provider": "gemini"}}),
        ("GET", "/providers/models", {}),
        ("GET", "/credentials/status", {}),
    ]:
        resp = client.request(method, path, **kwargs)
        assert resp.status_code == 401, f"{method} {path} should require auth"

    # 正しいトークン設定後でも不正トークンは401、正トークンで200
    monkeypatch.setenv("MUSE_BRIDGE_ADMIN_TOKEN", "admin-secret-token-2026")
    assert client.get("/config", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok_resp = client.get("/config", headers={"Authorization": "Bearer admin-secret-token-2026"})
    assert ok_resp.status_code == 200
    assert ok_resp.json()["default_provider"] == "gemini"


def test_credentials_status_and_remote_models_never_expose_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/credentials/status と /providers/models がキー値を一切返さずリモート一覧を取得できることを確認。"""
    monkeypatch.setenv("MUSE_BRIDGE_ADMIN_TOKEN", "admin-token-xyz")
    secret_gemini = "dummy-gemini-secret-check-12345"
    monkeypatch.setenv("GEMINI_API_KEY", secret_gemini)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and str(req.url).endswith("/models"):
            if "generativelanguage.googleapis.com" in str(req.url):
                return httpx.Response(
                    200,
                    json={"models": [{"name": "models/gemini-2.5-flash"}, {"name": "models/gemini-2.5-pro"}]},
                )
            if "api.anthropic.com" in str(req.url):
                return httpx.Response(
                    200,
                    json={"data": [{"id": "claude-3-7-sonnet-latest"}]},
                )
        return httpx.Response(404)

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    _, _, _, client = _make_isolated_env(tmp_path, http_client=async_client)
    auth = {"Authorization": "Bearer admin-token-xyz"}

    cred_resp = client.get("/credentials/status", headers=auth)
    assert cred_resp.status_code == 200
    assert secret_gemini not in cred_resp.text
    gemini_item = next(i for i in cred_resp.json()["items"] if i["provider"] == "gemini")
    assert gemini_item["configured"] is True
    assert gemini_item["source"] == "environment"

    models_resp = client.get("/providers/models?provider=gemini&remote=true", headers=auth)
    assert models_resp.status_code == 200
    assert models_resp.json()["source"] == "remote"
    assert "gemini-2.5-pro" in models_resp.json()["models"]

    # Custom Connector 経由の X-Provider-Api-Key ヘッダで Anthropic モデル一覧も取得可能
    anth_models = client.get(
        "/providers/models?provider=anthropic&remote=true",
        headers={**auth, "X-Provider-Api-Key": "dummy-ant-header-injected-999"},
    )
    assert anth_models.status_code == 200
    assert anth_models.json()["models"] == ["claude-3-7-sonnet-latest"]
    assert "dummy-ant-header-injected-999" not in anth_models.text


def test_patch_config_https_validation_and_rollback_on_test_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTPS強制検証、および接続テスト失敗時のメモリ・YAML・シークレット完全ロールバックを確認。"""
    monkeypatch.setenv("MUSE_BRIDGE_ADMIN_TOKEN", "admin-token-xyz")
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-initial-valid-key-1234")

    def handler(req: httpx.Request) -> httpx.Response:
        # openai のキーが不正という想定で 401 を返す
        if "api.openai.com" in str(req.url):
            return httpx.Response(401, json={"error": "invalid api key"})
        return httpx.Response(200, json={"models": [{"name": "models/gemini-2.5-flash"}]})

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg_path, cfg_manager, _, client = _make_isolated_env(tmp_path, http_client=async_client)
    auth = {"Authorization": "Bearer admin-token-xyz"}
    before_yaml = cfg_path.read_text(encoding="utf-8")

    # 1. リクエストJSONボディへの平文api_key混入はextra="forbid"で拒否される
    bad_body_resp = client.patch(
        "/config",
        headers=auth,
        json={"default_provider": "openai", "api_key": "dummy-plaintext-not-allowed"},
    )
    assert bad_body_resp.status_code == 422

    # 2. 外部平文HTTP URLの登録は拒否される
    http_url_resp = client.patch(
        "/config",
        headers=auth,
        json={
            "register_or_update_provider": {
                "name": "insecure-ext",
                "default_model": "m1",
                "base_url": "http://api.external-unencrypted.example.com/v1",
            }
        },
    )
    assert http_url_resp.status_code == 403

    # 3. OpenAIへ切り替え＋メモリシークレット登録を試みるが接続テスト401で失敗 -> 完全ロールバック
    fail_patch = client.patch(
        "/config",
        headers={**auth, "X-Provider-Api-Key": "dummy-bad-openai-key-123456"},
        json={
            "default_provider": "openai",
            "default_model": "gpt-4o",
            "save_header_api_key_in_memory": True,
            "max_cost_per_job_usd": 1.25,
            "timeout_seconds": 90.0,
        },
    )
    assert fail_patch.status_code == 401
    # ロールバック確認: メモリ設定、シークレット、YAMLすべて元のまま
    snap = cfg_manager.get_snapshot()
    assert snap.providers.default_provider == "gemini"
    assert snap.security.max_cost_per_job_usd == 0.50
    assert snap.retry.timeout_seconds == 45.0
    assert cfg_manager.secret_store.get_secret("openai") == ""
    assert cfg_path.read_text(encoding="utf-8") == before_yaml


def test_patch_config_register_update_delete_persist_without_saving_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """プロバイダー登録・変更・削除・永続化が再起動なしで反映され、APIキーが保存されないことを確認。"""
    monkeypatch.setenv("MUSE_BRIDGE_ADMIN_TOKEN", "admin-token-xyz")
    header_secret = "dummy-groq-runtime-secret-8888"

    def handler(req: httpx.Request) -> httpx.Response:
        if "api.groq.com" in str(req.url) and str(req.url).endswith("/models"):
            if req.headers.get("authorization") == f"Bearer {header_secret}":
                return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile"}]})
            return httpx.Response(401)
        return httpx.Response(200, json={"data": []})

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cfg_path, cfg_manager, _, client = _make_isolated_env(tmp_path, http_client=async_client)
    auth = {"Authorization": "Bearer admin-token-xyz"}

    # カスタムプロバイダー groq を登録してデフォルトへ切り替え（HTTPS URL + ヘッダ注入キー）
    reg_resp = client.patch(
        "/config",
        headers={**auth, "X-Provider-Api-Key": header_secret},
        json={
            "register_or_update_provider": {
                "name": "groq",
                "display_name": "Groq Cloud",
                "default_model": "llama-3.3-70b-versatile",
                "base_url": "https://api.groq.com/openai/v1",
                "provider_type": "openai_compatible",
            },
            "default_provider": "groq",
            "save_header_api_key_in_memory": True,
            "max_cost_per_job_usd": 0.75,
            "timeout_seconds": 60.0,
        },
    )
    assert reg_resp.status_code == 200
    body = reg_resp.json()
    assert body["updated"] is True
    assert body["connection_test"]["success"] is True
    assert body["config"]["default_provider"] == "groq"
    assert body["config"]["providers"]["groq"]["api_key_configured"] is True
    assert body["config"]["providers"]["groq"]["credential_source"] == "runtime_secret"

    # 再起動なしで GET /providers に groq が現れる
    prov_list = client.get("/providers").json()
    assert any(p["name"] == "groq" and p["default_model"] == "llama-3.3-70b-versatile" for p in prov_list)

    # YAML・ログ・レスポンスのどこにも平文シークレットが保存・出力されていないこと
    saved_yaml = cfg_path.read_text(encoding="utf-8")
    assert "groq" in saved_yaml
    assert header_secret not in saved_yaml
    assert header_secret not in reg_resp.text
    log_file = tmp_path / "logs" / "muse_bridge.log"
    if log_file.exists():
        assert header_secret not in log_file.read_text(encoding="utf-8")

    # デフォルトを openai_compatible へ戻してから groq を削除
    switch_back = client.patch(
        "/config",
        headers=auth,
        json={"default_provider": "openai_compatible"},
    )
    assert switch_back.status_code == 200

    del_resp = client.patch(
        "/config",
        headers=auth,
        json={"delete_provider": "groq", "verify_connection": False},
    )
    assert del_resp.status_code == 200
    assert "groq" not in del_resp.json()["config"]["providers"]
    assert cfg_manager.secret_store.get_secret("groq") == ""


@pytest.mark.asyncio
async def test_running_job_isolated_from_config_patch_and_next_job_uses_new_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実行中のジョブには設定変更を反映せず、次のジョブから新設定が反映されることを確認。"""
    monkeypatch.setenv("MUSE_BRIDGE_ADMIN_TOKEN", "admin-token-xyz")
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-job-iso-gemini-12345")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-job-iso-openai-12345")

    cfg_path, cfg_manager, job_manager, _ = _make_isolated_env(tmp_path)

    observed_calls: list[tuple[str, str]] = []
    job1_started = asyncio.Event()
    allow_job1_finish = asyncio.Event()

    class StubProvider:
        def __init__(self, pid: str, model: str) -> None:
            self.provider_id = pid
            self.model = model

        async def analyze(self, material):
            from src.schemas.analysis import ProviderCallResult, QuestionAnalysisResult
            from src.schemas.enums import UncertaintyLevel

            observed_calls.append((self.provider_id, self.model))
            if len(observed_calls) == 1:
                job1_started.set()
                await allow_job1_finish.wait()
            return ProviderCallResult(
                provider=self.provider_id,
                model=self.model,
                results=[
                    QuestionAnalysisResult(
                        question_id=material.questions[0].question_id,
                        answer_candidates=["A"],
                        explanation="解説",
                        uncertainty=UncertaintyLevel.LOW,
                        confidence=0.95,
                        needs_review=False,
                    )
                ],
                input_tokens=10,
                output_tokens=10,
                total_tokens=20,
                estimated_cost_usd=0.0001,
            )

    def fake_factory(cfg: AppConfig, *, provider_name=None, model=None, **kwargs):
        p_id = (
            provider_name.value
            if hasattr(provider_name, "value")
            else (provider_name or cfg.providers.default_provider)
        )
        entry = cfg.providers.get_provider_config(p_id)
        m_name = model or entry.default_model
        return StubProvider(p_id, m_name)

    monkeypatch.setattr("src.jobs.manager.create_provider", fake_factory)

    from src.schemas.config_api import ConfigPatchRequest
    from src.schemas.question import MaterialInput

    mat1 = MaterialInput.model_validate(
        {
            "material_id": "iso-1",
            "material_name": "教材1",
            "ai_usage_approved": True,
            "questions": [{"question_id": "q1", "question_number": "問1", "prompt": "Q1"}],
        }
    )
    job1 = job_manager.create_job(mat1)
    assert job1.provider.value == "gemini"
    task1 = job_manager.start_background_job(job1.job_id)
    await job1_started.wait()

    # ジョブ1の実行中にデフォルト設定を openai / gpt-4o へ変更
    patch_res = await cfg_manager.apply_patch(
        ConfigPatchRequest(
            default_provider="openai",
            default_model="gpt-4o",
            verify_connection=False,
        )
    )
    assert patch_res.updated is True

    # ジョブ1を完了させる（開始時のスナップショット gemini のままで完了すること）
    allow_job1_finish.set()
    done1 = await task1
    assert done1.provider.value == "gemini"
    assert done1.model == "gemini-2.5-flash"

    # 次のジョブ2は再起動なしで新設定 openai / gpt-4o を使うこと
    mat2 = MaterialInput.model_validate(
        {
            "material_id": "iso-2",
            "material_name": "教材2",
            "ai_usage_approved": True,
            "questions": [{"question_id": "q1", "question_number": "問1", "prompt": "Q2"}],
        }
    )
    job2 = job_manager.create_job(mat2)
    done2 = await job_manager.execute_job(job2.job_id)
    assert done2.provider.value == "openai"
    assert done2.model == "gpt-4o"
    assert observed_calls == [("gemini", "gemini-2.5-flash"), ("openai", "gpt-4o")]
