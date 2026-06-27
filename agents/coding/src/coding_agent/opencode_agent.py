"""
opencode-based agentic coding loop.

Runs `opencode run --dir <repo> --model <model> <prompt>` as a subprocess,
streams output to the log, and returns a summary string.
opencode handles all file reads/writes/commands inside the repo directory.
"""

from __future__ import annotations
import os
import shutil
import subprocess
import structlog

log = structlog.get_logger()

DEFAULT_TIMEOUT = 600  # 10 minutes per story


def run_opencode_agent(
    story_title: str,
    story_description: str,
    repo_dir: str,
    model: str,
    openrouter_api_key: str = "",
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """
    Run opencode non-interactively on the given repo and story.
    Returns a summary of what was implemented.
    Raises RuntimeError on failure.
    """
    opencode_bin = shutil.which("opencode")
    if not opencode_bin:
        raise RuntimeError(
            "opencode not found in PATH. "
            "Mount the opencode binary into the container or install it."
        )

    prompt = (
        f"## Story: {story_title}\n\n"
        f"{story_description or '(no description)'}\n\n"
        "INSTRUCTIONS:\n"
        "1. First, list all files in the repo to understand what already exists.\n"
        "2. If the repo has an existing app (e.g. main.py, app.py, package.json, go.mod), "
        "read the key files and implement the story on top of that stack.\n"
        "3. If the repo is EMPTY or only has a README, create a minimal working app from "
        "scratch using Python + FastAPI (unless the story or description specifies another "
        "language/framework). Set up pyproject.toml or requirements.txt, then implement "
        "the story on top of that scaffold.\n"
        "4. Make focused, working changes. Commit-worthy code only — no placeholders.\n"
        "5. Do not modify files unrelated to this story."
    )

    cmd = [opencode_bin, "run", "--dir", repo_dir, "--model", model, prompt]
    env = {**os.environ}
    if openrouter_api_key:
        env["OPENROUTER_API_KEY"] = openrouter_api_key

    log.info("opencode_start", model=model, repo=repo_dir, story=story_title)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"opencode timed out after {timeout}s")

    # Log output regardless of exit code
    if result.stdout:
        log.info("opencode_stdout", output=result.stdout[-1000:])
    if result.stderr:
        log.warning("opencode_stderr", output=result.stderr[-500:])

    if result.returncode != 0:
        raise RuntimeError(
            f"opencode exited {result.returncode}: {result.stderr.strip()[-400:]}"
        )

    log.info("opencode_done", story=story_title)
    # Use last 2000 chars of stdout as implementation summary
    return (result.stdout.strip() or "Implementation complete")[-2000:]
