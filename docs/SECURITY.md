# Security

Ariadne controls orchestration state; it is not a complete untrusted-code sandbox.

## Built-in protections

- subprocess argument arrays; no `shell=True`;
- role-specific provider configuration;
- common API keys, tokens, secrets, passwords, credentials, private-key variables, and proxy variables are removed from offline-agent environments;
- literature is not placed in offline prompts;
- optional fail-closed requirement for an OS sandbox prefix;
- immutable, content-addressed run artifacts;
- recorded network policy and isolation status for every agent run.

## What remains the operator’s responsibility

- network namespace or container isolation;
- filesystem mounts and write restrictions;
- CPU, memory, process, and runtime limits;
- safe handling of model-written code;
- credentials for any explicitly literature-aware author, researcher, or
  sentinel role;
- reviewing provider commands before execution.

A strict deployment should run offline researchers in separate containers with
no network and a minimal read/write workspace. Literature-aware roles should run
in distinct environments. In `offline_sentinel`, the sentinel communicates with
offline researchers only through structured route interventions; in
`literature_guided`, the shared dossier is deliberately visible from epoch one.

## Codex CLI roles

The packaged Codex provider adds defense-in-depth controls for mathematical role
calls:

- a fresh temporary workspace with no project files;
- `--sandbox read-only` and `--ask-for-approval never`;
- `--ignore-user-config` and `--ignore-rules`;
- web search forced to `disabled` for contract, offline-research, responder,
  verifier, and conceptual-pivot roles; only the explicitly literature-aware
  author/researcher/sentinel roles can opt in;
- Codex subagents, memories, shell tools, login-shell semantics, and reasoning
  output disabled;
- an ephemeral session and a strict role-specific output schema;
- secret-variable redaction for Ariadne roles with `network_policy = "deny"`,
  while preserving `CODEX_HOME` for saved Codex authentication.

A hosted Codex invocation still needs network access from the CLI to the model
service. Therefore this mode is *literature-offline and tool-isolated*, not a
host-level disconnected process. Strict physical network denial is possible only
with a local model/provider or a network architecture that separately permits the
model endpoint while denying all other egress. See `docs/CODEX.md`.
