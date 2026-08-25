# Ariadne repository instructions for Codex

## Purpose

This repository implements a cost-aware mathematical-research controller. Codex
may help maintain the software and may be used as a bounded role provider, but it
must not collapse the controller, researchers, literature sentinel, verifiers,
and formalizer into one unstructured session.

## Mathematical integrity

- The problem contract under `.ariadne/problem_contract.json` is immutable during
  a campaign. Never weaken or silently reinterpret it.
- A route is not a claim. Failure of a route does not refute the proposition.
- Numerical evidence is not a proof. An exact counterexample has only its stated
  scope until independently checked.
- Do not write directly to `.ariadne/state.sqlite`, `.ariadne/events.jsonl`, or
  content-addressed artifacts. Use the `ariadne` CLI and store APIs.
- Do not promote a claim by editing status fields. Status transitions require the
  evidence artifacts enforced by Ariadne.
- Preserve the live activity and human-control plane. Human pause requests,
  instructions, and route-status decisions must remain durable, auditable, and
  subordinate to the immutable problem contract.
- Lean is forbidden as a discovery mechanism. It may be used only after the
  complete natural-language proof has a recorded human approval.

## Codex integration

- Automated campaigns use `examples/config.codex.toml` and the
  `ariadne-codex-provider` entry point.
- Each mathematical role is a separate ephemeral `codex exec` invocation with a
  role-specific output schema.
- Offline researchers must not receive the literature dossier or web search.
- Literature-aware roles (`literature_author`, `literature_researcher`, and
  `literature_sentinel`) may receive the dossier and opt-in live search. Offline,
  verifier, responder, and conceptual roles must have web search forced off.
- Do not use Codex native subagents from inside a bounded role invocation.
  Ariadne owns concurrency, budgets, route identity, failure clustering, and
  early-stop negotiation.
- Do not expose or request private chain-of-thought. Persist concise claims,
  routes, failures, evidence, and audit objects only.

## Validation after code changes

Run:

```bash
PYTHONPATH=src python -m compileall -q src
PYTHONPATH=src python -m unittest discover -s tests -v
```

When changing modes, setup, TUI, campaign activity, or intervention controls, also run:

```bash
PYTHONPATH=src python -m unittest tests.test_activity_and_intervention tests.test_modes_setup_tui -v
```

When changing the Codex wrapper, also run:

```bash
PYTHONPATH=src python -m unittest tests.test_codex_provider -v
```
