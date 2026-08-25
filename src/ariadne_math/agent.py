from __future__ import annotations

from dataclasses import asdict
import time

from .activity import ActivityReporter, NullActivityReporter
from .artifacts import ArtifactStore
from .config import HarnessConfig
from .models import AgentCall, ProviderResponse
from .providers import ProviderError, create_provider
from .store import ResearchStore
from .util import extract_json_object


class BudgetExceeded(RuntimeError):
    pass


class AgentRunner:
    def __init__(
        self,
        store: ResearchStore,
        config: HarnessConfig,
        reporter: ActivityReporter | None = None,
    ):
        self.store = store
        self.config = config
        self.artifacts = ArtifactStore(store.paths)
        self.reporter = reporter or NullActivityReporter()

    def call(self, call: AgentCall) -> ProviderResponse:
        role_cfg = self.config.roles.get(call.role)
        if role_cfg is None:
            raise KeyError(f"No configuration for role {call.role!r}")
        provider_cfg = self.config.provider_for_role(call.role)
        instructions = self.store.instructions_for_agent(
            campaign_id=call.campaign_id,
            role=call.role,
            route_id=call.route_id,
        )
        prompt_text = self._prompt_with_human_instructions(call.prompt, instructions)
        artifact_context = self._artifact_context_for_call(call)
        if artifact_context:
            prompt_text += (
                "\n\n# Local artifact context\n"
                "A curated, read-only snapshot of the referenced project material is available "
                "inside this invocation at ariadne-context/. Use ariadne-context/MANIFEST.json "
                "to map IDs to files, inspect an exact file only when needed, and do not write there.\n"
            )
        prompt_artifact = self.artifacts.put_text(
            prompt_text,
            kind="agent_prompt",
            suffix=".md",
            metadata={
                "role": call.role,
                "slot": call.slot,
                "network_policy": call.network_policy,
                "route_id": call.route_id,
                "epoch": call.epoch,
            },
        )
        self.store.record_artifact(prompt_artifact)
        isolation_status = self._isolation_status(provider_cfg.kind, call.network_policy, bool(provider_cfg.sandbox_prefix))
        task_summary = str(
            (call.metadata or {}).get("task_summary", "bounded mathematical role task")
        )
        task_id: str | None = None
        if call.campaign_id:
            raw_task_id = (call.metadata or {}).get("task_id")
            if raw_task_id:
                task_id = str(raw_task_id)
            else:
                task_id = self.store.add_task(
                    campaign_id=call.campaign_id,
                    epoch=int(call.epoch or 0),
                    slot=call.slot,
                    role=call.role,
                    route_id=call.route_id,
                    summary=task_summary,
                )

        run_id = self.store.start_agent_run(
            campaign_id=call.campaign_id,
            role=call.role,
            slot=call.slot,
            route_id=call.route_id,
            epoch=call.epoch,
            task_summary=task_summary,
            provider=provider_cfg.name,
            network_policy=call.network_policy,
            isolation_status=isolation_status,
            prompt_artifact_id=prompt_artifact.artifact_id,
        )

        reserved_cost_usd = provider_cfg.estimated_cost_usd
        if call.campaign_id and not self.store.reserve_budget(
            call.campaign_id,
            reservation_id=run_id,
            estimated_cost_usd=reserved_cost_usd,
        ):
            self.store.finish_agent_run(
                run_id,
                status="SKIPPED_BUDGET",
                response_artifact_id=None,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
            )
            if task_id:
                self.store.finish_task(task_id, status="CANCELLED")
            raise BudgetExceeded(
                f"Campaign {call.campaign_id} has no budget for another {call.role} call"
            )

        if task_id:
            self.store.start_task(task_id, run_id=run_id)

        self.reporter.call_started(
            run_id=run_id,
            role=call.role,
            slot=call.slot,
            route_id=call.route_id,
            provider=provider_cfg.name,
            task=task_summary,
            instruction_count=len(instructions),
        )
        started = time.monotonic()
        provider = create_provider(provider_cfg)
        effective_call = AgentCall(
            role=call.role,
            slot=call.slot,
            prompt=prompt_text,
            project_root=call.project_root,
            network_policy=call.network_policy,
            campaign_id=call.campaign_id,
            route_id=call.route_id,
            epoch=call.epoch,
            metadata={
                **(call.metadata or {}),
                "artifact_context": artifact_context,
            },
        )
        try:
            response = provider.run(effective_call)
        except Exception as exc:
            elapsed = time.monotonic() - started
            self.reporter.call_failed(
                run_id=run_id,
                role=call.role,
                slot=call.slot,
                route_id=call.route_id,
                elapsed_seconds=elapsed,
                error=f"{type(exc).__name__}: {exc}",
            )
            final_cost_usd = reserved_cost_usd
            self.store.finish_agent_run(
                run_id,
                status="FAILED",
                response_artifact_id=None,
                input_tokens=0,
                output_tokens=0,
                cost_usd=final_cost_usd,
            )
            if task_id:
                self.store.finish_task(task_id, status="FAILED")
            if call.campaign_id:
                self.store.settle_budget(run_id, settled_cost_usd=final_cost_usd)
            raise

        try:
            structured_response = extract_json_object(response.text)
        except (TypeError, ValueError):
            structured_response = {}
        for event in structured_response.get("tool_activity", []):
            if isinstance(event, dict):
                kind = str(event.get("kind", "research_tool"))
                detail = str(event.get("message", kind))
                self.reporter.emit(
                    "research_tool",
                    f"{call.slot}: {detail}",
                    run_id=run_id,
                    role=call.role,
                    slot=call.slot,
                    route_id=call.route_id,
                    tool=kind,
                    **{key: value for key, value in event.items() if key not in {"kind", "message"}},
                )

        response_artifact = self.artifacts.put_text(
            response.text,
            kind="agent_response",
            suffix=".md",
            metadata={
                "role": call.role,
                "slot": call.slot,
                "route_id": call.route_id,
                "epoch": call.epoch,
                "usage": asdict(response.usage),
            },
        )
        self.store.record_artifact(response_artifact)
        final_cost_usd = self._settled_cost_usd(
            provider_cfg=provider_cfg,
            response=response,
            reserved_cost_usd=reserved_cost_usd,
        )
        self.store.finish_agent_run(
            run_id,
            status="SUCCEEDED",
            response_artifact_id=response_artifact.artifact_id,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=final_cost_usd,
        )
        if task_id:
            self.store.finish_task(task_id, status="COMPLETED")
        if call.campaign_id:
            self.store.settle_budget(run_id, settled_cost_usd=final_cost_usd)
        elapsed = time.monotonic() - started
        self.reporter.call_finished(
            run_id=run_id,
            role=call.role,
            slot=call.slot,
            route_id=call.route_id,
            elapsed_seconds=elapsed,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=final_cost_usd,
        )
        if instructions:
            self.store.events.append(
                "human_instructions_applied",
                {
                    "run_id": run_id,
                    "campaign_id": call.campaign_id,
                    "role": call.role,
                    "route_id": call.route_id,
                    "instruction_ids": [item["instruction_id"] for item in instructions],
                },
            )
        return response

    @staticmethod
    def _settled_cost_usd(
        *, provider_cfg, response: ProviderResponse, reserved_cost_usd: float
    ) -> float:
        """Use an invoice, then metered token pricing, then a conservative reserve.

        A reservation is only a concurrency safeguard. Codex CLI currently
        exposes usage counts rather than an invoice total, so configured token
        prices settle a successful call to its measured input/cache/output use.
        """
        usage = response.usage
        if usage.reported_cost_usd is not None:
            return float(usage.reported_cost_usd)
        prices = (
            provider_cfg.input_cost_per_million_usd,
            provider_cfg.cached_input_cost_per_million_usd,
            provider_cfg.output_cost_per_million_usd,
        )
        observed_tokens = usage.input_tokens + usage.output_tokens
        if observed_tokens and all(price is not None for price in prices):
            cached_input = min(max(0, usage.cached_input_tokens), usage.input_tokens)
            uncached_input = usage.input_tokens - cached_input
            return (
                uncached_input * float(prices[0])
                + cached_input * float(prices[1])
                + usage.output_tokens * float(prices[2])
            ) / 1_000_000
        return reserved_cost_usd

    def _artifact_context_for_call(self, call: AgentCall) -> list[dict[str, str]]:
        # These are copied into a per-invocation scratch snapshot by the Codex
        # wrapper. The compact prompt still carries the index, so exact files are
        # opened only on demand rather than consuming every role's context.
        records: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(record: dict[str, object], identifier: str) -> None:
            relative = str(record.get("relative_path", "")).strip()
            if not relative or relative in seen:
                return
            seen.add(relative)
            records.append(
                {
                    "id": identifier,
                    "kind": str(record.get("kind", "artifact")),
                    "relative_path": relative,
                }
            )

        literature_roles = {
            "literature_researcher",
            "literature_author",
            "literature_sentinel",
            "contract_resolver",
            "proof_expander",
        }
        for artifact in self.store.list_artifacts(limit=24):
            metadata = artifact.get("metadata", {})
            if (
                call.role == "offline_researcher"
                and (
                    str(metadata.get("role", "")) in literature_roles
                    or "literature" in str(artifact.get("kind", "")).lower()
                )
            ):
                continue
            add(artifact, str(artifact.get("artifact_id", "")))
        if call.role in literature_roles:
            query = call.prompt
            if call.route_id:
                try:
                    route = self.store.get_route(call.route_id)
                    query = " ".join(
                        str(route.get(field, ""))
                        for field in (
                            "title",
                            "method_family",
                            "representation",
                            "key_lemma",
                            "central_mechanism",
                            "decisive_test",
                        )
                    )
                except KeyError:
                    pass
            for source in self.store.select_literature_sources(query=query, limit=12):
                add(
                    {
                        "kind": "literature_source",
                        "relative_path": source.get("relative_path", ""),
                    },
                    str(source.get("source_id", "")),
                )
        return records[:36]

    @staticmethod
    def _prompt_with_human_instructions(
        prompt: str, instructions: list[dict[str, object]]
    ) -> str:
        if not instructions:
            return prompt
        lines = [
            "<HUMAN_INTERVENTIONS>",
            "The following are explicit owner instructions added after campaign start.",
            "Follow them unless they conflict with the immutable problem contract or epistemic policy.",
        ]
        for item in instructions:
            route = f" route={item.get('route_id')}" if item.get("route_id") else ""
            lines.append(
                f"- [{item.get('instruction_id')} audience={item.get('audience')}{route}] "
                f"{item.get('instruction_text')}"
            )
        lines.append("</HUMAN_INTERVENTIONS>")
        return "\n".join(lines) + "\n\n" + prompt

    @staticmethod
    def _isolation_status(provider_kind: str, network_policy: str, has_prefix: bool) -> str:
        if provider_kind == "mock":
            return "MOCK_ISOLATED"
        if network_policy != "deny":
            return "NETWORK_ALLOWED_BY_ROLE"
        if has_prefix:
            return "OS_SANDBOX_CONFIGURED"
        return "PROTOCOL_ONLY_NOT_OS_ENFORCED"
