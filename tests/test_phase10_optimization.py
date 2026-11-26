"""Phase 10: API通信最適化・25問ベンチマーク・429/503エラー処理・利用量/無料枠保護テスト。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.core.cache import OptimizationCacheStore
from src.core.config import AppConfig, load_config
from src.core.exceptions import BudgetExceededError, PermissionDeniedError, ProviderRateLimitError
from src.core.http_pool import HttpClientPool
from src.core.rate_limit import AsyncRateLimiter, RateLimitKind
from src.jobs.manager import JobManager
from src.providers.factory import create_provider
from src.providers.local_solver import try_solve_question_locally
from src.schemas.enums import JobStatus, QuestionFormat
from src.schemas.question import MaterialInput, QuestionItem


def _opt_config(tmp_path: Path) -> AppConfig:
    """高速テスト用に再試行待機のみ短縮した設定を返す。"""
    cfg = load_config(config_path="config.yaml", env_path=None)
    cfg.app.data_dir = str(tmp_path / "data")
    cfg.app.logs_dir = str(tmp_path / "logs")
    cfg.app.db_path = str(tmp_path / "data" / "opt.db")
    cfg.retry.base_delay_seconds = 0.005
    cfg.retry.max_delay_seconds = 0.02
    return cfg


def _build_25_dummy_questions() -> tuple[MaterialInput, dict[str, list[str]]]:
    """25問のベンチマーク用ダミー教材と正解マップ（選択・複数選択・計算・重複・記述混合）を生成する。"""
    questions: list[QuestionItem] = []
    ground_truth: dict[str, list[str]] = {}

    # 1〜8: 共通指示文・共通選択肢付きの文法選択問題（q-03 と q-05 のみ q-01 と完全重複）
    grammar_prompts = [
        ("q-01", "Every day he ( ) to the library.", "goes"),
        ("q-02", "Yesterday she ( ) to the station.", "went"),
        ("q-03", "Every day he ( ) to the library.", "goes"),  # q-01と重複
        ("q-04", "Last night Ken ( ) home early.", "went"),
        ("q-05", "Every day he ( ) to the library.", "goes"),  # q-01と重複
        ("q-06", "I usually ( ) for a walk in the morning.", "go"),
        ("q-07", "She is ( ) to Osaka next Sunday.", "going"),
        ("q-08", "We ( ) shopping together two days ago.", "went"),
    ]
    for idx, (qid, body_prompt, ans) in enumerate(grammar_prompts, start=1):
        prompt = f"共通指示：次の英文の空欄に入る最も適切な動詞を選択してください。 {body_prompt}"
        questions.append(
            QuestionItem(
                question_id=qid,
                question_number=f"問{idx}",
                prompt=prompt,
                choices=["go", "goes", "going", "went"],
                question_format=QuestionFormat.SINGLE_CHOICE,
            )
        )
        ground_truth[qid] = [ans]

    # 9〜12: 純粋算術式（AI不要ローカル解決対象 + 1択固定）
    arith_cases = [
        ("q-09", "12 + 34 =", ["44", "46", "48", "50"], "46"),
        ("q-10", "次の計算をしなさい: (15 - 3) * 4 =", ["42", "44", "48", "52"], "48"),
        ("q-11", "2^5 =", ["16", "32", "64", "128"], "32"),
        ("q-12", "確認完了項目です（そのまま選択）", ["確認しました"], "確認しました"),
    ]
    for qid, prompt, choices, ans in arith_cases:
        questions.append(
            QuestionItem(
                question_id=qid,
                question_number=f"問{qid[-2:]}",
                prompt=prompt,
                choices=choices,
                question_format=QuestionFormat.SINGLE_CHOICE,
            )
        )
        ground_truth[qid] = [ans]

    # 13〜22: 知識・数式・読解の一般選択問題（AI解析必須）
    general_cases = [
        ("q-13", "HTTPSで通信を暗号化するプロトコルはどれか。", ["HTTP", "TLS", "FTP", "ARP"], ["TLS"]),
        ("q-14", "1バイトは何ビットか。", ["4", "8", "16", "32"], ["8"]),
        ("q-15", "$f(x) = x^2 - 4x + 7$ の最小値を求めよ。", ["1", "2", "3", "4"], ["3"]),
        ("q-16", "日本の首都はどこか。", ["大阪", "京都", "東京", "名古屋"], ["東京"]),
        ("q-17", "水の化学式を選べ。", ["CO2", "H2O", "O2", "NaCl"], ["H2O"]),
        ("q-18", "素数のみをすべて選べ。", ["2", "3", "4", "5"], ["2", "3", "5"]),
        ("q-19", "光合成に必要な気体はどれか。", ["窒素", "二酸化炭素", "ヘリウム", "アルゴン"], ["二酸化炭素"]),
        ("q-20", "CPUの基本役割として適切なものを選べ。", ["演算と制御", "印刷", "電源供給", "無線通信"], ["演算と制御"]),
        ("q-21", "SQLiteでテーブルを作成する文はどれか。", ["DROP", "CREATE TABLE", "DELETE", "TRUNCATE"], ["CREATE TABLE"]),
        ("q-22", "公開鍵暗号方式で復号に使う鍵はどれか。", ["公開鍵", "秘密鍵", "共通鍵", "セッション鍵"], ["秘密鍵"]),
    ]
    for qid, prompt, choices, ans_list in general_cases:
        fmt = QuestionFormat.MULTIPLE_CHOICE if qid == "q-18" else QuestionFormat.SINGLE_CHOICE
        questions.append(
            QuestionItem(
                question_id=qid,
                question_number=f"問{qid[-2:]}",
                prompt=prompt,
                choices=choices,
                question_format=fmt,
            )
        )
        ground_truth[qid] = ans_list

    # 23〜25: 記述式問題（AI解析必須・部分思考維持対象）
    essay_cases = [
        ("q-23", "TLSが通信の安全性を高める理由を簡潔に述べよ。", ["盗聴や改ざんを防ぐため"]),
        ("q-24", "HTTP/1.1 Keep-Aliveの利点を簡潔に述べよ。", ["TCP接続を再利用し遅延を削減できる"]),
        ("q-25", "キャッシュを使う主な目的を簡潔に述べよ。", ["重複計算や通信を省き高速化するため"]),
    ]
    for qid, prompt, ans_list in essay_cases:
        questions.append(
            QuestionItem(
                question_id=qid,
                question_number=f"問{qid[-2:]}",
                prompt=prompt,
                choices=[],
                question_format=QuestionFormat.SHORT_TEXT,
            )
        )
        ground_truth[qid] = ans_list

    material = MaterialInput(
        material_id="mat-bench-25",
        material_name="25問総合ベンチマーク模擬問題集",
        ai_usage_approved=True,
        questions=questions,
    )
    return material, ground_truth


@pytest.mark.asyncio
async def test_benchmark_25_questions_before_vs_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ダミーデータ25問を用いて、改善前後のAPI回数・トークン数・処理時間・回答精度を比較検証する。"""
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-bench-gemini-key-123456")
    material, truth = _build_25_dummy_questions()

    async def mock_gemini_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        user_text = body["contents"][0]["parts"][0]["text"]
        sys_text = body.get("systemInstruction", {}).get("parts", [{}])[0].get("text", "")
        use_compact_out = '"r":[{"i":' in sys_text
        gen_cfg = body.get("generationConfig", {})
        thinking_cfg = gen_cfg.get("thinkingConfig", {})
        thinking_budget = int(thinking_cfg.get("thinkingBudget", 2048))

        # JSON中の対象question_idを特定（フル形式/超短縮ワイヤ両対応）
        json_start = user_text.find("{")
        json_end = user_text.rfind("}") + 1
        sent_mat = json.loads(user_text[json_start:json_end])
        raw_qs = sent_mat.get("q") or sent_mat.get("questions", [])
        sent_qids = [str(q.get("i") or q.get("question_id")) for q in raw_qs]

        # 現実的な入力文字数比例トークン + 思考設定比例トークン + ネットワーク遅延シミュレート
        in_tokens = max(20, len(json.dumps(body, ensure_ascii=False)) // 3)
        think_tokens = len(sent_qids) * (thinking_budget // 16 if thinking_budget > 0 else 0)
        await asyncio.sleep(0.018 + 0.002 * len(sent_qids) + (0.012 if thinking_budget > 512 else 0.0))

        if use_compact_out:
            results_payload = [
                {
                    "i": qid,
                    "a": truth.get(qid, []),
                    "e": "根拠正答。",
                    "u": "l",
                    "c": 0.99,
                }
                for qid in sent_qids
            ]
            raw_out = json.dumps({"r": results_payload}, ensure_ascii=False, separators=(",", ":"))
        else:
            results_payload = [
                {
                    "question_id": qid,
                    "answer_candidates": truth.get(qid, []),
                    "explanation": "詳細な長文解説を出力するため出力トークンを消費します。" * 3,
                    "uncertainty": "low",
                    "confidence": 0.99,
                    "needs_review": False,
                    "review_reason": None,
                }
                for qid in sent_qids
            ]
            raw_out = json.dumps({"results": results_payload}, ensure_ascii=False)

        out_tokens = max(12, len(raw_out) // 3)
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": raw_out}]}}],
                "usageMetadata": {
                    "promptTokenCount": in_tokens,
                    "candidatesTokenCount": out_tokens,
                    "thoughtsTokenCount": think_tokens,
                    "totalTokenCount": in_tokens + out_tokens + think_tokens,
                },
            },
        )

    # ------------------------------------------------------------------
    # 1) 改善前ベースライン相当: 1問ずつ・非圧縮・ローカル解決OFF・重複除外OFF・思考HIGH・キャッシュOFF
    # ------------------------------------------------------------------
    cfg_before = _opt_config(tmp_path / "before")
    cfg_before.optimization.enable_cache = False
    cfg_before.optimization.enable_local_solver = False
    cfg_before.optimization.enable_deduplication = False
    cfg_before.optimization.enable_compact_prompt = False
    cfg_before.optimization.ultra_compact_wire = False
    cfg_before.optimization.minimal_output = False
    cfg_before.optimization.cascade_escalation = False
    cfg_before.optimization.batch_size = 1
    cfg_before.optimization.max_concurrency = 1
    cfg_before.optimization.thinking_mode = "high"

    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_gemini_handler)) as client_before:
        prov_before = create_provider(cfg_before, provider_name="gemini", http_client=client_before)
        t0 = time.monotonic()
        res_before = await prov_before.analyze(material)
        before_sec = time.monotonic() - t0

    acc_before = sum(1 for r in res_before.results if r.answer_candidates == truth[r.question_id])

    # ------------------------------------------------------------------
    # 2) 改善後最適化版（初回実行）: 1バッチ集約・超短縮ワイヤ・最小出力・ローカル解決・重複除外・思考0・キャッシュON
    # ------------------------------------------------------------------
    cfg_after = _opt_config(tmp_path / "after")
    cache_store = OptimizationCacheStore(cfg_after.app.db_path)
    rate_limiter = AsyncRateLimiter()
    http_pool = HttpClientPool(timeout_seconds=cfg_after.retry.timeout_seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_gemini_handler)) as client_after:
        prov_after = create_provider(
            cfg_after,
            provider_name="gemini",
            http_client=client_after,
            cache_store=cache_store,
            rate_limiter=rate_limiter,
            http_pool=http_pool,
        )
        t1 = time.monotonic()
        res_after = await prov_after.analyze(material)
        after_sec = time.monotonic() - t1

        # ------------------------------------------------------------------
        # 3) 改善後最適化版（2回目・キャッシュ全ヒット検証）
        # ------------------------------------------------------------------
        t2 = time.monotonic()
        res_cached = await prov_after.analyze(material)
        cached_sec = time.monotonic() - t2

    await http_pool.close()

    acc_after = sum(1 for r in res_after.results if r.answer_candidates == truth[r.question_id])
    acc_cached = sum(1 for r in res_cached.results if r.answer_candidates == truth[r.question_id])

    # 正答率は前後とも25/25 (100%) を維持
    assert acc_before == 25
    assert acc_after == 25
    assert acc_cached == 25

    # APIリクエスト回数: 改善前 25回 -> 改善後 1回（ローカル4問+重複2問除外で残り19問を1リクエストに集約）
    assert res_before.api_requests_count == 25
    assert res_after.api_requests_count == 1
    assert res_after.local_solved_count == 4
    assert res_after.deduplicated_count == 2
    assert res_after.thinking_tokens == 0

    # 極限トークン削減 & 高速化確認
    assert res_after.input_tokens < res_before.input_tokens * 0.25
    assert res_after.output_tokens < res_before.output_tokens * 0.45
    assert res_after.total_tokens < res_before.total_tokens * 0.20
    assert after_sec < before_sec * 0.25

    # 2回目（キャッシュ再利用）は API リクエスト 0 回・トークン 0・超高速
    assert res_cached.api_requests_count == 0
    assert res_cached.total_tokens == 0
    assert cached_sec < after_sec

    print(
        f"\n[25Q BENCHMARK] "
        f"BEFORE(req={res_before.api_requests_count}, in={res_before.input_tokens}, "
        f"out={res_before.output_tokens}, think={res_before.thinking_tokens}, "
        f"total={res_before.total_tokens}, time={before_sec:.4f}s, acc={acc_before}/25) | "
        f"AFTER(req={res_after.api_requests_count}, in={res_after.input_tokens}, "
        f"out={res_after.output_tokens}, think={res_after.thinking_tokens}, "
        f"total={res_after.total_tokens}, time={after_sec:.4f}s, acc={acc_after}/25, "
        f"local={res_after.local_solved_count}, dedup={res_after.deduplicated_count}) | "
        f"CACHED(req={res_cached.api_requests_count}, total={res_cached.total_tokens}, "
        f"time={cached_sec:.4f}s, acc={acc_cached}/25)"
    )


@pytest.mark.asyncio
async def test_429_rpm_tpm_rpd_and_retry_info_respect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429の詳細JSONからRPM/TPM/RPDを判定し、RetryInfo待機時間を尊重・RPD即停止できることを確認。"""
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-secret-429-key-999999")
    cfg = _opt_config(tmp_path)
    cfg.retry.base_delay_seconds = 0.001

    mat = MaterialInput(
        material_id="mat-429",
        material_name="429テスト",
        ai_usage_approved=True,
        questions=[
            QuestionItem(
                question_id="q1",
                question_number="問1",
                prompt="日本の首都は？",
                choices=["東京", "大阪"],
                question_format=QuestionFormat.SINGLE_CHOICE,
            )
        ],
    )

    # 1. Gemini形式のTPM 429 + RetryInfo (retryDelay="0.03s") -> 待機尊重して2回目で成功
    calls = 0

    def tpm_then_ok(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={
                    "error": {
                        "code": 429,
                        "message": "Quota exceeded for dummy-secret-429-key-999999 TokensPerMinute",
                        "status": "RESOURCE_EXHAUSTED",
                        "details": [
                            {
                                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                "violations": [
                                    {
                                        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count",
                                        "quotaId": "GenerateContentInputTokensPerModelPerMinute-FreeTier",
                                    }
                                ],
                            },
                            {
                                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                                "retryDelay": "0.03s",
                            },
                        ],
                    }
                },
            )
        ok_json = json.dumps(
            {
                "results": [
                    {
                        "question_id": "q1",
                        "answer_candidates": ["東京"],
                        "explanation": "首都は東京。",
                        "uncertainty": "low",
                        "confidence": 0.99,
                        "needs_review": False,
                    }
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": ok_json}]}}],
                "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 20, "totalTokenCount": 70},
            },
        )

    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.MockTransport(tpm_then_ok)) as client:
        prov = create_provider(cfg, provider_name="gemini", http_client=client)
        res = await prov.analyze(mat)
    elapsed = time.monotonic() - started
    assert calls == 2
    assert res.results[0].answer_candidates == ["東京"]
    # RetryInfoの0.03s待機が尊重されていること（base_delay=0.001sではない）
    assert elapsed >= 0.025

    # 2. RPD（日次上限）429 -> 無限/無駄リトライせず1回目で即座に停止し、シークレットがマスクされること
    rpd_calls = 0

    def always_rpd(req: httpx.Request) -> httpx.Response:
        nonlocal rpd_calls
        rpd_calls += 1
        return httpx.Response(
            429,
            json={
                "error": {
                    "code": 429,
                    "message": "Daily quota exhausted with key dummy-secret-429-key-999999",
                    "status": "RESOURCE_EXHAUSTED",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                            "violations": [
                                {
                                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                }
                            ],
                        }
                    ],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(always_rpd)) as client:
        prov_rpd = create_provider(cfg, provider_name="gemini", http_client=client)
        with pytest.raises(ProviderRateLimitError) as exc_info:
            await prov_rpd.analyze(mat)

    assert rpd_calls == 1
    assert exc_info.value.retryable is False
    assert exc_info.value.details["kind"] == RateLimitKind.RPD.value
    assert "dummy-secret-429-key-999999" not in json.dumps(exc_info.value.details, ensure_ascii=False)


@pytest.mark.asyncio
async def test_503_exponential_backoff_and_partial_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503連続失敗で無限リトライせず安全停止し、保存済み部分結果を使ってresumeで未処理分のみ再実行できることを確認。"""
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-resume-gemini-key-123456")
    cfg = _opt_config(tmp_path)
    cfg.optimization.batch_size = 2
    cfg.optimization.max_concurrency = 1  # バッチ1成功後にバッチ2を実行させ部分保存を確定的に検証
    cfg.retry.max_attempts = 3

    mat = MaterialInput(
        material_id="mat-resume",
        material_name="部分再開検証",
        ai_usage_approved=True,
        questions=[
            QuestionItem(
                question_id=f"rq-{i}",
                question_number=f"問{i}",
                prompt=f"次の設問{i}に答えよ。",
                choices=["A", "B"],
                question_format=QuestionFormat.SINGLE_CHOICE,
            )
            for i in range(1, 5)
        ],
    )

    fail_second_batch = True
    seen_qids_per_call: list[list[str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8"))
        txt = body["contents"][0]["parts"][0]["text"]
        payload = json.loads(txt[txt.find("{") : txt.rfind("}") + 1])
        raw_qs = payload.get("q") or payload.get("questions", [])
        qids = [str(q.get("i") or q.get("question_id")) for q in raw_qs]
        seen_qids_per_call.append(qids)

        if "rq-3" in qids and fail_second_batch:
            return httpx.Response(503, json={"error": {"message": "Service Unavailable"}})

        res_json = json.dumps(
            {
                "results": [
                    {
                        "question_id": qid,
                        "answer_candidates": ["A"],
                        "explanation": "正答A。",
                        "uncertainty": "low",
                        "confidence": 0.99,
                        "needs_review": False,
                    }
                    for qid in qids
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": res_json}]}}],
                "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 20, "totalTokenCount": 60},
            },
        )

    transport = httpx.MockTransport(handler)
    shared_http = httpx.AsyncClient(transport=transport)
    orig_create = create_provider
    monkeypatch.setattr(
        "src.jobs.manager.create_provider",
        lambda c, *, provider_name=None, model=None, http_client=None, **kw: orig_create(
            c, provider_name=provider_name, model=model, http_client=shared_http, **kw
        ),
    )

    manager = JobManager(cfg)
    job = manager.create_job(mat)
    failed = await manager.execute_job(job.job_id)

    # バッチ1(rq-1, rq-2)は成功し、バッチ2(rq-3, rq-4)は503を3回リトライして停止
    assert failed.status == JobStatus.FAILED
    assert failed.error is not None
    assert failed.error.code == "NETWORK_CONNECTION_ERROR"
    assert failed.error.details["saved_partial_count"] == 2
    assert len(seen_qids_per_call) == 4  # バッチ1(1回) + バッチ2(3回リトライ)

    # 障害復旧後に再開すると、保存済みのrq-1/rq-2は二度とAPI送信されず、rq-3/rq-4のみ送信される
    fail_second_batch = False
    seen_qids_per_call.clear()
    resumed = await manager.resume_job(job.job_id)
    await shared_http.aclose()

    assert resumed.status == JobStatus.COMPLETED
    assert resumed.processed_questions == 4
    assert seen_qids_per_call == [["rq-3", "rq-4"]]
    report = manager.get_result(job.job_id)
    assert [r.question_id for r in report.results] == ["rq-1", "rq-2", "rq-3", "rq-4"]


@pytest.mark.asyncio
async def test_usage_api_and_free_tier_paid_api_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/usage集計・思考トークン記録・無料枠上限停止・有料API無断利用禁止を検証する。"""
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-paid-openai-key-123456")
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-free-gemini-key-123456")
    cfg = _opt_config(tmp_path)
    cfg.providers.openai.billing_tier = "paid"
    cfg.providers.gemini.rpd_limit = 1
    cfg.security.allow_paid_api = False

    def handler(req: httpx.Request) -> httpx.Response:
        out = json.dumps(
            {
                "results": [
                    {
                        "question_id": "uq-1",
                        "answer_candidates": ["東京"],
                        "explanation": "首都は東京。",
                        "uncertainty": "low",
                        "confidence": 0.99,
                        "needs_review": False,
                    }
                ]
            },
            ensure_ascii=False,
        )
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": out}]}}],
                "usageMetadata": {
                    "promptTokenCount": 30,
                    "candidatesTokenCount": 15,
                    "thoughtsTokenCount": 40,
                    "totalTokenCount": 85,
                },
            },
        )

    shared_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    orig_create = create_provider
    monkeypatch.setattr(
        "src.jobs.manager.create_provider",
        lambda c, *, provider_name=None, model=None, http_client=None, **kw: orig_create(
            c, provider_name=provider_name, model=model, http_client=shared_http, **kw
        ),
    )

    manager = JobManager(cfg)
    app = create_app(cfg, manager=manager)
    client = TestClient(app)

    # 1. 有料設定プロバイダーは allow_paid_api 未許可時は 403 で拒否される
    paid_req = {
        "material_id": "mat-paid",
        "material_name": "有料チェック",
        "ai_usage_approved": True,
        "provider": "openai",
        "questions": [
            {
                "question_id": "uq-1",
                "question_number": "問1",
                "prompt": "日本の首都は？",
                "choices": ["東京", "大阪"],
                "question_format": "single_choice",
            }
        ],
    }
    res_paid_denied = client.post("/jobs?wait=true", json=paid_req)
    assert res_paid_denied.status_code == 403
    assert res_paid_denied.json()["error"]["code"] == "PAID_API_APPROVAL_REQUIRED"

    # 2. 無料枠(rpd_limit=1)での1回目実行は成功し、思考トークンとモデル別処理時間が /usage に記録される
    free_req = {**paid_req, "material_id": "mat-free-1", "provider": "gemini"}
    res_ok = client.post("/jobs?wait=true", json=free_req)
    assert res_ok.status_code == 202
    job_body = res_ok.json()
    assert job_body["status"] == JobStatus.COMPLETED.value
    assert job_body["token_usage"]["thinking_tokens"] == 40
    assert job_body["token_usage"]["api_requests_count"] == 1

    usage_resp = client.get("/usage")
    assert usage_resp.status_code == 200
    usage_data = usage_resp.json()
    assert usage_data["summary"]["total_requests"] == 1
    assert usage_data["summary"]["thinking_tokens"] == 40
    assert usage_data["provider_quotas"]["gemini"]["remaining_rpd"] == 0
    assert any(m["provider"] == "gemini" and m["processing_time_seconds"] >= 0 for m in usage_data["by_model"])

    # 3. 別問題で2回目を実行すると、無料枠日次上限到達 (FREE_TIER_RPD_EXCEEDED) で停止（有料APIへの無断切替なし）
    free_req_2 = {
        **free_req,
        "material_id": "mat-free-2",
        "questions": [
            {
                "question_id": "uq-2",
                "question_number": "問1",
                "prompt": "フランスの首都は？",
                "choices": ["パリ", "ロンドン"],
                "question_format": "single_choice",
            }
        ],
    }
    res_quota = client.post("/jobs?wait=true", json=free_req_2)
    assert res_quota.status_code == 202
    assert res_quota.json()["status"] == JobStatus.FAILED.value
    assert res_quota.json()["error"]["code"] == "FREE_TIER_RPD_EXCEEDED"
    await shared_http.aclose()


def test_local_solver_precision_safety() -> None:
    """AI不要ローカル解決が純粋算術・1択のみを安全に解決し、変数式はAIへ委ねることを確認。"""
    pure = QuestionItem(
        question_id="l1",
        question_number="問1",
        prompt="次の計算をしなさい: (8 + 4) * 3 =",
        choices=["32", "36", "40"],
        question_format=QuestionFormat.SINGLE_CHOICE,
    )
    res_pure = try_solve_question_locally(pure)
    assert res_pure is not None
    assert res_pure.answer_candidates == ["36"]
    assert res_pure.confidence == 1.0

    algebra = QuestionItem(
        question_id="l2",
        question_number="問2",
        prompt="2x + 3 = 11 のとき x の値を求めよ。",
        choices=["2", "3", "4", "5"],
        question_format=QuestionFormat.SINGLE_CHOICE,
    )
    assert try_solve_question_locally(algebra) is None


@pytest.mark.asyncio
async def test_cascade_escalation_and_normalized_cache_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """低思考で低確信の難問だけエスカレーション再確認し、問題番号違いの同一問題にもキャッシュが効くことを確認。"""
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-cascade-key-123456")
    cfg = _opt_config(tmp_path)
    cache_store = OptimizationCacheStore(cfg.app.db_path)

    budgets_seen: list[tuple[int, list[str]]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content.decode("utf-8"))
        t_budget = int(body.get("generationConfig", {}).get("thinkingConfig", {}).get("thinkingBudget", -1))
        txt = body["contents"][0]["parts"][0]["text"]
        payload = json.loads(txt[txt.find("{") : txt.rfind("}") + 1])
        qids = [str(q.get("i") or q.get("question_id")) for q in (payload.get("q") or payload.get("questions", []))]
        budgets_seen.append((t_budget, qids))

        if t_budget == 0:
            # cq-1 は低確信、cq-2 は高確信("TLS")で返す -> cq-1 だけエスカレーション対象
            res = [
                {"i": "cq-1", "a": ["A"], "e": "仮判定", "u": "m", "c": 0.45, "nr": True, "rr": "再確認推奨"},
                {"i": "cq-2", "a": ["TLS"], "e": "確定", "u": "l", "c": 0.99},
            ]
        else:
            res = [
                {"i": "cq-1", "a": ["C"], "e": "再推論で確定", "u": "l", "c": 0.96},
            ]

        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": json.dumps({"r": res}, ensure_ascii=False)}]}}],
                "usageMetadata": {
                    "promptTokenCount": 40,
                    "candidatesTokenCount": 20,
                    "thoughtsTokenCount": 0 if t_budget == 0 else 50,
                    "totalTokenCount": 60 if t_budget == 0 else 110,
                },
            },
        )

    mat1 = MaterialInput(
        material_id="mat-cas-1",
        material_name="カスケード検証1",
        ai_usage_approved=True,
        questions=[
            QuestionItem(
                question_id="cq-1",
                question_number="問1",
                prompt="問1. 次の論理パズルの正答を選べ。",
                choices=["A", "B", "C"],
                question_format=QuestionFormat.SINGLE_CHOICE,
            ),
            QuestionItem(
                question_id="cq-2",
                question_number="問2",
                prompt="問2. HTTPの暗号化方式は？",
                choices=["ARP", "TLS"],
                question_format=QuestionFormat.SINGLE_CHOICE,
            ),
        ],
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        prov = create_provider(cfg, provider_name="gemini", http_client=client, cache_store=cache_store)
        r1 = await prov.analyze(mat1)

    # 1回目: thinkingBudget=0 で cq-1, cq-2 送信 -> cq-1 のみ thinkingBudget=256 で再確認
    assert budgets_seen == [(0, ["cq-1", "cq-2"]), (256, ["cq-1"])]
    assert r1.results[0].answer_candidates == ["C"]
    assert r1.results[1].answer_candidates == ["TLS"]
    assert r1.thinking_tokens == 50

    # 別テストで問題番号表記（問1. -> Q99:）と全角スペースだけ異なる同一問題 -> キャッシュ全ヒットで0リクエスト
    mat2 = MaterialInput(
        material_id="mat-cas-2",
        material_name="カスケード検証2",
        ai_usage_approved=True,
        questions=[
            QuestionItem(
                question_id="new-1",
                question_number="Q99",
                prompt="Q99: 次の論理パズルの正答を選べ。",
                choices=["A", "B", "C"],
                question_format=QuestionFormat.SINGLE_CHOICE,
            ),
            QuestionItem(
                question_id="new-2",
                question_number="Q100",
                prompt="Q100: HTTPの暗号化方式は？",
                choices=["ARP", "ＴＬＳ"],
                question_format=QuestionFormat.SINGLE_CHOICE,
            ),
        ],
    )

    budgets_seen.clear()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        prov2 = create_provider(cfg, provider_name="gemini", http_client=client, cache_store=cache_store)
        r2 = await prov2.analyze(mat2)

    assert budgets_seen == []
    assert r2.api_requests_count == 0
    assert r2.cache_hits == 2
    assert r2.results[0].answer_candidates == ["C"]
    assert r2.results[1].answer_candidates == ["TLS"]
