"""
HTTP Basic Auth for the board UI/API.

The board has no user model — this is a single shared operator credential meant to
keep an exposed URL from being driven by strangers (who could run up the LLM bill).
It is enforced only when ``board_auth_password`` is set; leave it blank to disable.

Exempt paths (never require basic auth):
  /health     — liveness probe (Docker healthcheck)
  /webhook/   — authenticated by HMAC signature; callers (Forgejo) can't send basic auth
  /internal/  — service-to-service calls from the agents on the internal network
"""

from __future__ import annotations
import base64
import binascii
import hmac

_EXEMPT_PREFIXES = ("/health", "/webhook/", "/internal/")


def is_exempt(path: str) -> bool:
    """True if `path` should bypass basic auth (machine-to-machine / liveness)."""
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


def check_basic_auth(header: str, user: str, password: str) -> bool:
    """
    Constant-time validation of an ``Authorization: Basic <base64(user:pass)>`` header.
    Returns False for a missing/malformed header or wrong credentials.
    """
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    got_user, sep, got_pass = decoded.partition(":")
    if not sep:
        return False
    # Compare both fields with constant-time equality to avoid timing leaks.
    user_ok = hmac.compare_digest(got_user, user)
    pass_ok = hmac.compare_digest(got_pass, password)
    return user_ok and pass_ok
