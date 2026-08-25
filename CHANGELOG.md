# Changelog

## 0.4.0 — execution modes, TUI, interactive contract generation

- Real controller branches for `offline_sentinel`, `offline_only`, and `literature_guided`.
- `research_agents` for independent literature-guided Codex workers.
- Separate literature-researcher prompt and structured schema with source claims.
- Interactive setup interview.
- Separate `contract_author` and `literature_author` bounded roles.
- Automatic generation of problem-contract JSON and mode-specific literature document.
- Full-screen TUI with campaign, task queue, routes, claims, failures, artifacts, budget, controls, and activity panels.
- Persistent task queue shared by CLI and TUI.
- TUI campaign launch/resume, safe pause, human instruction, route status, artifact browsing, and setup controls.

## 0.3.0 — live campaign activity and human intervention

- Live campaign target, plans, role/task activity, heartbeats, summaries, failures, sentinel decisions, verifier verdicts, and budgets.
- Cooperative pause, persistent instructions, and explicit route control.

## 0.2.0 — Codex role provider

- Fresh bounded Codex role calls with structured schemas and clean-room controls.

## 0.1.0 — initial prototype

- Claim, route, failure, evidence, literature, audit, decision, and formalization state.
