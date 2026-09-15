# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## What this project is

The avatar + multi-agent reasoning layer for APM (Agentic Process
Management) — the Supervisor and every specialized business-process
agent (Order-Renewal today; Churn Prevention, Customer Onboarding, ...
later). See `README.md`.

Its sibling repo,
[`apm_connectors`](https://github.com/ravindraaws77/apm_connectors_enterprise_systems),
holds the connectors and the approval gate and stays reasoning-free —
see that repo's `CLAUDE.md` and `docs/roadmap.md` for why the split
exists and what belongs on which side of it.

## Non-negotiable rule

**No code in this repo may call `apm_connectors`' `POST
/tools/actions/{action_id}/decision` route.** That's the approval
boundary: a real authenticated human hitting that endpoint directly (or
through a human-facing review surface built for exactly that), never
something a Supervisor or specialized agent calls itself, regardless of
how sophisticated this repo's orchestration gets. `ConnectorsClient.
decide_action` exists only for that human-facing surface to use — never
wire it into an SDK tool any agent can call.

This repo talks to `apm_connectors` only over HTTP against its `/tools/*`
API (`src/apm_orchestrator/connectors_client.py`) — never by importing
that repo's internals.

## Working conventions

- **Agents are decomposed by business process, not by connector.** A
  new specialized agent is a new `agents/<name>/` package (its own
  `policy.yaml` + `agent.py`), holding whatever subset of
  `src/apm_orchestrator/tools.py`'s tools that business process needs as
  its toolbelt — never a per-connector agent. Register its delegate tool
  in `supervisor.py`.
- **Policy is data, not code.** What counts as "a renewal" (or whatever
  the next agent's equivalent trigger is), SLA windows, and
  blocking-ticket rules belong in that agent's `policy.yaml`, loaded at
  run time — a policy change should never require a code change.
- Never commit secrets. `.env.example` documents required variables;
  real values go in a local, gitignored `.env`.
- Adding a new `@tool` to `tools.py` is additive; changing an existing
  one's `input_schema` to match a breaking change in `apm_connectors`'
  own `/tools/*` contract needs both repos updated together.

## Layout

```
src/apm_orchestrator/
  config.py           env/config loading
  connectors_client.py  the only HTTP boundary to apm_connectors
  tools.py             one Claude Agent SDK @tool per apm_connectors route
  supervisor.py         business-intent routing, delegates to agents
  agents/
    order_renewal/      pilot agent: policy.yaml + policy.py + agent.py
  cli.py               manual single-prompt smoke test
tests/                 unit tests, no live credentials or running server needed
```
