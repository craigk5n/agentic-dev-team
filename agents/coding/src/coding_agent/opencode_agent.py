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

DEFAULT_TIMEOUT = 900  # 15 minutes per story


def run_opencode_agent(
    story_title: str,
    story_description: str,
    repo_dir: str,
    model: str,
    openrouter_api_key: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    review_comments: list[dict] | None = None,
    prompt_template: str = "",
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

    if review_comments:
        feedback = "\n".join(
            f"- [{c.get('path', 'general')}] {c.get('body', '').strip()}"
            for c in review_comments if c.get("body", "").strip()
        )
        _DEFAULT_FIX = (
            "## Fix review feedback for: {story_title}\n\n"
            "## Review comments to address\n{feedback}\n\n"
            "## Original story description\n{story_description}\n\n"
            "INSTRUCTIONS:\n"
            "1. Read the existing code in the repo first.\n"
            "2. Address ALL review comments listed above — that is the primary objective.\n"
            "3. Make only the changes needed to satisfy the feedback.\n"
            "4. Do not introduce unrelated changes."
        )
        template = prompt_template or _DEFAULT_FIX
        try:
            prompt = template.format(
                story_title=story_title,
                story_description=story_description or "(no description)",
                feedback=feedback,
            )
        except (KeyError, ValueError):
            prompt = template
    else:
        _DEFAULT_STORY = (
            "You are a coding agent. Implement the following story right now. "
            "Do NOT ask for clarification — the story description is your complete specification. "
            "Do NOT say you are ready to help. Just implement it.\n\n"
            "## Story: {story_title}\n\n"
            "{story_description}\n\n"
            "STEPS:\n"
            "1. List the files in the repo to understand what exists.\n"
            "2. Read the relevant existing files.\n"
            "3. Implement the story by writing or modifying files. "
            "If an existing app is present, build on top of it. "
            "If the repo is empty or only has a README, create a minimal scaffold "
            "(Python + FastAPI by default) then implement the story.\n"
            "4. Write real, working code — no placeholders or stubs unless the story explicitly asks for them.\n"
            "5. Do not touch files unrelated to this story.\n"
            "Start implementing now."
        )
        template = prompt_template or _DEFAULT_STORY
        try:
            prompt = template.format(
                story_title=story_title,
                story_description=story_description or "(no description)",
            )
        except (KeyError, ValueError):
            prompt = template

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
    return result.stdout.strip() or "Implementation complete"
