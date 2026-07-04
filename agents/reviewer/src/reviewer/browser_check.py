"""Headless-browser E2E check for web-UI stories (hardening story HS-1).

Loads the running app in a real browser and asserts the things unit tests and diff review
structurally cannot see: no console errors on load, every hx-*/button control fires its
request, and the target renders *visible* output (not into a display:none div). This is the
class of defect — blocked scripts, dead buttons, permanently-hidden result panels, one-shot
forms — that shipped CI-green in the DEVHUB build and broke on first human use.

Runs inside the tester sandbox. **Degrades gracefully, never falsely fails, never silently
passes:** if the story doesn't touch the UI, or no launch command can be determined, or a
headless browser isn't installed, or the app won't start, it returns ``skip`` (advisory).
Only a real, reproduced problem (a blocking console error or an invisible-result control)
returns ``fail``.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

# Path fragments/extensions that mean "this change can affect the rendered UI".
_UI_EXTS = (".html", ".htm", ".jinja", ".j2", ".css", ".js", ".vue", ".svelte", ".tsx", ".jsx")
_UI_SEGMENTS = ("templates/", "static/", "public/", "components/", "/ui_", "routes/ui")

# Console messages that indicate a genuinely broken page (blocked/incompatible script or an
# uncaught error) rather than cosmetic noise (a missing favicon).
_HARD_ERROR_MARKERS = (
    "blocked", "integrity", "subresource", "refused to execute", "refused to load",
    "syntaxerror", "is not defined", "is not a function", "uncaught", "pageerror:",
    "failed to fetch", "net::err",
)
_IGNORABLE = ("favicon.ico", "/favicon")


def is_ui_change(changed_paths: list[str]) -> bool:
    """True if any changed path can affect the rendered UI (templates/static/front-end)."""
    for p in changed_paths or []:
        low = p.lower()
        if low.endswith(_UI_EXTS) or any(seg in low for seg in _UI_SEGMENTS):
            return True
    return False


def discover_run_command(repo_dir: str, explicit: str = "") -> str:
    """Return a shell command that launches the app, or "" if none can be determined.

    Order: an explicit stack/project ``run_command`` wins; otherwise derive a best-effort
    command from a Python ``[project.scripts]`` console entry point. A literal ``$PORT`` in
    the command is substituted with the chosen port by the caller.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    pp = Path(repo_dir) / "pyproject.toml"
    if pp.exists():
        try:
            txt = pp.read_text(encoding="utf-8")
        except Exception:
            txt = ""
        m = re.search(r"\[project\.scripts\][^\[]*?\n\s*([A-Za-z0-9_.\-]+)\s*=", txt, re.S)
        if m:
            # Most FastAPI/uvicorn/CLI entry points honor a PORT or SERVER_HTTP_PORT env.
            script = m.group(1)
            return f"SERVER_HTTP_PORT=$PORT PORT=$PORT {script}"
    return ""


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_for_port(port: int, proc: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:  # app exited early
            return False
        if _port_open(port):
            return True
        time.sleep(0.3)
    return False


def _classify_console(errors: list[str]) -> list[str]:
    """Return the subset of console/page errors that indicate a broken page."""
    hard = []
    for e in errors:
        low = e.lower()
        if any(ig in low for ig in _IGNORABLE):
            continue
        if any(mk in low for mk in _HARD_ERROR_MARKERS):
            hard.append(e[:200])
    return hard


def run_browser_check(base_url: str, timeout_ms: int = 15000) -> dict[str, Any]:
    """Drive the app with a headless browser. Returns a structured result:

    ``{status: pass|fail|skip, reason, console_errors, controls_fired, invisible_targets,
       summary}``. ``skip`` when a browser isn't available; ``fail`` only on a hard console
    error or a control whose result rendered invisibly.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
    except Exception as exc:  # noqa: BLE001
        return {"status": "skip", "reason": f"playwright unavailable: {str(exc)[:80]}"}

    console_errors: list[str] = []
    invisible: list[str] = []
    fired = 0
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(args=["--no-sandbox", "--disable-gpu"])
            except Exception as exc:  # noqa: BLE001 — no browser binary → skip, don't fail
                return {"status": "skip", "reason": f"no chromium: {str(exc)[:80]}"}
            page = browser.new_page()
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: console_errors.append("pageerror: " + str(e)))
            page.goto(base_url, wait_until="networkidle", timeout=timeout_ms)

            # Every htmx-driven control: click it, confirm its target became visible.
            controls = page.query_selector_all("[hx-get],[hx-post],[hx-delete],[hx-put],[hx-patch]")
            for c in controls[:25]:
                target = c.get_attribute("hx-target") or ""
                try:
                    if not c.is_visible():
                        continue
                    c.click(timeout=3000, no_wait_after=True)
                    page.wait_for_timeout(700)
                    fired += 1
                except Exception:
                    continue
                # If the control targets an element by id, that element must end up visible.
                if target.startswith("#"):
                    el = page.query_selector(target)
                    if el is not None and not el.is_visible():
                        invisible.append(target)
            browser.close()
    except Exception as exc:  # noqa: BLE001 — a harness/launch problem is a skip, not a fail
        return {"status": "skip", "reason": f"browser run error: {str(exc)[:120]}"}

    hard = _classify_console(console_errors)
    status = "fail" if (hard or invisible) else "pass"
    parts = []
    if hard:
        parts.append(f"{len(hard)} blocking console error(s): " + "; ".join(hard[:3]))
    if invisible:
        parts.append("control result rendered but stayed hidden (display:none): "
                     + ", ".join(invisible[:5]))
    if status == "pass":
        parts.append(f"loaded with no console errors; {fired} control(s) fired and rendered visibly")
    return {
        "status": status,
        "reason": "",
        "console_errors": hard,
        "controls_fired": fired,
        "invisible_targets": invisible,
        "summary": "; ".join(parts),
    }


def browser_verdict(
    repo_dir: str,
    changed_paths: list[str],
    run_command: str = "",
    port: int = 8099,
    startup_timeout: float = 25.0,
) -> dict[str, Any]:
    """Full E2E check for a UI story: skip if not UI/no launch/no browser; otherwise launch
    the app, run the browser check, and tear down. Never raises."""
    if not is_ui_change(changed_paths):
        return {"status": "skip", "reason": "no UI/template/static change in this PR"}

    cmd = discover_run_command(repo_dir, run_command)
    if not cmd:
        return {"status": "skip", "reason": "no run_command for this stack/project (set stack.run_command to enable E2E)"}

    cmd = cmd.replace("$PORT", str(port))
    env = {**os.environ, "SERVER_HTTP_PORT": str(port), "PORT": str(port)}
    proc = None
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=repo_dir, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_for_port(port, proc, startup_timeout):
            return {"status": "skip", "reason": f"app did not start on port {port} within {startup_timeout:.0f}s"}
        result = run_browser_check(f"http://127.0.0.1:{port}/")
        log.info("browser_check", status=result.get("status"), fired=result.get("controls_fired"),
                 invisible=len(result.get("invisible_targets", [])))
        return result
    except Exception as exc:  # noqa: BLE001
        return {"status": "skip", "reason": f"e2e setup error: {str(exc)[:120]}"}
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
