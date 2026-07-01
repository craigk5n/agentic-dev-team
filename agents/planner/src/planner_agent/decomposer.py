"""LLM-based idea decomposition: approved idea → epics → stories.

Two-level planning: first break the project into epics (major feature areas, ordered
foundational-first), then decompose each epic into independently-shippable, test-first
stories, then run a completeness critic that adds anything the plan misses. The result
is a flat, globally-sequenced list of stories, each tagged with its epic — so the
execution engine (strict sequential build on merged, working code) is unchanged while
plans scale with the real scope of the idea instead of a fixed 3–7 cap.
"""

from __future__ import annotations
import json
import re

import litellm
import structlog

log = structlog.get_logger()

# Bounds — plan size scales with scope, but stays bounded.
_MIN_EPICS, _MAX_EPICS = 3, 8
_MIN_STORIES, _MAX_STORIES = 2, 6

_SYSTEM = (
    "You are a senior technical project manager and software architect. You break a "
    "software project into a thorough, well-ordered plan that another engineer can "
    "implement one small, independently-verifiable slice at a time."
)

_NO_STUB = """\
The target repository is ALREADY scaffolded (build files, test harness, CI exist). So:
- Do NOT create setup/scaffolding/"initialize the project" stories — that work is done.
- Every story must deliver a working, independently testable vertical slice — real
  behavior PLUS its tests — that can pass code review and CI on its own.
- NEVER a story that only declares a signature, stub, or placeholder: it fails review
  and cannot be fixed within its own scope."""

_EPICS_PROMPT = """\
Break this project into EPICS — the major feature areas needed to deliver it in full.

Title: {title}
Description:
{description}

Rules:
- Produce {min_e} to {max_e} epics. COVER THE FULL SCOPE — a large project needs more
  epics; do not under-decompose. Prefer completeness over brevity.
- Order them FOUNDATIONAL-FIRST: data model, core domain logic, and base layout/routing
  come BEFORE the features that depend on them, which come before polish (a11y, docs).
- Each epic is a coherent area of functionality, not a single task.
The repo is already scaffolded — do NOT include a setup/scaffolding epic.

Respond ONLY with valid JSON (no markdown fences):
{{"project_name": "<short name>", "epics": [{{"name": "<epic name>", "description": "<one sentence>"}}]}}
"""

_STORIES_PROMPT = """\
Project: {title}
{description}

You are decomposing ONE epic of this project into implementable stories.
Epic: {epic_name} — {epic_desc}
Other epics (do NOT duplicate their work): {other_epics}
{style_block}
Rules:
- Produce {min_s} to {max_s} stories for THIS epic only, ordered so each builds on the
  last. More for a substantial epic, fewer for a small one.
- Each story is an independently shippable, testable vertical slice.
{no_stub}
- Each story description MUST start with "repo: {default_repo}" on its own line.

Respond ONLY with valid JSON (no markdown fences):
{{"stories": [{{"title": "<title>", "description": "repo: {default_repo}\\n<2-3 sentences: what to implement and how>", "priority": "urgent"|"high"|"medium"|"low"|"none"}}]}}
"""

_CRITIC_PROMPT = """\
Project: {title}
{description}

Here is the proposed plan (epics, each with its stories):
{plan}

Act as a critical reviewer. Identify functionality that the project REQUIRES but the
plan MISSES or under-covers — gaps between the stories and what "done" actually means
for this project. For each gap, propose a concrete story assigned to an existing epic
(by exact name) or a new epic. Do NOT restate work already covered. If the plan is
genuinely complete, return an empty list.

Respond ONLY with valid JSON (no markdown fences):
{{"missing": [{{"epic": "<existing or new epic name>", "title": "<title>", "description": "repo: {default_repo}\\n<what to implement>", "priority": "high"|"medium"|"low"}}]}}
"""


def _style_block(sdlc_directive: str, best_practices: str) -> str:
    parts = []
    if sdlc_directive:
        parts.append("Development style — follow strictly:\n" + sdlc_directive)
    if best_practices:
        parts.append("Stack conventions to honor:\n" + best_practices)
    return ("\n" + "\n\n".join(parts) + "\n") if parts else ""


def _complete_json(messages, *, model, api_key, stack, redis_conn):
    kwargs: dict = {"model": model, "messages": messages, "temperature": 0.2}
    if api_key:
        kwargs["api_key"] = api_key
    resp = litellm.completion(**kwargs)
    if redis_conn is not None:
        try:
            from reviewer.telemetry import record_usage
            record_usage(redis_conn, "planner", model, resp, stack=stack)
        except Exception:
            pass
    raw = resp.choices[0].message.content or ""
    clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
    return json.loads(clean)


def _ensure_repo_prefix(desc: str, default_repo: str) -> str:
    desc = (desc or "").strip()
    if not desc.startswith("repo:"):
        desc = f"repo: {default_repo}\n{desc}"
    return desc


def decompose_idea(
    title: str,
    description: str,
    *,
    model: str,
    api_key: str = "",
    default_repo: str = "devadmin/sandbox",
    sdlc_directive: str = "",
    best_practices: str = "",
    stack: str = "",
    redis_conn=None,
) -> dict:
    """Return {project_name, module_name, epics, stories}. `stories` is the flat,
    globally-ordered list (epic by epic), each tagged with its `epic` name."""
    call = dict(model=model, api_key=api_key, stack=stack, redis_conn=redis_conn)
    desc = (description or "")[:4000]

    # ── Pass 1: epics (breadth, foundational-first) ──────────────────────────
    try:
        epics_data = _complete_json(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": _EPICS_PROMPT.format(
                 title=title, description=desc, min_e=_MIN_EPICS, max_e=_MAX_EPICS)}],
            **call)
        project_name = (epics_data.get("project_name") or title)[:80]
        epics = [{"name": e["name"], "description": e.get("description", "")}
                 for e in epics_data.get("epics", []) if e.get("name")][:_MAX_EPICS]
    except Exception as exc:
        log.warning("epics_pass_failed", error=str(exc)[:160])
        epics, project_name = [], title[:80]
    if not epics:
        return _fallback(title, default_repo)

    # ── Pass 2: stories per epic (depth) ─────────────────────────────────────
    epic_stories: dict[str, list[dict]] = {}
    for epic in epics:
        others = ", ".join(e["name"] for e in epics if e["name"] != epic["name"]) or "none"
        try:
            data = _complete_json(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": _STORIES_PROMPT.format(
                     title=title, description=desc, epic_name=epic["name"],
                     epic_desc=epic["description"], other_epics=others,
                     style_block=_style_block(sdlc_directive, best_practices),
                     min_s=_MIN_STORIES, max_s=_MAX_STORIES, no_stub=_NO_STUB,
                     default_repo=default_repo)}],
                **call)
            stories = data.get("stories", [])[:_MAX_STORIES]
        except Exception as exc:
            log.warning("epic_stories_pass_failed", epic=epic["name"], error=str(exc)[:120])
            stories = []
        epic_stories[epic["name"]] = [s for s in stories if s.get("title")]

    # ── Pass 3: completeness critic (coverage) ───────────────────────────────
    try:
        plan_summary = "\n".join(
            f'- {e["name"]}: ' + "; ".join(s["title"] for s in epic_stories.get(e["name"], []))
            for e in epics)
        crit = _complete_json(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": _CRITIC_PROMPT.format(
                 title=title, description=desc, plan=plan_summary, default_repo=default_repo)}],
            **call)
        for m in crit.get("missing", []):
            if not m.get("title"):
                continue
            ename = m.get("epic") or (epics[-1]["name"] if epics else "Additional")
            if ename not in epic_stories:               # critic introduced a new epic
                epics.append({"name": ename, "description": "Added by completeness review."})
                epic_stories[ename] = []
            epic_stories[ename].append(
                {"title": m["title"], "description": m.get("description", ""),
                 "priority": m.get("priority", "medium")})
        log.info("plan_critic_done", added=len(crit.get("missing", [])))
    except Exception as exc:
        log.warning("plan_critic_failed", error=str(exc)[:120])

    # ── Assemble: flat, epic-ordered, globally-sequenced ─────────────────────
    flat: list[dict] = []
    for epic in epics:
        for s in epic_stories.get(epic["name"], []):
            flat.append({
                "title": s["title"][:200],
                "description": _ensure_repo_prefix(s.get("description", ""), default_repo),
                "priority": s.get("priority", "medium"),
                "epic": epic["name"],
            })
    if not flat:
        return _fallback(title, default_repo)

    log.info("plan_assembled", project=project_name, epics=len(epics), stories=len(flat))
    return {"project_name": project_name, "module_name": project_name,
            "epics": epics, "stories": flat}


def _fallback(title: str, default_repo: str) -> dict:
    """LLM unavailable/unparseable — a single story so the pipeline still moves."""
    return {
        "project_name": title[:80], "module_name": title[:80],
        "epics": [{"name": title[:60], "description": f"Implement: {title}"}],
        "stories": [{
            "title": f"Implement {title}"[:200],
            "description": f"repo: {default_repo}\nImplement the feature as described in the idea.",
            "priority": "medium", "epic": title[:60],
        }],
    }
