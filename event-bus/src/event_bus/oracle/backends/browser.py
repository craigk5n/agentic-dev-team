"""Browser oracle backend — Playwright journeys (EPIC 2, Story 2.3).

The T1.3 anchor: loads the running app, clicks controls, asserts *visible* output, and runs
each flow twice — catching the exact DEVHUB defect classes that unit tests + diff review
missed:

- **script-blocked / dead UI** (fabricated SRI hashes) → ``expect_no_console_errors``
- **permanently hidden result panels** → ``expect_visible`` (asserts real visibility)
- **dead routes** (download button → 404) → ``expect_status_ok``
- **form-replaces-itself after one use** → journey ``repeat`` (must pass every iteration)

target = {"url": "http://host:port"}
each spec check is a journey =
    {"name": "...", "repeat": 2, "steps": [ {"action": ..., ...}, ... ]}

The Playwright driver is injected via ``driver_factory`` so the journey logic here is fully
unit-tested against a fake driver; the real adapter (``_playwright_driver``) is the boundary
that must be validated live in the sandbox (browsers + a running app).
"""
from __future__ import annotations


def _run_step(driver, base_url: str, step: dict) -> dict | None:
    """Execute one journey step. Return None on success or a typed defect on failure."""
    action = step.get("action")
    if action == "goto":
        driver.goto(base_url + step.get("path", ""))
        return None
    if action == "click":
        driver.click(step["selector"])
        return None
    if action == "fill":
        driver.fill(step["selector"], step.get("text", ""))
        return None
    if action == "expect_visible":
        if not driver.is_visible(step["selector"]):
            return {"class": "not-visible", "selector": step["selector"],
                    "description": f"{step['selector']} is not visible (hidden panel / dead UI)"}
        return None
    if action == "expect_text":
        actual = driver.text(step["selector"]) or ""
        want = step.get("contains", "")
        if want not in actual:
            return {"class": "text-mismatch", "selector": step["selector"],
                    "expected": want, "actual": actual,
                    "description": f"{step['selector']} lacks {want!r}"}
        return None
    if action == "expect_no_console_errors":
        errs = driver.console_errors() or []
        if errs:
            return {"class": "console-error", "errors": list(errs)[:5],
                    "description": "console errors present (script blocked / broken UI)"}
        return None
    if action == "expect_status_ok":
        status = driver.request_status(base_url + step.get("path", ""))
        if not (200 <= int(status) < 400):
            return {"class": "dead-route", "path": step.get("path", ""), "status": status,
                    "description": f"{step.get('path', '')} returned {status}"}
        return None
    return {"class": "unknown-step", "description": f"unknown action {action!r}"}


def _run_journey(driver, base_url: str, journey: dict) -> dict | None:
    """Run a journey once. Return None on success or a defect (tagged with the check)."""
    for step in journey.get("steps", []):
        defect = _run_step(driver, base_url, step)
        if defect:
            return {**defect, "check": journey.get("name")}
    return None


def _default_driver_factory(base_url: str):
    """Real Playwright driver (context manager). Import is lazy so a missing Playwright
    surfaces as a defect at run time, not an import error."""
    from event_bus.oracle.backends._playwright_driver import PlaywrightDriver
    return PlaywrightDriver(base_url)


def browser_backend(spec: dict, target: dict, driver_factory=None) -> tuple[list[dict], list[dict]]:
    """Run browser journeys against a running app. Returns (checks, defects).

    A journey with ``repeat: N`` must pass all N runs (catches form-replaces-itself). A
    missing Playwright/browser yields a single ``playwright-unavailable`` defect rather than
    crashing, so the oracle degrades gracefully where browsers aren't installed."""
    base_url = (target or {}).get("url", "")
    factory = driver_factory or _default_driver_factory
    try:
        cm = factory(base_url)
        driver = cm.__enter__()
    except Exception as exc:  # Playwright/browser missing, launch failed, etc.
        return ([{"name": "_setup", "passed": False, "detail": str(exc)[:200]}],
                [{"class": "playwright-unavailable", "description": str(exc)[:200]}])

    checks_out: list[dict] = []
    defects: list[dict] = []
    try:
        for journey in spec.get("checks", []):
            name = journey.get("name")
            repeat = max(1, int(journey.get("repeat", 1)))
            jdefect = None
            for _ in range(repeat):
                jdefect = _run_journey(driver, base_url, journey)
                if jdefect:
                    break
            checks_out.append({"name": name, "passed": jdefect is None,
                               "detail": "ok" if jdefect is None else jdefect.get("class")})
            if jdefect:
                defects.append(jdefect)
    finally:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass
    return checks_out, defects
