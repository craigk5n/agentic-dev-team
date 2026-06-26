"""
Entry point for the sandboxed Docker container.

Reads AGENT_FUNC (dotted Python path) and AGENT_KWARGS_B64 (base64 JSON) from env,
calls the function, and prints the result as a single JSON line to stdout.

Run via:
  python -m event_bus.sandbox_runner
"""

from __future__ import annotations
import base64
import importlib
import json
import os
import sys


def main() -> None:
    func_path = os.environ.get("AGENT_FUNC", "")
    kwargs_b64 = os.environ.get("AGENT_KWARGS_B64", "")

    if not func_path or not kwargs_b64:
        print(json.dumps({"status": "error", "reason": "AGENT_FUNC or AGENT_KWARGS_B64 not set"}))
        sys.exit(1)

    try:
        kwargs = json.loads(base64.b64decode(kwargs_b64))
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": f"failed to decode kwargs: {exc}"}))
        sys.exit(1)

    try:
        module_path, func_name = func_path.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        func = getattr(mod, func_name)
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": f"failed to import {func_path}: {exc}"}))
        sys.exit(1)

    try:
        result = func(**kwargs)
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
