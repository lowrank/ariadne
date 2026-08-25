from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .activity import ActivityReporter, NullActivityReporter
from .agent import AgentRunner, BudgetExceeded
from .artifacts import ArtifactStore
from .config import HarnessConfig, operational_config_snapshot
from .enums import (
    AuditVerdict,
    CampaignStatus,
    ClaimStatus,
    EvidenceType,
    FailureClass,
    InterventionKind,
    RouteMode,
    RouteStatus,
)
from .failures import fingerprint_failure
from .models import AgentCall
from .prompt_loader import render_prompt
from .resources import assess_experiment_resources
from .reports import (
    LatexValidationError,
    validate_proof_latex,
    write_agent_audited_counterexample_note,
    write_agent_audited_proof_report,
    write_proof_candidate_note,
    write_unsolved_campaign_note,
)
from .sentinel import (
    arbitrate_intervention, early_stop_has_exact_literature_evidence,
    novelty_evidence_satisfies,
)
from .store import ResearchStore
from .successor import record_campaign_epoch
from .transitions import InvalidTransition, transition_claim
from .util import (
    canonical_json,
    content_hash,
    extract_json_object,
    jaccard_similarity,
    normalize_signature,
    read_json,
    write_json,
)


_DECISIVE_TYPES = {
    "LOAD_BEARING_LEMMA",
    "COUNTEREXAMPLE",
    "EXACT_REDUCTION",
    "NEW_REPRESENTATION",
    "ROUTE_ELIMINATED",
    "SOURCE_MATCH",
    "PRECISE_BRIDGE",
}


class CampaignController:
    def __init__(
        self,
        project_root: Path,
        config: HarnessConfig,
        reporter: ActivityReporter | None = None,
        config_path: Path | None = None,
    ):
        self.store = ResearchStore(project_root)
        self.config = config
        self.config_path = config_path.resolve() if config_path is not None else None
        self.reporter = reporter or NullActivityReporter()
        self.runner = AgentRunner(self.store, config, reporter=self.reporter)
        self.artifacts = ArtifactStore(self.store.paths)

    def run(self, *, new_campaign: bool = True) -> dict[str, Any]:
        # Protect the entire admission sequence, including campaign creation
        # and selecting the next epoch. Without this, two TUI sessions can
        # both read the same campaign and independently schedule the same
        # literature slots.
        with self.store.campaign_controller_lock():
            return self._run_locked(new_campaign=new_campaign)

    def _run_locked(self, *, new_campaign: bool) -> dict[str, Any]:
        try:
            contract = self._load_contract(seal_if_unstarted=True)
        except RuntimeError as exc:
            if "Problem contract fingerprint mismatch" in str(exc):
                self._mark_latest_campaign_contract_changed(str(exc))
            raise
        root_claim_id = self._ensure_root_claim(contract)
        latest = self.store.latest_campaign()
        if new_campaign or not latest:
            campaign_id = self.store.create_campaign(
                mode=self.config.mode.name,
                max_epochs=self.config.budget.max_epochs,
                max_calls=self.config.budget.max_calls,
                max_cost_usd=self.config.budget.max_cost_usd,
            )
            start_epoch = 1
            resumed = False
        else:
            campaign_id = str(latest["campaign_id"])
            start_epoch = max(1, int(latest.get("epoch", 0)) + 1)
            resumed = True

        config_revision = self._record_operational_config_revision(
            campaign_id=campaign_id, resumed=resumed
        )
        campaign_budget = self.store.get_campaign(campaign_id)
        max_epochs = int(campaign_budget["max_epochs"])
        max_calls = int(campaign_budget["max_calls"])
        max_cost_usd = float(campaign_budget["max_cost_usd"])
        self.store.update_campaign(campaign_id, status=CampaignStatus.RUNNING)
        self.reporter.start(
            campaign_id=campaign_id,
            control_provider=lambda: self.store.get_campaign_control(campaign_id),
            budget_provider=lambda: self.store.get_campaign(campaign_id),
        )
        statement = self._contract_statement(contract).replace("\n", " ").strip()
        if len(statement) > 320:
            statement = statement[:317] + "..."
        self.reporter.emit(
            "campaign_resumed" if resumed else "campaign_started",
            f"{campaign_id} in mode {self.config.mode.name}; target: {statement}",
            start_epoch=start_epoch,
            max_epochs=max_epochs,
            max_calls=max_calls,
            max_cost_usd=max_cost_usd,
        )
        if config_revision["recorded"]:
            revision = config_revision["revision"]
            changes = config_revision["changes"]
            if resumed and int(revision["revision_number"]) > 1:
                changed_keys = sorted(changes)
                preview = ", ".join(changed_keys[:6]) if changed_keys else "source file only"
                if len(changed_keys) > 6:
                    preview += f" (+{len(changed_keys) - 6} more)"
                self.reporter.emit(
                    "campaign_config_revision",
                    f"Operational configuration revision {revision['revision_number']} recorded: {preview}.",
                    revision_id=revision["revision_id"],
                    revision_number=revision["revision_number"],
                    changes=changes,
                )
            elif not resumed:
                self.reporter.emit(
                    "campaign_config_snapshot",
                    "Initial redacted operational configuration snapshot recorded.",
                    revision_id=revision["revision_id"],
                    revision_number=revision["revision_number"],
                )

        self.reporter.emit(
            "human_controls",
            "To intervene from another terminal: `ariadne campaign budget PROJECT --max-cost-usd AMOUNT --reason TEXT` "
            "takes effect before the next epoch; `ariadne campaign pause PROJECT` remains available for a safe stop.",
        )
        active_instructions = self.store.list_human_instructions(
            campaign_id, active_only=True
        )
        if active_instructions:
            self.reporter.emit(
                "human_instruction_state",
                "; ".join(
                    f"{item['instruction_id']} [{item['audience']}] "
                    f"{str(item['instruction_text'])[:180]}"
                    for item in active_instructions
                ),
                instruction_count=len(active_instructions),
            )

        terminal_status: str | None = None
        try:
            if self._pause_if_requested(campaign_id, checkpoint="campaign start"):
                terminal_status = CampaignStatus.PAUSED_HUMAN

            if terminal_status is None:
                for epoch in range(start_epoch, 1_000_001):
                    if self._pause_if_requested(campaign_id, checkpoint=f"before epoch {epoch}"):
                        terminal_status = CampaignStatus.PAUSED_HUMAN
                        break
                    applied_actions = self.store.apply_scheduled_campaign_actions(campaign_id)
                    for action in applied_actions:
                        outcome = action["outcome"]
                        if action["status"] == "APPLIED":
                            self.reporter.emit(
                                "scheduled_control_applied",
                                f"Applied {action['kind']} control before epoch {epoch}.",
                                epoch=epoch,
                                action_id=action["action_id"],
                                kind=action["kind"],
                                outcome=outcome,
                            )
                        else:
                            self.reporter.emit(
                                "scheduled_control_rejected",
                                f"Could not apply {action['kind']} control before epoch {epoch}: "
                                f"{outcome.get('error', 'unknown validation error')}",
                                epoch=epoch,
                                action_id=action["action_id"],
                                kind=action["kind"],
                                outcome=outcome,
                            )
                    boundary_budget = self.store.get_campaign(campaign_id)
                    max_epochs = int(boundary_budget["max_epochs"])
                    if not self.store.budget_available(campaign_id):
                        terminal_status = CampaignStatus.BUDGET_EXHAUSTED
                        self.reporter.emit(
                            "budget_exhausted",
                            "No budget remains for another bounded agent call.",
                            epoch=epoch,
                        )
                        break
                    if epoch > max_epochs:
                        break
                    self.store.update_campaign(campaign_id, epoch=epoch)
                    active_routes = self.store.list_routes(campaign_id, active_only=True)
                    self.reporter.emit(
                        "epoch_started",
                        f"Epoch {epoch}/{max_epochs}; "
                        f"{len(active_routes)} active route(s), "
                        f"{len(self.store.list_failures())} failure cluster(s).",
                        epoch=epoch,
                        active_routes=len(active_routes),
                    )
                    calls = self._build_research_calls(
                        campaign_id=campaign_id,
                        epoch=epoch,
                        contract=contract,
                        root_claim_id=root_claim_id,
                        active_routes=active_routes,
                    )
                    if not calls:
                        terminal_status = CampaignStatus.COMPLETED_UNSOLVED
                        self.reporter.emit(
                            "no_tasks",
                            "No admissible research task remains under the current route and stopping policies.",
                            epoch=epoch,
                        )
                        self._record_epoch_git_snapshot(campaign_id=campaign_id, epoch=epoch)
                        break

                    plan_items = [
                        {
                            "slot": call.slot,
                            "role": call.role,
                            "route_id": call.route_id,
                            "task": str((call.metadata or {}).get("task_summary", "bounded role task")),
                        }
                        for call in calls
                    ]
                    self.reporter.emit(
                        "epoch_plan",
                        "; ".join(
                            f"{item['slot']} [{item['role']}]: {item['task']}"
                            for item in plan_items
                        ),
                        epoch=epoch,
                        tasks=plan_items,
                    )

                    calls = self._queue_calls(campaign_id, epoch, calls)
                    results = self._execute_calls(calls)
                    epoch_route_ids: list[str] = []
                    any_candidate = False
                    any_refutation = False

                    for call, outcome in results:
                        route_id, candidate, refutation = self._process_research_outcome(
                            campaign_id=campaign_id,
                            epoch=epoch,
                            call=call,
                            outcome=outcome,
                            root_claim_id=root_claim_id,
                        )
                        self._report_research_outcome(
                            epoch=epoch,
                            call=call,
                            outcome=outcome,
                            route_id=route_id,
                            proof_candidate=candidate,
                            refutation_candidate=refutation,
                        )
                        if route_id:
                            epoch_route_ids.append(route_id)
                        any_candidate = any_candidate or candidate
                        any_refutation = any_refutation or refutation

                    # A pause requested while agents were active is honored before
                    # launching the literature sentinel or any additional verifier.
                    if self._pause_if_requested(
                        campaign_id, checkpoint=f"after research calls in epoch {epoch}"
                    ):
                        terminal_status = CampaignStatus.PAUSED_HUMAN
                        self._record_epoch_git_snapshot(campaign_id=campaign_id, epoch=epoch)
                        break

                    if (
                        self.config.mode.sentinel_enabled
                        and "literature_sentinel" in self.config.roles
                    ):
                        self.reporter.emit(
                            "sentinel_stage",
                            "Literature sentinel is comparing declared route mechanisms with the frozen dossier.",
                            epoch=epoch,
                        )
                        self._run_literature_sentinel(
                            campaign_id=campaign_id,
                            epoch=epoch,
                            contract=contract,
                            route_ids=epoch_route_ids
                            or [
                                str(r["route_id"])
                                for r in self.store.list_routes(
                                    campaign_id, active_only=True
                                )
                            ],
                        )

                    campaign = self.store.get_campaign(campaign_id)
                    if campaign["status"] == CampaignStatus.PAUSED_HUMAN:
                        terminal_status = CampaignStatus.PAUSED_HUMAN
                        self.reporter.emit(
                            "human_review_needed",
                            "A sentinel or conceptual decision requires human review.",
                            epoch=epoch,
                        )
                        self._record_epoch_git_snapshot(campaign_id=campaign_id, epoch=epoch)
                        break
                    if self._pause_if_requested(
                        campaign_id, checkpoint=f"after sentinel in epoch {epoch}"
                    ):
                        terminal_status = CampaignStatus.PAUSED_HUMAN
                        self._record_epoch_git_snapshot(campaign_id=campaign_id, epoch=epoch)
                        break
                    if any_candidate:
                        terminal_status = CampaignStatus.COMPLETE_PROOF_CANDIDATE
                        self._record_epoch_git_snapshot(campaign_id=campaign_id, epoch=epoch)
                        break
                    if any_refutation:
                        terminal_status = CampaignStatus.REFUTATION_CANDIDATE
                        self._record_epoch_git_snapshot(campaign_id=campaign_id, epoch=epoch)
                        break

                    self._apply_stagnation_policy(campaign_id)
                    active_after = self.store.list_routes(campaign_id, active_only=True)
                    if not active_after:
                        self.reporter.emit(
                            "conceptual_pivot_stage",
                            "All active routes are exhausted or frozen; requesting a representation-level pivot.",
                            epoch=epoch,
                        )
                        pivot_created = self._run_conceptual_pivot_if_allowed(
                            campaign_id=campaign_id, epoch=epoch, contract=contract
                        )
                        if not pivot_created:
                            campaign = self.store.get_campaign(campaign_id)
                            if campaign["status"] == CampaignStatus.PAUSED_HUMAN:
                                terminal_status = CampaignStatus.PAUSED_HUMAN
                            else:
                                terminal_status = CampaignStatus.COMPLETED_UNSOLVED
                            self._record_epoch_git_snapshot(campaign_id=campaign_id, epoch=epoch)
                            break

                    summary = self._epoch_summary(campaign_id, epoch)
                    self.reporter.emit(
                        "epoch_summary",
                        summary,
                        epoch=epoch,
                    )
                    self._record_epoch_git_snapshot(
                        campaign_id=campaign_id, epoch=epoch, summary=summary
                    )
                    action, payload = self.reporter.interactive_checkpoint(
                        epoch=epoch, summary=summary
                    )
                    if action == "instruction" and payload:
                        instruction_id = self.store.add_human_instruction(
                            campaign_id=campaign_id,
                            instruction_text=payload,
                            audience="researchers",
                            author="interactive-operator",
                        )
                        self.reporter.emit(
                            "human_instruction_added",
                            f"Saved {instruction_id}; it will be injected into the next researcher call.",
                            instruction_id=instruction_id,
                        )
                    elif action == "pause":
                        self.store.request_campaign_pause(
                            campaign_id,
                            reason=payload or "Interactive pause requested",
                            requested_by="interactive-operator",
                        )
                        self._pause_if_requested(
                            campaign_id, checkpoint=f"interactive checkpoint after epoch {epoch}"
                        )
                        terminal_status = CampaignStatus.PAUSED_HUMAN
                        break

            if terminal_status is None:
                campaign = self.store.get_campaign(campaign_id)
                if int(campaign["calls_used"]) >= int(campaign["max_calls"]) or float(
                    campaign["cost_used"]
                ) >= float(campaign["max_cost_usd"]):
                    terminal_status = CampaignStatus.BUDGET_EXHAUSTED
                else:
                    terminal_status = CampaignStatus.COMPLETED_UNSOLVED
            self.store.update_campaign(campaign_id, status=terminal_status)
            result = self.store.get_campaign(campaign_id)
            if terminal_status in {
                CampaignStatus.COMPLETED_UNSOLVED,
                CampaignStatus.BUDGET_EXHAUSTED,
            }:
                self._synthesize_strongest_partial_result(
                    campaign_id=campaign_id,
                    epoch=int(result["epoch"]),
                    contract=contract,
                )
                self._record_unsolved_campaign_note(campaign_id=campaign_id)
            self.reporter.emit(
                "campaign_finished" if terminal_status != CampaignStatus.PAUSED_HUMAN else "campaign_paused",
                f"Campaign {campaign_id} ended this invocation with status {terminal_status}; "
                f"calls {result['calls_used']}/{result['max_calls']}, "
                f"cost ${float(result['cost_used']):.4f}/${float(result['max_cost_usd']):.2f}.",
                status=terminal_status,
                epoch=result["epoch"],
            )
            return result
        except Exception:
            # Leave recoverable state instead of a permanent RUNNING campaign if
            # orchestration itself fails outside an individual bounded call.
            if terminal_status is None:
                try:
                    current = self.store.get_campaign(campaign_id)
                    if str(current.get("status")) == CampaignStatus.RUNNING:
                        self.store.update_campaign(
                            campaign_id, status=CampaignStatus.PAUSED_HUMAN
                        )
                        self.reporter.emit(
                            "campaign_interrupted",
                            "Controller failed outside a bounded role call; campaign paused for operator review.",
                            campaign_id=campaign_id,
                        )
                except Exception:
                    pass
            raise
        finally:
            self.reporter.stop()

    def _mark_latest_campaign_contract_changed(self, diagnostic: str) -> None:
        """Record that the immutable contract no longer matches the active campaign.

        A contract mismatch is neither a mathematical conclusion nor a human pause.
        Marking the latest campaign makes the reason visible to every interface and
        prevents an operator from accidentally resuming against altered premises.
        """
        latest = self.store.latest_campaign()
        if latest is None:
            return
        campaign_id = str(latest["campaign_id"])
        if str(latest.get("status")) == CampaignStatus.CONTRACT_CHANGED:
            return
        self.store.update_campaign(campaign_id, status=CampaignStatus.CONTRACT_CHANGED)
        self.store.events.append(
            "campaign_contract_changed",
            {
                "campaign_id": campaign_id,
                "reason": CampaignStatus.CONTRACT_CHANGED,
                "diagnostic": diagnostic,
            },
        )

    def _pause_if_requested(self, campaign_id: str, *, checkpoint: str) -> bool:
        control = self.store.get_campaign_control(campaign_id)
        if not bool(control.get("pause_requested")):
            return False
        reason = str(control.get("reason", "")).strip() or "Human pause requested"
        self.store.update_campaign(campaign_id, status=CampaignStatus.PAUSED_HUMAN)
        self.store.add_decision(
            campaign_id=campaign_id,
            epoch=int(self.store.get_campaign(campaign_id)["epoch"]),
            kind="HUMAN_PAUSE_SAFE_CHECKPOINT",
            available={"control": control, "checkpoint": checkpoint},
            selected={"action": "PAUSE_HUMAN"},
            rationale=reason,
            expected_event="Human instructions followed by an explicit resume",
            stop_condition="Operator clears the pause request and resumes the campaign",
            cost_cap=0.0,
        )
        self.reporter.emit(
            "campaign_pausing",
            f"Pausing at safe checkpoint `{checkpoint}`: {reason}",
            checkpoint=checkpoint,
            reason=reason,
        )
        return True

    def _report_research_outcome(
        self,
        *,
        epoch: int,
        call: AgentCall,
        outcome: dict[str, Any] | Exception,
        route_id: str | None,
        proof_candidate: bool,
        refutation_candidate: bool,
    ) -> None:
        if isinstance(outcome, Exception):
            self.reporter.emit(
                "research_outcome_failed",
                f"{call.slot}: provider/protocol failure recorded; no mathematical conclusion: {outcome}",
                epoch=epoch,
                slot=call.slot,
                route_id=route_id,
            )
            return
        status = str(outcome.get("status", "PROGRESS"))
        summary = str(outcome.get("summary", "")).strip() or "No summary supplied"
        next_task = str(outcome.get("next_task", "")).strip()
        decisive = [
            str(item.get("type"))
            for item in outcome.get("decisive_events", [])
            if isinstance(item, dict) and item.get("type")
        ]
        failures = [
            str(item.get("failure_class"))
            for item in outcome.get("failures", [])
            if isinstance(item, dict) and item.get("failure_class")
        ]
        parts = [f"{call.slot} -> {route_id or 'no route'} [{status}]: {summary}"]
        if decisive:
            parts.append("decisive events: " + ", ".join(decisive))
        if failures:
            parts.append("failures: " + ", ".join(failures))
        if proof_candidate:
            parts.append("proof candidate recorded; audit stage invoked")
        if refutation_candidate:
            parts.append("counterexample candidate recorded")
        if next_task:
            parts.append("next task: " + next_task)
        self.reporter.emit(
            "research_outcome",
            "; ".join(parts),
            epoch=epoch,
            slot=call.slot,
            route_id=route_id,
            status=status,
            summary=summary,
            next_task=next_task,
            decisive_events=decisive,
            failure_classes=failures,
            proof_candidate=proof_candidate,
            refutation_candidate=refutation_candidate,
        )

    def _record_epoch_git_snapshot(
        self, *, campaign_id: str, epoch: int, summary: str | None = None
    ) -> None:
        """Best-effort Git provenance at every completed epoch checkpoint."""
        attempts = [
            item for item in self.store.list_attempts()
            if item["campaign_id"] == campaign_id and int(item["epoch"]) == epoch
        ]
        decisive_events = sum(int(item.get("decisive_event", 0)) for item in attempts)
        record = record_campaign_epoch(
            self.store.paths.root,
            campaign_id=campaign_id,
            epoch=epoch,
            summary=summary or self._epoch_summary(campaign_id, epoch),
            attempt_count=len(attempts),
            decisive_events=decisive_events,
            status=str(self.store.get_campaign(campaign_id)["status"]),
        )
        if not record.get("enabled"):
            return
        if record.get("recorded"):
            message = f"Recorded Git snapshot for epoch {epoch}."
            if record.get("tagged"):
                message += f" Tagged meaningful progress as {record.get('tag')}."
            self.reporter.emit(
                "git_epoch_snapshot", message, epoch=epoch,
                commit=record.get("commit"), tag=record.get("tag"),
            )
        else:
            self.reporter.emit(
                "git_epoch_snapshot_failed",
                f"Could not record the optional Git snapshot for epoch {epoch}: {record.get('error', 'unknown error')}",
                epoch=epoch,
            )

    def _epoch_summary(self, campaign_id: str, epoch: int) -> str:
        routes = self.store.list_routes(campaign_id)
        status_counts: dict[str, int] = {}
        for route in routes:
            status = str(route["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        attempts = [
            item
            for item in self.store.list_attempts()
            if item["campaign_id"] == campaign_id and int(item["epoch"]) == epoch
        ]
        decisive = sum(int(item.get("decisive_event", 0)) for item in attempts)
        campaign = self.store.get_campaign(campaign_id)
        statuses = ", ".join(
            f"{name}={count}" for name, count in sorted(status_counts.items())
        ) or "no routes"
        return (
            f"Epoch {epoch}: {len(attempts)} attempt(s), {decisive} decisive event(s); "
            f"routes {statuses}; failure clusters={len(self.store.list_failures())}; "
            f"budget calls {campaign['calls_used']}/{campaign['max_calls']}, "
            f"cost ${float(campaign['cost_used']):.4f}/${float(campaign['max_cost_usd']):.2f}."
        )

    def _build_research_calls(
        self,
        *,
        campaign_id: str,
        epoch: int,
        contract: dict[str, Any],
        root_claim_id: str,
        active_routes: list[dict[str, Any]],
    ) -> list[AgentCall]:
        role = self.config.mode.researcher_role
        if role not in self.config.roles:
            raise KeyError(f"No configured researcher role {role!r}")
        root_claim = self.store.get_claim(root_claim_id)
        literature_guided = self.config.mode.name == "literature_guided"
        # Each role receives a compact, role-specific work packet.  Large source
        # conversions and older outcomes remain addressable by stable local paths
        # and artifact IDs instead of being re-sent on every bounded invocation.
        project_state = self._research_context_packet(
            campaign_id=campaign_id,
            literature_guided=literature_guided,
        )
        dossier = self._compact_literature_dossier() if literature_guided else []
        calls: list[AgentCall] = []

        if not self.store.list_routes(campaign_id):
            for index, slot in enumerate(self._researcher_slots(), start=1):
                if literature_guided:
                    assignment = self._literature_assignment(index)
                    prompt = render_prompt(
                        "literature_researcher.md",
                        slot=slot,
                        epoch=epoch,
                        assignment=assignment,
                        problem_contract=json.dumps(contract, indent=2),
                        root_claim=json.dumps(root_claim, indent=2),
                        route_state="No route exists yet. Create one route responsive to the assigned literature-aware function.",
                        project_state=json.dumps(project_state, indent=2),
                        literature=json.dumps(dossier, indent=2),
                    )
                    task = assignment
                else:
                    prompt = render_prompt(
                        "offline_researcher.md",
                        slot=slot,
                        epoch=epoch,
                        problem_contract=json.dumps(contract, indent=2),
                        root_claim=json.dumps(root_claim, indent=2),
                        route_state="No route exists yet. Create one route that is structurally distinct from obvious alternatives.",
                        project_state=json.dumps(project_state, indent=2),
                        novelty_obligation="None",
                    )
                    task = (
                        "Create one structurally independent proof or refutation route; "
                        "state its key lemma, mechanism, decisive test, and immediate next task"
                    )
                calls.append(
                    AgentCall(
                        role=role,
                        slot=slot,
                        prompt=prompt,
                        project_root=self.store.paths.root,
                        network_policy=self.config.roles[role].network_policy,
                        campaign_id=campaign_id,
                        epoch=epoch,
                        metadata={"task_summary": task},
                    )
                )
            return calls

        for route in active_routes:
            obligation = route.get("novelty_obligation", {})
            if obligation and int(obligation.get("deadline_epoch", epoch)) < epoch:
                self.store.update_route(
                    str(route["route_id"]),
                    status=RouteStatus.EARLY_STOPPED_KNOWN_ROUTE,
                    novelty_obligation={},
                )
                continue
            slot = str(route["owner_slot"])
            if literature_guided:
                prompt = render_prompt(
                    "literature_researcher.md",
                    slot=slot,
                    epoch=epoch,
                    assignment=(
                        f"Continue route `{route['title']}` while auditing every cited theorem, "
                        "separating sourced steps from new mathematics, and attacking the current bridge lemma."
                    ),
                    problem_contract=json.dumps(contract, indent=2),
                    root_claim=json.dumps(root_claim, indent=2),
                    route_state=json.dumps(route, indent=2),
                    project_state=json.dumps(project_state, indent=2),
                    literature=json.dumps(dossier, indent=2),
                )
            else:
                prompt = render_prompt(
                    "offline_researcher.md",
                    slot=slot,
                    epoch=epoch,
                    problem_contract=json.dumps(contract, indent=2),
                    root_claim=json.dumps(root_claim, indent=2),
                    route_state=json.dumps(route, indent=2),
                    project_state=json.dumps(project_state, indent=2),
                    novelty_obligation=json.dumps(obligation or {}, indent=2),
                )
            calls.append(
                AgentCall(
                    role=role,
                    slot=slot,
                    prompt=prompt,
                    project_root=self.store.paths.root,
                    network_policy=self.config.roles[role].network_policy,
                    campaign_id=campaign_id,
                    route_id=str(route["route_id"]),
                    epoch=epoch,
                    metadata={
                        "task_summary": (
                            f"Continue route `{route['title']}`; target key lemma: "
                            f"{route['key_lemma']}; decisive test: {route['decisive_test']}"
                        )
                    },
                )
            )
        return calls

    def _researcher_slots(self) -> list[str]:
        count = self.config.mode.researcher_count
        prefix = "literature" if self.config.mode.name == "literature_guided" else "offline"
        return [f"{prefix}-{index}" for index in range(1, count + 1)]

    @staticmethod
    def _literature_assignment(index: int) -> str:
        assignments = [
            (
                "Construct the shortest complete proof route supported by the audited literature. "
                "Identify exactly which steps are cited and isolate the genuinely new bridge lemma."
            ),
            (
                "Act as an adversarial source-and-gap researcher: verify theorem applicability, "
                "endpoint and uniformity conditions, and try to refute the proposed target or expose a missing hypothesis."
            ),
            (
                "Seek a materially different proof mechanism or counterexample route, using the literature map "
                "to avoid duplicating the primary construction."
            ),
        ]
        if index <= len(assignments):
            return assignments[index - 1]
        return (
            f"Develop independent literature-guided route {index}; choose a mechanism not already covered, "
            "state a decisive falsification test, and avoid parallel paraphrase of another agent."
        )

    def _queue_calls(
        self, campaign_id: str, epoch: int, calls: list[AgentCall]
    ) -> list[AgentCall]:
        queued: list[AgentCall] = []
        for position, call in enumerate(calls, start=1):
            metadata = dict(call.metadata or {})
            task_id = self.store.add_task(
                campaign_id=campaign_id,
                epoch=epoch,
                slot=call.slot,
                role=call.role,
                route_id=call.route_id,
                summary=str(metadata.get("task_summary", "bounded role task")),
                priority=position,
            )
            metadata["task_id"] = task_id
            queued.append(replace(call, metadata=metadata))
        return queued

    def _execute_calls(
        self, calls: list[AgentCall]
    ) -> list[tuple[AgentCall, dict[str, Any] | Exception]]:
        if not self.config.mode.parallel or len(calls) <= 1:
            output = []
            for call in calls:
                try:
                    response = self.runner.call(call)
                    output.append((call, extract_json_object(response.text)))
                except Exception as exc:  # preserved as an auditable failed attempt
                    output.append((call, exc))
            return output

        output: list[tuple[AgentCall, dict[str, Any] | Exception]] = []
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = {pool.submit(self.runner.call, call): call for call in calls}
            for future in as_completed(futures):
                call = futures[future]
                try:
                    response = future.result()
                    output.append((call, extract_json_object(response.text)))
                except Exception as exc:
                    output.append((call, exc))
        output.sort(key=lambda pair: pair[0].slot)
        return output

    def _process_research_outcome(
        self,
        *,
        campaign_id: str,
        epoch: int,
        call: AgentCall,
        outcome: dict[str, Any] | Exception,
        root_claim_id: str,
    ) -> tuple[str | None, bool, bool]:
        if isinstance(outcome, Exception):
            route_id = call.route_id
            artifact = self.artifacts.put_text(
                f"Agent execution/protocol failure:\n\n{type(outcome).__name__}: {outcome}\n",
                kind="agent_failure",
                suffix=".md",
            )
            self.store.record_artifact(artifact)
            attempt_id = self.store.add_attempt(
                campaign_id=campaign_id,
                route_id=route_id,
                epoch=epoch,
                agent_slot=call.slot,
                task=f"{call.role} epoch",
                result_kind="AGENT_FAILURE",
                summary=str(outcome),
                artifact_id=artifact.artifact_id,
                decisive_event=False,
                cost_usd=0.0,
                usage={},
            )
            fp = fingerprint_failure(
                {
                    "failure_class": FailureClass.RESOURCE_EXHAUSTION,
                    "signature": type(outcome).__name__,
                    "logical_scope": "agent execution only; no mathematical conclusion",
                }
            )
            self.store.upsert_failure(
                canonical_key=fp.canonical_key,
                failure_class=fp.failure_class,
                signature=fp.signature,
                logical_scope=fp.logical_scope,
                revival_conditions="repair provider or structured-output protocol",
                attempt_id=attempt_id,
                cost_usd=0.0,
            )
            return route_id, False, False

        route_id = call.route_id
        route_payload = outcome.get("route")
        if route_id is None and isinstance(route_payload, dict):
            route_id = self._create_route_from_payload(
                campaign_id=campaign_id,
                owner_slot=call.slot,
                target_claim_id=root_claim_id,
                route=route_payload,
            )
        if route_id is None:
            return None, False, False

        structured_artifact = self.artifacts.put_text(
            json.dumps(outcome, ensure_ascii=False, indent=2),
            kind="structured_research_outcome",
            suffix=".json",
            metadata={
                "route_id": route_id, "epoch": epoch, "slot": call.slot,
                "status": str(outcome.get("status", "PROGRESS")),
            },
        )
        self.store.record_artifact(structured_artifact)
        self._record_numerical_artifacts(
            campaign_id=campaign_id,
            epoch=epoch,
            route_id=route_id,
            root_claim_id=root_claim_id,
            slot=call.slot,
            outcome=outcome,
        )

        decisive_events = outcome.get("decisive_events", [])
        decisive = any(
            isinstance(item, dict) and str(item.get("type", "")) in _DECISIVE_TYPES
            for item in decisive_events
        )
        result_kind = str(outcome.get("status", "PROGRESS"))
        proof_payload = outcome.get("proof_candidate")
        proof_text = (
            str(proof_payload.get("proof_latex", "")).strip()
            if isinstance(proof_payload, dict)
            else ""
        )
        complete_proof_payload = (
            result_kind == "CANDIDATE_PROOF"
            and isinstance(proof_payload, dict)
            and len(proof_text) >= 500
        )
        if result_kind == "CANDIDATE_PROOF" and not complete_proof_payload:
            # Preserve the structured response for audit/debugging, but do not
            # promote a summary or sketch into a proof-candidate artifact.
            result_kind = "PROGRESS"
            outcome["status"] = "PROGRESS"
            outcome["summary"] = (
                str(outcome.get("summary", "")).rstrip()
                + " Full proof text was not supplied; candidate promotion withheld."
            ).strip()
        counterexample_payload = outcome.get("counterexample_candidate")
        if (
            isinstance(proof_payload, dict) and proof_payload
            and isinstance(counterexample_payload, dict) and counterexample_payload
        ):
            # A single bounded response cannot simultaneously establish a proof
            # and a refutation of the immutable root claim. Retain neither as a
            # decisive event; a human or a fresh route must resolve the conflict.
            result_kind = "PROGRESS"
            outcome["status"] = "PROGRESS"
            outcome["summary"] = (
                str(outcome.get("summary", "")).rstrip()
                + " Conflicting proof and counterexample candidates were withheld."
            ).strip()
            outcome["proof_candidate"] = None
            outcome["counterexample_candidate"] = None
            complete_proof_payload = False
            decisive = False
            self.store.events.append(
                "conflicting_candidate_payload",
                {"campaign_id": campaign_id, "epoch": epoch, "route_id": route_id},
            )
            self.reporter.emit(
                "conflicting_candidate_payload",
                "The response submitted both proof and counterexample candidates; neither can stop the campaign.",
                epoch=epoch, route_id=route_id,
            )
        attempt_id = self.store.add_attempt(
            campaign_id=campaign_id,
            route_id=route_id,
            epoch=epoch,
            agent_slot=call.slot,
            task=str(outcome.get("next_task", f"{call.role} epoch")),
            result_kind=result_kind,
            summary=str(outcome.get("summary", "")),
            artifact_id=structured_artifact.artifact_id,
            decisive_event=decisive,
            cost_usd=0.0,
            usage={},
        )

        for claim in outcome.get("claims", []):
            if not isinstance(claim, dict) or not str(claim.get("statement", "")).strip():
                continue
            claim_id = self.store.add_claim(
                statement=str(claim["statement"]),
                assumptions=[str(x) for x in claim.get("assumptions", [])],
                scope=str(claim.get("scope", "route-local")),
                status=ClaimStatus.CANDIDATE_LEMMA,
                criticality=str(claim.get("criticality", "supporting")),
                source=f"route:{route_id}",
            )
            self.store.add_claim_edge(claim_id, root_claim_id, "supports")
            # A durable partial result must be independently inspectable from
            # the compact claim index. The structured outcome remains the full
            # agent response; this artifact is the claim-sized research note.
            partial_result = self.artifacts.put_text(
                json.dumps(
                    {
                        "claim_id": claim_id,
                        "statement": str(claim["statement"]),
                        "assumptions": [str(x) for x in claim.get("assumptions", [])],
                        "scope": str(claim.get("scope", "route-local")),
                        "criticality": str(claim.get("criticality", "supporting")),
                        "status": str(ClaimStatus.CANDIDATE_LEMMA),
                        "route_id": route_id,
                        "campaign_id": campaign_id,
                        "epoch": epoch,
                        "source_outcome_artifact_id": structured_artifact.artifact_id,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                kind="partial_result",
                suffix=".json",
                metadata={
                    "status": str(ClaimStatus.CANDIDATE_LEMMA),
                    "claim_id": claim_id,
                    "route_id": route_id,
                    "slot": call.slot,
                    "statement": str(claim["statement"]),
                },
            )
            self.store.record_artifact(partial_result)

        for source_claim in outcome.get("source_claims", []):
            if not isinstance(source_claim, dict):
                continue
            statement = str(source_claim.get("statement", "")).strip()
            if not statement:
                continue
            source_ref = str(source_claim.get("source_ref", "unspecified-source"))
            source_claim_id = self.store.add_claim(
                statement=statement,
                assumptions=[str(x) for x in source_claim.get("assumptions", [])],
                scope=str(source_claim.get("scope", "literature theorem")),
                status=ClaimStatus.SOURCE_REPORTED,
                criticality=str(source_claim.get("criticality", "supporting")),
                source=f"literature:{source_ref}",
            )
            self.store.add_claim_edge(source_claim_id, root_claim_id, "source_supports")
            self.store.add_evidence(
                claim_id=source_claim_id,
                evidence_type=EvidenceType.LITERATURE_THEOREM,
                logical_force="Source-reported theorem; applicability and exact hypotheses remain auditable.",
                scope=str(source_claim.get("scope", "as stated in cited source")),
                artifact_id=structured_artifact.artifact_id,
                status="SOURCE_REPORTED",
            )

        duplicate_failure = False
        for failure in outcome.get("failures", []):
            if not isinstance(failure, dict):
                continue
            if self._is_literature_access_uncertainty(failure):
                self.store.events.append(
                    "literature_access_uncertainty",
                    {"route_id": route_id, "epoch": epoch, "failure": failure},
                )
                self.reporter.emit(
                    "literature_access_uncertainty",
                    "Literature access uncertainty recorded; it is not a mathematical obstruction and the route remains eligible.",
                    epoch=epoch, route_id=route_id,
                )
                continue
            fp = fingerprint_failure(failure)
            _, count = self.store.upsert_failure(
                canonical_key=fp.canonical_key,
                failure_class=fp.failure_class,
                signature=fp.signature,
                logical_scope=fp.logical_scope,
                revival_conditions=str(failure.get("revival_conditions", "")),
                attempt_id=attempt_id,
                cost_usd=0.0,
            )
            duplicate_failure = duplicate_failure or (
                count >= self.config.budget.duplicate_failure_limit
            )

        route = self.store.get_route(route_id)
        obligation = route.get("novelty_obligation", {})
        novelty_evidence = [
            item
            for item in outcome.get("novelty_evidence", [])
            if isinstance(item, dict)
        ]
        if obligation:
            if novelty_evidence_satisfies(obligation, novelty_evidence):
                # Evidence is recorded, but the literature sentinel still decides whether
                # the difference is sufficient. Keep the obligation until that response.
                obligation = {**obligation, "evidence_submitted": novelty_evidence}
                self.store.update_route(route_id, novelty_obligation=obligation)
            elif epoch >= int(obligation.get("deadline_epoch", epoch)):
                self.store.update_route(
                    route_id,
                    status=RouteStatus.EARLY_STOPPED_KNOWN_ROUTE,
                    novelty_obligation={},
                )
                return route_id, False, False

        proof_candidate = outcome.get("proof_candidate")
        counterexample = outcome.get("counterexample_candidate")
        proof_found = complete_proof_payload
        refutation_found = False
        if isinstance(counterexample, dict) and counterexample:
            refutation_found = self._handle_counterexample_candidate(
                campaign_id=campaign_id,
                epoch=epoch,
                route_id=route_id,
                root_claim_id=root_claim_id,
                candidate=counterexample,
            )
            if not refutation_found:
                # A proposed example that fails or lacks independent checks is
                # useful route-local information, but it is not a decisive
                # counterexample and must not freeze the campaign as a refutation.
                decisive = False
                outcome["summary"] = (
                    str(outcome.get("summary", "")).rstrip()
                    + " Counterexample candidate retained, but independent audits did not establish an exact refutation."
                ).strip()

        if duplicate_failure and not decisive:
            self.store.update_route(route_id, status=RouteStatus.METHOD_FAILED)
        else:
            no_progress = 0 if decisive else int(route["epochs_without_progress"]) + 1
            status = RouteStatus.ACTIVE
            if result_kind == "BLOCKED":
                status = RouteStatus.BLOCKED
            if result_kind == "CANDIDATE_PROOF":
                status = RouteStatus.COMPLETE_CANDIDATE
            if result_kind == "COUNTEREXAMPLE_CANDIDATE" and refutation_found:
                status = RouteStatus.COMPLETE_CANDIDATE
            self.store.update_route(
                route_id, status=status, epochs_without_progress=no_progress
            )

        if proof_found:
            self._handle_proof_candidate(
                campaign_id=campaign_id,
                epoch=epoch,
                route_id=route_id,
                root_claim_id=root_claim_id,
                proof_candidate=proof_candidate,
            )
        return route_id, proof_found, refutation_found

    @staticmethod
    def _is_literature_access_uncertainty(failure: dict[str, Any]) -> bool:
        if str(failure.get("failure_class", "")) != FailureClass.SOURCE_MISMATCH:
            return False
        text = " ".join(
            str(failure.get(key, ""))
            for key in ("signature", "logical_scope", "revival_conditions")
        ).casefold()
        markers = ("paywall", "paywalled", "inaccessible", "unavailable", "access denied", "not retriev", "could not retrieve")
        return any(marker in text for marker in markers)

    def _record_numerical_artifacts(
        self,
        *,
        campaign_id: str,
        epoch: int,
        route_id: str,
        root_claim_id: str,
        slot: str,
        outcome: dict[str, Any],
    ) -> None:
        request = outcome.get("experiment_request")
        hpc_required = False
        if isinstance(request, dict) and request:
            resource_assessment = assess_experiment_resources(request)
            common_metadata = {
                "campaign_id": campaign_id, "epoch": epoch,
                "route_id": route_id, "slot": slot,
            }
            if resource_assessment["needs_hpc"]:
                hpc_required = True
                handoff = {
                    "request": request,
                    "resource_assessment": resource_assessment,
                    "operator_action": (
                        "Run the supplied code and scheduler instructions on an HPC system, "
                        "then retain the exact output, observed runtime, and reproducibility command."
                    ),
                }
                artifact = self.artifacts.put_text(
                    json.dumps(handoff, ensure_ascii=False, indent=2),
                    kind="hpc_resource_request", suffix=".json",
                    metadata={
                        **common_metadata, "status": "NEEDS_HUMAN_RESOURCES",
                        "minimum_cpu_cores": resource_assessment["requested"]["minimum_cpu_cores"],
                        "minimum_memory_gb": resource_assessment["requested"]["minimum_memory_gb"],
                        "requires_cuda": resource_assessment["requested"]["requires_cuda"],
                    },
                )
                self.store.record_artifact(artifact)
                self.store.events.append(
                    "hpc_resource_requested",
                    {**common_metadata, "artifact_id": artifact.artifact_id},
                )
                self.reporter.emit(
                    "hpc_resource_requested",
                    "Large numerical work was retained as an HPC resource request; no local run is authorized.",
                    epoch=epoch, route_id=route_id, artifact_id=artifact.artifact_id,
                )
            else:
                artifact = self.artifacts.put_text(
                    json.dumps(
                        {"request": request, "resource_assessment": resource_assessment},
                        ensure_ascii=False, indent=2,
                    ),
                    kind="numerical_experiment_plan", suffix=".json",
                    metadata={
                        **common_metadata,
                        "status": "LOCAL_RESOURCES_AVAILABLE" if resource_assessment["is_large"] else "PLANNED",
                    },
                )
                self.store.record_artifact(artifact)
                self.store.events.append(
                    "numerical_experiment_planned",
                    {**common_metadata, "artifact_id": artifact.artifact_id},
                )
                self.reporter.emit(
                    "numerical_experiment_planned",
                    "A bounded numerical experiment plan was retained as an artifact; it has no deductive force.",
                    epoch=epoch, route_id=route_id, artifact_id=artifact.artifact_id,
                )
        numerical = outcome.get("numerical_evidence")
        if hpc_required:
            # The complete response remains in its structured outcome artifact,
            # but no claimed observation may enter the evidence ledger before the
            # requested external run has an auditable result.
            return
        if not isinstance(numerical, dict) or not numerical:
            return
        kind = str(numerical.get("kind", EvidenceType.FLOATING_POINT_EXPERIMENT))
        try:
            evidence_type = EvidenceType(kind)
        except ValueError:
            evidence_type = EvidenceType.FLOATING_POINT_EXPERIMENT
            kind = str(evidence_type)
        artifact = self.artifacts.put_text(
            json.dumps(numerical, ensure_ascii=False, indent=2),
            kind="numerical_evidence",
            suffix=".json",
            metadata={
                "campaign_id": campaign_id, "epoch": epoch, "route_id": route_id,
                "slot": slot, "status": "OBSERVED", "evidence_type": kind,
            },
        )
        self.store.record_artifact(artifact)
        self.store.add_evidence(
            claim_id=root_claim_id,
            evidence_type=evidence_type,
            logical_force=(
                "Numerical/computational observation only; it does not prove the root claim. "
                + str(numerical.get("logical_force", ""))
            ).strip(),
            scope=f"route {route_id}; reported computation",
            artifact_id=artifact.artifact_id,
            status=ClaimStatus.EMPIRICALLY_OBSERVED,
        )
        self.store.events.append(
            "numerical_evidence_recorded",
            {"campaign_id": campaign_id, "route_id": route_id,
             "artifact_id": artifact.artifact_id, "evidence_type": kind},
        )
        self.reporter.emit(
            "numerical_evidence_recorded",
            "Numerical evidence was retained as an artifact and marked non-deductive.",
            epoch=epoch, route_id=route_id, artifact_id=artifact.artifact_id,
            evidence_type=kind,
        )

    def _create_route_from_payload(
        self,
        *,
        campaign_id: str,
        owner_slot: str,
        target_claim_id: str,
        route: dict[str, Any],
    ) -> str:
        title = str(route.get("title", "Untitled route"))
        method = str(route.get("method_family", "unspecified"))
        representation = str(route.get("representation", "unspecified"))
        key_lemma = str(route.get("key_lemma", "unspecified"))
        mechanism = str(route.get("central_mechanism", "unspecified"))
        fingerprint = normalize_signature(
            " | ".join([method, representation, key_lemma, mechanism])
        )
        mechanism_signature = normalize_signature(
            " | ".join([title, method, representation, key_lemma, mechanism])
        )
        difference = str(route.get("difference_from_existing", ""))
        revival_route_id = str(route.get("revives_route_id", "")).strip()
        revival_certificate = str(route.get("revival_certificate", "")).strip()
        status: str = RouteStatus.ACTIVE
        blocked_by_failed_route: str | None = None
        revival_needing_review: str | None = None
        for existing in self.store.list_routes(campaign_id):
            similarity = jaccard_similarity(fingerprint, str(existing["fingerprint"]))
            existing_signature = normalize_signature(
                " | ".join(
                    str(existing.get(field, ""))
                    for field in ("title", "method_family", "representation", "key_lemma", "central_mechanism")
                )
            )
            failed_similarity = jaccard_similarity(mechanism_signature, existing_signature)
            if (
                str(existing["status"]) == RouteStatus.METHOD_FAILED
                and failed_similarity >= 0.55
            ):
                # A long prose difference is not a new method. Retrying a failed
                # mechanism needs an exact route reference and a substantive
                # certificate, then remains inactive pending human review.
                if revival_route_id != str(existing["route_id"]):
                    status = RouteStatus.SUBSUMED
                    blocked_by_failed_route = str(existing["route_id"])
                    break
                if len(revival_certificate) < 160:
                    status = RouteStatus.SUBSUMED
                    blocked_by_failed_route = str(existing["route_id"])
                    break
                status = RouteStatus.NEEDS_HUMAN_IDEA
                revival_needing_review = str(existing["route_id"])
                break
            if (
                similarity >= self.config.mode.route_similarity_threshold
                and len(difference.strip()) < 40
            ):
                status = RouteStatus.SUBSUMED
                break
        mode = str(route.get("mode", RouteMode.DEDUCTIVE))
        if mode not in {item.value for item in RouteMode}:
            mode = RouteMode.DEDUCTIVE
        route_id = self.store.add_route(
            campaign_id=campaign_id,
            title=title,
            target_claim_id=target_claim_id,
            mode=mode,
            method_family=method,
            representation=representation,
            key_lemma=key_lemma,
            central_mechanism=mechanism,
            decisive_test=str(route.get("decisive_test", "")),
            difference_from_existing=difference,
            fingerprint=fingerprint,
            independence_cluster=str(
                route.get("independence_cluster", normalize_signature(method)[:80])
            ),
            owner_slot=owner_slot,
            status=status,
        )
        if blocked_by_failed_route:
            self.store.events.append(
                "route_blocked_failed_method",
                {
                    "campaign_id": campaign_id,
                    "blocked_route_id": blocked_by_failed_route,
                    "proposed_title": title,
                    "proposed_method_family": method,
                    "mechanism_similarity": failed_similarity,
                },
            )
        elif revival_needing_review:
            self.store.events.append(
                "route_revival_needs_human_review",
                {
                    "campaign_id": campaign_id,
                    "failed_route_id": revival_needing_review,
                    "proposed_title": title,
                    "revival_certificate": revival_certificate,
                },
            )
        return route_id

    def _run_literature_sentinel(
        self,
        *,
        campaign_id: str,
        epoch: int,
        contract: dict[str, Any],
        route_ids: list[str],
    ) -> None:
        routes = [self.store.get_route(route_id) for route_id in route_ids]
        prior = self.store.list_interventions(campaign_id)
        prompt = render_prompt(
            "literature_sentinel.md",
            epoch=epoch,
            problem_contract=json.dumps(contract, indent=2),
            routes=json.dumps(routes, indent=2),
            interventions=json.dumps(prior, indent=2),
            literature=json.dumps(self._literature_dossier(), indent=2),
        )
        call = AgentCall(
            role="literature_sentinel",
            slot="literature-sentinel",
            prompt=prompt,
            project_root=self.store.paths.root,
            network_policy=self.config.roles["literature_sentinel"].network_policy,
            campaign_id=campaign_id,
            epoch=epoch,
            metadata={
                "task_summary": (
                    "Compare declared route mechanisms with exact literature routes and known dead ends; "
                    "intervene only on a mechanism-level match"
                )
            },
        )
        try:
            response = self.runner.call(call)
            data = extract_json_object(response.text)
        except Exception as exc:
            self.store.events.append(
                "literature_sentinel_failed",
                {"campaign_id": campaign_id, "epoch": epoch, "error": str(exc)},
            )
            self.reporter.emit(
                "sentinel_failed",
                f"Literature sentinel failed; research state is preserved: {exc}",
                epoch=epoch,
            )
            return

        interventions = [item for item in data.get("interventions", []) if isinstance(item, dict)]
        self.reporter.emit(
            "sentinel_summary",
            f"Literature sentinel returned {len(interventions)} intervention record(s).",
            epoch=epoch,
            intervention_count=len(interventions),
        )
        for item in interventions:
            if not isinstance(item, dict):
                continue
            route_id = str(item.get("route_id", ""))
            if not route_id or route_id not in {str(r["route_id"]) for r in routes}:
                continue
            kind = str(item.get("kind", InterventionKind.NO_INTERVENTION))
            requested_early_stop = bool(item.get("early_stop", False))
            early_stop = early_stop_has_exact_literature_evidence(item)
            if requested_early_stop and not early_stop:
                item = {**item, "early_stop": False}
                self.store.events.append(
                    "literature_access_uncertainty",
                    {
                        "campaign_id": campaign_id, "epoch": epoch, "route_id": route_id,
                        "evidence_status": str(item.get("evidence_status", "UNRESOLVED")),
                        "message": str(item.get("message", "")),
                    },
                )
                self.reporter.emit(
                    "literature_access_uncertainty",
                    "Literature proposal was retained as a non-blocking lead because exact accessible source evidence was not supplied.",
                    epoch=epoch, route_id=route_id,
                )
            intervention_id = self.store.add_intervention(
                campaign_id=campaign_id,
                route_id=route_id,
                kind=kind,
                source_refs=[str(x) for x in item.get("source_refs", [])],
                message=str(item.get("message", "")),
                early_stop=early_stop,
                applicability=[str(x) for x in item.get("applicability_conditions", [])],
                deadline_epoch=(
                    epoch + self.config.mode.novelty_deadline_epochs
                    if early_stop
                    else None
                ),
            )
            self.reporter.emit(
                "literature_intervention",
                f"{intervention_id} for {route_id}: {kind}; "
                f"early_stop={early_stop}; {str(item.get('message', '')).strip()}",
                epoch=epoch,
                intervention_id=intervention_id,
                route_id=route_id,
                kind=kind,
                early_stop=early_stop,
            )
            if kind == InterventionKind.DIFFERENCE_CONFIRMED:
                result = arbitrate_intervention(
                    intervention_id=intervention_id,
                    intervention=item,
                    response={},
                    current_epoch=epoch,
                    novelty_deadline_epochs=self.config.mode.novelty_deadline_epochs,
                    require_certificate=self.config.mode.require_route_difference_certificate,
                )
                self.store.update_intervention(
                    intervention_id,
                    status=result.intervention_status,
                    response={},
                )
                self.store.update_route(
                    route_id,
                    status=result.route_status,
                    novelty_obligation=result.novelty_obligation,
                )
                # Also clear prior provisional interventions on this route.
                for old in self.store.list_interventions(campaign_id, route_id):
                    if old["intervention_id"] != intervention_id and old["status"] in {
                        "REJECTED_DIFFERENT_ROUTE_PROVISIONAL",
                        "REJECTED_NOT_APPLICABLE_PROVISIONAL",
                    }:
                        self.store.update_intervention(
                            old["intervention_id"],
                            status="RESOLVED_DIFFERENCE_CONFIRMED",
                            response=old.get("response", {}),
                        )
                continue
            if not early_stop:
                self.store.update_intervention(
                    intervention_id, status="RECORDED_NO_STOP", response={}
                )
                continue
            self._negotiate_intervention(
                campaign_id=campaign_id,
                epoch=epoch,
                intervention_id=intervention_id,
                intervention=item,
            )

    def _negotiate_intervention(
        self,
        *,
        campaign_id: str,
        epoch: int,
        intervention_id: str,
        intervention: dict[str, Any],
    ) -> None:
        route_id = str(intervention["route_id"])
        route = self.store.get_route(route_id)
        prompt = render_prompt(
            "intervention_response.md",
            slot=route["owner_slot"],
            epoch=epoch,
            route=json.dumps(route, indent=2),
            intervention=json.dumps(
                {"intervention_id": intervention_id, **intervention}, indent=2
            ),
        )
        call = AgentCall(
            role="intervention_responder",
            slot=str(route["owner_slot"]),
            prompt=prompt,
            project_root=self.store.paths.root,
            network_policy=self.config.roles["intervention_responder"].network_policy,
            campaign_id=campaign_id,
            route_id=route_id,
            epoch=epoch,
            metadata={
                "task_summary": (
                    "Answer the literature early-stop proposal: accept it or provide a concrete "
                    "route-difference certificate and decisive novelty test"
                )
            },
        )
        try:
            response = self.runner.call(call)
            response_data = extract_json_object(response.text)
        except Exception as exc:
            response_data = {
                "decision": "NEED_HUMAN_REVIEW",
                "reason": f"Intervention responder failed: {exc}",
            }
        result = arbitrate_intervention(
            intervention_id=intervention_id,
            intervention=intervention,
            response=response_data,
            current_epoch=epoch,
            novelty_deadline_epochs=self.config.mode.novelty_deadline_epochs,
            require_certificate=self.config.mode.require_route_difference_certificate,
        )
        self.store.update_intervention(
            intervention_id,
            status=result.intervention_status,
            response=response_data,
        )
        self.store.update_route(
            route_id,
            status=result.route_status,
            novelty_obligation=result.novelty_obligation,
        )
        self.store.add_decision(
            campaign_id=campaign_id,
            epoch=epoch,
            kind="LITERATURE_EARLY_STOP_ARBITRATION",
            available={"intervention": intervention, "response": response_data},
            selected={"action": result.action, "route_status": result.route_status},
            rationale=result.rationale,
            expected_event=(
                "A distinguishing mathematical result before the novelty deadline"
                if result.action == "CONTINUE_PROVISIONAL"
                else "No further work on the duplicate route"
            ),
            stop_condition="Novelty obligation missed, route accepted as known, or human adjudication",
            cost_cap=0.0,
        )
        self.reporter.emit(
            "intervention_decision",
            f"{intervention_id} on {route_id}: action={result.action}, "
            f"route_status={result.route_status}; {result.rationale}",
            epoch=epoch,
            intervention_id=intervention_id,
            route_id=route_id,
            action=result.action,
            route_status=result.route_status,
        )
        if result.action == "PAUSE_HUMAN":
            self.store.update_campaign(campaign_id, status=CampaignStatus.PAUSED_HUMAN)

    def _apply_stagnation_policy(self, campaign_id: str) -> None:
        for route in self.store.list_routes(campaign_id, active_only=True):
            if int(route["epochs_without_progress"]) >= self.config.budget.stagnation_epochs:
                self.store.update_route(
                    str(route["route_id"]),
                    status=RouteStatus.NEEDS_REPRESENTATION_CHANGE,
                )
                self.reporter.emit(
                    "route_stagnation",
                    f"Freezing {route['route_id']} `{route['title']}` after "
                    f"{route['epochs_without_progress']} epoch(s) without a decisive event; "
                    "a new representation or human idea is required.",
                    route_id=str(route["route_id"]),
                )
                self.store.add_decision(
                    campaign_id=campaign_id,
                    epoch=int(self.store.get_campaign(campaign_id)["epoch"]),
                    kind="STAGNATION_FREEZE",
                    available={"route": route},
                    selected={"status": RouteStatus.NEEDS_REPRESENTATION_CHANGE},
                    rationale="The route produced no decisive event within the stagnation window.",
                    expected_event="A genuinely new representation or human idea",
                    stop_condition="No retry without a novelty certificate",
                    cost_cap=0.0,
                )


    def _run_conceptual_pivot_if_allowed(
        self, *, campaign_id: str, epoch: int, contract: dict[str, Any]
    ) -> bool:
        if "conceptual_pivot" not in self.config.roles:
            return False
        marker = f"conceptual_pivot_done:{campaign_id}"
        if self.store.get_meta(marker) == "1":
            return False
        if not self.store.budget_available(campaign_id):
            return False
        prompt = render_prompt(
            "conceptual_pivot.md",
            problem_contract=json.dumps(contract, indent=2),
            failures=json.dumps(self.store.list_failures(), indent=2),
            claims=json.dumps(self.store.list_claims(), indent=2),
        )
        call = AgentCall(
            role="conceptual_pivot",
            slot="conceptual-pivot",
            prompt=prompt,
            project_root=self.store.paths.root,
            network_policy=self.config.roles["conceptual_pivot"].network_policy,
            campaign_id=campaign_id,
            epoch=epoch,
            metadata={
                "task_summary": (
                    "Diagnose the shared obstruction and propose genuinely new representations, "
                    "or identify one high-leverage human question"
                )
            },
        )
        self.store.set_meta(marker, "1")
        try:
            response = self.runner.call(call)
            data = extract_json_object(response.text)
        except Exception as exc:
            self.store.events.append(
                "conceptual_pivot_failed",
                {"campaign_id": campaign_id, "epoch": epoch, "error": str(exc)},
            )
            return False
        if bool(data.get("needs_human", False)):
            self.store.update_campaign(campaign_id, status=CampaignStatus.PAUSED_HUMAN)
            self.reporter.emit(
                "conceptual_stall",
                str(data.get("human_question", "A domain-level idea is needed.")),
                epoch=epoch,
            )
            self.store.add_decision(
                campaign_id=campaign_id,
                epoch=epoch,
                kind="CONCEPTUAL_STALL_HUMAN",
                available=data,
                selected={"action": "PAUSE_HUMAN"},
                rationale=str(data.get("human_question", "A domain-level idea is needed.")),
                expected_event="One high-leverage conceptual hint",
                stop_condition="Human response",
                cost_cap=0.0,
            )
            return False
        root_claim_id = str(self.store.get_meta("root_claim_id"))
        created: list[str] = []
        for index, item in enumerate(data.get("new_representations", []), start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", f"Conceptual pivot {index}"))
            difference = str(item.get("difference", ""))
            test = str(item.get("decisive_test", ""))
            route_id = self.store.add_route(
                campaign_id=campaign_id,
                title=title,
                target_claim_id=root_claim_id,
                mode=RouteMode.CONCEPTUAL,
                method_family="conceptual representation change",
                representation=title,
                key_lemma="derive and validate the new formulation",
                central_mechanism=difference,
                decisive_test=test,
                difference_from_existing=difference,
                fingerprint=normalize_signature(title + " " + difference + " " + test),
                independence_cluster="conceptual-pivot",
                owner_slot=self._researcher_slots()[(index - 1) % len(self._researcher_slots())],
                status=RouteStatus.ACTIVE,
            )
            created.append(route_id)
        self.reporter.emit(
            "conceptual_pivot_result",
            f"Created {len(created)} new conceptual route(s): " + ", ".join(created),
            epoch=epoch,
            created_routes=created,
        )
        self.store.add_decision(
            campaign_id=campaign_id,
            epoch=epoch,
            kind="CONCEPTUAL_PIVOT",
            available=data,
            selected={"created_routes": created},
            rationale="All active routes were exhausted or frozen; switch representations rather than repeat them.",
            expected_event="An exact identity or obstruction in a new formulation",
            stop_condition="No decisive event within the next bounded route epoch",
            cost_cap=0.0,
        )
        return bool(created)

    def _expand_proof_candidate(
        self, *, campaign_id: str, epoch: int, route_id: str,
        root_claim_id: str, proof_candidate: dict[str, Any]
    ) -> dict[str, Any]:
        if "proof_expander" not in self.config.roles:
            return proof_candidate
        packet = {
            "claim": self.store.get_claim(root_claim_id),
            "proof_candidate": proof_candidate,
        }
        call = AgentCall(
            role="proof_expander",
            slot="proof-expander",
            prompt=render_prompt(
                "proof_expander.md",
                problem_contract=json.dumps(self._load_contract(), indent=2),
                proof_packet=json.dumps(packet, indent=2),
                literature=json.dumps(self._literature_dossier(), indent=2),
            ),
            project_root=self.store.paths.root,
            network_policy=self.config.roles["proof_expander"].network_policy,
            campaign_id=campaign_id,
            route_id=route_id,
            epoch=epoch,
            metadata={"task_summary": "Expand and literature-audit the proof candidate"},
        )
        try:
            expanded = extract_json_object(self.runner.call(call).text)
            latex = str(expanded.get("proof_latex", "")).strip()
            if len(latex) < 500:
                raise ValueError("proof-expander returned incomplete proof_latex")
            latex = validate_proof_latex(latex)
            self.reporter.emit(
                "proof_expansion_completed",
                "Literature-backed proof expansion completed; independent audits starting",
                epoch=epoch, route_id=route_id,
            )
            return {
                **proof_candidate,
                "proof_latex": latex,
                "open_obligations": expanded.get("open_obligations", []),
                "expansion_plan": expanded.get("plan", []),
                "literature_review": expanded.get("literature_review", ""),
                "sources": expanded.get("sources", []),
            }
        except Exception as exc:
            self.reporter.emit(
                "proof_expansion_failed",
                f"Proof expansion failed; candidate retained for audit: {exc}",
                epoch=epoch, route_id=route_id,
            )
            return proof_candidate

    def _handle_proof_candidate(
        self,
        *,
        campaign_id: str,
        epoch: int,
        route_id: str,
        root_claim_id: str,
        proof_candidate: dict[str, Any],
    ) -> None:
        proof_candidate = self._expand_proof_candidate(
            campaign_id=campaign_id, epoch=epoch, route_id=route_id,
            root_claim_id=root_claim_id, proof_candidate=proof_candidate,
        )
        raw_proof = str(proof_candidate.get("proof_latex", ""))
        try:
            proof_latex = validate_proof_latex(raw_proof)
        except LatexValidationError as exc:
            artifact = self.artifacts.put_text(
                raw_proof,
                kind="proof_candidate_render_rejected",
                suffix=".txt",
                metadata={
                    "route_id": route_id,
                    "root_claim_id": root_claim_id,
                    "render_status": "REJECTED",
                    "latex_error": str(exc),
                },
            )
            self.store.record_artifact(artifact)
            self.store.events.append(
                "proof_candidate_render_rejected",
                {
                    "artifact_id": artifact.artifact_id,
                    "route_id": route_id,
                    "reason": str(exc),
                },
            )
            self.reporter.emit(
                "proof_candidate_render_rejected",
                "Proof candidate retained as source text; LaTeX/PDF publication was blocked: " + str(exc),
                epoch=epoch,
                route_id=route_id,
                artifact_id=artifact.artifact_id,
            )
            return
        proof_candidate = {**proof_candidate, "proof_latex": proof_latex}
        artifact = self.artifacts.put_text(
            proof_latex,
            kind="proof_candidate_latex",
            suffix=".tex",
            metadata={"route_id": route_id, "root_claim_id": root_claim_id, "render_status": "VALIDATED"},
        )
        self.store.record_artifact(artifact)
        proof_record = self.artifacts.put_text(
            json.dumps(proof_candidate, ensure_ascii=False, indent=2),
            kind="proof_candidate_record",
            suffix=".json",
            metadata={
                "route_id": route_id,
                "root_claim_id": root_claim_id,
                "proof_artifact_id": artifact.artifact_id,
                "status": "PENDING_AUDIT",
            },
        )
        self.store.record_artifact(proof_record)
        self.store.add_evidence(
            claim_id=root_claim_id,
            evidence_type="DEDUCTIVE_PROOF_CANDIDATE",
            logical_force="Candidate natural-language proof only; requires local, global, and human audit.",
            scope="exact root claim as submitted by the route",
            artifact_id=artifact.artifact_id,
            status="CANDIDATE",
        )
        note_tex, note_pdf = write_proof_candidate_note(
            self.store, proof_candidate=proof_candidate, route_id=route_id,
            artifact_id=artifact.artifact_id,
        )
        if note_tex is None or note_pdf is None:
            self.store.events.append(
                "proof_candidate_note_failed",
                {"artifact_id": artifact.artifact_id, "reason": "pdflatex did not produce a PDF"},
            )
            self.reporter.emit(
                "proof_candidate_note_failed",
                "Validated proof body retained, but journal-note publication was blocked because PDF generation failed.",
                epoch=epoch,
                route_id=route_id,
                artifact_id=artifact.artifact_id,
            )
            return
        else:
            self.store.events.append(
                "proof_candidate_note_created",
                {
                    "artifact_id": artifact.artifact_id,
                    "tex_path": str(note_tex.relative_to(self.store.paths.root)),
                    "pdf_path": str(note_pdf.relative_to(self.store.paths.root)),
                },
            )
        if "local_verifier" not in self.config.roles:
            if "global_verifier" in self.config.roles:
                self._run_global_audit(
                    campaign_id=campaign_id, epoch=epoch, route_id=route_id,
                    root_claim_id=root_claim_id, proof_candidate=proof_candidate,
                    proof_artifact_id=artifact.artifact_id,
                )
            return
        packet = {
            "claim": self.store.get_claim(root_claim_id),
            "proof": proof_candidate,
        }
        prompt = render_prompt(
            "local_verifier.md",
            problem_contract=json.dumps(self._load_contract(), indent=2),
            proof_packet=json.dumps(packet, indent=2),
        )
        call = AgentCall(
            role="local_verifier",
            slot="local-verifier",
            prompt=prompt,
            project_root=self.store.paths.root,
            network_policy=self.config.roles["local_verifier"].network_policy,
            campaign_id=campaign_id,
            route_id=route_id,
            epoch=epoch,
            metadata={
                "task_summary": (
                    "Audit the proof candidate locally against the exact claim; identify the minimal failed obligation"
                )
            },
        )
        try:
            response = self.runner.call(call)
            audit = extract_json_object(response.text)
        except Exception as exc:
            audit = {
                "verdict": AuditVerdict.UNCERTAIN,
                "failure_class": FailureClass.VERIFIER_UNCERTAINTY,
                "minimal_failed_obligation": str(exc),
                "local_repairable": False,
            }
        audit_artifact = self.artifacts.put_text(
            json.dumps(audit, indent=2), kind="local_audit", suffix=".json",
            metadata={
                "route_id": route_id,
                "root_claim_id": root_claim_id,
                "proof_artifact_id": artifact.artifact_id,
            },
        )
        self.store.record_artifact(audit_artifact)
        self.store.add_audit(
            target_type="claim",
            target_id=root_claim_id,
            audit_type="LOCAL_PROOF_AUDIT",
            verdict=str(audit.get("verdict", AuditVerdict.UNCERTAIN)),
            failure_class=str(audit.get("failure_class", "")),
            minimal_obligation=str(audit.get("minimal_failed_obligation", "")),
            local_repairable=bool(audit.get("local_repairable", False)),
            artifact_id=audit_artifact.artifact_id,
            auditor_profile="local_verifier",
        )
        self.reporter.emit(
            "local_audit",
            f"Local verifier verdict={audit.get('verdict', AuditVerdict.UNCERTAIN)}; "
            f"minimal obligation: {audit.get('minimal_failed_obligation', '')}",
            epoch=epoch,
            route_id=route_id,
            verdict=str(audit.get("verdict", AuditVerdict.UNCERTAIN)),
        )
        if str(audit.get("verdict")) == AuditVerdict.PASS:
            try:
                transition_claim(
                    self.store, root_claim_id, ClaimStatus.CANDIDATE_LEMMA
                )
                transition_claim(
                    self.store, root_claim_id, ClaimStatus.AGENT_AUDITED_LOCAL
                )
            except InvalidTransition:
                pass
        if "global_verifier" in self.config.roles:
            self._run_global_audit(
                campaign_id=campaign_id,
                epoch=epoch,
                route_id=route_id,
                root_claim_id=root_claim_id,
                proof_candidate=proof_candidate,
                proof_artifact_id=artifact.artifact_id,
            )

    def _run_global_audit(
        self,
        *,
        campaign_id: str,
        epoch: int,
        route_id: str,
        root_claim_id: str,
        proof_candidate: dict[str, Any],
        proof_artifact_id: str | None = None,
    ) -> None:
        proof_core = {
            "root_claim": self.store.get_claim(root_claim_id),
            "proof_candidate": proof_candidate,
        }
        prompt = render_prompt(
            "global_verifier.md",
            problem_contract=json.dumps(self._load_contract(), indent=2),
            proof_core=json.dumps(proof_core, indent=2),
        )
        call = AgentCall(
            role="global_verifier",
            slot="fresh-global-verifier",
            prompt=prompt,
            project_root=self.store.paths.root,
            network_policy=self.config.roles["global_verifier"].network_policy,
            campaign_id=campaign_id,
            route_id=route_id,
            epoch=epoch,
            metadata={
                "task_summary": (
                    "Fresh global audit of statement fidelity, dependencies, interfaces, endpoints, and the assembled proof"
                )
            },
        )
        try:
            response = self.runner.call(call)
            audit = extract_json_object(response.text)
        except Exception as exc:
            audit = {
                "verdict": AuditVerdict.UNCERTAIN,
                "failure_class": FailureClass.VERIFIER_UNCERTAINTY,
                "minimal_failed_obligation": str(exc),
                "local_repairable": False,
            }
        audit_artifact = self.artifacts.put_text(
            json.dumps(audit, indent=2), kind="global_audit", suffix=".json",
            metadata={
                "route_id": route_id,
                "root_claim_id": root_claim_id,
                "proof_artifact_id": proof_artifact_id or "",
            },
        )
        self.store.record_artifact(audit_artifact)
        self.store.add_audit(
            target_type="claim",
            target_id=root_claim_id,
            audit_type="GLOBAL_PROOF_AUDIT",
            verdict=str(audit.get("verdict", AuditVerdict.UNCERTAIN)),
            failure_class=str(audit.get("failure_class", "")),
            minimal_obligation=str(audit.get("minimal_failed_obligation", "")),
            local_repairable=False,
            artifact_id=audit_artifact.artifact_id,
            auditor_profile="fresh-global-verifier",
        )
        report_paths = write_agent_audited_proof_report(
            self.store, proof_artifact_id=proof_artifact_id
        )
        for tex_path, pdf_path in report_paths:
            report_artifact = self.artifacts.put_text(
                tex_path.read_text(encoding="utf-8"),
                kind="agent_audited_proof_report",
                suffix=".tex",
                metadata={
                    "route_id": route_id,
                    "root_claim_id": root_claim_id,
                    "proof_artifact_id": proof_artifact_id or "",
                    "status": "AGENT_AUDITED",
                    "pdf_path": str(pdf_path.relative_to(self.store.paths.root)) if pdf_path else "",
                },
            )
            self.store.record_artifact(report_artifact)
            self.store.events.append(
                "agent_audited_proof_report_created",
                {
                    "proof_artifact_id": proof_artifact_id,
                    "artifact_id": report_artifact.artifact_id,
                    "tex_path": str(tex_path.relative_to(self.store.paths.root)),
                    "pdf_path": str(pdf_path.relative_to(self.store.paths.root)) if pdf_path else None,
                },
            )
        self.reporter.emit(
            "global_audit",
            f"Fresh global verifier verdict={audit.get('verdict', AuditVerdict.UNCERTAIN)}; "
            f"minimal obligation: {audit.get('minimal_failed_obligation', '')}",
            epoch=epoch,
            route_id=route_id,
            verdict=str(audit.get("verdict", AuditVerdict.UNCERTAIN)),
        )
        if str(audit.get("verdict")) == AuditVerdict.PASS:
            if not report_paths:
                self.reporter.emit(
                    "agent_audited_proof_report_failed",
                    "Both proof audits passed, but final PDF publication failed; promotion is withheld.",
                    epoch=epoch,
                    route_id=route_id,
                    proof_artifact_id=proof_artifact_id or "",
                )
                return
            try:
                transition_claim(
                    self.store, root_claim_id, ClaimStatus.AGENT_AUDITED_GLOBAL
                )
            except InvalidTransition:
                pass

    def _handle_counterexample_candidate(
        self,
        *,
        campaign_id: str,
        epoch: int,
        route_id: str,
        root_claim_id: str,
        candidate: dict[str, Any],
    ) -> bool:
        artifact = self.artifacts.put_text(
            json.dumps(candidate, ensure_ascii=False, indent=2),
            kind="counterexample_candidate",
            suffix=".json",
            metadata={
                "route_id": route_id,
                "root_claim_id": root_claim_id,
                "status": "PENDING_AUDIT",
            },
        )
        self.store.record_artifact(artifact)
        self.store.add_evidence(
            claim_id=root_claim_id,
            evidence_type="COUNTEREXAMPLE_CANDIDATE",
            logical_force="Refutation candidate only; exact admissibility and violation must be audited.",
            scope=str(candidate.get("scope", "submitted candidate scope")),
            artifact_id=artifact.artifact_id,
            status="CANDIDATE",
        )
        self.store.events.append(
            "counterexample_candidate_recorded",
            {
                "campaign_id": campaign_id,
                "epoch": epoch,
                "route_id": route_id,
                "root_claim_id": root_claim_id,
                "artifact_id": artifact.artifact_id,
            },
        )
        self.reporter.emit(
            "counterexample_candidate",
            f"Route {route_id} produced a counterexample candidate; exact admissibility and violation remain to be audited.",
            epoch=epoch,
            route_id=route_id,
            artifact_id=artifact.artifact_id,
        )
        audits = self._run_counterexample_audits(
            campaign_id=campaign_id,
            epoch=epoch,
            route_id=route_id,
            root_claim_id=root_claim_id,
            candidate=candidate,
            candidate_artifact_id=artifact.artifact_id,
        )
        required_checks = {"verified_object", "verified_admissibility", "verified_violation"}
        source_reported = (
            str(candidate.get("origin", "")).upper() == "REFERENCE_REPORTED"
            or bool(str(candidate.get("source_reference", "")).strip())
        )
        passed = len(audits) == 2 and all(
            str(audit.get("verdict")) == AuditVerdict.PASS
            and not bool(audit.get("statement_drift", False))
            and all(
                isinstance(audit.get(field), str) and bool(audit[field].strip())
                for field in required_checks
            )
            and (
                not source_reported
                or (
                    isinstance(audit.get("verified_source_independence"), str)
                    and bool(audit["verified_source_independence"].strip())
                )
            )
            for audit in audits.values()
        )
        if passed:
            passed = self._record_audited_counterexample_note(
                campaign_id=campaign_id,
                epoch=epoch,
                route_id=route_id,
                root_claim_id=root_claim_id,
                counterexample_artifact_id=artifact.artifact_id,
            )
        self.reporter.emit(
            "counterexample_candidate_verified" if passed else "counterexample_candidate_unverified",
            (
                "Both independent counterexample audits passed; the campaign may stop at REFUTATION_CANDIDATE."
                if passed
                else "Counterexample candidate did not pass both independent audits; campaign research continues."
            ),
            epoch=epoch,
            route_id=route_id,
            verified=passed,
        )
        return passed

    def _record_audited_counterexample_note(
        self,
        *,
        campaign_id: str,
        epoch: int,
        route_id: str,
        root_claim_id: str,
        counterexample_artifact_id: str,
    ) -> bool:
        note_paths = write_agent_audited_counterexample_note(
            self.store, counterexample_artifact_id=counterexample_artifact_id
        )
        if not note_paths:
            self.reporter.emit(
                "agent_audited_counterexample_note_failed",
                "Both counterexample audits passed, but PDF publication failed; refutation status is withheld.",
                epoch=epoch,
                route_id=route_id,
                counterexample_artifact_id=counterexample_artifact_id,
            )
            return False
        for tex_path, pdf_path in note_paths:
            tex_artifact = self.artifacts.put_text(
                tex_path.read_text(encoding="utf-8"),
                kind="agent_audited_counterexample_note_latex",
                suffix=".tex",
                metadata={
                    "counterexample_artifact_id": counterexample_artifact_id,
                    "route_id": route_id,
                    "root_claim_id": root_claim_id,
                },
            )
            self.store.record_artifact(tex_artifact)
            pdf_artifact_id = None
            if pdf_path is not None:
                pdf_artifact = self.artifacts.put_bytes(
                    pdf_path.read_bytes(),
                    kind="agent_audited_counterexample_note_pdf",
                    suffix=".pdf",
                    media_type="application/pdf",
                    metadata={
                        "counterexample_artifact_id": counterexample_artifact_id,
                        "tex_artifact_id": tex_artifact.artifact_id,
                        "route_id": route_id,
                        "root_claim_id": root_claim_id,
                    },
                )
                self.store.record_artifact(pdf_artifact)
                pdf_artifact_id = pdf_artifact.artifact_id
            self.store.events.append(
                "agent_audited_counterexample_note_created",
                {
                    "campaign_id": campaign_id,
                    "epoch": epoch,
                    "counterexample_artifact_id": counterexample_artifact_id,
                    "tex_artifact_id": tex_artifact.artifact_id,
                    "pdf_artifact_id": pdf_artifact_id,
                },
            )
            self.reporter.emit(
                "agent_audited_counterexample_note_created",
                "Journal-style LaTeX/PDF note recorded for the independently audited counterexample.",
                epoch=epoch,
                route_id=route_id,
                artifact_id=tex_artifact.artifact_id,
            )
        return True

    def _synthesize_strongest_partial_result(
        self, *, campaign_id: str, epoch: int, contract: dict[str, Any]
    ) -> str | None:
        """Preserve a useful exhausted-campaign result without changing any verdict.

        This is deliberately an archival synthesis rather than a research route:
        it cannot create claims, evidence, routes, or status transitions.  The
        role must return ``NO_MEANINGFUL_RESULT`` unless an exact, useful and
        artifact-backed scoped statement is available.
        """
        role = "result_synthesizer"
        marker = f"result_synthesis_done:{campaign_id}"
        if self.store.get_meta(marker) == "1":
            return None
        if role not in self.config.roles:
            self.reporter.emit(
                "result_synthesis_skipped",
                "No result-synthesizer role is configured; no compromise result was proposed.",
                campaign_id=campaign_id,
                epoch=epoch,
            )
            return None
        provider = self.config.provider_for_role(role)
        if not self.store.budget_available(campaign_id, provider.estimated_cost_usd):
            self.reporter.emit(
                "result_synthesis_skipped",
                "No remaining provider budget for final synthesis; no compromise result was proposed.",
                campaign_id=campaign_id,
                epoch=epoch,
            )
            return None

        artifacts = self.store.list_artifacts(limit=160)
        artifact_ids = {str(item["artifact_id"]) for item in artifacts}
        artifact_digest: list[dict[str, Any]] = []
        remaining_excerpt = 90000
        for artifact in artifacts:
            item = {
                "artifact_id": artifact["artifact_id"],
                "kind": artifact["kind"],
                "relative_path": artifact["relative_path"],
                "metadata": artifact["metadata"],
            }
            path = self.store.paths.root / str(artifact["relative_path"])
            if (
                remaining_excerpt > 0
                and path.exists()
                and path.is_file()
                and path.suffix.lower() in {".json", ".md", ".txt", ".tex"}
            ):
                try:
                    excerpt = path.read_text(encoding="utf-8", errors="replace")[:5000]
                except OSError:
                    excerpt = ""
                if excerpt:
                    item["excerpt"] = excerpt[:remaining_excerpt]
                    remaining_excerpt -= len(item["excerpt"])
            artifact_digest.append(item)

        campaign_state = {
            "campaign": self.store.get_campaign(campaign_id),
            "claims": self.store.list_claims(),
            "routes": self.store.list_routes(campaign_id),
            "attempts": [
                item for item in self.store.list_attempts()
                if str(item.get("campaign_id")) == campaign_id
            ],
            "failure_clusters": self.store.list_failures(),
            "evidence": self.store.list_evidence(),
            "instruction": (
                "This is a non-decisive archival synthesis. It must not claim that the "
                "immutable target is proved or refuted."
            ),
        }
        prompt = render_prompt(
            "result_synthesizer.md",
            problem_contract=json.dumps(contract, indent=2),
            campaign_state=json.dumps(campaign_state, indent=2),
            artifact_digest=json.dumps(artifact_digest, indent=2),
        )
        call = AgentCall(
            role=role,
            slot="result-synthesizer",
            prompt=prompt,
            project_root=self.store.paths.root,
            network_policy=self.config.roles[role].network_policy,
            campaign_id=campaign_id,
            epoch=epoch,
            metadata={
                "task_summary": (
                    "Archive the strongest genuinely useful, precisely scoped partial result, "
                    "or certify that the campaign produced none."
                )
            },
        )
        self.store.set_meta(marker, "1")
        try:
            response = self.runner.call(call)
            data = extract_json_object(response.text)
        except BudgetExceeded as exc:
            self.store.events.append(
                "result_synthesis_skipped",
                {"campaign_id": campaign_id, "epoch": epoch, "reason": str(exc)},
            )
            self.reporter.emit(
                "result_synthesis_skipped",
                "Final synthesis could not reserve budget; no compromise result was proposed.",
                campaign_id=campaign_id,
                epoch=epoch,
            )
            return None
        except Exception as exc:
            self.store.events.append(
                "result_synthesis_failed",
                {"campaign_id": campaign_id, "epoch": epoch, "error": str(exc)},
            )
            self.reporter.emit(
                "result_synthesis_failed",
                "Final synthesis failed; no compromise result was proposed.",
                campaign_id=campaign_id,
                epoch=epoch,
            )
            return None

        verdict = str(data.get("verdict", "NO_MEANINGFUL_RESULT"))
        proposal = data.get("proposal")
        reason = str(data.get("reason_no_proposal", "")).strip()
        used_ids = [str(item) for item in data.get("used_artifact_ids", []) if str(item)]
        valid_proposal = (
            verdict == "MEANINGFUL_PARTIAL_RESULT"
            and isinstance(proposal, dict)
            and all(str(proposal.get(key, "")).strip() for key in (
                "title", "statement", "scope", "support_level", "derivation_or_proof",
                "limitations", "continuation_value",
            ))
            and isinstance(proposal.get("supporting_artifact_ids"), list)
            and bool(proposal.get("supporting_artifact_ids"))
            and all(str(item) in artifact_ids for item in proposal.get("supporting_artifact_ids", []))
            and all(item in artifact_ids for item in used_ids)
        )
        if not valid_proposal:
            detail = reason or (
                "No precisely scoped, artifact-backed partial statement met the final synthesis gate."
            )
            record = {
                "verdict": "NO_MEANINGFUL_RESULT",
                "reason_no_proposal": detail,
                "used_artifact_ids": [item for item in used_ids if item in artifact_ids],
                "campaign_id": campaign_id,
                "epoch": epoch,
            }
            artifact = self.artifacts.put_bytes(
                canonical_json(record).encode("utf-8"),
                kind="result_synthesis_no_proposal",
                suffix=".json",
                media_type="application/json",
                metadata={
                    "campaign_id": campaign_id,
                    "epoch": epoch,
                    "status": "NO_MEANINGFUL_RESULT",
                },
            )
            self.store.record_artifact(artifact)
            self.store.events.append(
                "result_synthesis_no_proposal",
                {"campaign_id": campaign_id, "epoch": epoch, "artifact_id": artifact.artifact_id},
            )
            self.reporter.emit(
                "result_synthesis_no_proposal",
                "Final synthesis found no meaningful artifact-backed partial result; none was proposed.",
                campaign_id=campaign_id,
                epoch=epoch,
                artifact_id=artifact.artifact_id,
            )
            return None

        record = {
            "verdict": "MEANINGFUL_PARTIAL_RESULT",
            "proposal": proposal,
            "used_artifact_ids": used_ids,
            "campaign_id": campaign_id,
            "epoch": epoch,
            "non_decisive": True,
            "status_note": (
                "This archived scoped result does not prove or refute the immutable campaign target."
            ),
        }
        artifact = self.artifacts.put_bytes(
            canonical_json(record).encode("utf-8"),
            kind="strongest_partial_result",
            suffix=".json",
            media_type="application/json",
            metadata={
                "campaign_id": campaign_id,
                "epoch": epoch,
                "status": "PROPOSED_NONDECISIVE",
                "support_level": str(proposal["support_level"]),
                "supporting_artifact_ids": list(proposal["supporting_artifact_ids"]),
            },
        )
        self.store.record_artifact(artifact)
        self.store.events.append(
            "strongest_partial_result_recorded",
            {"campaign_id": campaign_id, "epoch": epoch, "artifact_id": artifact.artifact_id},
        )
        self.reporter.emit(
            "strongest_partial_result_recorded",
            "Final synthesis preserved an artifact-backed scoped partial result; the target remains unsolved.",
            campaign_id=campaign_id,
            epoch=epoch,
            artifact_id=artifact.artifact_id,
        )
        return artifact.artifact_id

    def _record_unsolved_campaign_note(self, *, campaign_id: str) -> None:
        note = write_unsolved_campaign_note(self.store, campaign_id=campaign_id)
        if note is None:
            return
        tex_path, pdf_path = note
        tex_artifact = self.artifacts.put_text(
            tex_path.read_text(encoding="utf-8"),
            kind="unsolved_campaign_note_latex",
            suffix=".tex",
            metadata={"campaign_id": campaign_id, "status": CampaignStatus.COMPLETED_UNSOLVED},
        )
        self.store.record_artifact(tex_artifact)
        pdf_artifact_id = None
        if pdf_path is not None:
            pdf_artifact = self.artifacts.put_bytes(
                pdf_path.read_bytes(),
                kind="unsolved_campaign_note_pdf",
                suffix=".pdf",
                media_type="application/pdf",
                metadata={"campaign_id": campaign_id, "tex_artifact_id": tex_artifact.artifact_id},
            )
            self.store.record_artifact(pdf_artifact)
            pdf_artifact_id = pdf_artifact.artifact_id
        self.store.events.append(
            "unsolved_campaign_note_created",
            {
                "campaign_id": campaign_id,
                "tex_artifact_id": tex_artifact.artifact_id,
                "pdf_artifact_id": pdf_artifact_id,
            },
        )
        self.reporter.emit(
            "unsolved_campaign_note_created",
            "Journal-style LaTeX record created for the exhausted unsolved campaign.",
            campaign_id=campaign_id,
            artifact_id=tex_artifact.artifact_id,
        )

    def _run_counterexample_audits(
        self,
        *,
        campaign_id: str,
        epoch: int,
        route_id: str,
        root_claim_id: str,
        candidate: dict[str, Any],
        candidate_artifact_id: str,
    ) -> dict[str, dict[str, Any]]:
        """Record independent checks of a proposed exact counterexample.

        A counterexample candidate is never promoted to a refutation here. These
        bounded audits check admissibility and the claimed violation, while the
        existing exact-evidence transition remains the only route to REFUTED.
        """
        audits: dict[str, dict[str, Any]] = {}
        audit_specs = (
            (
                "local_verifier",
                "local-counterexample-verifier",
                "local_counterexample_verifier.md",
                "counterexample_packet",
                "LOCAL_COUNTEREXAMPLE_AUDIT",
                "local_counterexample_audit",
                "Local counterexample verifier",
            ),
            (
                "global_verifier",
                "fresh-global-counterexample-verifier",
                "global_counterexample_verifier.md",
                "counterexample_core",
                "GLOBAL_COUNTEREXAMPLE_AUDIT",
                "global_counterexample_audit",
                "Fresh global counterexample verifier",
            ),
        )
        for (
            role,
            slot,
            template,
            packet_name,
            audit_type,
            artifact_kind,
            auditor_profile,
        ) in audit_specs:
            if role not in self.config.roles:
                self.reporter.emit(
                    "counterexample_audit_skipped",
                    f"{auditor_profile} is not configured; the counterexample remains unchecked.",
                    epoch=epoch,
                    route_id=route_id,
                    role=role,
                )
                continue
            packet = {
                "claim": self.store.get_claim(root_claim_id),
                "counterexample_candidate": candidate,
            }
            call = AgentCall(
                role=role,
                slot=slot,
                prompt=render_prompt(
                    template,
                    problem_contract=json.dumps(self._load_contract(), indent=2),
                    **{packet_name: json.dumps(packet, indent=2)},
                ),
                project_root=self.store.paths.root,
                network_policy=self.config.roles[role].network_policy,
                campaign_id=campaign_id,
                route_id=route_id,
                epoch=epoch,
                metadata={
                    "task_summary": (
                        "Independently check counterexample admissibility and the exact claimed violation"
                    )
                },
            )
            try:
                audit = extract_json_object(self.runner.call(call).text)
            except Exception as exc:
                audit = {
                    "verdict": AuditVerdict.UNCERTAIN,
                    "failure_class": FailureClass.VERIFIER_UNCERTAINTY,
                    "minimal_failed_obligation": str(exc),
                    "local_repairable": False,
                }
            audit_artifact = self.artifacts.put_text(
                json.dumps(audit, indent=2), kind=artifact_kind, suffix=".json",
                metadata={
                    "route_id": route_id,
                    "root_claim_id": root_claim_id,
                    "counterexample_artifact_id": candidate_artifact_id,
                    "status": str(audit.get("verdict", AuditVerdict.UNCERTAIN)),
                },
            )
            self.store.record_artifact(audit_artifact)
            self.store.add_audit(
                target_type="claim",
                target_id=root_claim_id,
                audit_type=audit_type,
                verdict=str(audit.get("verdict", AuditVerdict.UNCERTAIN)),
                failure_class=str(audit.get("failure_class", "")),
                minimal_obligation=str(audit.get("minimal_failed_obligation", "")),
                local_repairable=bool(audit.get("local_repairable", False)),
                artifact_id=audit_artifact.artifact_id,
                auditor_profile=auditor_profile,
            )
            audits[role] = audit
            self.reporter.emit(
                "counterexample_audit",
                f"{auditor_profile} verdict={audit.get('verdict', AuditVerdict.UNCERTAIN)}; "
                f"minimal obligation: {audit.get('minimal_failed_obligation', '')}",
                epoch=epoch,
                route_id=route_id,
                role=role,
                verdict=str(audit.get("verdict", AuditVerdict.UNCERTAIN)),
            )
        return audits

    @staticmethod
    def _context_text(value: Any, limit: int) -> str:
        """Return an auditable, bounded excerpt for a role work packet."""
        text = str(value or "").strip()
        return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"

    def _research_context_packet(
        self, *, campaign_id: str, literature_guided: bool
    ) -> dict[str, Any]:
        """Build a compact skill-like handoff for a bounded research invocation.

        Full material is retained in immutable artifacts and source files.  This
        packet carries only recent, high-signal state plus those stable pointers,
        preventing the same dossier/history from consuming every agent context.
        """
        claims = self.store.list_claims()
        if not literature_guided:
            claims = [
                claim for claim in claims
                if not str(claim.get("source", "")).startswith("literature")
            ]
        compact_claims = [
            {
                "claim_id": claim["claim_id"],
                "statement": self._context_text(claim.get("statement"), 900),
                "assumptions": [self._context_text(item, 240) for item in claim.get("assumptions", [])[:8]],
                "scope": self._context_text(claim.get("scope"), 360),
                "status": claim.get("status"),
                "criticality": claim.get("criticality"),
                "source": claim.get("source"),
            }
            for claim in claims[-16:]
        ]
        failures = self.store.list_failures()[-12:]
        compact_failures = [
            {
                "failure_id": item.get("failure_id"),
                "failure_class": item.get("failure_class"),
                "signature": self._context_text(item.get("signature"), 360),
                "logical_scope": self._context_text(item.get("logical_scope"), 720),
                "revival_conditions": self._context_text(item.get("revival_conditions"), 720),
                "status": item.get("status"),
            }
            for item in failures
        ]
        attempts = [
            item for item in self.store.list_attempts()
            if str(item.get("campaign_id")) == campaign_id
        ][-10:]
        compact_attempts = [
            {
                "attempt_id": item.get("attempt_id"),
                "route_id": item.get("route_id"),
                "result_kind": item.get("result_kind"),
                "summary": self._context_text(item.get("summary"), 900),
                "artifact_id": item.get("artifact_id"),
            }
            for item in attempts
        ]
        route_history = self.store.list_routes(campaign_id)
        failed_routes = [item for item in route_history if str(item.get("status")) == RouteStatus.METHOD_FAILED]
        retained_routes = (failed_routes + route_history[-16:])[-28:]
        route_ledger = [
            {
                "route_id": item.get("route_id"),
                "status": item.get("status"),
                "title": self._context_text(item.get("title"), 240),
                "method_family": self._context_text(item.get("method_family"), 240),
                "representation": self._context_text(item.get("representation"), 240),
                "key_lemma": self._context_text(item.get("key_lemma"), 420),
                "central_mechanism": self._context_text(item.get("central_mechanism"), 420),
            }
            for item in retained_routes
        ]
        artifact_index = [
            {
                "artifact_id": item["artifact_id"],
                "kind": item["kind"],
                "relative_path": item["relative_path"],
                "status": item.get("metadata", {}).get("status", ""),
            }
            for item in self.store.list_artifacts(limit=24)
        ]
        return {
            "work_packet_version": 1,
            "claims": compact_claims,
            "failure_clusters": compact_failures,
            "recent_attempts": compact_attempts,
            "route_ledger": route_ledger,
            "artifact_index": artifact_index,
            "retrieval_rule": (
                "Use an artifact ID or relative path only when its exact contents are needed; "
                "do not infer omitted details from this compact packet."
            ),
            "instruction": (
                "Literature is available with a compact source index; verify exact statements from "
                "the cited local source before relying on them."
                if literature_guided else
                "No literature records are included in this offline work packet."
            ),
        }

    def _compact_literature_dossier(self) -> list[dict[str, Any]]:
        """Return a bounded source index with enough exact text to route research."""
        dossier: list[dict[str, Any]] = []
        remaining = 30000
        for source in self.store.list_literature_sources()[-12:]:
            if remaining <= 0:
                break
            item = {
                "source_id": source.get("source_id"),
                "title": self._context_text(source.get("title"), 300),
                "citation": self._context_text(source.get("citation"), 500),
                "source_kind": source.get("source_kind"),
                "exact_statement": self._context_text(source.get("exact_statement"), 1800),
                "assumptions": [self._context_text(value, 240) for value in source.get("assumptions", [])[:10]],
                "locator": self._context_text(source.get("locator"), 500),
                "relative_path": source.get("relative_path"),
                "audit_status": source.get("audit_status"),
            }
            relative = str(source.get("relative_path", ""))
            local = self.store.paths.root / relative if relative else None
            if local and local.exists() and local.is_file() and remaining > 0:
                try:
                    excerpt = local.read_text(encoding="utf-8", errors="replace")[:2600]
                except OSError:
                    excerpt = ""
                if excerpt:
                    excerpt = self._context_text(excerpt, min(2600, remaining))
                    item["local_excerpt"] = excerpt
                    remaining -= len(excerpt)
            dossier.append(item)
        return dossier

    def _offline_project_state(self) -> dict[str, Any]:
        claims = [
            claim
            for claim in self.store.list_claims()
            if not str(claim.get("source", "")).startswith("literature")
        ]
        return {
            "claims": claims,
            "failure_clusters": self.store.list_failures(),
            "instruction": "No literature records are included in this offline view.",
        }

    def _guided_project_state(self) -> dict[str, Any]:
        return {
            "claims": self.store.list_claims(),
            "failure_clusters": self.store.list_failures(),
            "recent_attempts": self.store.list_attempts()[-12:],
            "instruction": (
                "Literature is available. Every imported theorem must be stated with exact assumptions, "
                "a source reference, and an applicability argument. Separate cited mathematics from new work."
            ),
        }

    def _literature_dossier(self) -> list[dict[str, Any]]:
        dossier: list[dict[str, Any]] = []
        for source in self.store.list_literature_sources():
            item = dict(source)
            relative = str(item.get("relative_path", ""))
            path = self.store.paths.root / relative if relative else None
            if path and path.exists() and path.is_file() and path.suffix.lower() in {
                ".md",
                ".txt",
                ".tex",
            }:
                item["local_excerpt"] = path.read_text(encoding="utf-8")[:12000]
            dossier.append(item)
        return dossier

    def _record_operational_config_revision(
        self, *, campaign_id: str, resumed: bool
    ) -> dict[str, Any]:
        snapshot = operational_config_snapshot(self.config)
        effective_sha256 = content_hash(canonical_json(snapshot))
        source_path = "<in-memory-config>"
        source_sha256 = effective_sha256
        if self.config_path is not None:
            source_path = str(self.config_path)
            try:
                source_sha256 = content_hash(self.config_path.read_bytes())
            except OSError:
                source_path = f"{source_path} (unreadable at run time)"
        return self.store.record_campaign_config_revision(
            campaign_id,
            snapshot=snapshot,
            effective_sha256=effective_sha256,
            source_sha256=source_sha256,
            source_path=source_path,
            reason=(
                "Operator resumed with this operational configuration revision."
                if resumed
                else "Initial operational configuration snapshot."
            ),
            author="campaign-resume" if resumed else "campaign-start",
        )

    def _load_contract(self, *, seal_if_unstarted: bool = False) -> dict[str, Any]:
        if not self.store.paths.contract.exists():
            raise FileNotFoundError(
                f"No problem contract at {self.store.paths.contract}. "
                "Run `ariadne contract set` first."
            )
        contract = read_json(self.store.paths.contract)
        actual = content_hash(self.store.paths.contract.read_bytes())
        expected = self.store.get_meta("problem_contract_sha256")
        if expected is not None and expected != actual:
            raise RuntimeError(
                "Problem contract fingerprint mismatch. The frozen contract was modified; "
                "create a new project for a revised statement."
            )
        if expected is None and seal_if_unstarted:
            if self.store.latest_campaign() is not None:
                raise RuntimeError(
                    "Existing campaign has no recorded contract fingerprint; refusing to resume "
                    "an unverifiable contract. Create a new project or explicitly migrate it."
                )
            self.store.set_meta("problem_contract_sha256", actual)
        return contract

    def _ensure_root_claim(self, contract: dict[str, Any]) -> str:
        existing = self.store.get_meta("root_claim_id")
        if existing:
            return existing
        statement = self._contract_statement(contract)
        assumptions = [str(x) for x in contract.get("hypotheses", [])]
        claim_id = self.store.add_claim(
            statement=statement,
            assumptions=assumptions,
            scope="root problem contract",
            status=ClaimStatus.PROPOSED,
            criticality="target",
            source="problem_contract",
        )
        self.store.set_meta("root_claim_id", claim_id)
        return claim_id

    @staticmethod
    def _contract_statement(contract: dict[str, Any]) -> str:
        statement = contract.get("statement", "")
        if isinstance(statement, dict):
            return str(statement.get("text", canonical_json(statement)))
        return str(statement)
