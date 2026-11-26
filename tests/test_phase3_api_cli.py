"""Phase 3: FastAPIエンドポイントおよびCLI連携の自動テスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main as cli_main
from src.api.app import create_app
from src.core.config import AppConfig, load_config
from src.jobs.manager import JobManager
from src.schemas.analysis import ProviderCallResult, QuestionAnalysisResult
from src.schemas.enums import JobStatus, ProviderName, UncertaintyLevel


def _isolated_config(tmp_path: Path) -> AppConfig:
    """一時ディレクトリを使うテスト用AppConfigを生成する。"""
    cfg = load_config(config_path="config.yaml", env_path=None)
    cfg.app.data_dir = str(tmp_path / "data")
    cfg.app.logs_dir = str(tmp_path / "logs")
    cfg.app.db_path = str(tmp_path / "data" / "test.db")
    return cfg


def _sample_request_dict() -> dict:
    """Museからの標準問題入力JSONを返す。"""
    return {
        "material_id": "mat-math-01",
        "material_name": "数学I 確認演習",
        "material_category": "practice",
        "ai_usage_approved": True,
        "auto_submit_allowed": False,
        "questions": [
            {
                "question_id": "q1",
                "question_number": "問1",
                "prompt": "2x + 3 = 11 のとき x の値を求めよ。",
                "choices": ["2", "3", "4", "5"],
                "question_format": "single_choice",
            }
        ],
    }


def _mock_provider_factory(monkeypatch: pytest.MonkeyPatch, *, needs_review: bool = False) -> None:
    """create_providerをモック化して外部通信なしで解析結果を返す。"""

    class FakeProvider:
        provider_name = ProviderName.GEMINI
        model = "gemini-2.5-flash"

        async def analyze(self, material):
            return ProviderCallResult(
                provider=ProviderName.GEMINI,
                model="gemini-2.5-flash",
                results=[
                    QuestionAnalysisResult(
                        question_id=material.questions[0].question_id,
                        answer_candidates=["4"],
                        explanation="2x = 8 より x = 4。",
                        uncertainty=UncertaintyLevel.HIGH if needs_review else UncertaintyLevel.LOW,
                        confidence=0.4 if needs_review else 0.99,
                        needs_review=needs_review,
                        review_reason="手書き図形が一部不鮮明" if needs_review else None,
                    )
                ],
                input_tokens=50,
                output_tokens=30,
                total_tokens=80,
                estimated_cost_usd=0.0001,
            )

    monkeypatch.setattr("src.jobs.manager.create_provider", lambda *args, **kwargs: FakeProvider())


def test_api_health_and_providers(tmp_path: Path) -> None:
    """GET /health と GET /providers が正常応答することを確認。"""
    app = create_app(_isolated_config(tmp_path))
    client = TestClient(app)

    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json()["status"] == "ok"

    res_providers = client.get("/providers")
    assert res_providers.status_code == 200
    names = {p["name"] for p in res_providers.json()}
    assert {"gemini", "deepseek", "openai", "anthropic", "openai_compatible"} <= names


def test_api_job_lifecycle_sync_and_screenshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /jobs?wait=true -> GET /jobs/{id} -> GET /result -> POST /screenshots の一連を確認。"""
    _mock_provider_factory(monkeypatch, needs_review=False)
    app = create_app(_isolated_config(tmp_path))
    client = TestClient(app)

    create_res = client.post("/jobs?wait=true", json=_sample_request_dict())
    assert create_res.status_code == 202
    job = create_res.json()
    job_id = job["job_id"]
    assert job["status"] == JobStatus.COMPLETED.value

    status_res = client.get(f"/jobs/{job_id}")
    assert status_res.status_code == 200
    assert status_res.json()["processed_questions"] == 1

    result_res = client.get(f"/jobs/{job_id}/result")
    assert result_res.status_code == 200
    report = result_res.json()
    assert report["material_name"] == "数学I 確認演習"
    assert report["results"][0]["answer_candidates"] == ["4"]

    ss_res = client.post(
        f"/jobs/{job_id}/screenshots",
        json={"screenshot_paths": ["data/screenshots/result_01.png"]},
    )
    assert ss_res.status_code == 200
    assert ss_res.json()["permitted_screenshot_paths"] == ["data/screenshots/result_01.png"]


def test_api_cancel_and_error_responses(tmp_path: Path) -> None:
    """未許可教材エラー・存在しないジョブ・キャンセルAPIが機能することを確認。"""
    cfg = _isolated_config(tmp_path)
    manager = JobManager(cfg)
    app = create_app(cfg, manager=manager)
    client = TestClient(app)

    # AI利用未許可教材は403で拒否される
    bad_req = _sample_request_dict()
    bad_req["ai_usage_approved"] = False
    res_denied = client.post("/jobs", json=bad_req)
    assert res_denied.status_code == 403
    assert res_denied.json()["error"]["code"] == "PERMISSION_DENIED"

    # 存在しないジョブIDは404
    res_missing = client.get("/jobs/job-does-not-exist")
    assert res_missing.status_code == 404

    # 待機中ジョブのキャンセル
    from src.schemas.question import MaterialInput

    waiting = manager.create_job(MaterialInput.model_validate(_sample_request_dict()))
    cancel_res = client.post(f"/jobs/{waiting.job_id}/cancel")
    assert cancel_res.status_code == 200
    assert cancel_res.json()["status"] == JobStatus.CANCELLED.value


def test_cli_run_status_result_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI経由で run -> status -> result が実行できることを確認。"""
    _mock_provider_factory(monkeypatch, needs_review=True)

    cfg_path = tmp_path / "config.yaml"
    cfg_raw = Path("config.yaml").read_text(encoding="utf-8")
    cfg_raw = (
        cfg_raw.replace('data_dir: "data"', f'data_dir: "{(tmp_path / "data").as_posix()}"')
        .replace('logs_dir: "logs"', f'logs_dir: "{(tmp_path / "logs").as_posix()}"')
        .replace('db_path: "data/muse_bridge.db"', f'db_path: "{(tmp_path / "data" / "cli.db").as_posix()}"')
    )
    cfg_path.write_text(cfg_raw, encoding="utf-8")

    input_file = tmp_path / "input.json"
    input_file.write_text(json.dumps(_sample_request_dict(), ensure_ascii=False), encoding="utf-8")
    output_file = tmp_path / "report.json"

    rc = cli_main.main(
        [
            "--config",
            str(cfg_path),
            "run",
            "--input",
            str(input_file),
            "--output",
            str(output_file),
        ]
    )
    assert rc == 0
    report_data = json.loads(output_file.read_text(encoding="utf-8"))
    assert report_data["status"] == JobStatus.REVIEW_REQUIRED.value
    assert report_data["review_required_count"] == 1
    job_id = report_data["job_id"]

    capsys.readouterr()
    rc_status = cli_main.main(["--config", str(cfg_path), "status", job_id])
    assert rc_status == 0
    status_out = json.loads(capsys.readouterr().out)
    assert status_out["job_id"] == job_id

    rc_result = cli_main.main(["--config", str(cfg_path), "result", job_id])
    assert rc_result == 0
    result_out = json.loads(capsys.readouterr().out)
    assert result_out["job_id"] == job_id
