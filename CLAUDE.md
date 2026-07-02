# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

An autonomous multi-agent coding team system. A human operator describes what to build and approves ideas; agents handle planning, coding, review, testing, and security checking end-to-end.

**Core design principle:** The work board is the coordination backbone. Agents never talk to each other directly — they react to board/git events, claim work items, do their job, and post results back.

## Tech Stack

| Component | Tool |
|---|---|
| Work board | Embedded SQLite work store inside the event bus — REST API + internal events |
| Git forge | Forgejo (self-hosted Go binary) — PRs, branch protection, webhooks |
| CI (tester) | Forgejo Actions or Woodpecker CI |
| High-reasoning agents | Claude Agent SDK (Python or TS) — requires direct Anthropic API key, NOT subscription |
| Mechanical/high-volume agents | opencode (`-p` non-interactive, JSON output) — model-agnostic, supports local Ollama |
| Durable orchestration | Temporal (self-hosted) — durable execution, retries, human-approval wait signals |
| Static security analysis | Semgrep + secret scanning |
| Isolation | One container or microVM per agent run |

**Important:** high-volume agents (coder/reviewer/tester/security) must use a direct `ANTHROPIC_API_KEY` or OpenRouter (pay-as-you-go) — heavy headless subscription usage hits the weekly caps and hard-stops mid-cycle. **Exception — planning:** the idea and planner roles may optionally run on the operator's Claude Code subscription via the local `claude -p` CLI (`planner_agent/claude_code.py`; model `claude-code/sonnet|opus`, token from `claude setup-token` in `CLAUDE_CODE_OAUTH_TOKEN`). Planning is a handful of calls per project, which fits subscription limits; never point the coder fleet at it — `PATCH /api/config` rejects claude-code models on non-planning roles.

## Agent Roles

1. **Idea Agent** (Claude SDK) — generates proposals, writes them to the event-bus work store with status `pending-approval`
2. **Planner/PM Agent** (Claude SDK) — triggered on idea approval; decomposes into Module (epic) + Work Items (stories), sets status `ready`
3. **Coding Agent(s)** (Claude SDK or opencode) — claims `ready` story, creates branch, implements, opens PR, sets story `in-review`
4. **Code Reviewer** (Claude SDK) — triggered by Forgejo `pull_request` webhook; posts review verdict
5. **Tester** (opencode + CI) — triggered by PR event; runs test suite, reports pass/fail + coverage
6. **Security Reviewer** (opencode + Semgrep) — triggered by PR event; runs SAST + secret scanning, posts security verdict

A PR advances only when all three verdicts (reviewer, tester, security) are green plus any enabled human gate.

## Stack & SDLC catalog (stack-aware planning)

Each project is tailored to a **tech stack** + **SDLC style** chosen at idea approval. The
catalog is config-driven (no core-code change to add one):
`event-bus/src/event_bus/catalog/defaults/{stacks,sdlc}/*.yaml`, overridable via a mounted
`CATALOG_DIR`. The choice drives provisioning (per-stack CI workflow + scaffold + a
`.devagents/stack` marker), planning (SDLC `planner_directive` — TDD = tests-first), the
coder/reviewer prompts (`best_practices_prompt`), the coder sandbox image
(`stack.coder_image`, falls back to the default when unbuilt), and per-stack cost telemetry
(`GET /api/telemetry` → `by_stack`). Full guide: **docs/STACKS.md**. Per-stack coder image
Dockerfiles (build-on-demand): `infra/coder-images/`.

## Work Item Status Flow

```
idea:    pending-approval -> approved -> rejected
story:   backlog -> ready -> in-progress -> in-review -> merged -> done
                                         \-> changes-requested -> ready
```

`merged` is **transient**: when a PR merges, CI runs on `main`. On success the story
becomes `done` and the next sequenced story unlocks; on failure it returns to a
developer (`changes-requested`, with a capped automatic fix attempt). A story the
coder finds nothing to implement (`no_changes`) goes straight to `done` (no PR/CI).

## Human Approval Gates (config flags)

| Gate | Default |
|---|---|
| `gate.idea_approval` | ON — always required, operator approves intake items |
| `gate.pr_merge_approval` | OFF (toggleable) |
| `gate.security_signoff` | ON (recommended) — hold merge if security verdict is not green |

Gates are implemented as Temporal "wait for signal" steps (or board status polls) that are skipped when the flag is off.

## Coordination Model

- **Claiming:** Agents atomically transition `ready` → `in-progress` (with their own ID) before working — prevents double-pickup.
- **Triggers:** Work-store status changes and Forgejo webhooks fire the relevant agent (see §6 of PRD.md for full event→agent mapping).
- **Reporting:** Results posted as PR review comments and work-item comments. Status transitions are machine-readable; comments are the audit trail.

## Required Environment Variables

```
ANTHROPIC_API_KEY
FORGEJO_API_TOKEN, FORGEJO_WEBHOOK_SECRET, FORGEJO_BASE_URL
TEMPORAL_ADDRESS
```

Plus: model routing config per role, gate flags, per-agent rate limits and concurrency caps.

## Infra Layout

```
infra/
├── .env.example              # All env vars; copy to .env and fill in secrets
├── setup.sh                  # Start services, register runner
├── verify.sh                 # Smoke-test Forgejo + event-bus APIs; run after setup
├── forgejo/
│   ├── docker-compose.yml    # Forgejo + postgres + Actions runner
│   └── runner-config.yml     # Actions runner capacity/label config
├── event-bus/
│   └── docker-compose.yml    # Webhook receiver + RQ worker + SQLite work store + redis
└── temporal/
    └── docker-compose.yml    # Temporal server + Web UI + worker (Phase 6, optional)
```

Quick start:
```bash
cd infra
cp .env.example .env          # then edit secrets (setup.sh auto-fills <CHANGE_ME> values)
./setup.sh                    # starts all services; follow the printed manual steps
./verify.sh                   # confirm APIs are reachable
```

Forgejo runs on `FORGEJO_HTTP_PORT` (default 3000) and SSH on `FORGEJO_SSH_PORT` (default 2222); the event bus on `EVENT_BUS_PORT` (default 8090).

## Build Order (from PRD)

1. Stand up Forgejo + CI runner + event bus; verify APIs and webhooks
2. Webhook receiver → job queue → worker dispatch; define status model (work store)
3. Coding Agent end-to-end (claim → branch → PR) with manual trigger
4. Reviewer + Tester + Security agents triggered by PR webhook; verdict aggregation
5. Idea Agent + Planner Agent; wire `gate.idea_approval`
6. Toggleable gates + Temporal durable runs with human-approval waits
7. Per-agent sandboxing, rate/concurrency limits, cost telemetry, observability

## Key Constraints

- All components self-hosted; target: single modest server
- Each agent run executes in its own ephemeral container with scoped, least-privilege credentials (cannot push to `main`)
- Irreversible steps (open PR, merge, notify) run only after validation so retries are safe
- Model routing: use Claude Opus/Sonnet for idea/plan/review; cheaper or local models for tester/security triage
