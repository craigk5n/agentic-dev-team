"""Story 2.3 — browser oracle backend (journey logic tested via a fake driver)."""
from __future__ import annotations

import pytest

from event_bus import oracle
from event_bus.oracle.backends.browser import browser_backend


class FakeDriver:
    """Scriptable stand-in for the Playwright driver."""
    def __init__(self, *, visible=True, texts=None, console=None, statuses=None,
                 visible_seq=None):
        self._visible = visible
        self._visible_seq = list(visible_seq) if visible_seq is not None else None
        self._texts = texts or {}
        self._console = console or []
        self._statuses = statuses or {}
        self.actions: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def goto(self, url):
        self.actions.append(("goto", url))

    def click(self, sel):
        self.actions.append(("click", sel))

    def fill(self, sel, text):
        self.actions.append(("fill", sel, text))

    def is_visible(self, sel):
        if self._visible_seq is not None:
            return self._visible_seq.pop(0) if self._visible_seq else False
        return self._visible

    def text(self, sel):
        return self._texts.get(sel, "")

    def console_errors(self):
        return self._console

    def request_status(self, url):
        for path, st in self._statuses.items():
            if url.endswith(path):
                return st
        return 200


def _factory(driver):
    return lambda base_url: driver


_HAPPY = {"name": "register", "repeat": 2, "steps": [
    {"action": "goto"},
    {"action": "fill", "selector": "#url", "text": "http://x"},
    {"action": "click", "selector": "#submit"},
    {"action": "expect_no_console_errors"},
    {"action": "expect_visible", "selector": "#result"},
    {"action": "expect_text", "selector": "#result", "contains": "registered"},
]}


def _spec(*journeys):
    return {"name": "pastebin", "backend": "browser", "checks": list(journeys)}


class TestBrowserBackend:
    def test_happy_journey_passes(self):
        d = FakeDriver(visible=True, texts={"#result": "registered ok"}, console=[])
        checks, defects = browser_backend(_spec(_HAPPY), {"url": "http://app"},
                                          driver_factory=_factory(d))
        assert checks[0]["passed"] is True and defects == []

    def test_hidden_panel_defect(self):
        d = FakeDriver(visible=False, texts={"#result": "registered"})
        _, defects = browser_backend(_spec(_HAPPY), {"url": "http://app"},
                                     driver_factory=_factory(d))
        assert defects[0]["class"] == "not-visible"

    def test_console_error_dead_ui_defect(self):
        d = FakeDriver(visible=True, console=["Failed to load script (SRI mismatch)"])
        _, defects = browser_backend(_spec(_HAPPY), {"url": "http://app"},
                                     driver_factory=_factory(d))
        assert defects[0]["class"] == "console-error"
        assert defects[0]["errors"]

    def test_dead_route_defect(self):
        journey = {"name": "download", "steps": [
            {"action": "goto"},
            {"action": "expect_status_ok", "path": "/download/1"}]}
        d = FakeDriver(statuses={"/download/1": 404})
        _, defects = browser_backend(_spec(journey), {"url": "http://app"},
                                     driver_factory=_factory(d))
        assert defects[0]["class"] == "dead-route" and defects[0]["status"] == 404

    def test_text_mismatch_defect(self):
        d = FakeDriver(visible=True, texts={"#result": "error"})
        _, defects = browser_backend(_spec(_HAPPY), {"url": "http://app"},
                                     driver_factory=_factory(d))
        assert defects[0]["class"] == "text-mismatch"

    def test_form_replaces_itself_caught_by_repeat(self):
        # Visible on the first run, gone on the second → repeat:2 fails (form ate itself).
        d = FakeDriver(visible_seq=[True, False], texts={"#result": "registered"})
        _, defects = browser_backend(_spec(_HAPPY), {"url": "http://app"},
                                     driver_factory=_factory(d))
        assert defects[0]["class"] == "not-visible"

    def test_missing_playwright_is_defect_not_crash(self):
        def _boom(base_url):
            raise ImportError("No module named 'playwright'")
        checks, defects = browser_backend(_spec(_HAPPY), {"url": "http://app"},
                                          driver_factory=_boom)
        assert checks[0]["passed"] is False
        assert defects[0]["class"] == "playwright-unavailable"

    def test_registered_as_default_backend(self):
        assert oracle.get_backend("browser") is browser_backend
