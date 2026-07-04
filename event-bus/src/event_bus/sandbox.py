"""
Per-agent container sandbox (Phase 7).

Modes (set via SANDBOX_MODE env var):
  process (default) — run agent functions in-process (existing behavior, no isolation)
  docker            — spawn an ephemeral Docker container per job

Docker mode requires:
  - Docker socket mounted into the worker container:
      volumes: ["/var/run/docker.sock:/var/run/docker.sock"]
  - SANDBOX_IMAGE pointing to a built event-bus image (default: dev-agents/event-bus:latest)
  - `docker` Python package (pip install docker)
  - SANDBOX_MEMORY / SANDBOX_CPUS resource limits (default: 512m / 1.0)

Each Docker container receives only the env vars it needs for its agent role
(scoped credentials), plus AGENT_FUNC and AGENT_KWARGS_B64 for the job payload.
The container runs event_bus.sandbox_runner which calls the function and prints JSON.

This lets each agent run with least-privilege credentials and enforces that coding
agents cannot push to protected branches (Forgejo branch protection is the hard stop;
scoped env vars add defence-in-depth by not passing admin tokens at all).
"""

from __future__ import annotations
import base64
import json
import os
import socket
from typing import Callable, Any

import structlog

log = structlog.get_logger()

_MODE = os.environ.get("SANDBOX_MODE", "process").lower()
_IMAGE = os.environ.get("SANDBOX_IMAGE", "dev-agents/event-bus:latest")
_MEMORY = os.environ.get("SANDBOX_MEMORY", "512m")
_CPUS = float(os.environ.get("SANDBOX_CPUS", "1.0"))
# HOST path to the `claude` CLI, bound into the reviewer sandbox for claude-code/*
# (subscription) reviews. The image doesn't ship the binary (it's mounted into the
# worker); the reviewer runs in a fresh sandbox container, so it needs its own mount.
_CLAUDE_BIN = os.environ.get("SANDBOX_CLAUDE_BIN", "")

# Env var groups each agent role needs.
# Keys are prefixes that appear in func dotted paths.
_ROLE_ENV: dict[str, list[str]] = {
    "reviewer": [
        "FORGEJO_API_TOKEN", "FORGEJO_REVIEWER_TOKEN", "FORGEJO_BASE_URL", "FORGEJO_GIT_URL",
        "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY",
        # Subscription (claude-code/*) reviewer routes through the claude CLI, which needs
        # the OAuth token scoped into the sandbox. Unlike the planner (which runs
        # in-process), the reviewer runs here in an isolated container. (CLAUDE_CODE_BIN is
        # intentionally omitted — the binary is on the sandbox PATH and _scoped_env would
        # otherwise inject an empty value that overrides the adapter's "claude" default.)
        "CLAUDE_CODE_OAUTH_TOKEN",
        "MODEL_REVIEWER", "MODEL_TESTER", "MODEL_SECURITY",
        "REDIS_URL",
    ],
    "coding_agent": [
        "ANTHROPIC_API_KEY", "FORGEJO_API_TOKEN", "FORGEJO_CODER_TOKEN",
        "FORGEJO_BASE_URL", "FORGEJO_GIT_URL",
        "DEFAULT_REPO", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
        "REDIS_URL",
    ],
    "idea_agent": [
        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
        "MODEL_IDEA", "REDIS_URL",
    ],
    "planner_agent": [
        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
        "MODEL_PLANNER", "REDIS_URL",
    ],
}

_ALWAYS = ["LOG_LEVEL"]


def _parse_result(output: str) -> dict | None:
    """Extract the sandbox result — the last JSON object line in the container output.
    Agent logs and stderr may precede it, so scan lines from the end."""
    for line in reversed([ln for ln in output.splitlines() if ln.strip()]):
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                continue
    return None


def _scoped_env(func_module: str) -> dict[str, str]:
    """Return only the env vars needed for the agent package that owns the function."""
    env = {k: os.environ.get(k, "") for k in _ALWAYS}
    for prefix, var_names in _ROLE_ENV.items():
        if prefix in func_module:
            for name in var_names:
                env[name] = os.environ.get(name, "")
            break
    return {k: v for k, v in env.items() if v}  # omit unset vars


class Sandbox:
    """
    Thin wrapper that either calls a function in-process or in a Docker container.
    """

    def __init__(self, mode: str = _MODE, image: str = _IMAGE,
                 memory: str = _MEMORY, cpus: float = _CPUS):
        self.mode = mode
        self.image = image
        self.memory = memory
        self.cpus = cpus

    def run(self, func: Callable[..., Any], **kwargs: Any) -> Any:
        if self.mode == "docker":
            return self._docker_run(func, kwargs)
        # process mode: call directly
        return func(**kwargs)

    def _docker_run(self, func: Callable[..., Any], kwargs: dict) -> Any:
        try:
            import docker  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "SANDBOX_MODE=docker requires the 'docker' package. "
                "Add it to your dependencies or set SANDBOX_MODE=process."
            ) from exc

        func_path = f"{func.__module__}.{func.__qualname__}"
        payload = base64.b64encode(json.dumps(kwargs).encode()).decode()
        env = self._scoped_env(func.__module__)
        env["AGENT_FUNC"] = func_path
        env["AGENT_KWARGS_B64"] = payload

        # The reviewer may run a claude-code/* (subscription) verdict, which shells out to
        # the `claude` CLI. That binary isn't in the image (the worker gets it via a bind
        # mount), so bind it into this sandbox too. Only the reviewer needs it.
        volumes = None
        if _CLAUDE_BIN and "code_review" in func.__module__:
            volumes = {_CLAUDE_BIN: {"bind": "/usr/local/bin/claude", "mode": "ro"}}

        log.info("sandbox_docker_run", func=func_path, image=self.image,
                 memory=self.memory, cpus=self.cpus, claude_mount=bool(volumes))

        client = docker.from_env()
        # Share the worker's network namespace so the sandbox resolves the same
        # docker-network hostnames the worker uses (forgejo:3000, event-bus-redis:6379).
        # Host networking can't resolve these; a single bridge network can't reach
        # both forgejo_default and the event-bus net at once.
        netns = f"container:{socket.gethostname()}"
        try:
            raw: bytes = client.containers.run(
                self.image,
                command=["python", "-m", "event_bus.sandbox_runner"],
                environment=env,
                volumes=volumes,
                mem_limit=self.memory,
                nano_cpus=int(self.cpus * 1_000_000_000),
                network_mode=netns,
                remove=True,
                # Capture BOTH streams: sandbox_runner prints its result/error as JSON to
                # stdout, and a hard crash (traceback, OOM) shows on stderr. Capturing only
                # stdout meant a failed container surfaced an EMPTY error (invisible bug).
                stdout=True,
                stderr=True,
            )
            # sandbox_runner prints the JSON result as the final line; agent logs (and any
            # stderr) may precede it, so scan from the end for the last JSON object.
            return _parse_result(raw.decode()) or {"status": "error", "reason": "empty sandbox output"}
        except docker.errors.ContainerError as exc:
            # On non-zero exit docker-py raises before we can parse; the container's output
            # (with sandbox_runner's {"status":"error","reason":...}) is on exc.stderr.
            out = (exc.stderr or b"").decode(errors="replace")
            reason = (_parse_result(out) or {}).get("reason") or out.strip()[-400:] or "no output"
            log.error("sandbox_container_failed", func=func_path, exit_code=exc.exit_status,
                      reason=reason)
            raise
        except Exception as exc:
            log.error("sandbox_docker_error", func=func_path, error=str(exc))
            raise

    @staticmethod
    def _scoped_env(func_module: str) -> dict[str, str]:
        return _scoped_env(func_module)


# Module-level singleton — create once per worker process
_sandbox: Sandbox | None = None


def get_sandbox() -> Sandbox:
    global _sandbox
    if _sandbox is None:
        _sandbox = Sandbox()
    return _sandbox
