"""
Container entrypoint for sandboxed coding agent runs.

Reads story details from env vars, runs run_coding_agent(), and emits
the result as a CODING_RESULT:{json} sentinel line on stdout so the
spawning event-bus can parse it without sharing a filesystem.
"""
from __future__ import annotations
import json
import os
import sys

import structlog

log = structlog.get_logger()


def main() -> None:
    item_id = os.environ["STORY_ID"]
    title = os.environ["STORY_TITLE"]
    description = os.environ.get("STORY_DESCRIPTION", "")
    story_prompt = os.environ.get("STORY_PROMPT", "")

    try:
        from coding_agent.main import run_coding_agent
        result = run_coding_agent(
            item_id,
            title,
            description,
            story_prompt=story_prompt,
            log_line=lambda line: print(line, flush=True),
        )
    except Exception as exc:
        log.error("sandbox_run_failed", item_id=item_id, error=str(exc))
        result = {"status": "error", "item_id": item_id, "error": str(exc)}

    print(f"\nCODING_RESULT:{json.dumps(result)}", flush=True)
    sys.exit(0 if result.get("status") in ("success", "no_changes") else 1)


if __name__ == "__main__":
    main()
