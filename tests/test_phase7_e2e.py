"""Phase 7: 模擬Museクライアントによる正常系・異常系総合検証テスト。"""

from __future__ import annotations

import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from PIL import Image

from src.api.app import create_app
from src.core.config import AppConfig, load_config
from src.core.exceptions import (
    LoginFailedError,
    MaterialNotFoundError,
    MuseBridgeError,
    PageStructureChangedError,
    PermissionDeniedError,
)
from src.mock_client.simulator import MockMuseClient
from src.providers.factory import create_provider
from src.schemas.enums import JobStatus, ProviderName


class _QuietHandler(SimpleHTTPRequestHandler):
    """ログ出力を抑制したテスト用静的配信ハンドラ。"""

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def mock_portal_url() -> Iterator[str]:
    """mock_test ディレクトリを配信するローカルHTTPサーバーを起動する。"""
    root_dir = Path("mock_test").resolve()
    handler = functools.partial(_QuietHandler, directory=str(root_dir))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _e2e_config(tmp_path: Path) -> AppConfig:
    """E2Eテスト用の独立AppConfigを返す。"""
    cfg = load_config(config_path="config.yaml", env_path=None)
    cfg.app.data_dir = str(tmp_path / "data")
    cfg.app.logs_dir = str(tmp_path / "logs")
    cfg.app.db_path = str(tmp_path / "data" / "e2e.db")
    cfg.retry.base_delay_seconds = 0.001
    cfg.retry.max_delay_seconds = 0.002
    return cfg


def _install_mock_ai_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: httpx.MockTransport,
) -> None:
    """JobManager内のcreate_providerが指定MockTransportを使用するように差し替える。"""
    orig_create = create_provider

    def _patched_create_provider(
        config: AppConfig,
        *,
        provider_name: str | ProviderName | None = None,
        model: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        client = http_client or httpx.AsyncClient(transport=transport)
        return orig_create(
            config,
            provider_name=provider_name,
            model=model,
            http_client=client,
        )

    monkeypatch.setattr("src.jobs.manager.create_provider", _patched_create_provider)


@pytest.mark.asyncio
async def test_e2e_mock_exam_full_success_and_masked_screenshot(
    mock_portal_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常系E2E: ログイン->模擬テスト抽出->AI解析->自動提出採点->PIIマスクスクショ登録。"""
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-e2e-gemini-key-123456")

    def gemini_ok(request: httpx.Request) -> httpx.Response:
        body = json.dumps(
            {
                "results": [
                    {
                        "question_id": "m-q1",
                        "answer_candidates": ["8"],
                        "explanation": "1バイトは8ビットで構成される。",
                        "uncertainty": "low",
                        "confidence": 0.99,
                        "needs_review": False,
                    },
                    {
                        "question_id": "m-q2",
                        "answer_candidates": ["TLS"],
                        "explanation": "HTTPS通信の暗号化にはTLSが用いられる。",
                        "uncertainty": "low",
                        "confidence": 0.99,
                        "needs_review": False,
                    },
                    {
                        "question_id": "m-q3",
                        "answer_candidates": [
                            "暗号化と復号で異なる鍵を用いる",
                            "公開鍵は第三者に公開できる",
                        ],
                        "explanation": "公開鍵暗号では公開鍵で暗号化し秘密鍵で復号する。",
                        "uncertainty": "low",
                        "confidence": 0.98,
                        "needs_review": False,
                    },
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": body}]}}],
                "usageMetadata": {"promptTokenCount": 140, "candidatesTokenCount": 95, "totalTokenCount": 235},
            },
        )

    _install_mock_ai_transport(monkeypatch, httpx.MockTransport(gemini_ok))
    app = create_app(_e2e_config(tmp_path))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as api_client:
        muse = MockMuseClient(
            portal_base_url=mock_portal_url,
            api_client=api_client,
            screenshot_dir=tmp_path / "screenshots",
        )
        outcome = await muse.run_workflow(target_page="mock_exam.html")

    assert outcome.report.status == JobStatus.COMPLETED
    assert outcome.auto_submitted is True
    assert outcome.graded_score == "3 / 3 問正解"
    assert outcome.masked_screenshot_path is not None
    masked_file = Path(outcome.masked_screenshot_path)
    assert masked_file.exists()
    assert str(masked_file) in outcome.report.permitted_screenshot_paths

    # 左上または右上の .user-pill 領域が実際にマスキングされているか画像サイズとピクセルで確認
    with Image.open(masked_file) as img:
        assert img.width >= 1000
        # 全黒ではなくUIも描画されつつ、一部にマスク色 (24, 32, 46) が存在する
        colors = img.getcolors(maxcolors=500000) or []
        assert any(color == (24, 32, 46) for _, color in colors)


@pytest.mark.asyncio
async def test_e2e_practice_quiz_no_auto_submit_and_image_filtering(
    mock_portal_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常系E2E: 練習教材では自動提出せず下書き表示のみ・未許可画像はAIへ渡さない。"""
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-e2e-openai-key-123456")
    sent_payloads: list[str] = []

    def openai_ok(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(request.content.decode("utf-8"))
        body = json.dumps(
            {
                "results": [
                    {
                        "question_id": "p-q1",
                        "answer_candidates": ["went"],
                        "explanation": "last week があるため過去形 went。",
                        "uncertainty": "low",
                        "confidence": 0.99,
                        "needs_review": False,
                    },
                    {
                        "question_id": "p-q2",
                        "answer_candidates": ["2", "3", "5", "7"],
                        "explanation": "10以下の素数は2, 3, 5, 7。",
                        "uncertainty": "low",
                        "confidence": 0.99,
                        "needs_review": False,
                    },
                    {
                        "question_id": "p-q3",
                        "answer_candidates": ["(2, 5)"],
                        "explanation": "平方完成の形から頂点は(2, 5)。",
                        "uncertainty": "low",
                        "confidence": 0.98,
                        "needs_review": False,
                    },
                    {
                        "question_id": "p-q4",
                        "answer_candidates": ["5"],
                        "explanation": "三平方の定理より √(3^2+4^2) = 5。",
                        "uncertainty": "low",
                        "confidence": 0.99,
                        "needs_review": False,
                    },
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": body}}],
                "usage": {"prompt_tokens": 200, "completion_tokens": 120, "total_tokens": 320},
            },
        )

    _install_mock_ai_transport(monkeypatch, httpx.MockTransport(openai_ok))
    app = create_app(_e2e_config(tmp_path))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as api_client:
        muse = MockMuseClient(
            portal_base_url=mock_portal_url,
            api_client=api_client,
            screenshot_dir=tmp_path / "screenshots",
        )
        outcome = await muse.run_workflow(
            target_page="practice_quiz.html",
            provider=ProviderName.OPENAI,
        )

    assert outcome.report.status == JobStatus.COMPLETED
    assert outcome.auto_submitted is False
    assert outcome.draft_applied_count == 4
    assert any("除外しました" in n for n in outcome.notes)
    # 未許可画像 fig-private-memo はプロバイダーペイロードに含まれない
    assert "fig-private-memo" not in sent_payloads[0]
    assert "fig-triangle" in sent_payloads[0]


@pytest.mark.asyncio
async def test_e2e_error_scenarios(
    mock_portal_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """異常系E2E: 不許可教材・ログイン失敗・教材未発見・認証エラー・API切断・重複実行を検証。"""
    app = create_app(_e2e_config(tmp_path))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as api_client:
        muse = MockMuseClient(
            portal_base_url=mock_portal_url,
            api_client=api_client,
            screenshot_dir=tmp_path / "screenshots",
        )

        # 1. 利用許可が確認できない教材
        with pytest.raises(PermissionDeniedError):
            await muse.run_workflow(target_page="restricted_test.html")

        # 2. ログイン失敗
        with pytest.raises(LoginFailedError):
            await muse.run_workflow(target_page="mock_exam.html", password="xx")

        # 3. 対象教材が見つからない (404)
        with pytest.raises(MaterialNotFoundError):
            await muse.run_workflow(target_page="nonexistent.html")

        # 4. ページ構造変更（index.htmlには#material-rootが存在しない）
        with pytest.raises(PageStructureChangedError):
            await muse.run_workflow(target_page="index.html")

        # 5. API認証失敗 (401 -> 即FAILED)
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-e2e-invalid-key-123456")
        _install_mock_ai_transport(
            monkeypatch,
            httpx.MockTransport(lambda req: httpx.Response(401, json={"error": "bad key"})),
        )
        auth_fail = await muse.run_workflow(
            target_page="mock_exam.html",
            idempotency_key="case-auth-fail",
        )
        assert auth_fail.report.status == JobStatus.FAILED
        assert auth_fail.report.error is not None
        assert auth_fail.report.error.code == "PROVIDER_AUTH_ERROR"

        # 6. API切断 (ConnectError -> 3回再試行後FAILED)
        disconnect_calls = 0

        def disconnect_handler(req: httpx.Request) -> httpx.Response:
            nonlocal disconnect_calls
            disconnect_calls += 1
            raise httpx.ConnectError("network unreachable", request=req)

        _install_mock_ai_transport(monkeypatch, httpx.MockTransport(disconnect_handler))
        net_fail = await muse.run_workflow(
            target_page="mock_exam.html",
            idempotency_key="case-net-fail",
        )
        assert disconnect_calls == 3
        assert net_fail.report.status == JobStatus.FAILED
        assert net_fail.report.error is not None
        assert net_fail.report.error.code == "NETWORK_CONNECTION_ERROR"

        # 7. 正常完了後の同一キー二重実行拒否 (DUPLICATE_JOB)
        _install_mock_ai_transport(
            monkeypatch,
            httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "content": {
                                    "parts": [
                                        {
                                            "text": json.dumps(
                                                {
                                                    "results": [
                                                        {
                                                            "question_id": "m-q1",
                                                            "answer_candidates": ["8"],
                                                            "explanation": "8ビット",
                                                            "uncertainty": "low",
                                                            "confidence": 0.9,
                                                            "needs_review": False,
                                                        },
                                                        {
                                                            "question_id": "m-q2",
                                                            "answer_candidates": ["TLS"],
                                                            "explanation": "TLS",
                                                            "uncertainty": "low",
                                                            "confidence": 0.9,
                                                            "needs_review": False,
                                                        },
                                                        {
                                                            "question_id": "m-q3",
                                                            "answer_candidates": [
                                                                "暗号化と復号で異なる鍵を用いる",
                                                                "公開鍵は第三者に公開できる",
                                                            ],
                                                            "explanation": "公開鍵暗号",
                                                            "uncertainty": "low",
                                                            "confidence": 0.9,
                                                            "needs_review": False,
                                                        },
                                                    ]
                                                }
                                            )
                                        }
                                    ]
                                }
                            }
                        ],
                        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 10, "totalTokenCount": 20},
                    },
                )
            ),
        )
        ok_once = await muse.run_workflow(
            target_page="mock_exam.html",
            idempotency_key="dup-key-01",
        )
        assert ok_once.report.status == JobStatus.COMPLETED

        with pytest.raises(MuseBridgeError) as dup_exc:
            await muse.run_workflow(
                target_page="mock_exam.html",
                idempotency_key="dup-key-01",
            )
        assert dup_exc.value.code == "DUPLICATE_JOB"
