"""HS-7: non-functional requirements (NFRs) captured per project and enforced.

"Local-first" was in the DEVHUB PRD, but the build reached for public CDNs *and* added SSRF
guards that then blocked localhost/LAN — the live bug. NFRs were neither reconciled across
stories nor verified. This module is the single source of truth: each NFR carries ONE
reconciliation note (injected into every story's coder prompt, so stories don't each
re-decide the same trade-off) and machine-checkable assertions the reviewer verifies.
"""
from __future__ import annotations

# id -> {display_name, detect (keywords), reconciliation_note (coder), assertions (verify)}
_NFRS: dict[str, dict] = {
    "local-first": {
        "display_name": "Local-first",
        "detect": ("local-first", "local first", "self-host", "self host", "on-prem",
                   "on prem", "on premise", "on-premise", "lan", "intranet", "air-gap",
                   "air gap", "runs locally", "homelab", "home lab"),
        "reconciliation_note": (
            "LOCAL-FIRST app. Reconcile this ONCE for the whole project (do not re-decide "
            "per story): (1) VENDOR all JS/CSS locally (e.g. static/vendor/) and reference "
            "by relative path — never a public CDN, never a guessed SRI/integrity hash. "
            "(2) Any SSRF/URL guard MUST still allow loopback/private/LAN targets when the "
            "operator enables them via an explicit config flag — do NOT hardcode a "
            "public-only block that leaves the app unable to reach its own services."
        ),
        "assertions": (
            "No external network is required to load/render the UI (all assets vendored "
            "locally; no CDN or remote <script>/<link>). Private/loopback/LAN targets are "
            "reachable when the operator enables them (the SSRF guard is opt-in, not a hard "
            "public-only block)."
        ),
    },
    "offline-capable": {
        "display_name": "Offline-capable",
        "detect": ("offline", "no internet", "without internet", "no external network",
                   "fully offline", "works offline"),
        "reconciliation_note": (
            "OFFLINE-CAPABLE: the app must start and serve its core UI with no outbound "
            "internet access. Bundle every asset and dependency; never fetch from a remote "
            "host at runtime to render a page."
        ),
        "assertions": (
            "The app starts and serves its core UI with outbound internet disabled (no "
            "runtime fetches to remote hosts are needed to render a page)."
        ),
    },
}


def all_nfrs() -> dict:
    """The full NFR registry."""
    return _NFRS


def is_known(nfr_id: str) -> bool:
    return nfr_id in _NFRS


def detect_nfrs(text: str) -> list[str]:
    """Detect applicable NFR ids from free text (idea title + description + prompt)."""
    low = (text or "").lower()
    return sorted(nid for nid, spec in _NFRS.items()
                  if any(k in low for k in spec["detect"]))


def reconciliation_note(nfr_ids) -> str:
    """The single global reconciliation note for the coder prompt (empty when no NFRs)."""
    notes = [_NFRS[n]["reconciliation_note"] for n in (nfr_ids or []) if n in _NFRS]
    if not notes:
        return ""
    return ("Project NFRs — reconciled once for every story (do not re-decide per story):\n"
            + "\n".join(f"- {n}" for n in notes))


def assertions(nfr_ids) -> str:
    """The NFR assertions the reviewer must verify (empty when no NFRs)."""
    items = [_NFRS[n]["assertions"] for n in (nfr_ids or []) if n in _NFRS]
    if not items:
        return ""
    return ("Project NFRs to verify (flag any violation as a blocking finding):\n"
            + "\n".join(f"- {a}" for a in items))
