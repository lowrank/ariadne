# Running Ariadne with the Codex CLI

Ariadne and Codex have deliberately different responsibilities.

- **Ariadne** owns the immutable problem contract, operational mode, researcher
  count, persistent task queue, claims, routes, evidence, failure clusters,
  budgets, human controls, audits, and the final Lean gate.
- **Codex** performs one bounded role call and returns one schema-checked public
  research object.

Do not run an entire campaign as one long Codex conversation. That would merge
research, literature, verification, and control contexts and would bypass route
identity, failure deduplication, budgets, and intervention policy.

## 1. Install and authenticate

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

codex --version
codex login
codex login status
ariadne-codex-provider --check
```

The package installs:

```text
ariadne
ariadne-codex-provider
ariadne-tui
```

## 2. Initialize and use the interactive setup

```bash
ariadne init ./my-project --title "My theorem" --provider codex
ariadne setup ./my-project --config ./my-project/ariadne.codex.toml
```

Or use the full-screen interface:

```bash
ariadne tui ./my-project --config ./my-project/ariadne.codex.toml
```

Press `w` to run the setup interview. The interview persists its answers and
then invokes two separate bounded Codex roles:

```text
contract_author    -> exact validated problem-contract JSON
literature_author  -> mode-specific literature document
```

The contract author is web-disabled and must remain route-neutral. The
literature author runs in a separate context and may use live search only when
the user enables it.

The generated literature document depends on the selected mode:

```text
offline_sentinel   hidden sentinel note
offline_only       parked dossier for later human use
literature_guided  shared literature dossier visible at epoch 1
```

## 3. The three execution modes

Researcher counts are operational settings in `ariadne.codex.toml`, not
mathematical fields in the theorem contract.

### Offline with a literature sentinel

```toml
[mode]
name = "offline_sentinel"
offline_agents = 2
parallel = true
literature_intervention = true
require_route_difference_certificate = true
```

Ariadne launches independent `offline_researcher` calls. They do not receive the
literature dossier or web search. After routes are declared, a separate
`literature_sentinel` compares mechanisms with known routes and dead ends. A
researcher may reject an early stop only through the recorded difference-
certificate protocol.

### Offline without sentinel intervention

```toml
[mode]
name = "offline_only"
offline_agents = 2
parallel = true
literature_intervention = false
require_route_difference_certificate = false
```

Ariadne launches independent `offline_researcher` calls and never invokes the
sentinel. A parked literature dossier may exist, but it is not inserted into
research prompts.

### Literature-guided research

```toml
[mode]
name = "literature_guided"
research_agents = 2
parallel = true
literature_intervention = false
require_route_difference_certificate = false
```

Ariadne launches independent `literature_researcher` calls and includes the
shared dossier from the first epoch. The first agents receive different public
assignments: proof constructor, source/gap auditor, and—when a third is enabled—
alternative mechanism or refutation. Imported theorems are recorded as
`SOURCE_REPORTED`; citation does not automatically establish applicability.

The configured count means independent top-level Codex processes controlled by
Ariadne. Codex native subagents remain disabled inside each role call.

## 4. Role and web-search policy

The default Codex template contains these roles:

```text
contract_author          web forced disabled
literature_author        web follows codex_literature provider policy

offline_researcher       web forced disabled
literature_researcher    web follows codex_literature provider policy
literature_sentinel      web follows codex_literature provider policy
intervention_responder   web forced disabled
local_verifier           web forced disabled
global_verifier          web forced disabled
conceptual_pivot         web forced disabled
```

For a frozen, reproducible local dossier:

```toml
[providers.codex_literature.env]
ARIADNE_CODEX_WEB_SEARCH = "disabled"
ARIADNE_CODEX_REASONING_EFFORT = "high"
```

For current literature search by literature-aware roles:

```toml
[providers.codex_literature.env]
ARIADNE_CODEX_WEB_SEARCH = "live"
ARIADNE_CODEX_REASONING_EFFORT = "high"
```

The provider wrapper ignores a live-search request for every nonliterature role.

## 5. Bounded Codex invocation

Each role is launched through noninteractive `codex exec` with:

```text
fresh temporary workspace
read-only Codex sandbox
no command approvals
user config and exec-policy rules ignored
ephemeral session
role-specific instructions and JSON schema
Codex memories and native subagents disabled
shell, unified-exec, apps/connectors, and dependency installation disabled
raw and summarized private reasoning disabled
```

The Codex client must still communicate with the model service. Therefore
`network_policy="deny"` on an offline role means **research isolation**:
Ariadne withholds literature and project files and forces web search off. Strict
host-level network isolation requires an operator-configured container, VM, or
sandbox and can be required through the provider configuration.

Ariadne cannot remove mathematical facts already present in a pretrained model.
Role instructions require unreconstructed remembered results to be labeled
rather than treated as audited literature.

## 6. Run, observe, pause, and steer

CLI:

```bash
ariadne campaign run ./my-project \
  --config ./my-project/ariadne.codex.toml
```

TUI:

```bash
ariadne tui ./my-project --config ./my-project/ariadne.codex.toml
```

The CLI and TUI use the same SQLite database and event log. The TUI shows the
current plan, persistent task queue, active role calls, routes, partial claims,
failure clusters, artifacts, budget, human instructions, and recent activity.
It displays public task/result summaries, not hidden chain-of-thought.

Pause safely from either interface or a second terminal:

```bash
ariadne campaign pause ./my-project --reason "Change the next proof epoch"
ariadne campaign instruct ./my-project \
  --text "Prove the cancellation identity first; do not repeat route RTE-..."
ariadne campaign resume ./my-project \
  --config ./my-project/ariadne.codex.toml
```

A pause is honored at the next safe controller boundary after the currently
active bounded result has been recorded.

## 7. Late Lean gate

Research roles do not receive Lean as a proof-discovery tool. Formalization begins
only after a complete natural-language proof, local and fresh global audits, and
a recorded human proof check. See the main README and `EPISTEMIC_MODEL.md`.
