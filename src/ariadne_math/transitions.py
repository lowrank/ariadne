from __future__ import annotations

from .enums import ClaimStatus, EvidenceType
from .store import ResearchStore


class InvalidTransition(RuntimeError):
    pass


_ALLOWED = {
    ClaimStatus.PROPOSED: {
        ClaimStatus.HEURISTIC,
        ClaimStatus.EMPIRICALLY_OBSERVED,
        ClaimStatus.SOURCE_REPORTED,
        ClaimStatus.CANDIDATE_LEMMA,
        ClaimStatus.REFUTED,
        ClaimStatus.CHALLENGED,
    },
    ClaimStatus.HEURISTIC: {
        ClaimStatus.CANDIDATE_LEMMA,
        ClaimStatus.REFUTED,
        ClaimStatus.CHALLENGED,
    },
    ClaimStatus.EMPIRICALLY_OBSERVED: {
        ClaimStatus.CANDIDATE_LEMMA,
        ClaimStatus.REFUTED,
        ClaimStatus.CHALLENGED,
    },
    ClaimStatus.SOURCE_REPORTED: {
        ClaimStatus.CANDIDATE_LEMMA,
        ClaimStatus.CONDITIONAL,
        ClaimStatus.CHALLENGED,
    },
    ClaimStatus.CANDIDATE_LEMMA: {
        ClaimStatus.AGENT_AUDITED_LOCAL,
        ClaimStatus.CHALLENGED,
        ClaimStatus.REFUTED,
        ClaimStatus.CONDITIONAL,
    },
    ClaimStatus.AGENT_AUDITED_LOCAL: {
        ClaimStatus.AGENT_AUDITED_GLOBAL,
        ClaimStatus.CHALLENGED,
        ClaimStatus.REFUTED,
        ClaimStatus.CONDITIONAL,
    },
    ClaimStatus.AGENT_AUDITED_GLOBAL: {
        ClaimStatus.HUMAN_CHECKED,
        ClaimStatus.CHALLENGED,
        ClaimStatus.REFUTED,
        ClaimStatus.CONDITIONAL,
    },
    ClaimStatus.HUMAN_CHECKED: {
        ClaimStatus.FORMALLY_CERTIFIED,
        ClaimStatus.CHALLENGED,
        ClaimStatus.REVOKED,
    },
    ClaimStatus.FORMALLY_CERTIFIED: {ClaimStatus.REVOKED},
}


def transition_claim(
    store: ResearchStore,
    claim_id: str,
    target: ClaimStatus,
    *,
    evidence_type: EvidenceType | None = None,
    artifact_present: bool = False,
) -> None:
    current = ClaimStatus(store.get_claim(claim_id)["status"])
    if target not in _ALLOWED.get(current, set()):
        raise InvalidTransition(f"Cannot transition {current} -> {target}")

    if target == ClaimStatus.EMPIRICALLY_OBSERVED:
        if evidence_type not in {
            EvidenceType.FLOATING_POINT_EXPERIMENT,
            EvidenceType.EXHAUSTIVE_FINITE_SEARCH,
            EvidenceType.INTERVAL_CERTIFICATE,
            EvidenceType.SYMBOLIC_CERTIFICATE,
        } or not artifact_present:
            raise InvalidTransition(
                "EMPIRICALLY_OBSERVED requires a computation/certificate artifact"
            )
    if target == ClaimStatus.REFUTED:
        if evidence_type != EvidenceType.EXACT_COUNTEREXAMPLE or not artifact_present:
            raise InvalidTransition("REFUTED requires an exact counterexample artifact")
    if target == ClaimStatus.HUMAN_CHECKED:
        if not store.has_passing_human_review("claim", claim_id):
            raise InvalidTransition("HUMAN_CHECKED requires a passing human review")
    if target == ClaimStatus.FORMALLY_CERTIFIED:
        if evidence_type != EvidenceType.LEAN_PROOF or not artifact_present:
            raise InvalidTransition("FORMALLY_CERTIFIED requires a Lean proof artifact")

    store.transition_claim(claim_id, target)
