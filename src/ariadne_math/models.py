from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def state(self) -> Path:
        return self.root / ".ariadne"

    @property
    def database(self) -> Path:
        return self.state / "state.sqlite"

    @property
    def events(self) -> Path:
        return self.state / "events.jsonl"

    @property
    def artifacts(self) -> Path:
        return self.state / "artifacts"

    @property
    def literature(self) -> Path:
        return self.state / "literature"

    @property
    def reports(self) -> Path:
        return self.state / "reports"

    @property
    def formal(self) -> Path:
        return self.state / "formal"

    @property
    def contract(self) -> Path:
        return self.state / "problem_contract.json"


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    # None distinguishes an omitted provider cost from an explicitly reported
    # zero-cost local/mock call. The runner falls back to the configured
    # estimate only when this value is absent.
    reported_cost_usd: float | None = None


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    usage: Usage
    returncode: int = 0
    stderr: str = ""


@dataclass(frozen=True)
class AgentCall:
    role: str
    slot: str
    prompt: str
    project_root: Path
    network_policy: str
    campaign_id: str | None = None
    route_id: str | None = None
    epoch: int | None = None
    metadata: dict[str, Any] | None = None
