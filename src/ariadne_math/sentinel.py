from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .enums import InterventionDecision, InterventionKind, RouteStatus
from .failures import novelty_certificate_present


@dataclass(frozen=True)
class ArbitrationResult:
    action: str
    route_status: str | None
    intervention_status: str
    novelty_obligation: dict[str, Any]
    rationale: str


def early_stop_has_exact_literature_evidence(intervention: dict[str, Any]) -> bool:
    """Only accessible, exact, applicable source evidence can stop a route."""
    return (
        bool(intervention.get("early_stop", False))
        and str(intervention.get("evidence_status", "")) == "EXACT_VERIFIED"
        and bool([ref for ref in intervention.get("source_refs", []) if str(ref).strip()])
        and bool([condition for condition in intervention.get("applicability_conditions", []) if str(condition).strip()])
    )


def arbitrate_intervention(
    *,
    intervention_id: str,
    intervention: dict[str, Any],
    response: dict[str, Any],
    current_epoch: int,
    novelty_deadline_epochs: int,
    require_certificate: bool,
) -> ArbitrationResult:
    kind = str(intervention.get("kind", InterventionKind.KNOWN_ROUTE))
    early_stop = bool(intervention.get("early_stop", False))

    if kind == InterventionKind.DIFFERENCE_CONFIRMED:
        return ArbitrationResult(
            action="CONTINUE",
            route_status=RouteStatus.ACTIVE,
            intervention_status="WITHDRAWN_DIFFERENCE_CONFIRMED",
            novelty_obligation={},
            rationale="Literature sentinel confirmed a material difference.",
        )

    if not early_stop:
        return ArbitrationResult(
            action="CONTINUE",
            route_status=None,
            intervention_status="RECORDED_NO_STOP",
            novelty_obligation={},
            rationale="Intervention supplied information but did not request an early stop.",
        )

    raw_decision = str(response.get("decision", "NEED_HUMAN_REVIEW"))
    try:
        decision = InterventionDecision(raw_decision)
    except ValueError:
        decision = InterventionDecision.NEED_HUMAN_REVIEW

    if decision == InterventionDecision.ACCEPT_STOP:
        return ArbitrationResult(
            action="STOP_ROUTE",
            route_status=RouteStatus.EARLY_STOPPED_KNOWN_ROUTE,
            intervention_status="ACCEPTED_STOP",
            novelty_obligation={},
            rationale="Offline researcher accepted that the route duplicates a known route.",
        )

    if decision == InterventionDecision.NEED_HUMAN_REVIEW:
        return ArbitrationResult(
            action="PAUSE_HUMAN",
            route_status=RouteStatus.NEEDS_HUMAN_IDEA,
            intervention_status="NEEDS_HUMAN_REVIEW",
            novelty_obligation={},
            rationale="The literature/offline disagreement requires human adjudication.",
        )

    if decision == InterventionDecision.REJECT_DIFFERENT_ROUTE:
        certificate = response.get("difference_certificate")
        if require_certificate and not novelty_certificate_present(certificate):
            return ArbitrationResult(
                action="STOP_ROUTE",
                route_status=RouteStatus.EARLY_STOPPED_KNOWN_ROUTE,
                intervention_status="REJECTION_INVALID_NO_CERTIFICATE",
                novelty_obligation={},
                rationale="The rejection did not identify a material difference and decisive test.",
            )
        deadline = current_epoch + max(1, novelty_deadline_epochs)
        obligation = {
            "intervention_id": intervention_id,
            "kind": "DISTINGUISH_ROUTE_FROM_LITERATURE",
            "proposed_test": str(response.get("proposed_test", "")),
            "difference_certificate": certificate or {},
            "deadline_epoch": deadline,
        }
        return ArbitrationResult(
            action="CONTINUE_PROVISIONAL",
            route_status=RouteStatus.ACTIVE,
            intervention_status="REJECTED_DIFFERENT_ROUTE_PROVISIONAL",
            novelty_obligation=obligation,
            rationale="The route may continue for one bounded distinguishing test.",
        )

    if decision == InterventionDecision.REJECT_NOT_APPLICABLE:
        reason = str(response.get("reason", "")).strip()
        certificate = response.get("difference_certificate")
        if not reason or (require_certificate and not novelty_certificate_present(certificate)):
            return ArbitrationResult(
                action="PAUSE_HUMAN",
                route_status=RouteStatus.NEEDS_HUMAN_IDEA,
                intervention_status="APPLICABILITY_DISPUTE_UNRESOLVED",
                novelty_obligation={},
                rationale="Applicability was disputed without a sufficiently exact mismatch.",
            )
        obligation = {
            "intervention_id": intervention_id,
            "kind": "CHECK_SOURCE_APPLICABILITY_MISMATCH",
            "proposed_test": str(response.get("proposed_test", "")),
            "difference_certificate": certificate or {},
            "deadline_epoch": current_epoch + max(1, novelty_deadline_epochs),
        }
        return ArbitrationResult(
            action="CONTINUE_PROVISIONAL",
            route_status=RouteStatus.ACTIVE,
            intervention_status="REJECTED_NOT_APPLICABLE_PROVISIONAL",
            novelty_obligation=obligation,
            rationale="The route may continue while it demonstrates the claimed mismatch.",
        )

    return ArbitrationResult(
        action="PAUSE_HUMAN",
        route_status=RouteStatus.NEEDS_HUMAN_IDEA,
        intervention_status="UNRECOGNIZED_RESPONSE",
        novelty_obligation={},
        rationale="Unrecognized intervention response.",
    )


def novelty_evidence_satisfies(
    novelty_obligation: dict[str, Any], novelty_evidence: list[dict[str, Any]]
) -> bool:
    if not novelty_obligation:
        return True
    expected = str(novelty_obligation.get("intervention_id", ""))
    for item in novelty_evidence:
        if str(item.get("intervention_id", "")) == expected and str(
            item.get("evidence", "")
        ).strip():
            return True
    return False
