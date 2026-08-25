# Tutorial: a first Ariadne project

This tutorial creates a small prove-or-refute project, runs it through the
terminal UI, inspects the evidence, and shows how to restart the project later.
The sample statement is intentionally elementary; it demonstrates the workflow,
not a difficult research result.

## 1. Create a Python environment

Ariadne requires Python 3.11 or later. From a clone of this repository:

```bash
python -m venv .venv
source .venv/bin/activate              # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

`requirements.txt` installs the runtime UI and PDF reader plus SymPy. Installing
with `pip install -e .` also exposes these commands in the active environment:

```text
ariadne
ariadne-tui
ariadne-codex-provider
ariadne-research-tool
```

Check the installation:

```bash
ariadne --help
ariadne-codex-provider --check
```

For Codex-backed campaigns, authenticate the Codex CLI in the same shell:

```bash
codex login
codex login status
```

## 2. Choose project versioning during setup

On startup Ariadne detects whether the selected project is already a Git
worktree. Existing repositories are used without prompting. For a new
non-Git directory, setup asks `Enable Git version control for this project? y/n`
(default: `y`). Choose `y` to initialize the repository and commit a setup
snapshot.

After every completed epoch, Ariadne commits a concise entry to
`ARIADNE_EPOCHS.md` containing the campaign ID, epoch, status, attempt count,
decisive-event count, and summary. An epoch with a decisive event receives an
annotated `ariadne/<campaign>/epoch-<n>-progress` Git tag. This is provenance,
not a claim of proof or refutation. Ariadne excludes the mutable `.ariadne/`
runtime directory from Git, including its SQLite/WAL files and
content-addressed artifacts.

A later stronger or weaker target never changes the frozen parent contract.
With Git enabled, Ariadne creates a sibling worktree on a
`contract-variant-*` branch; otherwise it creates a sibling project directory
with an Ariadne provenance ledger. In either case, the successor goes through
fresh setup and mints its own contract.

This check also runs when reopening a paused project after `Ctrl+C`: if that
project is not already in Git, the TUI asks before it resumes the campaign.

## Token-priced campaign budgets

`estimated_cost_usd` is only reserved before a call starts, preventing parallel
calls from overspending the campaign ceiling. After a successful Codex call,
Ariadne settles the reservation from the emitted input, cached-input, and output
tokens using the provider's `*_cost_per_million_usd` values in
`ariadne.codex.toml`. A provider-supplied `usage.cost_usd`, when present, takes
precedence. Older TOMLs using `ARIADNE_CODEX_MODEL = "gpt-5.6-sol"`,
`gpt-5.6-terra`, or `gpt-5.6-luna` receive the bundled standard rates
automatically; add explicit TOML rates when you change model or billing tier.

The packaged GPT-5.6 Sol configuration uses the current standard API token
rates. A Codex subscription or a different billing arrangement may not expose
an invoice to the CLI, so the displayed total is the configured token-metered
amount in that case. See [OpenAI pricing](https://platform.openai.com/pricing)
for current rates.

## 2. Optional PDF and API configuration

### LlamaParse

LlamaParse is optional. It is used first for owner-supplied PDF task files when
the key is available; Ariadne then falls back through MinerU, `pypdf`, and
`pdftotext` according to `ARIADNE_PDF_BACKENDS`.

```bash
export LLAMAPARSE_API_KEY="your-key"
export ARIADNE_PDF_BACKENDS="llamaparse,mineru,pypdf,pdftotext"
```

### LaTeX/PDF proof notes

Install a working `pdflatex` distribution (for example TeX Live). Ariadne accepts a
journal-style proof or campaign note only after its generated PDF compiles. Proof
bodies must use portable ASCII LaTeX source: write `\\leq`, `\\mathbb{R}`, and
similar commands rather than Unicode mathematical glyphs. A malformed response is
retained as a non-published source artifact with its render error, so it can be
repaired without being mistaken for a deliverable.

Do not put `LLAMAPARSE_API_KEY` in a project TOML or report. It is read from the
environment and is forwarded only to configured literature-aware roles
(`literature_author`, `literature_researcher`, `literature_sentinel`, and
`proof_expander`). Offline researchers and verifiers do not receive it.

To avoid remote parsing entirely:

```bash
unset LLAMAPARSE_API_KEY
export ARIADNE_PDF_BACKENDS="mineru,pypdf,pdftotext"
```

### Optional local extractors

For openly accessible PDFs cited during literature authoring, Ariadne caches the
PDF and converted Markdown under `.ariadne/literature/cache/`. The same project
reuses these files in later setup and research work without downloading or
converting them again. Paywalls and HTML/article pages are not cached as PDFs.

`pypdf` is installed by `requirements.txt`. For a local MinerU installation,
install its CLI using its supported installation procedure, then either put
`mineru` on `PATH` or set its executable explicitly:

```bash
export ARIADNE_MINERU_BIN="/absolute/path/to/mineru"
```

If Poppler is installed, `pdftotext` is used automatically when listed in the
backend order. None of these tools changes a theorem; extracted text is source
material that still needs mathematical checking.

### Exact symbolic checks with SymPy

SymPy is installed so researchers and verifiers can use exact algebraic checks
when helpful. For example:

```bash
python - <<'SYMPY_CHECK'
from sympy import factor, symbols

a, b = symbols("a b", real=True)
assert factor(a**2 + b**2 - 2*a*b) == (a - b)**2
SYMPY_CHECK
```

This is a sanity check or a component of a verification artifact. It is not a
substitute for a complete proof, and floating-point experiments are never
promoted to deductive evidence.

## 3. Start the sample project

Copy the sample task into a new directory and open the TUI:

```bash
mkdir -p tutorial-amgm
cp examples/tutorial_task.md tutorial-amgm/task.md
ariadne-tui tutorial-amgm
```

Because `tutorial-amgm/.ariadne/` does not exist, Ariadne starts the interactive
setup automatically. At **Task description or task file path**, enter:

```text
./tutorial-amgm/task.md
```

Choose, for example:

```text
Research mode: 2                  # offline_only for the first run
Number of independent agents: 2
Run in parallel: y
Maximum campaign epochs: 2
Maximum provider calls: 12
Maximum campaign cost in USD: 5
```

The contract-author role derives and validates the immutable
`.ariadne/problem_contract.json`; the configuration TOML is completed from the
operator choices. Setup then starts the campaign automatically unless you use
`/setup manual`.

## 4. Work in the TUI

Typing `/` opens the command menu. The most useful commands are:

```text
/run                         start a new campaign or resume a safe interruption
/pause                       request a durable safe pause
/instruct TEXT               add a durable instruction to later agent calls
/budget                      adjust a safely paused or exhausted campaign budget
/artifact next|prev          inspect retained source, outcome, audit, or note artifacts
/report                      write research_report.md and continuation_brief.md
/model                       choose the configured Codex model/reasoning strength
/recover                     recover stale RUNNING state into a safe pause
```

Use the panels for current public tasks, routes, evidence, audit state, failures,
budget, and recent durable events. Select an artifact and scroll its preview for
the full retained content.

Typical output locations are:

```text
tutorial-amgm/
├── ariadne.codex.toml
└── .ariadne/
    ├── problem_contract.json       # frozen per campaign
    ├── artifacts/                  # immutable outcomes, audits, evidence, notes
    ├── literature/                 # dossier and optional restart handoffs
    └── reports/
        ├── research_report.md
        └── continuation_brief.md
```

## 5. Inspect or steer from another terminal

The CLI sees the same durable state as the TUI:

```bash
ariadne campaign status tutorial-amgm
ariadne routes tutorial-amgm
ariadne failures tutorial-amgm
ariadne report tutorial-amgm

ariadne campaign instruct tutorial-amgm \
  --text "Expand every use of nonnegativity and keep the exact real domain."
```

A pause must be respected before changing budget:

```bash
ariadne campaign pause tutorial-amgm --reason "Need another exact route"
ariadne campaign budget tutorial-amgm \
  --max-epochs 4 --max-calls 24 --max-cost-usd 10 \
  --reason "Continue with an independently bounded route"
ariadne campaign resume tutorial-amgm --config tutorial-amgm/ariadne.codex.toml
```

## 6. Restarting an existing project

The contract fingerprint is checked at startup.

- An interrupted campaign with no active human pause resumes automatically.
- An explicit human pause remains paused until the operator resumes it.
- A stale `RUNNING` state needs `/recover`; a budget-exhausted campaign needs
  `/budget`.
- If the prior campaign is terminal, Ariadne displays the path to its
  continuation brief and asks whether to use it as local literature for a fresh
  campaign.

Choosing **yes** registers one project-local continuation source for
literature-aware roles. It contains the route ledger, prior outcomes, linked
artifacts, failure revival conditions, and active instructions. It is
operational context—not a mathematical authority—and every candidate claim must
still be checked against its source artifact. Choosing **no** starts fresh with
the same immutable theorem but without the previous handoff in the literature
dossier.

If the frozen contract differs, Ariadne records `CONTRACT_CHANGED`. Restore the
exact contract to inspect the old campaign, or create a new project for a
revised statement.

## 7. What counts as a result?

- A failed route is not a refutation.
- Numerical and symbolic checks are evidence with only their recorded scope.
- A counterexample produces an audited journal-style note only after both
  independent audits verify the same exact witness.
- A proof candidate is retained and independently audited; Lean is available
  only after a full human proof check.
- When the final epoch ends unsolved, Ariadne writes a journal-style research
  record listing attempted routes, outcomes, failures, and revival conditions.

For configuration details, see [MODES.md](MODES.md), [TUI.md](TUI.md),
[INTERACTIVE_SETUP.md](INTERACTIVE_SETUP.md), and [CODEX.md](CODEX.md).

## Final partial-result synthesis

When the configured epoch bound is reached without resolving the target, Ariadne runs one bounded, web-disabled archival synthesis role. It may retain a precisely scoped, artifact-backed partial result for continuation. If the campaign has no genuinely meaningful partial result, it records that outcome and proposes nothing; it never weakens, proves, or refutes the frozen target by synthesis alone.
