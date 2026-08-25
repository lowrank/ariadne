# Cost and Stopping Policy

## Decisive events

A task counts as mathematical progress only when it produces at least one of:

- a load-bearing lemma;
- an exact counterexample;
- an exact reduction;
- a new representation outside a failed method family;
- elimination of a route;
- a precise unresolved bridge;
- an exact source match.

More prose, larger numerical ranges, and repeated verifier objections do not count.

## Route stagnation

Each active route tracks epochs without a decisive event. At the configured threshold it becomes `NEEDS_REPRESENTATION_CHANGE`. Retrying requires a novelty certificate.

## Duplicate failures

Failure clustering uses class, obstruction signature, and logical scope. At the duplicate limit, a route is frozen unless a new representation, key lemma, source theorem, auxiliary object, or falsification test is supplied.

## Verification budget

Verification is reserved for promotable claims and assembled proofs. Scratch mathematics is not sent repeatedly to an expensive verifier. A verifier rejection is classified; only exposition-level defects receive one local repair. Structural defects return to the route engine.

## Project completion

A campaign can terminate because:

- the budget is exhausted;
- all routes are paused or blocked;
- a proof candidate exists;
- a counterexample candidate exists;
- human adjudication is required;
- the epoch cap is reached.

None of these automatically changes the mathematical problem status.
