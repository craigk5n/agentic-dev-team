# PRD: Autonomous Multi-Agent Coding Team

## 1. Summary

Build a self-hosted system in which a team of autonomous coding agents plans, writes, reviews, tests, and security-checks software with minimal human intervention. The human operator seeds the system with a description of what to build and approves ideas; everything downstream runs automatically. Approval gates beyond idea-approval (e.g. PR merge) must be individually toggleable via configuration.

The design principle is **the work board is the coordination backbone**: agents do not talk to each other directly. They react to board/git events, claim work items, do their job, and post results back to the board and the pull request. A durable orchestrator wraps each agent run so long, failure-prone work survives crashes, retries safely, and can pause for human approval.

## 2. Goals

- Operator provides a project description and approves ideas; the rest is autonomous.
- All components self-hosted and resource-light (target: runs on a single modest server).
- Every decision is auditable on a work item or pull request.
- Human approval gates are configuration flags, not code changes.
- Cost-controllable: expensive models only where reasoning quality matters.

## 3. Non-Goals

- No public/multi-tenant SaaS. Single-operator, self-hosted.
- No custom Jira-style web app — use an existing self-hostable tool.
- Not targeting fully unattended merge-to-`main` at launch (see §8).

## 4. Tech Stack

| Layer | Tool | Notes |
|---|---|---|
| Work board / coordination state | **Plane** (Community Edition, self-hosted via Docker) | Free, AGPL-3.0. REST API + webhooks + native MCP server on the free edition. "Intake" feature is the idea-approval queue. Emulate epics with Modules/parent issues (epics+approval workflows are paid). |
| Git forge | **Forgejo** (self-hosted, single Go binary) | ~1 GB RAM, runs on a Pi. Pull requests, code review, branch protection, webhooks, REST API. Gitea is an interchangeable fallback. |
| CI (tester role) | **Forgejo Actions** (GitHub-workflow-compatible) or **Woodpecker CI** | Lightweight; avoids the GitLab monolith. |
| High-reasoning agents | **Claude Agent SDK** (headless, Python or TS) | Idea, planning, code review. Authenticate with a **direct Anthropic API key (pay-as-you-go)** — NOT a subscription. Headless subscription usage is metered against a small capped credit as of 2026-06-15 and will hard-stop mid-month. |
| Mechanical / high-volume agents | **opencode** (`-p` non-interactive mode, JSON output) | Model-agnostic (75+ providers incl. local Ollama). Use cheaper or local models for test-running and first-pass triage. |
| Durable orchestration | **Temporal** (self-hosted) | Durable execution, safe retries, and "wait for human approval" signals. May start with a lighter webhook→queue→worker loop and introduce Temporal as reliability needs grow. |
| Static security analysis | **Semgrep** (+ secret scanning) | The security agent runs real SAST tooling and reasons over results — does not rely on the LLM alone. |
| Agent isolation | One container (or microVM) per agent run | Agents execute shell commands; never run loose on the host. |

## 5. Agent Roles

Each agent is a stateless worker triggered by an event, operating on one work item or PR, and posting results back.

1. **Idea Agent** (Claude SDK) — Given the operator's project description and guidance, generates idea proposals and writes them into **Plane Intake** with status `pending-approval`. Does not proceed further.
2. **Planner / PM Agent** (Claude SDK) — Triggered when an Intake item is approved by the operator. Decomposes the idea into a Module ("epic") plus Work Items ("stories") sized to be independently buildable. Assigns stories to coding agents and sets status `ready`.
3. **Coding Agent(s)** (Claude SDK or opencode) — Claim a `ready` story, create a branch, implement, and open a **pull request** in Forgejo. Link the PR back to the Plane item. Set story status `in-review`.
4. **Code Reviewer** (Claude SDK) — Triggered by Forgejo `pull_request` webhook. Reviews the diff, posts review comments, sets a review verdict.
5. **Tester** (opencode + CI) — Triggered by the same PR event. Runs the test suite via CI and reports pass/fail + coverage back to the PR and item.
6. **Security Reviewer** (opencode + Semgrep) — Triggered by the same PR event. Runs SAST + secret scanning, reasons over findings, posts a security verdict.

A PR advances only when reviewer, tester, and security verdicts are all green (plus any enabled human gate).

## 6. Coordination Model

- **Blackboard:** Plane Work Items + statuses are the single source of truth for what needs doing and who owns it. Forgejo PRs are the source of truth for code state. The two are linked by an ID reference stored on the Plane item.
- **Triggers:** Plane webhooks and Forgejo webhooks fire the relevant agent. Mapping:
  - Intake item `approved` → Planner Agent
  - Story `ready` → Coding Agent
  - Forgejo `pull_request opened/updated` → Reviewer + Tester + Security (in parallel)
  - All verdicts green (+ gate) → merge step
- **Claiming:** An agent atomically transitions a work item from `ready` → `in-progress` (with its own ID) before working, to prevent double-pickup.
- **Reporting:** Agents post results as PR review comments and Plane item comments. Status transitions are the machine-readable signal; comments are the human-readable audit trail.

## 7. Work Item Status Model

```
idea:    pending-approval -> approved -> rejected
story:   backlog -> ready -> in-progress -> in-review -> approved -> merged -> done
                                         \-> changes-requested -> ready
```

## 8. Human Approval Gates

Gates are boolean config flags. Implemented as a Temporal "wait for signal" step (or a board status the merge step polls) that is skipped when the flag is off.

| Gate | Default | Notes |
|---|---|---|
| `gate.idea_approval` | **ON** (always) | Operator approves Intake items. Cannot be disabled. |
| `gate.pr_merge_approval` | OFF (toggleable) | Operator approves merge to `main`. |
| `gate.security_signoff` | **ON (recommended)** | Hold merge if security verdict is not green, even when other gates are off. |

**Recommendation:** keep `security_signoff` and merge-to-`main` gated initially even while other steps run unattended. Forgejo branch protection on `main` enforces this at the git layer.

## 9. Configuration / Secrets

Provide via environment variables / secrets manager:

- `ANTHROPIC_API_KEY` — direct API key for Claude Agent SDK agents.
- Model routing config — which model each role uses (e.g. Opus/Sonnet for idea/plan/review; cheaper or local model for tester/security triage).
- `PLANE_API_TOKEN`, `PLANE_WEBHOOK_SECRET`, `PLANE_BASE_URL`, workspace/project IDs.
- `FORGEJO_API_TOKEN`, `FORGEJO_WEBHOOK_SECRET`, `FORGEJO_BASE_URL`, default branch protection rules.
- `TEMPORAL_ADDRESS` (if Temporal enabled).
- Gate flags from §8.
- Per-agent rate limits and concurrency caps (cost control).

## 10. Non-Functional Requirements

- **Cost control:** continuous multi-agent execution at API rates is the dominant cost. Mix models per role, rate-limit polling, and cap concurrent agent runs. Surface a running token/cost estimate.
- **Isolation:** each agent run executes in its own ephemeral container with a scoped token; no shared host shell; least-privilege git credentials (cannot push to `main`).
- **Idempotency / retries:** agent work happens on a branch or scratch copy; irreversible steps (open PR, merge, notify) are gated behind a final explicit action that runs only after validation, so a retry is safe.
- **Observability:** structured logs per run, linked to the work item ID and PR; capture session/run IDs to resume rather than restart.
- **Auditability:** every status transition and verdict is traceable to an agent run and a board/PR comment.

## 11. Suggested Build Phases

1. **Infra:** Stand up Plane (Docker), Forgejo, and a CI runner. Configure branch protection on `main`. Verify REST APIs and webhooks for both.
2. **Event bus:** Build a webhook receiver that validates signatures and dispatches events to a job queue. Define the work-item status model (§7).
3. **First agent loop:** Implement the Coding Agent (claim `ready` story → branch → PR) end to end, manual trigger. Prove the claim/report/transition cycle.
4. **Review fan-out:** Add Reviewer, Tester (CI), and Security (Semgrep) agents triggered by the PR webhook; implement verdict aggregation.
5. **Planning + intake:** Add the Idea Agent (writes to Intake) and Planner Agent (idea-approval → epic/stories). Wire `gate.idea_approval`.
6. **Gates + durability:** Introduce toggleable gates and Temporal-backed durable runs with human-approval waits.
7. **Hardening:** Per-agent sandboxing, rate/concurrency limits, cost telemetry, and observability dashboards.

## 12. Acceptance Criteria

- Operator submits a project description; Idea Agent produces proposals in Intake.
- Approving an Intake item produces an epic + stories without further input.
- A story flows automatically to an open PR with reviewer, tester, and security verdicts attached.
- With `pr_merge_approval` OFF and security green, an approved PR merges autonomously; with it ON, merge waits for operator approval.
- A killed agent mid-run resumes or safely retries without duplicate PRs or corrupted state.
- Whole stack runs within the resource budget of a single modest server.

## 13. Open Decisions

- Confirm Forgejo Actions vs Woodpecker CI for the tester.
- Model routing table per role (cost vs quality).
- Temporal from day one vs deferred to Phase 6.
- Monorepo vs multi-repo, and how stories map to repos.
