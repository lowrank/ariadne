# Epistemic Model

Ariadne does not use a single confidence score.

## Claim statuses

- `PROPOSED`: statement entered the project.
- `HEURISTIC`: mechanism is plausible but not established.
- `EMPIRICALLY_OBSERVED`: supported on an explicitly recorded computational scope.
- `SOURCE_REPORTED`: exact source claim has been recorded but not necessarily applied.
- `CANDIDATE_LEMMA`: deductive proof candidate exists.
- `AGENT_AUDITED_LOCAL`: one local audit passed.
- `AGENT_AUDITED_GLOBAL`: a fresh complete-proof audit passed.
- `CHALLENGED`: a substantive objection exists.
- `REFUTED`: exact counterexample artifact exists.
- `CONDITIONAL`: proof depends on an unresolved imported assumption.
- `HUMAN_CHECKED`: complete proof checked by a recorded human reviewer.
- `FORMALLY_CERTIFIED`: a pinned formal verification command passed.
- `REVOKED`: evidence or dependencies were invalidated.

## Why evidence types are not linearly ordered

An exhaustive finite search can be conclusive on a finite domain while saying nothing about an infinite theorem. A literature theorem may be stronger than a local calculation but only if assumptions match. A Lean proof certifies the encoded statement but does not by itself certify semantic correspondence with the intended theorem.

## Transition enforcement

Status changes use dedicated operations. For example:

- `REFUTED` requires an exact counterexample artifact.
- `HUMAN_CHECKED` requires a passing human-review record.
- `FORMALLY_CERTIFIED` requires a successful formal verification manifest.

There is no generic `set_status` command.
