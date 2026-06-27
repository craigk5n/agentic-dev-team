"""
Claude SDK agentic loop for the Coding Agent.

Gives Claude a set of file-system tools scoped to the cloned repository and
iterates until Claude signals completion or the turn limit is reached.
"""

from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import anthropic
import structlog

log = structlog.get_logger()

MAX_TURNS = 30

SYSTEM_PROMPT = """You are an autonomous coding agent. You have been assigned a story to implement.

Work methodically:
1. Read the existing code to understand the codebase structure.
2. Plan what needs to be added or changed.
3. Implement the changes using the provided tools.
4. Ensure any existing tests still pass (run them if a test command is available).
5. When you are satisfied the implementation is complete, call the `done` tool.

Write clean, idiomatic code. Keep changes minimal and focused on the story.
Do not modify files unrelated to the story. Do not add debug prints.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read the contents of a file in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root"},
                "content": {"type": "string", "description": "Full file content"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_files",
        "description": "List files and directories at the given path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path relative to repo root (default: '.')"},
            },
            "required": [],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the repository directory. Use for builds, tests, linting.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "done",
        "description": "Signal that the implementation is complete. Call this when you have finished.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Brief description of what was implemented"},
            },
            "required": ["summary"],
        },
    },
]


def _safe_path(repo_dir: str, rel_path: str) -> Path:
    """Resolve path and ensure it stays within the repo directory."""
    resolved = (Path(repo_dir) / rel_path).resolve()
    if not str(resolved).startswith(str(Path(repo_dir).resolve())):
        raise ValueError(f"Path escapes repo root: {rel_path!r}")
    return resolved


def _execute_tool(tool_name: str, tool_input: dict, repo_dir: str) -> str:
    try:
        if tool_name == "read_file":
            p = _safe_path(repo_dir, tool_input["path"])
            if not p.exists():
                return f"Error: file not found: {tool_input['path']}"
            return p.read_text(errors="replace")

        elif tool_name == "write_file":
            p = _safe_path(repo_dir, tool_input["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(tool_input["content"])
            return f"Written: {tool_input['path']} ({len(tool_input['content'])} bytes)"

        elif tool_name == "list_files":
            rel = tool_input.get("path", ".")
            p = _safe_path(repo_dir, rel)
            if not p.exists():
                return f"Error: path not found: {rel}"
            entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
            lines = [f"{'d' if e.is_dir() else 'f'}  {e.name}" for e in entries if not e.name.startswith(".git")]
            return "\n".join(lines) or "(empty)"

        elif tool_name == "run_command":
            result = subprocess.run(
                tool_input["command"],
                shell=True,
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=120,
            )
            out = (result.stdout + result.stderr).strip()
            return f"Exit {result.returncode}:\n{out}" if out else f"Exit {result.returncode}"

        elif tool_name == "done":
            return "__DONE__"

        return f"Error: unknown tool {tool_name!r}"

    except Exception as exc:
        return f"Error: {exc}"


def run_agent(
    story_title: str,
    story_description: str,
    repo_dir: str,
    api_key: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 8192,
) -> str:
    """
    Run the Claude agentic loop and return the completion summary.
    Raises RuntimeError if the API key is missing or the turn limit is reached.
    """
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. "
            "The Coding Agent requires a direct Anthropic pay-as-you-go API key "
            "(set ANTHROPIC_API_KEY in infra/.env)."
        )

    client = anthropic.Anthropic(api_key=api_key)

    user_message = (
        f"## Story: {story_title}\n\n"
        f"{story_description or '(no description provided)'}\n\n"
        "Implement this story in the repository. Call `done` when finished."
    )
    messages: list[dict] = [{"role": "user", "content": user_message}]

    log.info("agent_start", story=story_title, model=model, repo=repo_dir)

    for turn in range(MAX_TURNS):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Claude finished without calling done — treat as done
            text = next((b.text for b in response.content if hasattr(b, "text")), "")
            log.info("agent_end_turn", turn=turn, text=text[:100])
            return text

        if response.stop_reason != "tool_use":
            raise RuntimeError(f"Unexpected stop_reason: {response.stop_reason}")

        # Execute all tool calls in this turn
        tool_results = []
        for block in response.content:
            if not hasattr(block, "type") or block.type != "tool_use":
                continue

            log.info("agent_tool_call", tool=block.name, input=str(block.input)[:120])
            result = _execute_tool(block.name, block.input, repo_dir)

            if result == "__DONE__":
                summary = block.input.get("summary", "Implementation complete")
                log.info("agent_done", summary=summary)
                return summary

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError(f"Coding agent exceeded {MAX_TURNS} turns without completing")
