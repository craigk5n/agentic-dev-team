# PRD: Agentic Dev Team

## 1. Summary

Build a self-hosted system in which a team of autonomous coding agents plans, writes, reviews, tests, and security-checks software with minimal human intervention. The human operator seeds the system with a description of what to build and approves ideas; everything downstream runs automatically. Approval gates beyond idea-approval (e.g. PR merge) must be individually toggleable via configuration.

The design principle is **the event-bus is the coordination backbone**: agents do not talk to each other directly. They react to board/git events, claim work items, do their job, and post results back. Work items are stored in a SQLite database embedded in the event-bus and surfaced through a built-in kanban board UI.

## 2. Goals

- Operator provides a project description and approves ideas; the rest is autonomous.
- All components self-hosted and resource-light (target: runs on a single modest server).
- Every decision is auditable on a work item or pull request.
- Human approval gates are configuration flags, not code changes.
- Cost-controllable: expensive models only where reasoning quality matters.

## 3. Non-Goals

- No public/multi-tenant SaaS. Single-operator, self-hosted.
- No external work board dependency — coordination state is self-contained in the event-bus.
- Not targeting fully unattended merge-to-`main` at launch (see §8).

## 4. Tech Stack

| Layer | Tool | Notes |
|---|---|---|
| Work board / coordination state | **SQLite** embedded in the event-bus | Work items (ideas + stories) stored locally. Built-in kanban board UI served by the event-bus at `/`. No external service required. |
| Git forge | **Forgejo** (self-hosted, single Go binary) | ~1 GB RAM, runs on a Pi. Pull requests, code review, branch protection, webhooks, REST API. Gitea is an interchangeable fallback. |
| CI (tester role) | **Forgejo Actions** (GitHub-workflow-compatible) or **Woodpecker CI** | Lightweight; avoids the GitLab monolith. |
| High-reasoning agents | **litellm** (Python) | Idea, planning, code review. Routes to any provider via a single API — OpenRouter, Anthropic direct, or local Ollama. Authenticate with `OPENROUTER_API_KEY` (recommended) or `ANTHROPIC_API_KEY`. |
| Mechanical / high-volume agents | **opencode** (`-p` non-interactive mode, JSON output) | Model-agnostic (75+ providers incl. local Ollama). Use cheaper or local models for test-running and first-pass triage. |
| Model routing | **OpenRouter** | Single API key routes to any provider/model. Free-tier models used by default for idea, planner, tester, and security agents; paid models for reviewer. |
| Job queue | **Redis + RQ** | Webhook events enqueued in Redis, processed by RQ workers. Replaces Temporal for most use cases. |
| Durable orchestration (optional) | **Temporal** (self-hosted) | Optional upgrade path for durable execution and human-approval wait signals. Activated by setting `TEMPORAL_ADDRESS`; RQ is the default. |
| Static security analysis | **Semgrep** (+ secret scanning) | The security agent runs real SAST tooling and reasons over results — does not rely on the LLM alone. |
| Agent isolation | One container (or microVM) per agent run | Agents execute shell commands; never run loose on the host. Controlled by `SANDBOX_MODE=docker` (default: `process`). |

## 5. Agent Roles

Each agent is a stateless worker triggered by an event, operating on one work item or PR, and posting results back.

1. **Idea Agent** (Claude SDK / OpenRouter) — Given the operator's project description and guidance, generates idea proposals and writes them into the work store with status `pending-approval`. Does not proceed further.
2. **Planner / PM Agent** (Claude SDK / OpenRouter) — Triggered when an idea is approved by the operator. Decomposes the idea into stories sized to be independently buildable. Sets each story's status to `ready` (first) or `backlog` (subsequent, unlocked sequentially).
3. **Coding Agent(s)** (opencode + OpenRouter) — Claim a `ready` story, create a branch, implement, and open a **pull request** in Forgejo. Link the PR back to the work item. Set story status `in-review`.
4. **Code Reviewer** (Claude SDK / OpenRouter) — Triggered by Forgejo `pull_request` webhook. Reviews the diff, posts review comments, sets a review verdict.
5. **Tester** (opencode + CI) — Triggered by the same PR event. Runs the test suite via CI and reports pass/fail + coverage back to the PR and item.
6. **Security Reviewer** (opencode + Semgrep) — Triggered by the same PR event. Runs SAST + secret scanning, reasons over findings, posts a security verdict.

A PR advances only when reviewer, tester, and security verdicts are all green (plus any enabled human gate).

## 6. Coordination Model

- **Blackboard:** SQLite work items + statuses are the single source of truth for what needs doing and who owns it. Forgejo PRs are the source of truth for code state. The two are linked by a `pr_url` field stored on the work item.
- **Triggers:** Forgejo webhooks fire the relevant agent. Mapping:
  - Idea `approved` (via board UI or API) → Planner Agent
  - Story `ready` → Coding Agent (claimed atomically)
  - Forgejo `pull_request opened/updated` → Reviewer + Tester + Security (in parallel)
  - All verdicts green (+ gate) → merge step
- **Claiming:** An agent atomically transitions a work item from `ready` → `in-progress` before working, to prevent double-pickup.
- **Reporting:** Agents post results as PR review comments and work item comments. Status transitions are the machine-readable signal; comments are the human-readable audit trail.
- **Sequential story unlock:** After a story merges, the next sequenced story in the same idea automatically transitions from `backlog` → `ready`.

## 7. Work Item Status Model

```
idea:    pending-approval -> approved -> rejected
story:   backlog -> ready -> in-progress -> in-review -> changes-requested -> merged -> done
```

## 8. Human Approval Gates

Gates are boolean config flags. Implemented as a board status the merge step polls (or a Temporal "wait for signal" when Temporal is enabled), skipped when the flag is off.

| Gate | Default | Notes |
|---|---|---|
| `gate.idea_approval` | **ON** (always) | Operator approves ideas via board UI or `POST /api/items/{id}/approve`. Cannot be disabled. |
| `gate.pr_merge_approval` | OFF (toggleable) | Operator approves merge to `main`. |
| `gate.security_signoff` | **ON (recommended)** | Hold merge if security verdict is not green, even when other gates are off. |

**Recommendation:** keep `security_signoff` and merge-to-`main` gated initially even while other steps run unattended. Forgejo branch protection on `main` enforces this at the git layer.

## 9. Configuration / Secrets

Provide via environment variables (`.env` file in `infra/`):

- `ANTHROPIC_API_KEY` — direct API key for Claude Agent SDK agents.
- `OPENROUTER_API_KEY` — routes idea, planner, reviewer, tester, and security agents through OpenRouter.
- Model routing config per role (all overridable via env var):
  - `MODEL_IDEA`, `MODEL_PLANNER` — default to `openrouter/nvidia/nemotron-3-super-120b-a12b:free`
  - `MODEL_CODER` — opencode with `openrouter/nvidia/nemotron-3-super-120b-a12b:free`
  - `MODEL_REVIEWER` — defaults to `openrouter/nvidia/nemotron-3-super-120b-a12b:free`
  - `MODEL_TESTER`, `MODEL_SECURITY` — default to `openrouter/meta-llama/llama-3.3-70b-instruct:free`
- `FORGEJO_API_TOKEN`, `FORGEJO_WEBHOOK_SECRET`, `FORGEJO_BASE_URL`, default branch protection rules.
- `DEFAULT_REPO` — default Forgejo repo for coding agent.
- `TEMPORAL_ADDRESS` (optional) — leave blank to use RQ fallback.
- `SANDBOX_MODE` — `process` (default) or `docker` (ephemeral container per agent run).
- Gate flags from §8.
- Per-agent rate limits and concurrency caps (cost control).

## 10. Testing Standards

- **Target coverage: 90%.** Failing threshold: **80%** — CI fails if any package drops below 80%.
- **Test-driven development (TDD):** write tests first (RED), implement to pass (GREEN), refactor (IMPROVE). No feature code ships without a corresponding test.
- **Local CI script:** `./ci.sh` at the repo root runs all packages' test suites with coverage in one command. Must pass before pushing.
- **Omit runtime-only entry points** (e.g. worker scripts, sandbox runners) from coverage measurement — these are integration-tested via the running system, not unit tests.

## 11. Non-Functional Requirements

- **Cost control:** continuous multi-agent execution at API rates is the dominant cost. Mix models per role via OpenRouter, rate-limit polling, and cap concurrent agent runs. Surface a running token/cost estimate via `/telemetry`.
- **Isolation:** each agent run executes in its own ephemeral container (when `SANDBOX_MODE=docker`) with a scoped token; no shared host shell; least-privilege git credentials (cannot push to `main`).
- **Idempotency / retries:** agent work happens on a branch or scratch copy; irreversible steps (open PR, merge, notify) are gated behind a final explicit action that runs only after validation, so a retry is safe.
- **Observability:** structured logs per run, linked to the work item ID and PR; `/telemetry` endpoint exposes cost and token counts per agent role.
- **Auditability:** every status transition and verdict is traceable to an agent run and a board/PR comment.

## 12. Suggested Build Phases

1. **Infra:** Stand up Forgejo and a CI runner. Configure branch protection on `main`. Verify REST APIs and webhooks.
2. **Event bus:** Build a webhook receiver that validates signatures and dispatches events to an RQ job queue. Implement the SQLite work item store and kanban board UI. Define the work-item status model (§7).
3. **First agent loop:** Implement the Coding Agent (claim `ready` story → branch → PR) end to end, manual trigger. Prove the claim/report/transition cycle.
4. **Review fan-out:** Add Reviewer, Tester (CI), and Security (Semgrep) agents triggered by the PR webhook; implement verdict aggregation.
5. **Planning + intake:** Add the Idea Agent (writes to work store) and Planner Agent (idea-approval → stories). Wire `gate.idea_approval`.
6. **Gates + durability:** Introduce toggleable gates; optionally upgrade from RQ to Temporal-backed durable runs with human-approval waits.
7. **Hardening:** Per-agent sandboxing, rate/concurrency limits, cost telemetry, and observability dashboards.

## 13. Acceptance Criteria

- Operator submits a project description; Idea Agent produces proposals in the board.
- Approving an idea produces stories without further input.
- A story flows automatically to an open PR with reviewer, tester, and security verdicts attached.
- With `pr_merge_approval` OFF and security green, an approved PR merges autonomously; with it ON, merge waits for operator approval.
- A killed agent mid-run resumes or safely retries without duplicate PRs or corrupted state.
- Whole stack runs within the resource budget of a single modest server.

## 14. Open Decisions

- Confirm Forgejo Actions vs Woodpecker CI for the tester.
- Whether to introduce Temporal (Phase 6) or stay on RQ long-term.
- Monorepo vs multi-repo, and how stories map to repos.
