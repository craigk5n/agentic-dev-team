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
}

STATE_ORDER = list(STATE_COLORS.keys())


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
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_state ON work_items(state);
        CREATE INDEX IF NOT EXISTS idx_parent ON work_items(parent_id);
    """)
    # Migrate existing DBs that pre-date optional columns
    for col, definition in [("pr_url", "TEXT"), ("sequence", "INTEGER"), ("repo", "TEXT")]:
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
    item_id: Optional[str] = None,
    created_at: Optional[str] = None,
) -> dict:
    item_id = item_id or str(uuid.uuid4())
    now = created_at or _now()
    with _lock:
        db = get_db()
        db.execute(
            """INSERT INTO work_items
               (id, type, title, prompt, description, state, parent_id, sequence,
                model_used, repo, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, item_type, title, prompt, description, state, parent_id,
             sequence, model_used, repo or None, now, now),
        )
        db.commit()
    return get_item(item_id)


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
    with _lock:
        db = get_db()
        db.execute(
            "UPDATE work_items SET state=?, updated_at=? WHERE id=?",
            (new_state, _now(), item_id),
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


def unlock_next_story(item_id: str) -> dict | None:
    """Transition the next sequenced backlog story to ready after item_id completes."""
    item = get_item(item_id)
    if not item or item.get("sequence") is None or not item.get("parent_id"):
        return None
    next_seq = item["sequence"] + 1
    with _lock:
        row = get_db().execute(
            "SELECT id FROM work_items WHERE parent_id=? AND sequence=? AND state='backlog'",
            (item["parent_id"], next_seq),
        ).fetchone()
    if not row:
        return None
    return update_state(row["id"], "ready")


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
    """Return all items grouped by state, in workflow order."""
    all_items = list_items()
    groups: dict[str, list[dict]] = {s: [] for s in STATE_ORDER}
    for item in all_items:
        s = item["state"]
        if s not in groups:
            groups[s] = []
        groups[s].append(item)
    return {k: v for k, v in groups.items() if v}
