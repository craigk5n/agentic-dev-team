# Agent-system hardening stories

Derived from the DEVHUB build post-mortems: `docs/devhub-story-failure-analysis.md`
(planning/security) and `docs/devhub-build-corrections.md` (runtime defects). These target
the **agentic-dev-team** system itself, ordered by leverage. Each is written so it could be
fed to the pipeline (or done by hand) as a self-contained change.

Priorities: **P0** would have prevented the most/costliest defects; **P1** high value; **P2**
worthwhile.

---

## HS-1 (P0) — Browser/E2E verification stage for web-UI stories
**Problem.** The single biggest gap: ~15 of the 17 runtime defects (dead buttons, hidden
result panels, blocked scripts, broken downloads, form-only-once) were invisible to unit tests
and diff review — nobody ever loaded the page and clicked. CI was green while the product was
unusable.

**Acceptance criteria.**
- Add an E2E step to the **tester** role that triggers when a story touches
  `*.html`, `templates/`, `static/`, or front-end routes (detect via changed paths).
- Launch the built app in the sandbox, drive it with a headless browser (Playwright/Chromium),
  and assert: (a) **zero console errors** on load, (b) every button/`hx-*` control fires its
  request, (c) the target renders **visible** output (`display != none`, `offsetParent != null`
  — not just present in the DOM), (d) at least one primary flow round-trips (e.g. submit form →
  see result), and repeat it twice where applicable.
- Emit a `browser` sub-verdict; a hard console error or an invisible-result assertion fails it.
- Where a real backend/browser can't run, `log()` that E2E was skipped (no silent pass).

**Files.** `agents/reviewer/src/reviewer/` (new `browser_check.py`), tester dispatch in
`event-bus/src/event_bus/jobs/pr_jobs.py`, sandbox image (bundle a headless browser).

**Would have caught:** corrections #1, #4, #13, #14, #15, #16, #17 (dead buttons, hidden panels,
invoke-once, downloads, trace panel).

---

## HS-2 (P0) — Security-aware planner directive + coder checklist (pre-review)
**Problem.** 39% of stories drew review pushback; the plan was silent on security and even
**mandated the CDN/SRI anti-pattern** ("via CDN, no build step") on every UI story. Security
requirements were discovered *after* the coder wrote insecure code, costing recode rounds.

**Acceptance criteria.**
- Add a per-stack **`security_checklist`** to the catalog stack config, injected into the coder
  prompt (`best_practices_prompt`) so it applies *before* review. For the python/web stack:
  escape shell metacharacters in any generated script; autoescape/`|e` all template output;
  validate outbound URLs (SSRF) and reconcile with local-first; require auth on state-changing
  endpoints; never hardcode credentials; **vendor JS/CSS locally, never CDN**.
- Change the web-stack `best_practices_prompt` / SDLC `planner_directive` to say **"vendor
  front-end assets under `static/vendor/`"** — remove any "via CDN" language.
- The reviewer prompt already asks for suggestions; align its checklist with the coder's so they
  don't fight (see HS-8 for the auth-bypass class).

**Files.** `event-bus/src/event_bus/catalog/defaults/stacks/*.yaml`,
`event-bus/src/event_bus/catalog/defaults/sdlc/*.yaml`, `event-bus/src/event_bus/prompt_store.py`.

**Would have prevented:** the SRI class (corrections #1), most SSRF/injection/XSS pushback
(analysis §1), and reduced recode cost on the priciest stories (#66, #92, #38, #100).

---

## HS-3 (P0) — Reject empty / under-specified stories at planning time
**Problem.** #117 shipped with an **empty description** and the coder responded by *deleting the
Dockerfile*; #111 (thin) mass-deleted files. Under-specified stories give the coder room to do
damage.

**Acceptance criteria.**
- Planner post-check: every emitted story must have a non-empty description with at least N
  acceptance-criteria bullets and named target file(s); otherwise the planner re-drafts it (or
  the story is held for operator review, not dispatched).
- Add a validation in `normalize_plan` / `decompose_idea` that fails a plan containing an
  empty-description story.

**Files.** `agents/planner/src/planner_agent/decomposer.py`, planner tests.

---

## HS-4 (P0) — "No unexpected deletions" coder guardrail
**Problem.** The costliest, most dangerous class was the coder **deleting existing files** it
shouldn't (#117 Dockerfile, #111 config/templates/tests, #81 health logic, #116 data-loss).

**Acceptance criteria.**
- In the coder/CI gate, compute deleted/removed paths in the PR diff. If the story isn't tagged
  as a refactor/cleanup and the PR deletes tracked files (or drops >X% of a file), **block** with
  a clear message and require explicit justification.
- Surface deletions prominently to the reviewer prompt ("this PR deletes: …").

**Files.** `event-bus/src/event_bus/jobs/*` (post-CI/gate), `agents/coding/src/coding_agent/`,
reviewer prompt.

---

## HS-5 (P1) — Make selected Semgrep categories block for web apps
**Problem.** Semgrep **flagged the SRI issue on every UI story** but it was `warn`, and
`security_signoff` only blocks on `fail`, so the pipeline shipped the exact defect that broke the
UI. Same for XSS sinks / hardcoded creds appearing only as warnings.

**Acceptance criteria.**
- Introduce a policy mapping certain Semgrep rule categories (SRI/integrity, template XSS sinks,
  hardcoded secrets) to **blocking** for web-facing stacks, independent of Semgrep's own severity.
- Config flag `gate.block_security_categories` (default on for web stacks); reflected in
  `apply_gate`.

**Files.** `agents/reviewer/src/reviewer/security_scan.py`, `agents/reviewer/src/reviewer/gate.py`,
`event-bus/src/event_bus/config_store.py`.

---

## HS-6 (P1) — Commit a dependency lockfile; fresh-venv installs from lock
**Problem.** Three distinct runtime breakages were pure **version drift** (corrections #3, #9,
#12): the fresh install pulled newer `starlette`/`anyio`/`mcp` than CI, and code that "passed"
broke. The scaffolds pin only lower bounds and ship no lockfile.

**Acceptance criteria.**
- Per-stack scaffold generates and commits a lockfile (`uv.lock` / pinned `requirements.txt`).
- The fresh-venv CI gate installs **from the lock**, and a nightly/opt-in job bumps + re-tests.

**Files.** `infra/coder-images/` scaffolds, `event-bus/src/event_bus/ci_workflow.py`.

---

## HS-7 (P1) — Enforce stated NFRs (local-first) as checks, reconciled once
**Problem.** "Local-first" was in the PRD but the build reached for public CDNs and added SSRF
guards that then **blocked localhost/LAN** — the live bug. NFRs were neither enforced nor
reconciled across stories.

**Acceptance criteria.**
- Capture key NFRs (local-first, offline-capable) at idea approval as machine-checkable
  assertions the tester/security roles verify (e.g. "no external network needed to render the
  UI"; "private/LAN targets reachable when configured").
- A single global reconciliation note injected into every relevant story's coder prompt, rather
  than each story re-deciding.

**Files.** planning inputs / `set_planning_inputs`, catalog, coder prompt.

---

## HS-8 (P2) — Don't let the plan prescribe a vulnerability
**Problem.** #15 told the coder to make **empty credentials = auth open** ("matches the Go
implementation"); the reviewer then flagged it as a critical bypass. Plan and reviewer fought,
and the pattern shipped (and bit the live instance).

**Acceptance criteria.**
- Planner directive: when porting behavior, flag security-relevant behaviors ("auth disabled when
  unset", "binds 0.0.0.0") as **decisions requiring an explicit secure default**, not silent
  ports. Prefer secure-by-default (deny) with an opt-in flag.

**Files.** `event-bus/src/event_bus/catalog/defaults/sdlc/*.yaml`, planner prompt.

---

## HS-9 (P2) — Per-story cost telemetry
**Problem.** We couldn't answer "which stories cost the most" directly — coder telemetry is
per-role, not per-story, and undercounts opencode (see pending task: wire opencode real usage).
We had to proxy cost from recode/escalation counts.

**Acceptance criteria.**
- Record real per-story cost keyed by item id (`telemetry:story:{date}` or on the work item),
  summing coder + verdict spend for that story's PR(s).
- Depends on capturing opencode's actual token/cost output.

**Files.** `event-bus/src/event_bus/main.py` (`_record_coder_usage`),
`agents/coding/src/coding_agent/opencode_agent.py`, telemetry.

---

### Suggested sequencing
1. **HS-1** (browser E2E) and **HS-2** (security-in-planning) — the two that would have prevented
   the most pain, and are independent.
2. **HS-3 / HS-4** — cheap guardrails against the worst outliers (empty stories, deletions).
3. **HS-5 / HS-6 / HS-7** — policy + supply-chain + NFR enforcement.
4. **HS-8 / HS-9** — polish + observability.
