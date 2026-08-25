# Upgrade to 0.4.0

```bash
pip install --upgrade ariadne_math_harness-0.4.0-py3-none-any.whl
```

Existing SQLite dossiers migrate automatically; the new `task_queue` table is created without rewriting claims, routes, failures, interventions, or reviews.

For an older Codex config, add these roles:

```toml
[roles.literature_researcher]
provider = "codex_literature"
network_policy = "allow"

[roles.contract_author]
provider = "codex_offline"
network_policy = "deny"

[roles.literature_author]
provider = "codex_literature"
network_policy = "allow"
```

Add `research_agents` to `[mode]`:

```toml
research_agents = 2
```

For `literature_guided`, set:

```toml
name = "literature_guided"
research_agents = 2
literature_intervention = false
require_route_difference_certificate = false
```

Then open:

```bash
ariadne tui ./my-project --config ./my-project/ariadne.codex.toml
```
