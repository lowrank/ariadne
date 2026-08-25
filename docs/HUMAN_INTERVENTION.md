# Live activity and human intervention

Ariadne campaign runs are visible and steerable. The controller reports public,
high-level activity while keeping model scratch work and hidden reasoning out of
the terminal and database.

## Live output

By default, `ariadne campaign run` writes activity to **stderr** and the final
campaign JSON to **stdout**. This keeps the final result scriptable while showing:

- the immutable target and campaign mode;
- the epoch number, route count, and failure-cluster count;
- the planned role calls and their current tasks;
- agent start, finish, elapsed time, token use, and recorded cost;
- periodic heartbeats while a provider is active;
- concise agent result summaries and next tasks;
- decisive-event and failure classifications;
- literature-sentinel interventions and arbitration;
- local and fresh-global verifier verdicts;
- conceptual-pivot requests;
- per-epoch and final budget summaries.

Ariadne does **not** print private chain-of-thought. The display contains only
controller state, task descriptions, declared result summaries, and public audit
objects.

```bash
ariadne campaign run ./my-project \
  --config ./my-project/ariadne.codex.toml
```

Change the heartbeat interval:

```bash
ariadne campaign run ./my-project \
  --config ./my-project/ariadne.codex.toml \
  --heartbeat-seconds 10
```

Machine-readable activity events:

```bash
ariadne campaign run ./my-project \
  --config ./my-project/ariadne.codex.toml \
  --json-events 2>activity.jsonl >final-result.json
```

Suppress live activity:

```bash
ariadne campaign run ./my-project \
  --config ./my-project/ariadne.codex.toml \
  --quiet
```

## Safe pause from another terminal

A pause is **cooperative and fail-safe**. Ariadne does not start a new bounded
role call after noticing the request. Calls already running are allowed to finish so their result and cost record are not lost. If such a call has already produced a proof candidate, its configured local/global audit chain may finish as one atomic stage. The controller then enters
`PAUSED_HUMAN` at the next safe checkpoint.

Terminal 1:

```bash
ariadne campaign run ./my-project \
  --config ./my-project/ariadne.codex.toml
```

Terminal 2:

```bash
ariadne campaign pause ./my-project \
  --reason "I want to redirect the proof architecture before the next epoch"
```

During a long call, the heartbeat reports `pause pending`. Wait until:

```bash
ariadne campaign status ./my-project
```

shows:

```json
{"status": "PAUSED_HUMAN"}
```

The status command also reports active agent runs, the pause request, active
human instructions, and recent attempts.

## Adjust a paused or exhausted campaign budget

Once the status is `PAUSED_HUMAN` or `BUDGET_EXHAUSTED`, adjust one or more limits on the same
campaign. An exhausted campaign is reopened as `PAUSED_HUMAN`, ready for an explicit resume. The command rejects ceilings below work already consumed, and records
the old limits, new limits, operator, and rationale as a durable decision.

```bash
ariadne campaign budget ./my-project \
  --max-epochs 5 \
  --max-calls 36 \
  --max-cost-usd 45.00 \
  --reason "Fund one additional independent route and its audits"
```

Then resume normally:

```bash
ariadne campaign resume ./my-project \
  --config ./my-project/ariadne.codex.toml
```

Do not edit `.ariadne/state.sqlite` directly. Changing the TOML controls future
provider settings (such as cost estimates), whereas this command changes the
stored limits of the current campaign.

## Revise operational TOML settings

Changing the TOML between epochs is supported. Pause when necessary, edit such
operational settings as model, reasoning strength, agent count, provider cost
estimate, timeout, or network policy, then resume. Ariadne records a redacted
configuration revision and its setting-level diff on the next resume; secret
values are never stored in the revision history. The immutable problem contract
is not changed by this workflow.

## Add persistent instructions

Campaign-wide instruction for researchers:

```bash
ariadne campaign instruct ./my-project \
  --text "Try the dual operator identity; do not repeat the absolute-value dyadic estimate."
```

Route-specific instruction:

```bash
ariadne campaign instruct ./my-project \
  --route RTE-... \
  --text "First prove the cancellation identity exactly; do not optimize constants yet."
```

Read a longer instruction from a file:

```bash
ariadne campaign instruct ./my-project \
  --file ./steering-note.md \
  --audience researchers
```

Audience choices are:

```text
all
researchers
sentinel
verifiers
```

An instruction is injected into future matching prompts inside a
`<HUMAN_INTERVENTIONS>` block. Already-running calls are unchanged. The immutable
problem contract and epistemic policy override any conflicting instruction.

Request a pause and add the instruction in one command:

```bash
ariadne campaign instruct ./my-project \
  --text "Switch to the variational formulation before further estimates." \
  --pause
```

List instructions:

```bash
ariadne campaign instructions ./my-project
```

Include retired instructions:

```bash
ariadne campaign instructions ./my-project --include-retired
```

Retire an instruction:

```bash
ariadne campaign retire-instruction ./my-project --id HIN-...
```

## Explicit route control

A human can park or reactivate a route without asking an LLM to interpret a prose
instruction:

```bash
ariadne campaign route-status ./my-project \
  --route RTE-... \
  --status PARKED \
  --note "This is the same failure cluster as the old route."
```

Reactivate after adding a genuinely new instruction:

```bash
ariadne campaign route-status ./my-project \
  --route RTE-... \
  --status ACTIVE \
  --note "New bridge lemma supplied by the owner."
```

Permitted human statuses are:

```text
ACTIVE
PARKED
OBSOLETE
NEEDS_HUMAN_IDEA
NEEDS_REPRESENTATION_CHANGE
```

Every route-control action is stored in the decision log.

## Resume

After adding instructions or changing route state:

```bash
ariadne campaign resume ./my-project \
  --config ./my-project/ariadne.codex.toml
```

`resume` clears the pause request, reuses the same campaign ID, preserves all
claims, routes, failures, interventions, costs, and attempts, and starts at the
next research epoch.

The older equivalent remains available:

```bash
ariadne campaign run ./my-project \
  --config ./my-project/ariadne.codex.toml \
  --resume
```

## Single-terminal interactive mode

For a terminal-attached session, request an epoch-boundary prompt:

```bash
ariadne campaign run ./my-project \
  --config ./my-project/ariadne.codex.toml \
  --interactive
```

At each completed epoch:

```text
Enter   continue
 i      add a campaign-wide researcher instruction
 p      pause
```

For long Codex calls, the two-terminal pause mechanism is preferable because it
can record the pause request while the call is still running.

## Keyboard interruption

`Ctrl+C` records `PAUSED_HUMAN` and a pause request when possible. Unlike the
cooperative two-terminal command, it may also interrupt the current provider
process. Inspect `campaign status` and the most recent agent run before resuming.
Use `campaign pause` from another terminal when preservation of the active call
is important.
