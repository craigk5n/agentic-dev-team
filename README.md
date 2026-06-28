# Agentic Dev Team

A self-hosted autonomous coding team. You describe what to build and approve ideas; a pipeline of AI agents handles planning, coding, review, testing, and security checking end-to-end.

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

Then open `http://localhost:8090` to access the board.

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
