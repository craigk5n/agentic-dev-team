# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

An autonomous multi-agent coding team system. A human operator describes what to build and approves ideas; agents handle planning, coding, review, testing, and security checking end-to-end.

**Core design principle:** The work board is the coordination backbone. Agents never talk to each other directly — they react to board/git events, claim work items, do their job, and post results back.

## Tech Stack

| Component | Tool |
|---|---|
| Work board | Plane Community Edition (self-hosted Docker) — REST API + webhooks + MCP server |
| Git forge | Forgejo (self-hosted Go binary) — PRs, branch protection, webhooks |
| CI (tester) | Forgejo Actions or Woodpecker CI |
| High-reasoning agents | Claude Agent SDK (Python or TS) — requires direct Anthropic API key, NOT subscription |
| Mechanical/high-volume agents | opencode (`-p` non-interactive, JSON output) — model-agnostic, supports local Ollama |
| Durable orchestration | Temporal (self-hosted) — durable execution, retries, human-approval wait signals |
| Static security analysis | Semgrep + secret scanning |
| Isolation | One container or microVM per agent run |

**Important:** Claude SDK agents must use a direct `ANTHROPIC_API_KEY` (pay-as-you-go). Headless subscription usage is metered against a small capped credit and will hard-stop mid-month.

## Agent Roles

1. **Idea Agent** (Claude SDK) — generates proposals, writes to Plane Intake with status `pending-approval`
2. **Planner/PM Agent** (Claude SDK) — triggered on Intake approval; decomposes into Module (epic) + Work Items (stories), sets status `ready`
3. **Coding Agent(s)** (Claude SDK or opencode) — claims `ready` story, creates branch, implements, opens PR, sets story `in-review`
4. **Code Reviewer** (Claude SDK) — triggered by Forgejo `pull_request` webhook; posts review verdict
5. **Tester** (opencode + CI) — triggered by PR event; runs test suite, reports pass/fail + coverage
6. **Security Reviewer** (opencode + Semgrep) — triggered by PR event; runs SAST + secret scanning, posts security verdict

A PR advances only when all three verdicts (reviewer, tester, security) are green plus any enabled human gate.

## Work Item Status Flow

```
idea:    pending-approval -> approved -> rejected
story:   backlog -> ready -> in-progress -> in-review -> approved -> merged -> done
                                         \-> changes-requested -> ready
```

## Human Approval Gates (config flags)

| Gate | Default |
|---|---|
| `gate.idea_approval` | ON — always required, operator approves Intake items |
| `gate.pr_merge_approval` | OFF (toggleable) |
| `gate.security_signoff` | ON (recommended) — hold merge if security verdict is not green |

Gates are implemented as Temporal "wait for signal" steps (or board status polls) that are skipped when the flag is off.

## Coordination Model

- **Claiming:** Agents atomically transition `ready` → `in-progress` (with their own ID) before working — prevents double-pickup.
- **Triggers:** Plane webhooks and Forgejo webhooks fire the relevant agent (see §6 of PRD.md for full event→agent mapping).
- **Reporting:** Results posted as PR review comments and Plane item comments. Status transitions are machine-readable; comments are the audit trail.

## Required Environment Variables

```
ANTHROPIC_API_KEY
PLANE_API_TOKEN, PLANE_WEBHOOK_SECRET, PLANE_BASE_URL
FORGEJO_API_TOKEN, FORGEJO_WEBHOOK_SECRET, FORGEJO_BASE_URL
TEMPORAL_ADDRESS
```

Plus: model routing config per role, gate flags, per-agent rate limits and concurrency caps.

## Infra Layout

```
infra/
├── .env.example              # All env vars; copy to .env and fill in secrets
├── setup.sh                  # Start services, init DBs, register runner
├── verify.sh                 # Smoke-test Plane + Forgejo APIs; run after setup
├── plane/
│   └── docker-compose.yml    # Plane CE (API, worker, beat, web, space, proxy, postgres, redis, minio)
└── forgejo/
    ├── docker-compose.yml    # Forgejo + postgres + Actions runner
    └── runner-config.yml     # Actions runner capacity/label config
```

Quick start:
```bash
cd infra
cp .env.example .env          # then edit secrets (setup.sh auto-fills <CHANGE_ME> values)
./setup.sh                    # starts all services; follow the printed manual steps
./verify.sh                   # confirm APIs are reachable
```

Plane runs on `PLANE_HTTP_PORT` (default 80); Forgejo on `FORGEJO_HTTP_PORT` (default 3000) and SSH on `FORGEJO_SSH_PORT` (default 2222).

## Build Order (from PRD)

1. Stand up Plane + Forgejo + CI runner; verify APIs and webhooks
2. Webhook receiver → job queue → worker dispatch; define status model
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
