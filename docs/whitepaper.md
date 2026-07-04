# Building software with an autonomous agent team: what we set out to do, and what we learned

*A field report from the Agentic Dev Team project and its first full build.*

---

## Abstract

We built an autonomous, multi-agent software-development pipeline — agents that plan,
code, review, test, and security-scan end-to-end, coordinating through a shared work board
rather than talking to each other — and put it to a real test: a **110-story, frontier-authored
plan** to re-implement a Go service ("MCP Dev Hub") in Python. The system delivered all 110
stories to a **CI-green, containerized, test-covered** application for roughly **$50** of model
spend, largely unattended. It also taught us that *CI-green is not the same as working*: the
delivered app was **broken on first human use**, and ~40% of stories required a review round
before merging. This paper records the goals and assumptions we started with, what held up,
where it broke, and the handful of takeaways that generalize beyond this project. Companion
documents: `devhub-build-corrections.md` (the 17 runtime defects), `devhub-story-failure-analysis.md`
(the review/security/cost analysis), and `hardening-stories.md` (the remediation backlog).

---

## 1. Motivation and goals

The premise: a human operator describes what to build and approves the idea; agents do the
rest. The design was explicitly **operator-driven, not enterprise-scale** — one modest
self-hosted server, roughly one story in flight at a time, optimized for being *"rock solid /
never stuck"* rather than for throughput.

Concrete goals:

- **End-to-end autonomy** from idea → plan → code → review/test/security → merge, with human
  approval only at deliberately chosen gates.
- **Cost discipline** through model routing: frontier models for the handful of high-reasoning
  calls (idea, planning, code review), cheap or local models for the high-volume execution
  (coding, test triage, security scanning).
- **Self-healing** so transient failures don't require a human: a stuck-job watchdog, a
  rate-limit circuit breaker, capped automatic recode attempts, and model escalation.
- **Isolation and least privilege**: every agent run in an ephemeral sandbox with scoped
  credentials that cannot push to `main`.

## 2. System design and principles

The load-bearing design decision is the **work board as coordination backbone**. Agents never
call each other. They react to board/git events, atomically claim a work item, do their job,
and post results back. A story advances only when three independent verdicts — reviewer,
tester, security — are green, plus any enabled human gate.

Key principles that shaped everything downstream:

- **Coordination by shared state, not by message-passing.** Claiming is an atomic status
  transition; triggers are board changes and forge webhooks; results are PR comments and status
  transitions. This makes the system restart-safe and auditable.
- **Model heterogeneity by role.** Planning and review are a few expensive calls; coding is
  many cheap ones. The router encodes this. (It also became a source of operational pain — see §6.)
- **Irreversible steps last.** Open-PR / merge / notify happen only after validation, so retries
  are safe.
- **Config-driven stacks and SDLC styles.** A project picks a tech stack and an SDLC style at
  approval; those drive scaffolding, the CI workflow, and — critically — the prompts injected
  into the planner, coder, and reviewer.

## 3. Assumptions we started with

Stated and unstated, these are the bets the design made. We mark each with how it held up.

1. **A frontier-authored plan + cheap execution + automated review gates can autonomously
   produce working software.** — *Partially validated.* It reliably produces **CI-green**
   software; "working" turned out to require verification the pipeline didn't have.
2. **Automated review catches the quality issues that matter.** — *Half true.* The reviewer LLM
   caught real vulnerabilities (SSRF, injection, XSS, auth bypass) well. But the SAST warnings
   were ignored by policy, and *no* automated stage caught rendering/UX correctness.
3. **Cheap models suffice for execution when review is strong.** — *Mostly held, with a caveat:*
   weak models struggled specifically with security-sensitive code (escaping, URL validation),
   which drove most of the rework cost.
4. **"CI green" is a good proxy for "done."** — *False, and this was the biggest lesson.*
5. **The stack/SDLC catalog captures enough context for good decomposition.** — *Under-specified.*
   The catalog carried language idioms but **no security or non-functional guidance**, so the
   plan under-specified security and even prescribed anti-patterns.
6. **The pipeline is self-healing enough to run unattended.** — *Aspirational.* It self-healed
   many transient faults, but a meaningful fraction of cost and wall-clock went to the pipeline
   fighting itself, and several classes still required operator intervention.

## 4. The proving ground: the DEVHUB build

We imported an externally-authored, optimized ~110-story plan (a Python re-implementation of a
Go MCP dev-hub: a service registry, a JSON-RPC reverse proxy, a server-rendered admin UI, tool
invocation with script downloads, tracing, conformance checks, health monitoring, plus a second
"Agent Workbench" slice). Seventeen epics, 110 stories, planned on premium models and executed
mostly on a cheap model, reviewed on a mix that eventually included the operator's Claude
subscription.

Outcome: **110/110 stories done**, `main` CI green, ~825 KB of source across 127 Python modules
with **63 test files** (tests > 2× source by size), a runnable two-stage Docker image, for about
**$50** of OpenRouter spend plus a little subscription usage.

## 5. What worked

- **The plan itself was coherent.** A frontier model decomposed a real product into a sensible
  epic/story tree that built up a working architecture in dependency order.
- **The coordination model was sound.** Claim → work → verdict-aggregate → merge functioned; the
  board stayed the single source of truth; the system survived many restarts.
- **Self-healing kept it moving.** The watchdog, circuit breaker, capped recodes, and escalation
  cleared the majority of transient failures without a human.
- **The reviewer LLM earned its keep.** It caught genuine, serious vulnerabilities across ~40% of
  stories — the kind a hurried human reviewer would miss.
- **Cost stayed low for the volume.** ~$50 to autonomously produce a tested, containerized,
  110-story application is a strong result on raw economics.

## 6. Where it broke

Two blind spots dominate, plus a cluster of operational realities.

### Blind spot 1 — CI-green ≠ works
The delivered app passed 63 test files and CI but was **broken the first time a human opened
it**. We logged **17 distinct runtime defects** while getting one MCP server registered and
usable, e.g.: fabricated Subresource-Integrity hashes that made the browser **block every
script** (dead UI); result panels shipped permanently `hidden` so buttons loaded content into
invisible divs; a middleware that 500'd on any request-body read; download buttons pointed at a
nonexistent route; a tool form that replaced itself after one use. **Every one was invisible to
unit tests and diff review** because nothing in the pipeline ever *ran the app in a browser and
clicked*. This is the central finding: **the verification surface didn't match how the software
is used.**

### Blind spot 2 — quality is won or lost at planning
Across 110 stories, **43 (39%) drew a code-review round**, and cost concentrated almost exactly
on the stories that touched an **untrusted boundary** — code that generates code (shell scripts →
command injection), code that renders untrusted data into a sink (HTML templates → XSS), and code
that makes outbound calls (→ SSRF). Reading the story text explains why:

- **The plan was silent on security** — a story that generates a shell script never said "escape
  the arguments," so the coder didn't, and the reviewer had to push back.
- **The plan prescribed the anti-pattern** — every UI story said *"via CDN, no build step,"* which
  produced the exact CDN/SRI dependency that later broke the UI. The security scanner *warned about
  it on every UI story* and the pipeline shipped it anyway, because those findings were rated
  `warn` and the gate only blocks on `fail`. **The system detected the defect that cost us a day
  and ignored it by policy.**
- **The plan sometimes prescribed the vulnerability** — one auth story specified "empty credentials
  = allow" (porting the original's behavior); the reviewer then flagged that exact line as a
  critical bypass. Plan and reviewer fought each other.
- **Empty stories became destructive changes** — a story shipped with an *empty description* and the
  coder responded by deleting the entire Dockerfile; another mass-deleted config, templates, and
  tests. Under-specification is not neutral; it invites damage.

### Operational realities
- **Free/local models are not viable for real coding** — they time out and throttle; every attempt
  to lean on them stalled.
- **Model economics are a live operational risk.** OpenRouter credit was exhausted mid-project;
  the coder blocked on 402s; we moved the reviewer onto the operator's Claude subscription to
  finish. Subscription usage has its own weekly caps. There is no free lunch on model spend, and
  it must be watched.
- **You can't manage what you can't measure.** Coder telemetry is per-role, not per-story, and
  **undercounts** because the coding agent (opencode) is a subprocess black box — reported spend
  was ~$15 against an actual ~$50. "Which stories cost the most" had to be answered by proxy.
- **No lockfile = works-in-CI ≠ works-on-a-fresh-install.** Three separate runtime breakages were
  pure dependency drift: a fresh install pulled newer `starlette`/`anyio`/`mcp` than CI used, and
  code that "passed" broke.

## 7. LLM-specific failure modes worth naming

- **Confident fabrication of verifiable constants.** The coder invented plausible SRI hashes it
  could not have computed. Any pipeline that lets models emit checksums/hashes/pins needs a
  verification step or a "don't hand-author these" rule.
- **Spec-versus-reviewer conflict.** When the plan encodes a questionable behavior, a good reviewer
  will fight it — wasting rounds. The two prompts must be reconciled at the source.
- **Destructive edits under ambiguity.** Given a thin story, models will confidently rewrite or
  delete. Guardrails ("smallest change; don't delete unless asked") matter more than they seem.
- **Happy-path myopia.** Everything was built and tested against the spec-perfect case; a real,
  slightly-non-conformant MCP server (auth via a non-standard header, results without the standard
  wrapper) exposed several gaps at once.

## 8. Takeaways

1. **Verify the way the software is used, not just the way it's written.** Unit tests and diff
   review are necessary and insufficient. For anything with a UI, a **real-browser E2E stage** —
   load the page, click every control, assert *visible* output, exercise a flow twice — would have
   caught the large majority of our runtime defects. This is the single highest-leverage change.
2. **Move quality left, into planning.** Security requirements, non-functional constraints, and
   "don't do this" guardrails belong in the coder's prompt *before* it writes code, not in a
   review round after. We implemented this (a per-stack security checklist injected into coder and
   reviewer); it directly targets the 39% pushback rate.
3. **"CI green" must be redefined to include "runs."** Green currently means "imports resolve and
   assertions pass in a fresh venv." Done-ness should include an acceptance check that the product
   actually functions.
4. **Don't let policy silence your own detectors.** The security scanner found the SRI problem;
   the gate ignored it because it was `warn`. Certain categories (SRI, XSS sinks, hardcoded
   secrets) should *block* for web apps regardless of the tool's own severity.
5. **Pin your world.** Commit a lockfile and install from it; otherwise "works in CI" is a
   time-bomb against every fresh environment.
6. **Instrument honestly.** If a step is a black box (a subprocess agent), capture its real usage
   or you will mis-report cost by 3× and mis-prioritize.
7. **Autonomy is real but bounded; the operator is the quality bar.** Even a "fully autonomous"
   run needed human judgment to unstick wedges, choose models under budget pressure, approve an
   override-merge, and — decisively — to *find the defects the pipeline shipped*. The system is a
   powerful force multiplier, not a replacement for a responsible engineer.
8. **Under-specification is a defect.** An empty or vague story is not a small story; it's an
   invitation for the coder to do something surprising and occasionally destructive.

## 9. What we changed, and what's next

Implemented during and after the run: graceful handling of provider credit exhaustion across
reviewer/coder/planner; an idempotent-branch + PR-reuse fix for a retry wedge; a subscription
reviewer path (with the sandbox plumbing it needed); a capped `max_tokens` to stop a
runaway-cost review; an armed daily cost cap; and **HS-2** — a per-stack security checklist
(escape shell/template/URL sinks, auth on mutations, no secrets, **vendor assets locally, never
CDN**, no destructive deletions) now injected into both the coder and reviewer prompts.

Queued (`hardening-stories.md`), in leverage order: **HS-1** a browser/E2E verification stage (the
big one); HS-3/HS-4 planner + coder guardrails against empty stories and file deletions; HS-5
blocking security categories; HS-6 a committed lockfile; HS-7 enforced local-first NFRs; and
per-story cost telemetry.

## 10. Conclusion

The experiment validated the core thesis in a narrow, important sense: an agent team, coordinated
through a work board and routed across cheap and premium models, **can take a 110-story plan from
idea to a CI-green, tested, containerized application, largely unattended, for tens of dollars.**
It also drew a bright line under the thesis's limit: **passing tests is not the same as working,
and the pipeline optimized for the former.** The gap wasn't the coder's raw ability — it was the
*shape of the verification and the poverty of the plan's non-functional guidance.* The most
valuable output of this project isn't the MCP Dev Hub; it's the precise, evidence-backed map of
where an autonomous pipeline's confidence outruns its correctness — and a concrete, prioritized
plan to close that gap. Build the browser stage, move security into planning, pin the
dependencies, and keep a human on the quality bar; then the economics that already work start to
mean something you'd actually ship.
