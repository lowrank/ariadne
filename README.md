# Ariadne Math Harness 0.4.0

Ariadne is a **cost-aware, route-aware mathematical research harness**. It keeps the theorem fixed, separates claims from routes, compresses repeated failures, records typed evidence, exposes live campaign state, permits human intervention, and delays Lean until a complete proof has been human checked.

Version 0.4.0 adds:

- three real campaign modes: `offline_sentinel`, `offline_only`, and `literature_guided`;
- a configurable number of independent top-level researchers in every mode;
- a full-screen terminal UI with panels for plans, active/queued work, routes, claims, failures, artifacts, budget, and activity;
- an interactive setup wizard;
- one bounded Codex role that creates the problem-contract JSON;
- a **separate** literature role that creates a hidden sentinel note, parked dossier, or shared literature review;
- a persistent task queue shared by the CLI and TUI.

## Mathematical governance

1. **The problem contract is immutable during a campaign.** Quantifiers, domains, endpoints, coefficient fields, uniformity, and terminal criteria are explicit.
2. **A route is not a theorem.** Failure of a route does not refute the root claim.
3. **Evidence is typed.** Numerical observations, exact counterexamples, literature statements, LLM audits, human review, and Lean certificates are different objects.
4. **Failures are compressed.** Repeated attempts with one mathematical obstruction become one failure cluster.
5. **Research is budgeted by decisive information.** More rounds and more accepted microclaims are not automatically progress.
6. **The literature policy is explicit.** It is not inferred from prompt wording.
7. **Human intervention is persistent and auditable.** A user can pause, steer, park routes, and resume.
8. **Lean is terminal certification only.** It does not select or invent the proof route.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
```

The TUI uses `prompt_toolkit`; local PDF extraction uses `pypdf`; and `sympy` is available for exact symbolic verification checks.

For Codex-backed runs:

```bash
codex login
codex login status
ariadne-codex-provider --check
```

## Fastest start: TUI and setup wizard

```bash
ariadne init ./my-project --title "My theorem" --provider codex
ariadne tui ./my-project --config ./my-project/ariadne.codex.toml
```

Inside the TUI, type `/` to open the slash-command menu. Common commands are `/run`, `/pause`, `/instruct TEXT`, `/budget`, `/artifact next|prev`, `/report`, and `/quit`.

See the end-to-end [tutorial](docs/TUTORIAL.md), including LlamaParse, MinerU, PDF extraction, SymPy verification, and restart handoffs.

The setup wizard asks a small set of high-leverage questions about:

- the exact theorem or prove-or-refute statement;
- the desired improvement;
- hypotheses, domains, uniformity, endpoints, and exclusions;
- exact proof and refutation criteria;
- base papers/materials visible to the contract author;
- additional literature files visible only to the literature subagent;
- research mode;
- number of independent researchers;
- whether the literature role may search the live web.

It then runs two independent bounded roles:

```text
contract-author      -> .ariadne/problem_contract.json
literature-author    -> hidden sentinel / parked dossier / shared dossier
```

The contract author is web-disabled. If it explicitly cannot fix an underspecified problem from the owner material, Ariadne may call the separate web-enabled `contract_resolver` (only with live literature enabled), then retries the offline author with its auditable source packet. The literature author remains separate for the later dossier.
Owner-supplied source files are copied into the durable project dossier with
content-derived names; only bounded excerpts and basenames are sent to the
contract author.
Setup and `contract set` refuse to replace the theorem after a campaign has been
created. Start a new project directory for a revised statement.

The same workflow is available without the TUI:

```bash
ariadne setup ./my-project --config ./my-project/ariadne.codex.toml
```

For reproducible noninteractive setup:

```bash
ariadne setup ./my-project \
  --config ./my-project/ariadne.codex.toml \
  --answers-file ./setup_answers.json
```

See [`docs/INTERACTIVE_SETUP.md`](docs/INTERACTIVE_SETUP.md).

## Campaign modes

### `offline_sentinel`

```toml
[mode]
name = "offline_sentinel"
offline_agents = 2
parallel = true
literature_intervention = true
require_route_difference_certificate = true
```

Independent researchers do not receive the literature dossier. After they declare route mechanisms, a literature sentinel compares those routes with known methods and dead ends. A researcher can reject early stopping only with a concrete difference certificate and one bounded novelty epoch.

### `offline_only`

```toml
[mode]
name = "offline_only"
offline_agents = 2
parallel = true
literature_intervention = false
require_route_difference_certificate = false
```

Researchers work without literature and no sentinel intervenes. The setup wizard may still create a parked literature dossier for later human use, but the campaign does not share it.

### `literature_guided`

```toml
[mode]
name = "literature_guided"
research_agents = 2
parallel = true
literature_intervention = false
require_route_difference_certificate = false
```

Researchers receive the shared dossier from epoch one. The default assignments are deliberately different:

1. construct the shortest complete literature-supported route and isolate the new bridge;
2. audit sources, applicability, endpoints, uniformity, missing hypotheses, and counterexamples;
3. when configured, pursue an alternative mechanism or refutation lane;
4. further researchers must declare independent mechanisms rather than paraphrase another route.

A known route is allowed. The system records exact imported theorems as `SOURCE_REPORTED`; they are not silently promoted to proved project claims.

See [`docs/MODES.md`](docs/MODES.md).

## Live TUI panels

The full-screen TUI shows the same SQLite/event-log state used by the CLI:

- immutable target and current epoch plan;
- active and queued tasks;
- agent slot, role, route, and public task summary;
- route status and key bridge lemma;
- partial claims and recent attempt summaries;
- compressed failure clusters;
- budget calls and cost;
- active human instructions and pause state;
- artifacts with a selected text preview;
- recent events and provider activity.

The TUI does not expose hidden model reasoning. It exposes public mathematical tasks, artifacts, decisions, and results.

See [`docs/TUI.md`](docs/TUI.md).

## CLI campaign workflow

```bash
ariadne campaign run ./my-project \
  --config ./my-project/ariadne.codex.toml
```

Pause and intervene from another terminal:

```bash
ariadne campaign pause ./my-project \
  --reason "I want to change the proof strategy"

ariadne campaign instruct ./my-project \
  --text "Prove the cancellation identity first; do not repeat the absolute-value estimate."

ariadne campaign resume ./my-project \
  --config ./my-project/ariadne.codex.toml
```

Route-specific steering:

```bash
ariadne campaign instruct ./my-project \
  --route RTE-... \
  --text "Try the dual formulation and state a decisive test."

ariadne campaign route-status ./my-project \
  --route RTE-... \
  --status PARKED \
  --note "This repeats the same nonuniform-constant obstruction."
```

Inspect state:

```bash
ariadne campaign status ./my-project
ariadne routes ./my-project
ariadne failures ./my-project
ariadne report ./my-project --output ./research-report.md
```

See [`docs/HUMAN_INTERVENTION.md`](docs/HUMAN_INTERVENTION.md).

## Codex role isolation

Ariadne remains the controller. Each role is a fresh, bounded `codex exec` process with a structured-output schema.

```text
contract_author          web disabled
literature_author        web allowed as configured

offline_researcher       web forced disabled
literature_researcher    web allowed as configured
literature_sentinel      web allowed as configured
intervention_responder   web forced disabled
local_verifier           web forced disabled
global_verifier          web forced disabled
conceptual_pivot         web forced disabled
```

Nested Codex subagents and memories are disabled. The configured researcher count means independent top-level Codex processes controlled and budgeted by Ariadne, not hidden subagents inside one process.

See [`docs/CODEX.md`](docs/CODEX.md) and [`docs/SECURITY.md`](docs/SECURITY.md).

## Project state

```text
my-project/
├── ariadne.codex.toml
└── .ariadne/
    ├── problem_contract.json
    ├── state.sqlite
    ├── events.jsonl
    ├── artifacts/
    ├── literature/
    ├── reports/
    └── formal/
```

The database contains claims, routes, attempts, failure clusters, evidence, sources, interventions, audits, decisions, agent runs, a task queue, human instructions, reviews, and formalization records.

## Late Lean gate

The formalization gate opens only after:

1. the exact theorem is frozen;
2. a complete natural-language proof exists;
3. local and fresh global audits pass;
4. a human mathematician records a complete proof check.

Then:

```bash
ariadne approve ./my-project \
  --claim CLM-... \
  --reviewer "Name"

ariadne formalize ./my-project \
  --claim CLM-... \
  --toolchain "leanprover/lean4:vX.Y.Z" \
  --cwd ./lean-project \
  -- lake build
```

## Validation

```bash
python -m unittest discover -s tests -v
ariadne doctor ./my-project --config ./my-project/ariadne.codex.toml
```

The deterministic mock provider exercises all controller branches without claiming to solve a theorem.
