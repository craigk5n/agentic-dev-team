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
import time

import litellm
import structlog

log = structlog.get_logger()

# Fail fast instead of riding litellm's long default backoff, and retry the flaky
# empty/error responses free models emit (finish_reason='error' → empty content).
_CALL_TIMEOUT = 90.0        # seconds per attempt
_MAX_ATTEMPTS = 3           # initial + 2 retries
_RETRY_BACKOFF = 2.0        # seconds, ×attempt
_RATE_LIMIT_BACKOFF = 20.0  # seconds — 429s need a real pause, not a short retry

# Provider "out of credit / quota" markers — an unrecoverable failure (no point retrying).
_CREDIT_ERROR_MARKERS = (
    "insufficient credits", "requires more credits", "add more credits",
    "quota exceeded", "insufficient_quota", "payment required",
)


def _looks_like_credit_error(text: str) -> bool:
    low = (text or "").lower()
    return any(mk in low for mk in _CREDIT_ERROR_MARKERS)

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

# HS-8: the planner must not prescribe a vulnerability. #15 told the coder "empty
# credentials = auth open (matches the Go implementation)"; the reviewer then flagged it as
# a critical bypass, plan and review fought, and it shipped. When porting/adopting behavior,
# security-relevant defaults must be planned secure-by-default, not silently reproduced.
_SECURE_DEFAULTS = """\
- SECURE BY DEFAULT: when porting or adopting behavior from an existing implementation or
  spec, do NOT reproduce insecure defaults. Any security-relevant behavior — auth/authz
  disabled or bypassed when a value is unset ("empty credentials = allow"), binding to
  0.0.0.0, permissive CORS/CSRF, a default or hardcoded password/token, disabled
  TLS/verification — must be planned deny/closed by default, with any open or insecure mode
  behind an explicit opt-in config flag. If the source system is insecure, the story must
  call out the SECURE default (state it explicitly) rather than silently porting the
  insecure one — otherwise the coder builds it and the reviewer blocks it."""

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
{secure_defaults}
- Each story description MUST (a) name the specific target file(s) to create or modify and
  (b) state its acceptance criteria — the observable behavior that makes it "done". NEVER
  emit a story with an empty or one-line description; an under-specified story is rejected.
- Each story description MUST start with "repo: {default_repo}" on its own line.

Respond ONLY with valid JSON (no markdown fences):
{{"stories": [{{"title": "<title>", "description": "repo: {default_repo}\\n<what to implement, the target file(s), and 2-4 acceptance criteria>", "priority": "urgent"|"high"|"medium"|"low"|"none"}}]}}
"""

_IMPORT_CRITIC_PROMPT = """\
This plan was imported from an external spec that assumed a ready-made environment. It
will actually run in a FRESH scaffold: a minimal, GENERIC build file (e.g. pyproject)
exists but is NOT customized to this project, and declares NONE of its real dependencies.

Proposed plan (epics with their stories):
{plan}

Act as a release engineer doing a pre-flight check. List FOUNDATIONAL or CROSS-CUTTING
work the project needs to install cleanly, build, run, and deploy that NO story above
covers. Look hardest at the seams an external spec silently assumes:
- Every third-party library the code imports MUST be a declared dependency — the generic
  scaffold declares none of the project's real ones.
- Package metadata (name, version, entry points) finalized to THIS project.
- A clean-environment install + smoke/integration check that would catch an undeclared
  dependency or a broken build (not just per-unit tests).
- Container / build / CI / packaging artifacts the spec names but no story produces.
Do NOT restate feature work already covered. If genuinely complete, return an empty list.

Respond ONLY with valid JSON (no markdown fences):
{{"missing": [{{"epic": "<existing or new epic name>", "title": "<title>", "description": "repo: {default_repo}\\n<what to implement + how to verify>", "priority": "high"|"medium"|"low"}}]}}
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


_STRUCTURED_MEMO: dict[str, bool] = {}


def _supports_structured(redis_conn, model: str) -> bool:
    """Whether the model advertises structured-output support (from the model-meta cache).
    Memoized per process; safe no-op when the cache/event_bus package isn't available."""
    if model in _STRUCTURED_MEMO:
        return _STRUCTURED_MEMO[model]
    ok = False
    try:
        if redis_conn is not None:
            from event_bus.models_catalog import supports_structured as _ss
            ok = bool(_ss(redis_conn, model))
    except Exception:
        ok = False
    _STRUCTURED_MEMO[model] = ok
    return ok


def _complete_json(messages, *, model, api_key, stack, redis_conn, max_tokens=None,
                   timeout=None, attempts=None, project=""):
    """One LLM call returning parsed JSON, with a bounded timeout + retry. Raises the
    last error after the attempt budget so callers can fall back.

    ``claude-code*`` models route through the local claude CLI (operator's Claude Code
    subscription — no per-token billing, no telemetry cost to record); everything else
    goes through litellm as before. Models that support structured outputs are told to
    return a JSON object so they can't answer with prose.
    """
    from planner_agent import claude_code

    use_subscription = claude_code.is_claude_code_model(model)
    kwargs: dict = {"model": model, "messages": messages, "temperature": 0.2,
                    "timeout": timeout or _CALL_TIMEOUT, "num_retries": 0}
    if api_key:
        kwargs["api_key"] = api_key
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if not use_subscription and _supports_structured(redis_conn, model):
        kwargs["response_format"] = {"type": "json_object"}
    last_exc: Exception | None = None
    max_attempts = attempts or _MAX_ATTEMPTS
    for attempt in range(1, max_attempts + 1):
        try:
            if use_subscription:
                raw = claude_code.complete(messages, model=model,
                                           timeout=timeout or _CALL_TIMEOUT)
            else:
                resp = litellm.completion(**kwargs)
                if redis_conn is not None:
                    try:
                        from reviewer.telemetry import record_usage
                        record_usage(redis_conn, "planner", model, resp, stack=stack, project=project)
                    except Exception:
                        pass
                raw = resp.choices[0].message.content or ""
            clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
            clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
            return json.loads(clean)   # empty/malformed → JSONDecodeError → retry
        except Exception as exc:       # timeout, provider error, or bad JSON
            last_exc = exc
            # Out of provider credit/quota is unrecoverable — retrying can't refill the
            # balance, so abort immediately with a clear error instead of burning attempts.
            if _looks_like_credit_error(str(exc)):
                log.error("planner_llm_insufficient_credits", model=model, error=str(exc)[:200])
                raise
            # Rate limits (429 — common on subscription/Claude Code planning) need a
            # much longer pause than a transient hiccup; a short backoff just wastes the
            # attempt. If it doesn't clear, the caller aborts and the import is resumable.
            rate_limited = "429" in str(exc) or "rate" in str(exc).lower()
            log.warning("planner_llm_retry", attempt=attempt, max=max_attempts,
                        rate_limited=rate_limited, error=str(exc)[:140])
            if attempt < max_attempts:
                time.sleep(_RATE_LIMIT_BACKOFF if rate_limited else _RETRY_BACKOFF * attempt)
    raise last_exc


def _ensure_repo_prefix(desc: str, default_repo: str) -> str:
    desc = (desc or "").strip()
    if not desc.startswith("repo:"):
        desc = f"repo: {default_repo}\n{desc}"
    return desc


# ── HS-3: reject empty / under-specified stories at planning time ────────────
# #117 shipped with an EMPTY description and the coder responded by deleting the
# Dockerfile; #111 (thin) mass-deleted files. An under-specified story gives the coder
# room to do damage. The floor is raised in two places: the story prompts now demand
# named target files + acceptance criteria, and this post-assembly guardrail never lets
# an empty-description story reach dispatch.
_MIN_STORY_CHARS = 30
_MIN_STORY_WORDS = 5
# A filename-with-extension anywhere in the body (e.g. `src/app.py`, `Dockerfile.web`).
_FILE_RE = re.compile(r"[\w./\-]+\.[A-Za-z0-9]{1,6}\b")


def _story_body(description: str) -> str:
    """The story description minus its leading ``repo: ...`` line (the real spec)."""
    lines = (description or "").splitlines()
    if lines and lines[0].strip().lower().startswith("repo:"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def story_defects(story: dict) -> list[str]:
    """Classify why a story is under-specified, or ``[]`` if it's implementable.

    ``empty-description`` is the dangerous class — the demonstrated #117 failure — and is
    the only one this module hard-gates (redraft or drop). ``under-specified`` (too thin)
    and ``no-named-target-file`` are advisory: logged, but the story is kept, because
    dropping real scope is worse than dispatching a terse-but-present story.
    """
    body = _story_body(story.get("description", ""))
    if not body:
        return ["empty-description"]
    defects: list[str] = []
    if len(body) < _MIN_STORY_CHARS or len(body.split()) < _MIN_STORY_WORDS:
        defects.append("under-specified")
    if not _FILE_RE.search(body):
        defects.append("no-named-target-file")
    return defects


_REDRAFT_PROMPT = """\
This story has an EMPTY or unusable description and cannot be safely implemented. An
under-specified story once led a coding agent to DELETE files it shouldn't have. Rewrite
it into a concrete, self-contained, implementable story.

Project: {title}
Epic: {epic}
Story title: {story_title}
Current description: {current}

The rewrite MUST:
- Name the specific target file(s) to create or modify.
- List 2-4 acceptance criteria describing the finished, observable behavior.
- Deliver a working, independently testable slice — never a stub or a pure deletion.
- Start the description with "repo: {default_repo}" on its own line.

Respond ONLY with valid JSON (no markdown fences):
{{"title": "<title>", "description": "repo: {default_repo}\\n<what to implement + acceptance criteria>", "priority": "high"|"medium"|"low"}}
"""


def _redraft_story(story: dict, *, project_title: str, default_repo: str, **call) -> dict | None:
    """One LLM call to turn an empty/unusable story into an implementable one. Returns the
    redrafted story (tags preserved) or ``None`` if the call fails or still yields nothing."""
    try:
        data = _complete_json(
            [{"role": "system", "content": _SYSTEM},
             {"role": "user", "content": _REDRAFT_PROMPT.format(
                 title=project_title, epic=story.get("epic", ""),
                 story_title=story.get("title", ""),
                 current=(_story_body(story.get("description", "")) or "(empty)"),
                 default_repo=default_repo)}],
            max_tokens=1200, **call)
    except Exception as exc:  # noqa: BLE001 — a failed redraft falls through to a drop
        log.warning("story_redraft_failed", title=story.get("title", "")[:80], error=str(exc)[:120])
        return None
    desc = _ensure_repo_prefix(data.get("description", ""), default_repo)
    if not _story_body(desc):
        return None
    return {**story,
            "title": (data.get("title") or story.get("title", ""))[:200],
            "description": desc,
            "priority": data.get("priority", story.get("priority", "medium"))}


def _finalize_stories(flat: list[dict], *, project_title: str, default_repo: str,
                      **call) -> list[dict]:
    """HS-3 guardrail applied to the assembled story list. Never dispatch an
    empty-description story: redraft it once, and drop it (logged, never silent) if it
    can't be salvaged. Thin / file-less stories are logged advisory but kept."""
    out: list[dict] = []
    dropped: list[str] = []
    thin = 0
    for s in flat:
        defects = story_defects(s)
        if "empty-description" in defects:
            fixed = _redraft_story(s, project_title=project_title,
                                   default_repo=default_repo, **call)
            if fixed and "empty-description" not in story_defects(fixed):
                log.info("story_redrafted", title=fixed["title"][:80])
                out.append(fixed)
            else:
                dropped.append(s.get("title", "")[:80])
            continue
        if defects:
            thin += 1
        out.append(s)
    if thin:
        log.warning("underspecified_stories_kept", count=thin)
    if dropped:
        log.error("empty_stories_dropped", count=len(dropped), titles=dropped)
    return out


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
    decisions: str = "",
    redis_conn=None,
    project: str = "",
) -> dict:
    """Return {project_name, module_name, epics, stories}. `stories` is the flat,
    globally-ordered list (epic by epic), each tagged with its `epic` name.
    `decisions`: operator-locked design decisions the whole plan must honor.
    `project`: idea/project id — attributes this planning spend to the project."""
    call = dict(model=model, api_key=api_key, stack=stack, redis_conn=redis_conn, project=project)
    desc = (description or "")[:4000]
    if decisions:
        desc = f"{desc}\n\n{decisions}"   # ride along on every planning prompt's context

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
                     secure_defaults=_SECURE_DEFAULTS, default_repo=default_repo)}],
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

    # HS-3: never dispatch an empty-description story (redraft or drop).
    flat = _finalize_stories(flat, project_title=project_name, default_repo=default_repo, **call)
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


_MAX_PLAN_CHARS = 200_000   # fits a large PRD on a big-context model (Sonnet); free
                            # models with smaller context degrade to the fallback

_MAX_IMPORT_EPICS = 25   # backstop for a very large PRD

# Import pass-2 calls legitimately generate long output (an epic's full specs), so they
# get a longer timeout and a bigger output budget than ordinary planning calls — but
# only 2 attempts, and the whole import aborts after 3 consecutive epic failures so a
# systematic problem can't burn money grinding through every epic with retries.
_IMPORT_TIMEOUT = 300.0
_IMPORT_STORY_TOKENS = 16000
_IMPORT_ATTEMPTS = 2
_IMPORT_MAX_CONSECUTIVE_FAILURES = 3


def _split_plan_by_epics(plan: str, epic_names: list[str]) -> dict[str, str]:
    """Split the pasted plan into per-epic sections LOCALLY (no LLM), by matching each
    epic name against the plan's markdown headings. Returns {epic_name: section_text}
    for the epics whose heading was found; callers fall back to the full plan for any
    epic that isn't matched.

    This is the cost fix: pass 2 then sends each call only its epic's slice (a few KB)
    instead of re-sending the entire plan per epic (input cost = epics × plan size).
    """
    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    headings = [(m.start(), m.group(0)) for m in re.finditer(r"(?m)^#{1,3} .+$", plan)]
    if not headings:
        return {}

    # Locate the heading that introduces each epic (first heading whose text contains
    # the epic name, or vice versa — tolerates "Epic 4 — MCP Protocol Module" etc.)
    starts: list[tuple[int, str]] = []
    for name in epic_names:
        n = norm(name)
        if not n:
            continue
        for pos, text in headings:
            h = norm(text.lstrip("#"))
            if n in h or (len(h) > 8 and h in n):
                starts.append((pos, name))
                break
    if not starts:
        return {}

    # Slice from each epic's heading to the next epic's heading (plan order).
    starts.sort()
    sections: dict[str, str] = {}
    for i, (pos, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(plan)
        sections[name] = plan[pos:end]
    return sections

_NORMALIZE_EPICS_PROMPT = """\
Read this EXISTING, pre-written implementation plan and extract ONLY its EPIC list. You
are RE-STRUCTURING an existing plan, not inventing one — preserve its epics/phases and
their order (if it has none, group related work into coherent epics, foundational-first).

PLAN:
{plan}

Return just the epic names + a one-sentence description each. Do NOT list individual
stories/tasks — those come next. OMIT any pure setup/scaffolding phase ("project
skeleton", "bootstrap pyproject") — the target repo is already scaffolded.

Respond ONLY with valid JSON (no markdown fences):
{{"project_name": "<name>",
  "epics": [{{"name": "<epic>", "description": "<one sentence>"}}]}}
"""

_NORMALIZE_STORIES_PROMPT = """\
From this section of an EXISTING implementation plan, extract and expand the stories of
ONE epic into implementable form for an autonomous coding pipeline.

Epic: {epic_name} — {epic_desc}

PLAN SECTION for this epic:
{plan}

Find EVERY story/task in this section that belongs to the epic and produce one story
for each. Rules:
- The target repo is ALREADY scaffolded — OMIT pure setup/scaffolding stories.
{no_stub}
{secure_defaults}
- Carry each story's concrete spec (schemas, endpoints, names, acceptance) FROM THE PLAN
  into its description so a coding agent can implement it without the original document.
  Summarize long code listings into their essential requirements — do not copy every line.
- Each story description MUST name the specific target file(s) and its acceptance criteria.
  NEVER emit a story with an empty or one-line description — an under-specified story is
  rejected before dispatch.
- Each story description MUST start with "repo: {default_repo}" on its own line.

Respond ONLY with valid JSON (no markdown fences):
{{"stories": [{{"title": "<title>", "description": "repo: {default_repo}\\n<full spec>", "priority": "high"|"medium"|"low"}}]}}
"""


def normalize_plan(
    plan_text: str,
    *,
    model: str,
    api_key: str = "",
    default_repo: str = "devadmin/sandbox",
    stack: str = "",
    redis_conn=None,
    skip_epics: set[str] | None = None,
    epics: list[dict] | None = None,
    project: str = "",
) -> dict:
    """Map an externally-authored plan (pasted PRD/markdown) onto our epic/story model.

    Chunked in two passes so a large (~100-story) plan can't overflow one response:
    pass 1 extracts the epic structure (terse), pass 2 expands each epic's stories in
    its own bounded call. Reconciled with the execution rules (scaffolded repo,
    shippable+testable slices). Returns the same shape as decompose_idea.

    ``skip_epics`` — epic names already normalized in a prior run. Their pass-2 call is
    skipped (no story produced, not counted as a failure), so a large import interrupted
    by a rate limit can be RESUMED to fill only the missing epics without redoing work.
    The returned ``epics`` is always the full list; ``stories`` covers only the epics
    that were actually processed this run.

    ``epics`` — a pre-determined epic list to use instead of re-running pass 1. Passing
    the list captured on the first run keeps epic identity STABLE across a resume (pass 1
    is non-deterministic — Opus renames epics between runs, which would break skip_epics
    matching and duplicate work).
    """
    skip = {e.strip() for e in (skip_epics or set()) if e}
    resuming = bool(skip)
    call = dict(model=model, api_key=api_key, stack=stack, redis_conn=redis_conn, project=project)
    plan = (plan_text or "")[:_MAX_PLAN_CHARS]

    # ── Pass 1: epic list only (names + descriptions) — tiny, truncation-proof ──
    # Reuse the caller-supplied list on resume; otherwise extract it from the plan.
    if epics:
        project_name = "Imported plan"
        top = {"epics": epics}
    else:
        try:
            top = _complete_json(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": _NORMALIZE_EPICS_PROMPT.format(plan=plan)}],
                max_tokens=2000, **call)
        except Exception as exc:
            log.warning("normalize_epics_failed", error=str(exc)[:160])
            return _fallback("Imported plan", default_repo)
        project_name = (top.get("project_name") or "Imported plan")[:80]

    epics = [{"name": e["name"], "description": e.get("description", "")}
             for e in top.get("epics", []) if e.get("name")][:_MAX_IMPORT_EPICS]
    if not epics:
        return _fallback(project_name, default_repo)
    log.info("import_epics_extracted", project=project_name, epics=len(epics))

    # Split the plan locally so each pass-2 call sends only its epic's section —
    # not the whole plan × epics (the cost blowup). Unmatched epics fall back to
    # the full plan for that one call.
    sections = _split_plan_by_epics(plan, [e["name"] for e in epics])
    log.info("import_plan_split", matched=len(sections), epics=len(epics))

    # ── Pass 2: per epic, find + expand its stories (bounded input AND output) ──
    flat = []
    consecutive_failures = 0
    for epic in epics:
        if epic["name"] in skip:            # already done in a prior run (resume)
            continue
        section = sections.get(epic["name"]) or plan
        try:
            data = _complete_json(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": _NORMALIZE_STORIES_PROMPT.format(
                     plan=section, epic_name=epic["name"], epic_desc=epic["description"],
                     no_stub=_NO_STUB, secure_defaults=_SECURE_DEFAULTS,
                     default_repo=default_repo)}],
                max_tokens=_IMPORT_STORY_TOKENS, timeout=_IMPORT_TIMEOUT,
                attempts=_IMPORT_ATTEMPTS, **call)
            stories = data.get("stories", [])
            consecutive_failures = 0
        except Exception as exc:
            log.warning("normalize_epic_stories_failed", epic=epic["name"], error=str(exc)[:120])
            stories = []
            consecutive_failures += 1
            if consecutive_failures >= _IMPORT_MAX_CONSECUTIVE_FAILURES:
                # Spend guard: something systematic is wrong (provider down, plan shape
                # the model can't emit) — stop burning calls on the remaining epics.
                log.error("import_aborted_consecutive_failures",
                          failures=consecutive_failures, done=len(flat))
                break
        for s in stories:
            if s.get("title"):
                flat.append({
                    "title": s["title"][:200],
                    "description": _ensure_repo_prefix(s.get("description", ""), default_repo),
                    "priority": s.get("priority", "medium"),
                    "epic": epic["name"],
                })

    if not flat and not resuming:
        # Fresh import produced nothing → single-story fallback so work still moves.
        # On resume, an empty result just means "no new epics filled this round".
        return _fallback(project_name, default_repo)
    # ── Deployability critic: catch foundational/cross-cutting gaps an external spec
    # silently assumes (undeclared deps, unfinalized metadata, no clean-install check).
    # Skipped on resume (that pass fills known-missing epics, not re-critiques).
    if flat and not resuming:
        try:
            plan_txt = "\n".join(
                f"## {e['name']}\n" + "\n".join(
                    f"- {s['title']}" for s in flat if s["epic"] == e["name"])
                for e in epics)
            crit = _complete_json(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": _IMPORT_CRITIC_PROMPT.format(
                     plan=plan_txt[:_MAX_PLAN_CHARS], default_repo=default_repo)}],
                max_tokens=4000, **call)
            epic_names = {e["name"] for e in epics}
            added = 0
            for m in crit.get("missing", []):
                if not m.get("title"):
                    continue
                ename = m.get("epic") or "Project Finalization"
                if ename not in epic_names:
                    epics.append({"name": ename, "description": ""})
                    epic_names.add(ename)
                flat.append({
                    "title": m["title"][:200],
                    "description": _ensure_repo_prefix(m.get("description", ""), default_repo),
                    "priority": m.get("priority", "high"),
                    "epic": ename,
                })
                added += 1
            log.info("import_critic_done", added=added)
        except Exception as exc:
            log.warning("import_critic_failed", error=str(exc)[:120])

    # HS-3: never dispatch an empty-description story (redraft or drop).
    flat = _finalize_stories(flat, project_title=project_name, default_repo=default_repo, **call)

    log.info("plan_normalized", project=project_name, epics=len(epics),
             stories=len(flat), resumed=resuming)
    return {"project_name": project_name, "module_name": project_name,
            "epics": epics, "stories": flat}
