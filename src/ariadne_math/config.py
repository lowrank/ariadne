from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


SUPPORTED_MODES = {"offline_sentinel", "offline_only", "literature_guided"}

# Standard API USD-per-million token rates for the bundled GPT-5.6 models.
# They make older Ariadne TOMLs token-metered without a migration. Explicit
# provider TOML values always take precedence, including for other billing tiers.
_DEFAULT_TOKEN_PRICES_PER_MILLION: dict[str, tuple[float, float, float]] = {
    "gpt-5.6-sol": (2.50, 0.25, 15.00),
    "gpt-5.6-terra": (1.25, 0.125, 7.50),
    "gpt-5.6-luna": (0.50, 0.05, 3.00),
}


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str
    command: tuple[str, ...] = ()
    timeout_seconds: int = 900
    estimated_cost_usd: float = 0.0
    # Optional metered prices in USD per million tokens. When a provider does
    # not return an invoice amount, Ariadne settles completed calls from the
    # emitted token usage using these values.
    input_cost_per_million_usd: float | None = None
    cached_input_cost_per_million_usd: float | None = None
    output_cost_per_million_usd: float | None = None
    sandbox_prefix: tuple[str, ...] = ()
    require_os_network_isolation: bool = False
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RoleConfig:
    name: str
    provider: str
    network_policy: str = "inherit"


@dataclass(frozen=True)
class BudgetConfig:
    max_epochs: int = 3
    max_calls: int = 30
    max_cost_usd: float = 25.0
    stagnation_epochs: int = 2
    duplicate_failure_limit: int = 2


@dataclass(frozen=True)
class ModeConfig:
    name: str = "offline_sentinel"
    offline_agents: int = 2
    research_agents: int = 2
    parallel: bool = True
    literature_intervention: bool = True
    require_route_difference_certificate: bool = True
    novelty_deadline_epochs: int = 1
    allow_experiments: bool = False
    route_similarity_threshold: float = 0.82

    @property
    def researcher_count(self) -> int:
        if self.name == "literature_guided":
            return self.research_agents
        return self.offline_agents

    @property
    def researcher_role(self) -> str:
        return (
            "literature_researcher"
            if self.name == "literature_guided"
            else "offline_researcher"
        )

    @property
    def sentinel_enabled(self) -> bool:
        return self.name == "offline_sentinel" and self.literature_intervention


@dataclass(frozen=True)
class HarnessConfig:
    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleConfig]
    budget: BudgetConfig
    mode: ModeConfig

    def provider_for_role(self, role: str) -> ProviderConfig:
        role_cfg = self.roles.get(role)
        if role_cfg is None:
            raise KeyError(f"No provider configured for role {role!r}")
        try:
            return self.providers[role_cfg.provider]
        except KeyError as exc:
            raise KeyError(
                f"Role {role!r} refers to unknown provider {role_cfg.provider!r}"
            ) from exc


_SENSITIVE_CONFIG_ENV_PARTS = (
    "API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "PRIVATE_KEY",
)


def operational_config_snapshot(config: HarnessConfig) -> dict[str, Any]:
    """Return the mutable execution configuration without exposing secrets."""
    def redact_env(key: str, value: str) -> str:
        upper = key.upper()
        return "<redacted>" if any(part in upper for part in _SENSITIVE_CONFIG_ENV_PARTS) else value

    return {
        "budget": {
            "max_epochs": config.budget.max_epochs,
            "max_calls": config.budget.max_calls,
            "max_cost_usd": config.budget.max_cost_usd,
            "stagnation_epochs": config.budget.stagnation_epochs,
            "duplicate_failure_limit": config.budget.duplicate_failure_limit,
        },
        "mode": {
            "name": config.mode.name,
            "offline_agents": config.mode.offline_agents,
            "research_agents": config.mode.research_agents,
            "parallel": config.mode.parallel,
            "literature_intervention": config.mode.literature_intervention,
            "require_route_difference_certificate": config.mode.require_route_difference_certificate,
            "novelty_deadline_epochs": config.mode.novelty_deadline_epochs,
            "allow_experiments": config.mode.allow_experiments,
            "route_similarity_threshold": config.mode.route_similarity_threshold,
        },
        "providers": {
            name: {
                "kind": provider.kind,
                "command": list(provider.command),
                "timeout_seconds": provider.timeout_seconds,
                "estimated_cost_usd": provider.estimated_cost_usd,
                "input_cost_per_million_usd": provider.input_cost_per_million_usd,
                "cached_input_cost_per_million_usd": provider.cached_input_cost_per_million_usd,
                "output_cost_per_million_usd": provider.output_cost_per_million_usd,
                "sandbox_prefix": list(provider.sandbox_prefix),
                "require_os_network_isolation": provider.require_os_network_isolation,
                "env": {
                    key: redact_env(key, value)
                    for key, value in sorted(provider.env.items())
                },
            }
            for name, provider in sorted(config.providers.items())
        },
        "roles": {
            name: {"provider": role.provider, "network_policy": role.network_policy}
            for name, role in sorted(config.roles.items())
        },
    }


def _tuple_strings(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{field_name} must be an array of strings")
    return tuple(value)


def _validate_config(config: HarnessConfig) -> None:
    mode = config.mode
    if mode.name not in SUPPORTED_MODES:
        raise ValueError(
            f"Unsupported mode {mode.name!r}; expected one of {sorted(SUPPORTED_MODES)}"
        )
    if mode.offline_agents < 0 or mode.research_agents < 0:
        raise ValueError("offline_agents and research_agents must be nonnegative")
    if mode.name in {"offline_sentinel", "offline_only"}:
        if mode.offline_agents < 1:
            raise ValueError(f"Mode {mode.name!r} requires offline_agents >= 1")
        if "offline_researcher" not in config.roles:
            raise ValueError(f"Mode {mode.name!r} requires [roles.offline_researcher]")
    if mode.name == "literature_guided":
        if mode.research_agents < 1:
            raise ValueError("Mode 'literature_guided' requires research_agents >= 1")
        if "literature_researcher" not in config.roles:
            raise ValueError(
                "Mode 'literature_guided' requires [roles.literature_researcher]"
            )
    if mode.sentinel_enabled:
        missing = {
            role
            for role in ("literature_sentinel", "intervention_responder")
            if role not in config.roles
        }
        if missing:
            raise ValueError(
                "offline_sentinel mode requires roles: " + ", ".join(sorted(missing))
            )
    if mode.name == "literature_guided" and mode.literature_intervention:
        raise ValueError(
            "literature_guided mode must set literature_intervention=false; "
            "the dossier is shared from the first epoch rather than used for early-stop negotiation"
        )
    if not 0.0 <= mode.route_similarity_threshold <= 1.0:
        raise ValueError("route_similarity_threshold must lie in [0, 1]")
    if config.budget.max_epochs < 1 or config.budget.max_calls < 1:
        raise ValueError("max_epochs and max_calls must be positive")
    if config.budget.max_cost_usd < 0 or not math.isfinite(config.budget.max_cost_usd):
        raise ValueError("max_cost_usd must be a finite nonnegative number")
    if config.budget.stagnation_epochs < 1 or config.budget.duplicate_failure_limit < 1:
        raise ValueError("stagnation_epochs and duplicate_failure_limit must be positive")
    for provider in config.providers.values():
        if provider.kind not in {"command", "mock"}:
            raise ValueError(
                f"Provider {provider.name!r} has unsupported kind {provider.kind!r}"
            )
        if provider.timeout_seconds < 0:
            raise ValueError(
                f"Provider {provider.name!r} timeout_seconds must be nonnegative (0 means unlimited)"
            )
        if provider.estimated_cost_usd < 0 or not math.isfinite(provider.estimated_cost_usd):
            raise ValueError(
                f"Provider {provider.name!r} estimated_cost_usd must be a finite nonnegative number"
            )
        token_prices = (
            provider.input_cost_per_million_usd,
            provider.cached_input_cost_per_million_usd,
            provider.output_cost_per_million_usd,
        )
        if any(value is not None for value in token_prices) and any(value is None for value in token_prices):
            raise ValueError(
                f"Provider {provider.name!r} token pricing requires input, cached-input, and output rates"
            )
        for value in token_prices:
            if value is not None and (value < 0 or not math.isfinite(value)):
                raise ValueError(
                    f"Provider {provider.name!r} token prices must be finite nonnegative USD-per-million values"
                )
        if provider.kind == "command" and not provider.command:
            raise ValueError(f"Command provider {provider.name!r} must set command")
    for role in config.roles.values():
        if role.network_policy not in {"allow", "deny", "inherit"}:
            raise ValueError(
                f"Role {role.name!r} network_policy must be allow, deny, or inherit"
            )


def load_config(path: Path) -> HarnessConfig:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    provider_raw = raw.get("providers", {})
    if not isinstance(provider_raw, dict) or not provider_raw:
        raise ValueError("Configuration must define at least one [providers.<name>] table")
    providers: dict[str, ProviderConfig] = {}
    for name, item in provider_raw.items():
        if not isinstance(item, dict):
            raise ValueError(f"Provider {name!r} must be a table")
        env = {str(k): str(v) for k, v in dict(item.get("env", {})).items()}
        explicit_prices = (
            "input_cost_per_million_usd" in item
            or "cached_input_cost_per_million_usd" in item
            or "output_cost_per_million_usd" in item
        )
        default_prices = _DEFAULT_TOKEN_PRICES_PER_MILLION.get(
            env.get("ARIADNE_CODEX_MODEL", "").strip().casefold()
        ) if not explicit_prices else None
        providers[name] = ProviderConfig(
            name=name,
            kind=str(item.get("kind", "command")),
            command=_tuple_strings(item.get("command"), f"providers.{name}.command"),
            timeout_seconds=int(item.get("timeout_seconds", 900)),
            estimated_cost_usd=float(item.get("estimated_cost_usd", 0.0)),
            input_cost_per_million_usd=(
                float(item["input_cost_per_million_usd"])
                if "input_cost_per_million_usd" in item
                else (default_prices[0] if default_prices else None)
            ),
            cached_input_cost_per_million_usd=(
                float(item["cached_input_cost_per_million_usd"])
                if "cached_input_cost_per_million_usd" in item
                else (default_prices[1] if default_prices else None)
            ),
            output_cost_per_million_usd=(
                float(item["output_cost_per_million_usd"])
                if "output_cost_per_million_usd" in item
                else (default_prices[2] if default_prices else None)
            ),
            sandbox_prefix=_tuple_strings(
                item.get("sandbox_prefix"), f"providers.{name}.sandbox_prefix"
            ),
            require_os_network_isolation=bool(
                item.get("require_os_network_isolation", False)
            ),
            env=env,
        )

    role_raw = raw.get("roles", {})
    if not isinstance(role_raw, dict) or not role_raw:
        raise ValueError("Configuration must define [roles.<role>] tables")
    roles: dict[str, RoleConfig] = {}
    for name, item in role_raw.items():
        if not isinstance(item, dict):
            raise ValueError(f"Role {name!r} must be a table")
        roles[name] = RoleConfig(
            name=name,
            provider=str(item["provider"]),
            network_policy=str(item.get("network_policy", "inherit")),
        )

    budget_data = dict(raw.get("budget", {}))
    budget = BudgetConfig(
        max_epochs=int(budget_data.get("max_epochs", 3)),
        max_calls=int(budget_data.get("max_calls", 30)),
        max_cost_usd=float(budget_data.get("max_cost_usd", 25.0)),
        stagnation_epochs=int(budget_data.get("stagnation_epochs", 2)),
        duplicate_failure_limit=int(budget_data.get("duplicate_failure_limit", 2)),
    )

    mode_data = dict(raw.get("mode", {}))
    mode_name = str(mode_data.get("name", "offline_sentinel"))
    # Keep the configuration pleasant to author by hand: sentinel intervention
    # defaults on only for the mode that actually has a hidden sentinel. Older
    # configs that set these fields explicitly retain exactly their behaviour.
    default_intervention = mode_name == "offline_sentinel"
    mode = ModeConfig(
        name=mode_name,
        offline_agents=int(mode_data.get("offline_agents", 2)),
        research_agents=int(mode_data.get("research_agents", 2)),
        parallel=bool(mode_data.get("parallel", True)),
        literature_intervention=bool(
            mode_data.get("literature_intervention", default_intervention)
        ),
        require_route_difference_certificate=bool(
            mode_data.get(
                "require_route_difference_certificate", default_intervention
            )
        ),
        novelty_deadline_epochs=int(mode_data.get("novelty_deadline_epochs", 1)),
        allow_experiments=bool(mode_data.get("allow_experiments", False)),
        route_similarity_threshold=float(
            mode_data.get("route_similarity_threshold", 0.82)
        ),
    )

    config = HarnessConfig(providers=providers, roles=roles, budget=budget, mode=mode)
    _validate_config(config)
    return config
