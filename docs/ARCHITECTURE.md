# Architecture

## Layers

```text
Human mathematician
        |
Problem contract (immutable)
        |
Campaign controller (persistent, no claim promotion authority)
        |
Small route portfolio
  |          |            |
offline   refutation   conceptual
research      route       pivot
  \          |            /
   claim + route + failure + evidence + decision database
                        |
              literature sentinel
             (intervention only)
                        |
             local and global audits
                        |
                 human proof check
                        |
               late formal verification
```

## Why only the controller persists

Persistent swarms are expensive and encourage activity without mathematical leverage. Ariadne invokes workers for bounded epochs. The controller keeps the durable state, detects duplicate failures, and decides whether another call has a decisive expected outcome.

## Authority boundaries

- Offline researchers may propose routes, claims, failures, proof candidates, and counterexample candidates.
- The literature sentinel may propose interventions but cannot stop routes directly.
- The controller may allocate budget, freeze routes, and enforce deadlines but cannot promote a claim to human-checked or formally certified.
- Auditors write audit records only.
- A human review opens the formalization gate.
- A successful external verification command produces the formal certification artifact.

## Historical and present-facing state

`events.jsonl` preserves the history. SQLite provides a present-facing indexed view. Artifacts are content-addressed so prompts, responses, audits, and verification manifests are immutable and deduplicated.

## Main graphs

### Claim graph

Records exact mathematical statements and logical dependencies.

### Route graph

Records mechanisms, representations, key bridge lemmas, decisive tests, and independence clusters.

### Failure graph

Compresses repeated attempts into canonical obstructions with precise logical scope and revival conditions.

### Evidence graph

Separates proof, counterexample, finite search, interval certificate, floating-point experiment, source theorem, LLM audit, human review, and formal proof.

### Decision log

Records why the controller allocated resources, what decisive event was expected, the cost cap, and the stop rule.

## Live activity and human control plane

The controller exposes only high-level public state: target, epoch plan, active
role/task, heartbeat, declared summaries, interventions, audit verdicts, and
budget. It never exposes hidden model reasoning.

Human control is durable rather than conversationally ephemeral:

- `campaign_controls` records cooperative pause requests;
- `human_instructions` records campaign-wide or route-specific instructions and
  their intended audience;
- explicit route-status changes enter the decision log;
- active agent runs record epoch and task summary so another terminal can inspect
  current activity;
- resume preserves the same campaign, graph state, failure clusters, and budget.

A pause is honored at a safe boundary. Active bounded work is recorded. A proof-audit chain already triggered by a returned proof candidate may finish atomically; the controller otherwise starts no later campaign stage after observing the request.
Instructions are injected into future matching prompts, but the immutable problem
contract and epistemic transition rules always take precedence.
