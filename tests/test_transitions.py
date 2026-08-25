from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ariadne_math.enums import ClaimStatus, EvidenceType
from ariadne_math.formalization import FormalizationGateClosed, certify_formalization
from ariadne_math.store import ResearchStore
from ariadne_math.transitions import InvalidTransition, transition_claim


class TransitionTests(unittest.TestCase):
    def test_lean_gate_is_closed_before_human_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            claim = store.add_claim(statement="P", status=ClaimStatus.PROPOSED)
            with self.assertRaises(FormalizationGateClosed):
                certify_formalization(
                    store,
                    claim_id=claim,
                    toolchain="test",
                    verify_command=["python", "-c", "raise SystemExit(0)"],
                )

    def test_full_status_ladder_and_formal_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            claim = store.add_claim(statement="P", status=ClaimStatus.PROPOSED)
            transition_claim(store, claim, ClaimStatus.CANDIDATE_LEMMA)
            transition_claim(store, claim, ClaimStatus.AGENT_AUDITED_LOCAL)
            transition_claim(store, claim, ClaimStatus.AGENT_AUDITED_GLOBAL)
            store.add_human_review(
                target_type="claim",
                target_id=claim,
                reviewer="tester",
                verdict="PASS",
                notes="checked",
            )
            transition_claim(store, claim, ClaimStatus.HUMAN_CHECKED)
            formal_id = certify_formalization(
                store,
                claim_id=claim,
                toolchain="test-toolchain",
                verify_command=["python", "-c", "print('verified')"],
            )
            self.assertTrue(formal_id.startswith("FRM-"))
            self.assertEqual(
                store.get_claim(claim)["status"], ClaimStatus.FORMALLY_CERTIFIED
            )

    def test_refutation_requires_exact_counterexample_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            claim = store.add_claim(statement="P", status=ClaimStatus.PROPOSED)
            with self.assertRaises(InvalidTransition):
                transition_claim(store, claim, ClaimStatus.REFUTED)
            transition_claim(
                store,
                claim,
                ClaimStatus.REFUTED,
                evidence_type=EvidenceType.EXACT_COUNTEREXAMPLE,
                artifact_present=True,
            )


if __name__ == "__main__":
    unittest.main()
