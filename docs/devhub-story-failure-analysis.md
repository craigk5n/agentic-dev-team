# DEVHUB build — story/epic failure analysis

Analysis of the 110-story / 17-epic MCP Dev Hub build: where the agent pipeline pushed
back, why, and what in *planning* caused it. Data pulled from the work store + every PR's
comment history (review verdicts, Semgrep findings, escalations, recode rounds).

> **Two review roles, don't conflate them:** the **code reviewer is an LLM** (it caught the
> real vulnerabilities and logic bugs); the **"security" role is Semgrep** (SAST) + secret
> scanning — pattern findings, mostly non-blocking `warn`.

---

## 1. Code-review pushback — 43 / 110 stories (39%)

Concentrated by epic:

| Pushback / total | Epic |
|---|---|
| 8 / 14 | Templates & Static Assets |
| 6 / 13 | MCP Protocol Module |
| 4 / 6 | Admin UI: Conformance |
| 4 / 7 | Admin UI: Playground/Faults/Trace |
| 3 / 6 | Registry REST API |
| 3 / 4 | Packaging/Dockerfile/README |
| 3 / 4 | Admin UI: Tool Invocation & Downloads |

Five recurring failure classes:
- **SSRF** — missing outbound-URL validation (#38, #100, #65, #23).
- **Command injection** — unescaped vars in generated shell scripts / `sh -c` (#66, #77, #79, #91).
- **XSS** — unescaped vars in Jinja2 templates (#84, #85, #74, #87).
- **Auth bypass / missing auth** (#15, #13, #70, #73).
- **Destructive regressions** — the coder *deleting* things: #117 deleted the whole Dockerfile,
  #111 mass-deleted config/templates/tests, #116 caused data loss (unregister without
  re-register), #81 removed health-state logic.
- Tail: runtime `NameError`s and JSON-RPC/SSE spec mistakes (#86, #97, #19, #21, #24).

## 2. Semgrep ("security") findings

Backend stories: **median 1 finding**. UI/template stories: **20–26 each**, dominated by:
- **"missing 'integrity' subresource integrity attribute"** on the CDN `<script>` tags — the
  **SRI bug that later broke the entire UI**, flagged on every template as `🟡` medium.
- direct `jinja2` use, missing CSRF tokens, SHA1, over-permissive `0o700`.

**Key finding:** the pipeline's own scanner warned about the SRI problem on every UI story and
shipped anyway, because `security_signoff` only blocks on `fail` and Semgrep rated these
`warn`. **The system detected the defect that later cost a day of debugging and ignored it by
policy.**

## 3. Did story-writing contribute? Yes, decisively

- **Rich on functional behavior, near-silent on security.** #66 (generates a shell script) never
  says "escape shell metacharacters" → command injection. SSRF endpoints specify the fetch but
  never "reject internal/loopback targets."
- **Planning mandated the anti-pattern.** Every UI story says *"HTMX + hyperscript + Tailwind via
  CDN, no build step."* That produced the CDN+SRI dependency Semgrep flagged and that broke the
  UI. The plan prescribed the bug.
- **A story specified the vulnerability as a requirement.** #15 (BasicAuth): *"If user=='' or
  password=='', return True (auth disabled — matches the Go implementation)."* The reviewer then
  flagged that exact line as a **critical auth-bypass**. Plan vs reviewer fought each other — and
  this "empty creds = open" behavior also bit the live instance.
- **Empty/thin descriptions → destructive changes.** #117 had a **completely empty description**
  and the coder deleted the Dockerfile. #111 (thin) mass-deleted files.

## 4. Could the planning prompt have prevented these? Yes

1. **Fix the stack directive:** *"vendor JS/CSS locally under `static/vendor/`"* instead of *"via
   CDN, no build step"* — kills the SRI class and the Semgrep noise.
2. **Inject a per-stack security checklist into the coder prompt** (`best_practices_prompt`),
   applied *before* review: escape shell vars; autoescape template output; validate outbound
   URLs; auth on state-changing endpoints; no hardcoded creds.
3. **Forbid empty/near-empty story descriptions** (planner post-check).
4. **"Don't delete existing files unless the story is a refactor"** coder guardrail.
5. **Reconcile cross-cutting NFRs once, globally** (local-first ⇄ SSRF guard) instead of ad-hoc
   per-story guards that then conflict with the product goal.

## 5. Common themes — stuck stories (~20: escalated / ≥2 recodes / multi-PR)

- **Generated-artifact stories** (shell/python script templates: #66, #91, #67, #77) — injection.
- **Server-rendered template stories** (#92, #85, #81, #84, #87) — XSS + SRI churn.
- **External-call endpoints** (#38, #100, #65, #23) — SSRF.
- **Slice A / Agent Workbench** (#100, #106, #97) — a second subsystem bolted on late; #100 took
  15 recode rounds.
- Cross-cutting: the more a story touched an **untrusted boundary** (a shell, an HTML sink, an
  outbound URL), the more likely it stalled.

## 6. Themes — most expensive stories

Exact per-story dollars aren't tracked (coder telemetry isn't per-story and undercounts
opencode). Proxy = review rounds + 2×recodes + 3×escalations + re-dispatches:

| # | proxy | recodes | esc | theme |
|---|---|---|---|---|
| 92 | 54 | 15 | 2 | tool_script.py.j2 (template + injection) |
| 100 | 52 | 15 | 1 | POST /v1/agents/register (SSRF, Slice A) |
| 66 | 41 | 12 | 0 | bash download endpoint (command injection) |
| 38 | 36 | 12 | 0 | POST /v1/register (SSRF) |
| 60 | 23 | 6 | 0 | capabilities route |

**Cost tracked pushback almost perfectly.** Expense concentrated in two soil types: **code that
generates code** (shell scripts) and **code that renders untrusted data into a sink** (HTML
templates, outbound HTTP).

---

## Bottom line

~40% of stories needed rework; cost was dominated by injection/SSRF/XSS-prone stories plus a
few destructive-deletion incidents — nearly all **foreseeable at planning time**. The
highest-leverage fix is not a better coder; it's a **security-aware planner directive + coder
checklist + "don't delete / no empty stories" guardrails**, making certain Semgrep `warn`
categories block for web apps, and (from the corrections log) a **browser/E2E test stage** +
**dependency lockfile**. See `docs/devhub-build-corrections.md` for the runtime defects and
`docs/hardening-stories.md` for the proposed work.
