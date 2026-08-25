# Campaign modes

Ariadne implements three execution branches in the controller. The problem contract records the intended research mode, while `ariadne.codex.toml` controls operational counts, concurrency, budgets, and network permissions.

## Offline plus sentinel

`offline_sentinel` launches `offline_agents` independent clean-room researchers. Literature is withheld. After route declaration, the sentinel may propose early stopping for a strong mechanism-level match. A route can continue provisionally only after a valid difference certificate.

## Offline only

`offline_only` launches `offline_agents` and never invokes the sentinel. Literature sources stored in the dossier remain excluded from researcher prompts.

## Literature guided

`literature_guided` launches `research_agents`. Every researcher receives the shared dossier at the first epoch. There is no early-stop sentinel negotiation because reuse of a known route is allowed. Researchers must separate sourced results from new mathematics and provide applicability arguments.

The first three agents receive distinct functions: constructor, source/gap auditor, and alternative/refutation researcher. Counts above three create further independent route assignments.

## Counts

Counts are operational and belong in TOML, not in the immutable theorem JSON.

```toml
[mode]
name = "literature_guided"
research_agents = 3
parallel = true
literature_intervention = false
```

or:

```toml
[mode]
name = "offline_sentinel"
offline_agents = 2
parallel = true
literature_intervention = true
```

Nested Codex subagents remain disabled.
