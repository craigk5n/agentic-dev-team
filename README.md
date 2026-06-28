# Agentic Dev Team

A self-hosted autonomous coding team. You describe what to build and approve ideas; a pipeline of AI agents handles planning, coding, review, testing, and security checking end-to-end.

> ## ⚠️ Read this before you deploy
>
> **This is a single-user system with NO authentication on the board.** The board UI and API (`http://<host>:8090`) are wide open — there is no login. **Anyone who can reach that URL can submit ideas, and every idea kicks off LLM calls across the whole pipeline (idea → planner → coder → reviewer + tester + security).**
>
> **If you expose this URL on the public internet — or any network you don't fully trust — a stranger can run up your LLM bill,** exhaust your API quota, or get your provider account flagged for abuse. With a paid `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY`, that is real money.
>
> **Keep it bound to `localhost` or a trusted private LAN.** If you need remote access, put it behind a VPN or an authenticating reverse proxy. See [Security & access](#security--access).

## How it works

**Core principle:** The work board is the coordination backbone. Agents never talk to each other directly — they react to board and git events, claim work items, do their job, and post results back.

```
You submit an idea
  → Idea Agent expands it into a structured proposal
  → You approve it
    → Planner Agent decomposes it into stories
      → Coding Agent implements each story and opens a PR
        → Reviewer + Tester + Security agents evaluate the PR in parallel
          → All verdicts green → PR merges → next story picks up
```

## Agents

| Agent | Model | Trigger | Output |
|---|---|---|---|
| Idea | Claude / OpenRouter | Manual prompt | Structured proposal → `pending-approval` |
| Planner | Claude / OpenRouter | Idea approved | Stories with sequence, repo, description |
| Coder | opencode (any model) | Story `ready` | Branch + PR → story `in-review` |
| Reviewer | Claude / OpenRouter | PR opened | Code review verdict (pass/warn/fail) |
| Tester | OpenRouter | PR opened | Test run verdict |
| Security | OpenRouter | PR opened | SAST + secret scan verdict |

## Human approval gates

| Gate | Default | Description |
|---|---|---|
| `idea_approval` | **ON** | You approve each idea before planning starts |
| `pr_merge_approval` | OFF | Optionally require a human to approve each PR merge |
| `security_signoff` | ON | Holds merge if security verdict is not green |

## Tech stack

| Component | Tool |
|---|---|
| Event bus + work board | Custom FastAPI service with SQLite + Redis |
| Git forge | Forgejo (self-hosted) |
| LLM routing | OpenRouter (free and paid models) or direct Anthropic API |
| Coding agent | opencode (model-agnostic, supports Ollama) |
| Agent isolation | Ephemeral Docker containers per coding run |

## Quick start

```bash
git clone https://github.com/yourname/agentic-dev-team
cd agentic-dev-team/infra
cp .env.example .env        # fill in API keys and secrets
./setup.sh                  # starts Forgejo + event-bus + Redis
./verify.sh                 # smoke-tests all APIs
```

Then open `http://localhost:8090` to access the board — **on this machine only.** Do not forward this port or bind it to a public interface (see below).

## Security & access

This system is designed for a **single operator on a trusted machine or LAN.** Understand these properties before deploying:

- **The board has no authentication.** Every endpoint under `http://<host>:8090` — submitting ideas, approving/rejecting, approving PR merges, and changing runtime config (gate flags, model selection, rate limits) — is reachable by anyone who can open the URL. There is no login, token, or user concept on the board.
- **Open access = open wallet.** Submitting an idea triggers the full agent pipeline, and every stage is an LLM call. A malicious or careless visitor can submit ideas in a loop and **drive up your LLM bill, burn through your API quota, or get your provider account suspended for abuse.** This risk is highest with a paid `ANTHROPIC_API_KEY` or `OPENROUTER_API_KEY`; even "free" OpenRouter models have rate limits an attacker can exhaust.
- **Agents can act on your forge.** A submitted idea ultimately creates repos, branches, and PRs in Forgejo and merges them. Exposing the board hands that capability to whoever reaches it.

**Do:**
- Keep the board bound to `localhost` (default) or a private network segment you control.
- For remote access, front it with a **VPN** (e.g. WireGuard/Tailscale) or an **authenticating reverse proxy** (nginx/Caddy/oauth2-proxy with HTTP basic auth or SSO). Never put the raw port on the public internet.
- Set per-role **rate and concurrency limits** (`PATCH /api/config`) and prefer free/local models as a cost backstop — but treat these as defense-in-depth, not a substitute for network isolation.
- Keep `SANDBOX_MODE=docker` so coding agents run with scoped, least-privilege credentials that cannot push to `main`.

**Don't:**
- Don't port-forward `:8090` on your router, bind it to `0.0.0.0` on an untrusted network, or share the URL with people you wouldn't hand your API key to.

Forgejo itself *does* require a login (admin password in `FORGEJO_ADMIN_PASSWORD`); the open surface is the board. Adding authentication to the board is a known gap — until it exists, **network isolation is the control.**

## Environment variables

```
ANTHROPIC_API_KEY           # for idea, planner, reviewer agents
OPENROUTER_API_KEY          # alternative/supplement to Anthropic
FORGEJO_API_TOKEN
FORGEJO_WEBHOOK_SECRET
SANDBOX_MODE=docker         # run coding agents in isolated containers (recommended)
```

See `infra/.env.example` for the full list.

## Story status flow

```
idea:   pending-approval → approved → rejected
story:  backlog → ready → in-progress → in-review → approved → merged → done
                                      ↘ changes-requested → ready
```

## Repository layout

```
agents/
  idea/       # Idea Agent (litellm + OpenRouter)
  planner/    # Planner Agent (litellm + OpenRouter)
  coding/     # Coding Agent (opencode subprocess)
  reviewer/   # Reviewer, Tester, Security agents + telemetry
event-bus/    # FastAPI webhook receiver, work item store, board UI
infra/        # Docker Compose files, setup scripts, .env.example
```
