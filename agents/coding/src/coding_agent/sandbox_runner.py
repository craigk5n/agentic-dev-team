"""
Container entrypoint for sandboxed coding agent runs.

Reads story details from env vars, runs run_coding_agent(), and emits the result to
``/output/result.json`` — the authoritative channel the spawning event-bus reads back
via ``docker get_archive`` after the container exits, immune to stdout/log interleaving
and stray library prints. The legacy ``CODING_RESULT:{json}`` stdout sentinel is still
printed as a fallback for older readers.
"""
from __future__ import annotations
import json
import os
import sys

import structlog

log = structlog.get_logger()

_RESULT_PATH = "/output/result.json"


def _emit_result(result: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_RESULT_PATH), exist_ok=True)
        with open(_RESULT_PATH, "w", encoding="utf-8") as f:
            json.dump(result, f)
    except Exception as exc:  # noqa: BLE001 — fall back to the stdout sentinel below
        log.error("sandbox_result_write_failed", error=str(exc))
    print(f"\nCODING_RESULT:{json.dumps(result)}", flush=True)


def main() -> None:
    item_id = os.environ["STORY_ID"]
    title = os.environ["STORY_TITLE"]
    description = os.environ.get("STORY_DESCRIPTION", "")
    story_prompt = os.environ.get("STORY_PROMPT", "")
    test_command = os.environ.get("STORY_TEST_CMD", "")
    install_command = os.environ.get("STORY_INSTALL_CMD", "")

    try:
        from coding_agent.main import run_coding_agent
        result = run_coding_agent(
            item_id,
            title,
            description,
            story_prompt=story_prompt,
            test_command=test_command,
            install_command=install_command,
            log_line=lambda line: print(line, flush=True),
        )
    except Exception as exc:
        log.error("sandbox_run_failed", item_id=item_id, error=str(exc))
        result = {"status": "error", "item_id": item_id, "error": str(exc)}

    _emit_result(result)
    sys.exit(0 if result.get("status") in ("success", "no_changes") else 1)


if __name__ == "__main__":
    main()
