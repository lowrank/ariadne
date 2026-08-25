# Validation Record

Validated on 2026-08-22 with Python 3.13.

## Core checks

```text
PYTHONPATH=src python -m compileall -q src
PYTHONPATH=src python -m unittest discover -s tests -v
```

Result: **27 tests passed**.

The automated suite covers:

- claim, route, attempt, evidence, audit, decision, and failure persistence;
- content-based failure clustering and duplicate-route stopping;
- valid and invalid literature early-stop rejections;
- withdrawal of an early stop after bounded novelty evidence;
- all three controller branches:
  - `offline_sentinel`;
  - `offline_only`;
  - `literature_guided`;
- mode-dependent researcher counts and role selection;
- literature-guided source claims remaining `SOURCE_REPORTED`;
- no sentinel invocation in `offline_only` or `literature_guided`;
- the separate `contract_author` and `literature_author` setup calls;
- correct mode-specific literature document types;
- refusal to overwrite a frozen problem contract after campaign creation;
- durable copying of owner-supplied base and literature files;
- default disabling of intervention when a minimal literature-guided TOML omits
  the explicit flag;
- loading all packaged mode examples;
- TUI campaign, task, route, partial-result, failure, budget, and artifact panels;
- persistent task-queue lifecycle;
- live campaign plans, public role/task activity, and heartbeat output;
- cooperative pause while a bounded call is active;
- transition to `PAUSED_HUMAN` at a safe controller boundary;
- campaign-wide and route-specific human-instruction injection;
- resume of the same campaign at the next epoch;
- migration of existing databases to active-task and task-queue columns;
- Codex role schemas and command construction;
- fresh, read-only, ephemeral Codex workspaces;
- forced web-search disablement for nonliterature roles;
- opt-in web search only for `literature_author`, `literature_researcher`, and
  `literature_sentinel`;
- secret-variable redaction while preserving `CODEX_HOME`;
- claim-status transition enforcement;
- the human-review requirement before Lean;
- exact-counterexample evidence requirements for refutation;
- successful recording of an external formal-verification command.

## Three-mode CLI smoke test

A clean project was initialized in each mode, the noninteractive setup workflow
ran with the deterministic mock provider, and a campaign was executed:

```text
offline_sentinel  -> COMPLETED_UNSOLVED
offline_only      -> COMPLETED_UNSOLVED
literature_guided -> COMPLETED_UNSOLVED
```

The outputs were checked for the intended role separation:

- offline-sentinel campaigns used independent offline researchers and the
  sentinel negotiation path;
- offline-only campaigns never called the sentinel;
- literature-guided campaigns used the configured literature researchers and
  shared dossier from epoch one;
- every problem verdict remained `UNSOLVED`; the harness did not manufacture a
  theorem because the mock campaign completed.

## Setup workflow smoke test

The setup command was exercised in all three modes. It produced:

```text
offline_sentinel   -> literature_sentinel_note.md
offline_only       -> parked_literature_dossier.md
literature_guided  -> shared_literature_dossier.md
```

The contract and literature calls were separate agent-run records. Source files
were copied under `.ariadne/literature/source-materials/` with content-derived
filenames. The generated contract retained
`lean_allowed_only_after_human_checked_proof=true`.

## TUI validation

The full-screen interface is based on `prompt_toolkit`. Automated tests construct
its application and inspect every data panel against a populated SQLite dossier.
The command surfaces were also checked with:

```text
ariadne tui --help
ariadne-tui --help
```

Interactive terminal rendering, key handling, and dialogs still depend on the
operator's terminal and should be smoke-tested locally before a long paid run.
Quitting the TUI intentionally does not kill a running campaign subprocess.

## Human-intervention integration

A deterministic slow command provider was run in a background controller thread.
While it was active, a second store connection requested a pause. The heartbeat
reported the pending pause. After the bounded result was recorded, the campaign
entered `PAUSED_HUMAN`. A human instruction was added, the pause was cleared, and
the same campaign resumed. The next matching prompt contained the persisted
instruction; the earlier prompt did not.

## Codex wrapper tests

A deterministic fake `codex` executable exercised the provider protocol without
external API calls. The tests verify invocation with an ephemeral, read-only
workspace, role-specific JSON Schema, final-output file, web policy, disabled
native subagents/memories/shell/apps, and hidden private reasoning.

A live authenticated Codex service call was **not** made because user credentials
were unavailable in the build environment. Before a paid campaign, run:

```text
codex --version
codex login
codex login status
ariadne-codex-provider --check
```

## Installation note

Normal editable installation:

```text
pip install -e .
```

Offline editable installation when build dependencies are already present:

```text
pip install --no-build-isolation -e .
```

## Wheel and installed-command smoke test

A wheel was built with:

```text
python -m pip wheel --no-build-isolation --no-deps .
```

The `0.4.0` wheel was installed into a clean virtual environment using the
already-installed runtime dependencies. The installed commands passed:

```text
ariadne --help
ariadne-tui --help
```

An installed-wheel literature-guided project then completed setup and a mock
campaign, producing `shared_literature_dossier.md`, campaign status
`COMPLETED_UNSOLVED`, and problem verdict `UNSOLVED`.
