"""Chromium drives the real local UI, HTTP server, database and Office files."""

import io
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn
from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright

from birkin_lite.api import create_app
from birkin_lite.config import AppConfig, create_config


@contextmanager
def server(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path = tmp_path / "config.json"
    create_config(path, AppConfig(home="data", port=port))
    app = create_app(path)
    running = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False))
    worker = threading.Thread(target=running.run)
    worker.start()
    origin = f"http://127.0.0.1:{port}"
    try:
        with httpx.Client(trust_env=False) as client:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if client.get(origin + "/api/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise AssertionError("Local UI server did not start")
        yield app, origin
    finally:
        running.should_exit = True
        worker.join(60)
        assert not worker.is_alive(), "Real runtime did not finish shutdown"


def no_horizontal_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")


def test_browser_office_bot_memory_keyboard_and_reflow(tmp_path, monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    evidence = Path(".birkin-lite/evidence")
    evidence.mkdir(parents=True, exist_ok=True)
    with server(tmp_path) as (app, origin), sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000}, locale="ko-KR")
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin)
            expect(page.get_by_label("일회용 연결 코드")).to_be_focused()
            page.get_by_label("일회용 연결 코드").fill(app.state.bootstrap_secret)
            page.keyboard.press("Enter")
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#connection-state")).to_contain_text("연결")
            page.screenshot(path=str(evidence / "p11-ui-desktop.png"), full_page=True)
            no_horizontal_overflow(page)

            page.locator("#example").click()
            expect(page.locator("#notice")).to_contain_text("35,000", timeout=60000)
            expect(page.get_by_label("업무 요청")).to_have_value("금액 합계 내줘")
            page.locator("#run-submit").click()
            expect(page.locator("#run-status")).to_have_text("완료", timeout=90000)
            expect(page.locator("#todos")).to_contain_text("D")
            expect(page.locator("#progress-count")).to_have_text("4 / 4 완료")
            expect(page.locator("#run-progress")).to_have_attribute("value", "4")
            page.screenshot(path=str(evidence / "p11-ui-result.png"), full_page=True)
            with page.expect_download() as downloaded:
                page.locator('#run-results [data-kind="result"] a').click()
            data = Path(downloaded.value.path()).read_bytes()
            book = load_workbook(io.BytesIO(data), data_only=True)
            assert list(book["Result"].values) == [("그룹", "금액"), ("전체", "35000.00")]
            book.close()
            page.locator('#run-results [data-kind="result"]').get_by_role(
                "button", name="계속 보관", exact=True
            ).click()
            expect(page.get_by_role("button", name="고정 해제", exact=True)).to_be_visible()
            page.get_by_role("button", name="고정 해제", exact=True).click()
            expect(page.get_by_role("button", name="고정 해제", exact=True)).to_have_count(0)

            page.locator('[data-view="automations"]').click()
            page.locator("#new-automation").click()
            page.get_by_label("이름", exact=True).fill("매일 합계")
            page.get_by_label("업무 지시").fill("금액 합계 내줘")
            page.locator("#auto-source-mode").select_option("files")
            page.locator("#auto-sources input").first.check()
            page.locator("#auto-output").select_option("xlsx")
            page.get_by_role("button", name="검토안 저장", exact=True).click()
            expect(page.locator("#preview-dialog")).to_be_visible()
            expect(page.locator("#automation-preview")).to_contain_text("예제-금액.csv")
            page.locator("#enable-automation").click()
            expect(page.locator("#automation-list .badge")).to_have_text("활성")
            page.get_by_role("button", name="일시 정지", exact=True).click()
            expect(page.locator("#automation-list .badge")).to_have_text("비활성")
            page.screenshot(path=str(evidence / "kelly-ui-automations.png"), full_page=True)

            page.locator("#open-settings").click()
            expect(page.locator("#settings-dialog")).to_be_visible()
            assert page.evaluate(
                "document.querySelector('#settings-dialog').contains(document.activeElement)"
            )
            page.get_by_text("기억 · 명시적으로 저장한 내용", exact=True).click()
            literal = '<img src=x onerror="window.UI_INJECTED=1"> 명시한 선호'
            page.get_by_label("기억할 내용").fill(literal)
            page.get_by_role("button", name="기억 저장", exact=True).click()
            expect(page.locator("#memory-list")).to_contain_text(literal, timeout=60000)
            assert page.locator("#memory-list img").count() == 0
            assert page.evaluate("window.UI_INJECTED === undefined")
            page.locator("#memory-list").get_by_role("button", name="삭제", exact=True).click()
            expect(page.locator("#memory-list")).to_contain_text("저장한 기억이 없습니다")
            page.get_by_label("기억할 내용").fill("합성 원문 제거 확인")
            page.get_by_role("button", name="기억 저장", exact=True).click()
            expect(page.locator("#memory-list")).to_contain_text("합성 원문 제거 확인")
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="원문도 제거", exact=True).click()
            expect(page.locator("#memory-list")).to_contain_text("저장한 기억이 없습니다")
            page.screenshot(path=str(evidence / "kelly-ui-settings.png"), full_page=True)
            page.keyboard.press("Escape")
            expect(page.locator("#settings-dialog")).not_to_be_visible()
            expect(page.locator("#open-settings")).to_be_focused()

            page.locator('[data-view="history"]').click()
            expect(page.locator("#history-list")).to_contain_text("금액 합계 내줘")
            page.screenshot(path=str(evidence / "kelly-ui-history.png"), full_page=True)

            page.locator('[data-view="conversation"]').click()
            page.set_viewport_size({"width": 360, "height": 900})
            no_horizontal_overflow(page)
            page.screenshot(path=str(evidence / "p11-ui-mobile.png"), full_page=True)
            # A 1440px window at 200% has a 720 CSS-pixel layout viewport. CSS zoom would
            # incorrectly keep desktop media queries; this checks reflow, not native zoom.
            page.set_viewport_size({"width": 720, "height": 500})
            no_horizontal_overflow(page)
            page.screenshot(path=str(evidence / "p11-ui-720-reflow.png"), full_page=True)
            assert errors == []
        finally:
            browser.close()
