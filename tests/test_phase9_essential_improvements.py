"""Phase 9: 既存Firefox拡張機能知見の移植機能・総合模擬テスト・異常系検証。"""

from __future__ import annotations

import functools
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from src.api.app import create_app
from src.browser.extractor import PlaywrightLearningExtractor
from src.core.config import AppConfig, load_config
from src.core.exceptions import MuseCommunicationError, PermissionDeniedError
from src.mock_client.simulator import MockMuseClient
from src.providers.factory import create_provider
from src.schemas.enums import JobStatus, ProviderName, QuestionFormat, UncertaintyLevel
from src.schemas.question import MaterialInput, QuestionItem
from src.schemas.report import MuseReport


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def mock_portal_url() -> Iterator[str]:
    """mock_test ディレクトリを配信する一時HTTPサーバーを起動する。"""
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


def _cfg(tmp_path: Path) -> AppConfig:
    """テスト用AppConfigを生成する。"""
    cfg = load_config(config_path="config.yaml", env_path=None)
    cfg.app.data_dir = str(tmp_path / "data")
    cfg.app.logs_dir = str(tmp_path / "logs")
    cfg.app.db_path = str(tmp_path / "data" / "phase9.db")
    cfg.retry.base_delay_seconds = 0.001
    cfg.retry.max_delay_seconds = 0.002
    return cfg


def _install_mock_ai(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    """JobManager内のcreate_providerが指定MockTransportを使用するように差し替える。"""
    orig = create_provider

    def _patched(config: AppConfig, *, provider_name=None, model=None, http_client=None, **kw):
        return orig(
            config,
            provider_name=provider_name,
            model=model,
            http_client=http_client or httpx.AsyncClient(transport=transport),
            **kw,
        )

    monkeypatch.setattr("src.jobs.manager.create_provider", _patched)


@pytest.mark.asyncio
async def test_comprehensive_exam_multipage_math_image_and_classi_controls(
    mock_portal_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """選択・複数選択・image-selector・fill-in-text・画像・数式・ページ遷移・採点5/5の総合検証。"""
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-phase9-gemini-123456")
    sent_payloads: list[dict[str, Any]] = []

    def ai_handler(request: httpx.Request) -> httpx.Response:
        req_json = json.loads(request.content.decode("utf-8"))
        sent_payloads.append(req_json)
        blob = json.dumps(req_json, ensure_ascii=False)
        if "c-q1" in blob:
            results = [
                {
                    "question_id": "c-q1",
                    "answer_candidates": ["3"],
                    "explanation": "平方完成 f(x) = (x-2)^2 + 3 より最小値は 3。",
                    "uncertainty": "low",
                    "confidence": 0.99,
                    "needs_review": False,
                },
                {
                    "question_id": "c-q2",
                    "answer_candidates": ["2", "3"],
                    "explanation": "(x-2)(x-3)=0 より x=2, 3。",
                    "uncertainty": "low",
                    "confidence": 0.99,
                    "needs_review": False,
                },
                {
                    "question_id": "c-q3",
                    "answer_candidates": ["25"],
                    "explanation": "半径5の円の方程式は x^2 + y^2 = 5^2 = 25。",
                    "uncertainty": "low",
                    "confidence": 0.99,
                    "needs_review": False,
                },
                {
                    "question_id": "c-q4",
                    "answer_candidates": ["2"],
                    "explanation": "頂点(2, 3)より x 座標は 2。",
                    "uncertainty": "low",
                    "confidence": 0.99,
                    "needs_review": False,
                },
            ]
        else:
            results = [
                {
                    "question_id": "c-q5",
                    "answer_candidates": ["通信内容を暗号化して盗聴や改ざんを防ぐため。"],
                    "explanation": "TLSによる暗号化で第三者からの盗聴・改ざんを防止する。",
                    "uncertainty": "low",
                    "confidence": 0.98,
                    "needs_review": False,
                }
            ]
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": json.dumps({"results": results}, ensure_ascii=False)}]}}],
                "usageMetadata": {"promptTokenCount": 180, "candidatesTokenCount": 120, "totalTokenCount": 300},
            },
        )

    _install_mock_ai(monkeypatch, httpx.MockTransport(ai_handler))
    app = create_app(_cfg(tmp_path))
    extractor = PlaywrightLearningExtractor(headless=True)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as api_client:
        async with extractor.open_page() as page:
            await page.goto(f"{mock_portal_url}/comprehensive_exam.html", wait_until="networkidle")
            extracted_p1 = await extractor.extract_page_material(page)

            # 1. 数式・画像・問題形式の抽出検証
            q_by_id = {q.question_id: q for q in extracted_p1.material_input.questions}
            assert set(q_by_id.keys()) == {"c-q1", "c-q2", "c-q3", "c-q4"}
            assert q_by_id["c-q1"].question_format == QuestionFormat.SINGLE_CHOICE
            assert "$f(x) = x^2 - 4x + 7$" in q_by_id["c-q1"].prompt
            assert "x^2" in q_by_id["c-q1"].prompt
            # katex-htmlの「x2」が重複混入していないこと
            assert "x2 - 4x" not in q_by_id["c-q1"].prompt
            assert q_by_id["c-q2"].question_format == QuestionFormat.MULTIPLE_CHOICE
            assert q_by_id["c-q3"].question_format == QuestionFormat.DROPDOWN
            assert q_by_id["c-q3"].supplementary_info.get("blank_marker") == "ア"
            assert q_by_id["c-q4"].question_format == QuestionFormat.SHORT_TEXT
            assert q_by_id["c-q4"].supplementary_info.get("blank_marker") == "イ"
            assert len(q_by_id["c-q4"].images) == 2

            # 2. Page 1 解析ジョブ投入・完了待機・解答反映
            r1 = await api_client.post("/jobs?wait=true", json=extracted_p1.material_input.model_dump(mode="json"))
            assert r1.status_code == 202
            job1_id = r1.json()["job_id"]
            assert r1.json()["status"] == JobStatus.COMPLETED.value
            rep1 = MuseReport.model_validate((await api_client.get(f"/jobs/{job1_id}/result")).json())

            # 未許可画像 fig-student-memo が送信ペイロードに含まれないこと
            dumped_p1 = json.dumps(sent_payloads[0], ensure_ascii=False)
            assert "fig-parabola" in dumped_p1
            assert "fig-student-memo" not in dumped_p1

            apply_p1 = await extractor.apply_answers(page, extracted_p1, rep1, auto_submit_allowed=True)
            assert apply_p1["applied_count"] == 4

            # 3. Page 1 -> Page 2 へのページ遷移
            moved = await extractor.navigate_next_page_if_present(page)
            assert moved is True
            assert "comprehensive_page2.html" in page.url

            # 4. Page 2 抽出・解析・記入・提出・採点 5/5 確認
            extracted_p2 = await extractor.extract_page_material(page)
            assert len(extracted_p2.material_input.questions) == 1
            assert extracted_p2.material_input.questions[0].question_format == QuestionFormat.ESSAY

            r2 = await api_client.post("/jobs?wait=true", json=extracted_p2.material_input.model_dump(mode="json"))
            assert r2.status_code == 202
            job2_id = r2.json()["job_id"]
            assert r2.json()["status"] == JobStatus.COMPLETED.value
            rep2 = MuseReport.model_validate((await api_client.get(f"/jobs/{job2_id}/result")).json())

            apply_p2 = await extractor.apply_answers(page, extracted_p2, rep2, auto_submit_allowed=True)
            assert apply_p2["applied_count"] == 1

            await page.click("#submit-exam-btn")
            await page.locator("#result-screen").wait_for(state="visible", timeout=5000)
            assert "5 / 5 問正解" in await page.locator("#score-display").inner_text()
            assert "正答率 100%" in await page.locator("#score-rate").inner_text()


@pytest.mark.asyncio
async def test_partial_missing_recovery_and_abnormal_answer_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """欠落設問の部分再確認リカバリと、異常選択肢・インデックス正規化の検証。"""
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-phase9-openai-123456")
    call_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body = json.loads(req.content.decode("utf-8"))
        user_text = body["messages"][1]["content"]

        if call_count == 1:
            # 1回目: q1(0始まりインデックス文字列"1"返却), q2(選択肢に存在しない異常候補), q3(意図的に欠落)
            ai_out = {
                "results": [
                    {
                        "question_id": "q1",
                        "answer_candidates": ["1"],
                        "explanation": "配列1番目(0始まり)が正解。",
                        "uncertainty": "low",
                        "confidence": 0.9,
                        "needs_review": False,
                    },
                    {
                        "question_id": "q2",
                        "answer_candidates": ["存在しない選択肢XYZ"],
                        "explanation": "異常な候補。",
                        "uncertainty": "low",
                        "confidence": 0.9,
                        "needs_review": False,
                    },
                ]
            }
        else:
            # 2回目（不足分 q3 のみ再確認される）
            assert "q3" in user_text and "q1" not in user_text
            ai_out = {
                "results": [
                    {
                        "question_id": "q3",
                        "answer_candidates": ["Paris"],
                        "explanation": "フランスの首都はParis。",
                        "uncertainty": "low",
                        "confidence": 0.99,
                        "needs_review": False,
                    }
                ]
            }
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(ai_out, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
            },
        )

    material = MaterialInput(
        material_id="mat-recovery",
        material_name="リカバリ検証教材",
        ai_usage_approved=True,
        questions=[
            QuestionItem(
                question_id="q1",
                question_number="問1",
                prompt="共通指示文：次の問いに最も適するものを選びなさい。 Appleの意味は？",
                choices=["みかん", "りんご", "ぶどう"],
                question_format=QuestionFormat.SINGLE_CHOICE,
            ),
            QuestionItem(
                question_id="q2",
                question_number="問2",
                prompt="共通指示文：次の問いに最も適するものを選びなさい。 Dogの意味は？",
                choices=["猫", "犬", "鳥"],
                question_format=QuestionFormat.SINGLE_CHOICE,
            ),
            QuestionItem(
                question_id="q3",
                question_number="問3",
                prompt="共通指示文：次の問いに最も適するものを選びなさい。 フランスの首都は？",
                choices=["London", "Paris", "Berlin"],
                question_format=QuestionFormat.SINGLE_CHOICE,
            ),
        ],
    )

    cfg = _cfg(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = create_provider(cfg, provider_name="openai", http_client=client)
        # 共通指示文分離の確認
        sanitized = provider.build_sanitized_material_payload(material)
        assert "common_context" in sanitized
        assert provider.compute_max_output_tokens(material) >= 1024

        res = await provider.analyze(material)

    assert call_count == 2
    assert res.total_tokens == 160
    # q1: インデックス"1" -> "りんご" に正規化
    assert res.results[0].answer_candidates == ["りんご"]
    assert res.results[0].needs_review is False
    # q2: 異常候補は除外され needs_review=True に隔離
    assert res.results[1].answer_candidates == []
    assert res.results[1].needs_review is True
    assert res.results[1].uncertainty == UncertaintyLevel.HIGH
    # q3: 部分再確認で自動補完
    assert res.results[2].answer_candidates == ["Paris"]
    assert res.results[2].needs_review is False


@pytest.mark.asyncio
async def test_unsupported_canvas_detection_cdp_security_and_muse_disconnect(
    mock_portal_url: str,
    tmp_path: Path,
) -> None:
    """非対応Canvas形式検出・CDP接続先セキュリティ検証・Muse通信切断の異常系を確認。"""
    extractor = PlaywrightLearningExtractor(headless=True)

    # 1. 外部非暗号化CDPへの接続は拒否され、ローカルループバックのみ許可される
    with pytest.raises(PermissionDeniedError):
        extractor.validate_cdp_endpoint("http://remote-host.example.com:9222")
    assert extractor.validate_cdp_endpoint("http://127.0.0.1:9222") == "http://127.0.0.1:9222"

    # 2. Canvas問題が含まれる動的HTMLを検査し、unsupported_items に記録されること
    async with extractor.open_page() as page:
        await page.set_content(
            """
            <main id="material-root" data-material-id="m-mix" data-material-name="混合" data-ai-allowed="true">
              <div class="question-card" data-question-id="q-ok" data-question-number="問1" data-question-format="single_choice">
                <p class="question-prompt">1+1は？</p>
                <label><input type="radio" name="q1" value="2"><span>2</span></label>
                <label><input type="radio" name="q1" value="3"><span>3</span></label>
              </div>
              <div class="question-card" data-question-id="q-canvas">
                <p class="question-prompt">下のキャンバスに図形を描画しなさい。</p>
                <canvas width="200" height="100"></canvas>
              </div>
            </main>
            """
        )
        extracted = await extractor.extract_page_material(page)
        assert len(extracted.material_input.questions) == 1
        assert len(extracted.unsupported_items) == 1
        assert extracted.unsupported_items[0].question_id == "q-canvas"

    # 3. MuseとPython APIサーバーの通信切断シミュレーション
    muse = MockMuseClient(
        portal_base_url=mock_portal_url,
        api_base_url="http://127.0.0.1:1",  # 到達不能ポート
        screenshot_dir=tmp_path / "screenshots",
    )
    with pytest.raises(MuseCommunicationError):
        await muse.run_workflow(target_page="mock_exam.html")
