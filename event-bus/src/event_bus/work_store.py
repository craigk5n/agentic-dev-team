"""
Work item store — SQLite-backed; the coordination backbone for ideas and stories.

Schema
------
work_items(id, type, title, prompt, description, state, parent_id,
           model_used, repo, created_at, updated_at)

States
------
idea:   pending-approval → approved | rejected
story:  ready → in-progress → in-review → changes-requested → merged → done
"""

from __future__ import annotations
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DB_PATH = Path("/data/work_items.db")
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

STATE_COLORS: dict[str, str] = {
    "pending-approval":   "#f59e0b",
    "approved":           "#22c55e",
    "backlog":            "#4b5563",
    "ready":              "#3b82f6",
    "in-progress":        "#f59e0b",
    "in-review":          "#8b5cf6",
    "changes-requested":  "#f97316",
    "merged":             "#22c55e",
    "done":               "#46a758",
    "rejected":           "#6b7280",
    "abandoned":          "#6b7280",
}

STATE_ORDER = list(STATE_COLORS.keys())

# Story 5.2 — story label vocabularies (for the trust-boundary risk study, T1.1).
# A trust boundary is code that generates code, renders untrusted data into a sink, or
# makes outbound calls — where autonomous coding is most costly/risky.
TRUST_BOUNDARY_CLASSES = frozenset({
    "generates-code", "renders-untrusted", "calls-outward", "none"})
STORY_SIZES = frozenset({"xs", "s", "m", "l", "xl"})


def validate_story_label(value: str | None, allowed: frozenset, field: str) -> None:
    """Raise ValueError if a non-empty label is outside its vocabulary. Empty/None = unset."""
    if value and value not in allowed:
        raise ValueError(f"invalid {field}: {value!r} (one of {sorted(allowed)})")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS work_items (
            id          TEXT PRIMARY KEY,
            type        TEXT NOT NULL CHECK(type IN ('idea','story')),
            title       TEXT NOT NULL,
            prompt      TEXT,
            description TEXT,
            state       TEXT NOT NULL DEFAULT 'pending-approval',
            parent_id   TEXT REFERENCES work_items(id),
            sequence    INTEGER,
            model_used  TEXT,
            pr_url      TEXT,
            stack       TEXT,
            sdlc        TEXT,
            stack_rationale TEXT,
            style_guides TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_state ON work_items(state);
        CREATE INDEX IF NOT EXISTS idx_parent ON work_items(parent_id);

        -- Story 5.3: durable defect ledger. Records each observed runtime defect linked to
        -- a story/run/PR, so verdict precision/recall (T3.1) is computable and every run
        -- feeds the dataset. Verdicts live in Redis (TTL'd); the join is done at read time.
        CREATE TABLE IF NOT EXISTS defects (
            id           TEXT PRIMARY KEY,
            run_id       TEXT,
            story_id     TEXT REFERENCES work_items(id),
            pr_url       TEXT,
            source       TEXT,          -- 'oracle' | 'manual'
            class        TEXT,
            description  TEXT,
            detected_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_defect_story ON defects(story_id);
        CREATE INDEX IF NOT EXISTS idx_defect_run ON defects(run_id);
    """)
    # Migrate existing DBs that pre-date optional columns
    for col, definition in [("pr_url", "TEXT"), ("sequence", "INTEGER"), ("repo", "TEXT"),
                            ("stack", "TEXT"), ("sdlc", "TEXT"), ("stack_rationale", "TEXT"),
                            ("style_guides", "TEXT"), ("archived_at", "TEXT"),
                            ("epic", "TEXT"), ("design_decisions", "TEXT"),
                            ("planner_model", "TEXT"), ("started_at", "TEXT"),
                            ("nfrs", "TEXT"), ("run_id", "TEXT"),
                            ("trust_boundary_class", "TEXT"), ("size", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE work_items ADD COLUMN {col} {definition}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists


def create_item(
    *,
    item_type: str,
    title: str,
    prompt: str = "",
    description: str = "",
    state: str = "pending-approval",
    parent_id: Optional[str] = None,
    sequence: Optional[int] = None,
    model_used: str = "",
    repo: str = "",
    stack: str = "",
    sdlc: str = "",
    stack_rationale: str = "",
    style_guides: Optional[list[str]] = None,
    epic: str = "",
    design_decisions: str = "",
    planner_model: str = "",
    run_id: str = "",
    trust_boundary_class: str = "",
    size: str = "",
    item_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> dict:
    validate_story_label(trust_boundary_class, TRUST_BOUNDARY_CLASSES, "trust_boundary_class")
    validate_story_label(size, STORY_SIZES, "size")
    item_id = item_id or str(uuid.uuid4())
    now = created_at or _now()
    guides_csv = ",".join(style_guides) if style_guides else None
    with _lock:
        db = get_db()
        db.execute(
            """INSERT INTO work_items
               (id, type, title, prompt, description, state, parent_id, sequence,
                model_used, repo, stack, sdlc, stack_rationale, style_guides, epic,
                design_decisions, planner_model, run_id, trust_boundary_class, size,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, item_type, title, prompt, description, state, parent_id,
             sequence, model_used, repo or None, stack or None, sdlc or None,
             stack_rationale or None, guides_csv, epic or None,
             design_decisions or None, planner_model or None, run_id or None,
             trust_boundary_class or None, size or None, now, now),
        )
        db.commit()
    return get_item(item_id)


def _parse_guides(csv: str | None) -> list[str]:
    return [g for g in (csv or "").split(",") if g]


def set_stack_sdlc(item_id: str, stack: str | None, sdlc: str | None) -> dict | None:
    """Set/override the chosen stack and SDLC on an item (e.g. at approval)."""
    with _lock:
        db = get_db()
        db.execute(
            "UPDATE work_items SET stack=?, sdlc=?, updated_at=? WHERE id=?",
            (stack or None, sdlc or None, _now(), item_id),
        )
        db.commit()
    return get_item(item_id)


def set_style_guides(item_id: str, style_guides: list[str]) -> dict | None:
    """Set/override the selected style guides on an item (e.g. at approval)."""
    csv = ",".join(style_guides) if style_guides else None
    with _lock:
        db = get_db()
        db.execute(
            "UPDATE work_items SET style_guides=?, updated_at=? WHERE id=?",
            (csv, _now(), item_id),
        )
        db.commit()
    return get_item(item_id)


def get_style_guides_for_story(item_id: str) -> list[str]:
    """Return the style-guide ids for a story, inheriting from its parent idea when unset."""
    item = get_item(item_id)
    if not item:
        return []
    guides = _parse_guides(item.get("style_guides"))
    if not guides and item.get("parent_id"):
        parent = get_item(item["parent_id"]) or {}
        guides = _parse_guides(parent.get("style_guides"))
    return guides


def get_style_guides_for_repo(repo: str) -> list[str]:
    """Return the style-guide ids for a repo (all its items share the idea's guides)."""
    if not repo:
        return []
    with _lock:
        row = get_db().execute(
            "SELECT style_guides FROM work_items "
            "WHERE repo=? AND style_guides IS NOT NULL AND style_guides != '' LIMIT 1",
            (repo,),
        ).fetchone()
    return _parse_guides(row["style_guides"]) if row else []


def get_nfrs_for_story(item_id: str) -> list[str]:
    """Return the NFR ids for a story (HS-7), inheriting from its parent idea when unset."""
    item = get_item(item_id)
    if not item:
        return []
    nfrs = _parse_guides(item.get("nfrs"))
    if not nfrs and item.get("parent_id"):
        parent = get_item(item["parent_id"]) or {}
        nfrs = _parse_guides(parent.get("nfrs"))
    return nfrs


def get_nfrs_for_repo(repo: str) -> list[str]:
    """Return the NFR ids for a repo (HS-7) — all its items share the idea's NFRs."""
    if not repo:
        return []
    with _lock:
        row = get_db().execute(
            "SELECT nfrs FROM work_items "
            "WHERE repo=? AND nfrs IS NOT NULL AND nfrs != '' LIMIT 1",
            (repo,),
        ).fetchone()
    return _parse_guides(row["nfrs"]) if row else []


def get_stack_sdlc_for_story(item_id: str) -> tuple[str | None, str | None]:
    """Return (stack, sdlc) for a story, inheriting from its parent idea when unset."""
    item = get_item(item_id)
    if not item:
        return (None, None)
    stack, sdlc = item.get("stack"), item.get("sdlc")
    if (not stack or not sdlc) and item.get("parent_id"):
        parent = get_item(item["parent_id"]) or {}
        stack = stack or parent.get("stack")
        sdlc = sdlc or parent.get("sdlc")
    return (stack, sdlc)


def set_repo(item_id: str, repo: str) -> dict | None:
    with _lock:
        db = get_db()
        db.execute(
            "UPDATE work_items SET repo=?, updated_at=? WHERE id=?",
            (repo, _now(), item_id),
        )
        db.commit()
    return get_item(item_id)


def get_repo_for_story(item_id: str, default: str = "") -> str:
    """Return the repo for a story, falling back to its parent idea's repo."""
    item = get_item(item_id)
    if not item:
        return default
    if item.get("repo"):
        return item["repo"]
    if item.get("parent_id"):
        parent = get_item(item["parent_id"])
        if parent and parent.get("repo"):
            return parent["repo"]
    return default


def get_item(item_id: str) -> dict | None:
    with _lock:
        row = get_db().execute(
            "SELECT * FROM work_items WHERE id = ?", (item_id,)
        ).fetchone()
    return dict(row) if row else None


def list_items(state: str = "", item_type: str = "") -> list[dict]:
    with _lock:
        if state and item_type:
            rows = get_db().execute(
                "SELECT * FROM work_items WHERE state=? AND type=? ORDER BY created_at DESC",
                (state, item_type),
            ).fetchall()
        elif state:
            rows = get_db().execute(
                "SELECT * FROM work_items WHERE state=? ORDER BY created_at DESC", (state,)
            ).fetchall()
        elif item_type:
            rows = get_db().execute(
                "SELECT * FROM work_items WHERE type=? ORDER BY created_at DESC", (item_type,)
            ).fetchall()
        else:
            rows = get_db().execute(
                "SELECT * FROM work_items ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def update_state(item_id: str, new_state: str) -> dict | None:
    now = _now()
    with _lock:
        db = get_db()
        # Stamp started_at the FIRST time a story begins coding (recodes don't reset it),
        # so a completed story can show how long it actually took to build.
        if new_state == "in-progress":
            db.execute(
                "UPDATE work_items SET state=?, updated_at=?, "
                "started_at=COALESCE(started_at, ?) WHERE id=?",
                (new_state, now, now, item_id),
            )
        else:
            db.execute(
                "UPDATE work_items SET state=?, updated_at=? WHERE id=?",
                (new_state, now, item_id),
            )
        db.commit()
    return get_item(item_id)


def find_item_by_pr_url(pr_url: str) -> dict | None:
    """Find an in-review story by PR URL, matching on path to tolerate host differences."""
    from urllib.parse import urlparse
    target_path = urlparse(pr_url).path
    with _lock:
        rows = get_db().execute(
            "SELECT * FROM work_items WHERE pr_url IS NOT NULL AND state IN ('in-review','changes-requested')"
        ).fetchall()
    for row in rows:
        if urlparse(row["pr_url"]).path == target_path:
            return dict(row)
    return None


def find_story_id_by_pr(repo_full_name: str, pr_number: int) -> str:
    """Return the story item id whose PR is repo_full_name#pr_number, or "" (HS-9 per-story
    cost attribution). Matches the stored pr_url's path ending in /{owner}/{repo}/pulls/{n},
    independent of host and of the story's current state."""
    if not repo_full_name or not pr_number:
        return ""
    suffix = f"/{repo_full_name}/pulls/{pr_number}"
    from urllib.parse import urlparse
    with _lock:
        rows = get_db().execute(
            "SELECT id, pr_url FROM work_items WHERE type='story' AND pr_url IS NOT NULL "
            "AND pr_url != ''"
        ).fetchall()
    for row in rows:
        if urlparse(row["pr_url"]).path.endswith(suffix):
            return row["id"]
    return ""


def unlock_next_story(item_id: str) -> dict | None:
    """Transition the next backlog story to ready after item_id completes.

    Gap-tolerant: picks the lowest-sequence backlog story AFTER this one, not strictly
    sequence+1 — so a deleted/renumbered story (e.g. a de-duplicated plan) can't leave a
    hole that halts the whole chain.
    """
    item = get_item(item_id)
    if not item or item.get("sequence") is None or not item.get("parent_id"):
        return None
    with _lock:
        row = get_db().execute(
            "SELECT id FROM work_items WHERE parent_id=? AND sequence > ? "
            "AND type='story' AND state='backlog' ORDER BY sequence ASC LIMIT 1",
            (item["parent_id"], item["sequence"]),
        ).fetchone()
    if not row:
        return None
    return update_state(row["id"], "ready")


def set_planning_inputs(item_id: str, design_decisions: str | None = None,
                        planner_model: str | None = None,
                        nfrs: str | None = None) -> dict | None:
    """Persist the operator's answered design decisions (JSON), chosen planner model, and
    detected NFR ids (CSV) on an idea at approval, so the planner/coder/reviewer can use them."""
    sets, vals = [], []
    if design_decisions is not None:
        sets.append("design_decisions=?"); vals.append(design_decisions or None)
    if planner_model is not None:
        sets.append("planner_model=?"); vals.append(planner_model or None)
    if nfrs is not None:
        sets.append("nfrs=?"); vals.append(nfrs or None)
    if not sets:
        return get_item(item_id)
    with _lock:
        db = get_db()
        db.execute(f"UPDATE work_items SET {', '.join(sets)}, updated_at=? WHERE id=?",
                   (*vals, _now(), item_id))
        db.commit()
    return get_item(item_id)


def set_pr_url(item_id: str, pr_url: str) -> dict | None:
    with _lock:
        db = get_db()
        db.execute(
            "UPDATE work_items SET pr_url=?, updated_at=? WHERE id=?",
            (pr_url, _now(), item_id),
        )
        db.commit()
    return get_item(item_id)


def grouped_items() -> dict[str, list[dict]]:
    """Return active (non-archived) items grouped by state, in workflow order."""
    all_items = [i for i in list_items() if not i.get("archived_at")]
    groups: dict[str, list[dict]] = {s: [] for s in STATE_ORDER}
    for item in all_items:
        s = item["state"]
        if s not in groups:
            groups[s] = []
        groups[s].append(item)
    return {k: v for k, v in groups.items() if v}


def set_archived(idea_id: str, archived: bool) -> int:
    """Archive/restore a project: stamp (or clear) archived_at on the idea AND all its
    stories so the whole tree drops off / returns to the board. Returns rows touched."""
    stamp = _now() if archived else None
    with _lock:
        db = get_db()
        cur = db.execute(
            "UPDATE work_items SET archived_at=?, updated_at=? WHERE id=? OR parent_id=?",
            (stamp, _now(), idea_id, idea_id),
        )
        db.commit()
        return cur.rowcount


def delete_item_tree(idea_id: str) -> int:
    """Permanently delete a project: the idea and all its stories. Returns rows deleted."""
    with _lock:
        db = get_db()
        cur = db.execute(
            "DELETE FROM work_items WHERE id=? OR parent_id=?", (idea_id, idea_id)
        )
        db.commit()
        return cur.rowcount


def add_defect(story_id: str, defect_class: str, description: str, *,
               source: str = "manual", run_id: str = "", pr_url: str = "",
               detected_at: Optional[str] = None) -> dict:
    """Insert one defect into the ledger (Story 5.3). Returns the stored row."""
    did = str(uuid.uuid4())
    now = detected_at or _now()
    with _lock:
        db = get_db()
        db.execute(
            """INSERT INTO defects
               (id, run_id, story_id, pr_url, source, class, description, detected_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (did, run_id or None, story_id or None, pr_url or None, source,
             defect_class, description, now),
        )
        db.commit()
        row = db.execute("SELECT * FROM defects WHERE id=?", (did,)).fetchone()
    return dict(row)


def record_oracle_defects(story_id: str, oracle_result: dict, *,
                          run_id: str = "", pr_url: str = "") -> list[dict]:
    """Insert a ledger row per defect in an oracle result (EPIC 2 → ledger bridge). This is
    the integration point a post-merge oracle run calls after persist_oracle_result."""
    rows = []
    for d in (oracle_result or {}).get("defects", []):
        rows.append(add_defect(
            story_id, d.get("class", "unknown"), d.get("description", ""),
            source="oracle", run_id=run_id, pr_url=pr_url))
    return rows


def list_defects(story_id: str = "", run_id: str = "") -> list[dict]:
    """List ledger defects, optionally filtered by story or run (most-recent first)."""
    with _lock:
        db = get_db()
        if story_id:
            rows = db.execute("SELECT * FROM defects WHERE story_id=? ORDER BY detected_at DESC",
                              (story_id,)).fetchall()
        elif run_id:
            rows = db.execute("SELECT * FROM defects WHERE run_id=? ORDER BY detected_at DESC",
                              (run_id,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM defects ORDER BY detected_at DESC").fetchall()
    return [dict(r) for r in rows]


_GREEN_VERDICTS = frozenset({"pass", "green", "success"})


def defects_vs_verdicts(defects: list[dict], verdicts: dict) -> list[dict]:
    """Annotate each defect with the story's R/T/S verdict statuses and whether every
    verdict was green — i.e. the pipeline shipped the defect uncaught (the T3.1 signal).
    ``verdicts`` = {"code_review": "pass|warn|fail", "test_run": ..., "security": ...};
    injected by the caller (verdicts live in Redis, decoupled from this store)."""
    missed_by_all = bool(verdicts) and all(v in _GREEN_VERDICTS for v in verdicts.values())
    return [{**d, "verdicts": dict(verdicts), "missed_by_all": missed_by_all} for d in defects]


def list_items_by_run(run_id: str) -> list[dict]:
    """Return all work items tagged with ``run_id`` (experiment scoping, Story 5.1)."""
    if not run_id:
        return []
    with _lock:
        rows = get_db().execute(
            "SELECT * FROM work_items WHERE run_id=? ORDER BY created_at DESC", (run_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def list_projects() -> list[dict]:
    """All ideas (active + archived) with a rolled-up story summary, newest first —
    powers the Projects view."""
    items = list_items()
    stories_by_parent: dict[str, list[dict]] = {}
    for it in items:
        if it.get("type") == "story" and it.get("parent_id"):
            stories_by_parent.setdefault(it["parent_id"], []).append(it)
    import re
    projects = []
    for idea in items:
        if idea.get("type") != "idea":
            continue
        kids = stories_by_parent.get(idea["id"], [])
        done = sum(1 for s in kids if s["state"] == "done")
        # Derive the repo's browser URL from a story's PR URL (carries the public host).
        repo_url = None
        for s in kids:
            m = re.match(r"^(.*)/pulls/\d+", s.get("pr_url") or "")
            if m:
                repo_url = m.group(1)
                break
        projects.append({
            **idea,
            "story_count": len(kids),
            "stories_done": done,
            "archived": bool(idea.get("archived_at")),
            "repo_url": repo_url,
        })
    projects.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    return projects
