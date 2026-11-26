"""Phase 4: 自作HTML模擬テストページおよびPlaywright動作検証テスト。"""

from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest
from playwright.sync_api import sync_playwright


class _QuietHandler(SimpleHTTPRequestHandler):
    """ログ出力を抑制したテスト用HTTPハンドラ。"""

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture()
def mock_portal_url() -> Iterator[str]:
    """mock_test ディレクトリを配信する一時ローカルHTTPサーバーを起動する。"""
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


def test_mock_portal_login_and_practice_extraction(mock_portal_url: str) -> None:
    """模擬ポータルでのログイン、練習教材の問題抽出、および自動提出無効化を確認。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 840})
        page.goto(f"{mock_portal_url}/index.html", wait_until="networkidle")

        # ログイン前はポータル非表示
        assert page.locator("#login-section").is_visible()
        assert not page.locator("#portal-section").is_visible()

        # 模擬ログイン実行
        page.fill("#student-id", "A2026042")
        page.fill("#student-pass", "demo-pass-2026")
        page.click('#login-form button[type="submit"]')

        assert page.locator("#portal-section").is_visible()
        assert page.locator("#user-badge").is_visible()

        # 練習教材へ遷移
        page.click('a[href="practice_quiz.html"]')
        page.wait_for_load_state("networkidle")

        root = page.locator("#material-root")
        assert root.get_attribute("data-ai-allowed") == "true"
        assert root.get_attribute("data-auto-submit-allowed") == "false"
        assert page.locator("#submit-practice-btn").is_disabled()

        # 4問と画像許可属性を抽出できること
        cards = page.locator(".question-card")
        assert cards.count() == 4
        approved_imgs = page.locator('img[data-ai-approved="true"]').count()
        blocked_imgs = page.locator('img[data-ai-approved="false"]').count()
        assert approved_imgs == 1
        assert blocked_imgs == 1

        browser.close()


def test_mock_exam_auto_fill_grade_and_screenshot(
    mock_portal_url: str,
    tmp_path: Path,
) -> None:
    """自作模擬テストでの解答入力、提出、採点結果画面表示、スクリーンショット撮影を確認。"""
    screenshot_file = tmp_path / "mock_exam_result.png"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"{mock_portal_url}/mock_exam.html", wait_until="networkidle")

        root = page.locator("#material-root")
        assert root.get_attribute("data-ai-allowed") == "true"
        assert root.get_attribute("data-auto-submit-allowed") == "true"

        # 正答を入力
        page.check('input[name="m-q1"][value="8"]')
        page.select_option('select[name="m-q2"]', "TLS")
        page.check('input[name="m-q3"][value="暗号化と復号で異なる鍵を用いる"]')
        page.check('input[name="m-q3"][value="公開鍵は第三者に公開できる"]')

        # 提出して採点結果画面へ進む
        page.click("#submit-exam-btn")
        result_panel = page.locator("#result-screen")
        assert result_panel.is_visible()
        assert result_panel.get_attribute("data-graded") == "true"
        assert result_panel.get_attribute("data-score") == "3"
        assert "3 / 3 問正解" in page.locator("#score-display").inner_text()
        assert "正答率 100%" in page.locator("#score-rate").inner_text()

        page.screenshot(path=str(screenshot_file), full_page=True)
        assert screenshot_file.exists() and screenshot_file.stat().st_size > 1000
        browser.close()


def test_restricted_test_detected_as_disallowed(mock_portal_url: str) -> None:
    """学校評価テストページでAI利用不許可フラグが検出できることを確認。"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"{mock_portal_url}/restricted_test.html", wait_until="networkidle")
        root = page.locator("#material-root")
        assert root.get_attribute("data-ai-allowed") == "false"
        assert root.get_attribute("data-material-category") == "school_assessment"
        browser.close()
