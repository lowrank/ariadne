# Interactive problem setup

Run from the CLI:

```bash
ariadne setup PROJECT --config PROJECT/ariadne.codex.toml
```

or use `/setup` in the TUI.

The interview asks only high-leverage questions. It does not ask the owner to manually author the complete JSON schema.

It first detects whether the selected project is already a Git worktree. An
existing repository is used without prompting; a new non-Git project is asked
whether to enable local Git version control (default: yes). When enabled,
Ariadne initializes the repository after setup and records a setup snapshot.
Every completed epoch then commits a concise `ARIADNE_EPOCHS.md` record; a
decisive epoch receives an `ariadne/<campaign>/epoch-<n>-progress` tag. Runtime
campaign state under `.ariadne/` stays out of Git: it can be large, mutable,
and contains SQLite/WAL files. Source files, configuration, notes, and
`ARIADNE_BRANCHES.json` remain versionable.

If a later chat instruction proposes a stronger or weaker theorem, Ariadne
leaves the frozen parent contract untouched and creates a sibling Git worktree
on a `contract-variant-*` branch. If Git was declined, it instead creates a
sibling directory with the same Ariadne lineage/provenance files.

## Two-agent generation

After the interview, Ariadne invokes:

1. `contract_author`: web-disabled, route-neutral, converts the interview and supplied base-source excerpts into a validated JSON contract.
2. `literature_author`: separate context, literature-aware, creates the document appropriate to the mode.

Outputs:

```text
offline_sentinel   -> literature_sentinel_note.md
offline_only       -> parked_literature_dossier.md
literature_guided  -> shared_literature_dossier.md
```

The setup command rewrites only the operational `[mode]` table and literature web-search setting in the project TOML. It then reloads the config to validate the selected branch.

Setup is permitted only before the first campaign is created. Once a campaign
exists, the problem contract is immutable. A materially revised theorem must use
a new project directory so earlier claims, routes, failures, and audits cannot
be silently reinterpreted against a changed target.

## Local sources and context separation

The interview asks for two file groups:

1. base materials visible to both the route-neutral contract author and the literature author;
2. additional literature files visible **only** to the separate literature author.

Both groups accept comma-separated `.md`, `.txt`, `.tex`, `.json`, YAML, Python, or PDF paths. Text is excerpted into bounded role prompts. PDFs are extracted through `pypdf`. Missing paths are reported rather than silently ignored. This split prevents later proof routes from contaminating the frozen problem contract. If the offline contract author explicitly reports that a named result or reference remains underspecified, a separate web-enabled contract resolver may identify the exact source packet and the offline author then retries; it never gives web access directly to the contract author.

The supplied source bytes are also copied into the project under
`.ariadne/literature/source-materials/` with content-derived filenames and
source records. The contract prompt contains only bounded excerpts and file
basenames; the original absolute host paths are not exposed to the bounded role.

Setup emits the same public role activity and heartbeats as a campaign, so a
long contract or literature-author call is visible rather than appearing idle.

## Noninteractive reproducibility

Save the answers as JSON and run:

```bash
ariadne setup PROJECT --config CONFIG --answers-file setup_answers.json
```

The answers schema follows the `SetupAnswers` fields in `ariadne_math.setup_wizard`.
