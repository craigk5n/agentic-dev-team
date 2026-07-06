"""Real Playwright driver for the browser oracle backend (Story 2.3).

This is the untestable-in-CI boundary: it needs a Playwright install + browsers + a running
app, so it is excluded from unit-test coverage (see pyproject omit) and must be validated
live in the sandbox. The journey *logic* is unit-tested against a fake driver in
test_oracle_browser.py; this adapter only translates driver calls to Playwright's sync API.

Driver protocol (see browser.py):
    goto(url), click(selector), fill(selector, text), is_visible(selector) -> bool,
    text(selector) -> str, console_errors() -> list[str], request_status(url) -> int
"""
from __future__ import annotations


class PlaywrightDriver:
    def __init__(self, base_url: str, timeout_ms: int = 10_000):
        self._base_url = base_url
        self._timeout = timeout_ms
        self._pw = None
        self._browser = None
        self._page = None
        self._console_errors: list[str] = []

    def __enter__(self):
        # Lazy import: a missing Playwright raises here and is caught by browser_backend,
        # surfacing as a 'playwright-unavailable' defect rather than an import crash.
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._page = self._browser.new_page()
        self._page.set_default_timeout(self._timeout)
        # Capture console errors + failed responses (script-blocked / dead UI signals).
        self._page.on("console", lambda m: (
            self._console_errors.append(m.text) if m.type == "error" else None))
        self._page.on("pageerror", lambda e: self._console_errors.append(str(e)))
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def goto(self, url: str) -> None:
        self._console_errors.clear()
        self._page.goto(url, wait_until="networkidle")

    def click(self, selector: str) -> None:
        self._page.click(selector)

    def fill(self, selector: str, text: str) -> None:
        self._page.fill(selector, text)

    def is_visible(self, selector: str) -> bool:
        try:
            return bool(self._page.is_visible(selector))
        except Exception:
            return False

    def text(self, selector: str) -> str:
        try:
            return self._page.inner_text(selector)
        except Exception:
            return ""

    def console_errors(self) -> list[str]:
        return list(self._console_errors)

    def request_status(self, url: str) -> int:
        resp = self._page.request.get(url)
        return resp.status
