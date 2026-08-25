from __future__ import annotations

import unittest

from ariadne_math.enums import RouteStatus
from ariadne_math.sentinel import arbitrate_intervention, early_stop_has_exact_literature_evidence


class SentinelTests(unittest.TestCase):
    def test_valid_difference_rejection_gets_bounded_continuation(self) -> None:
        result = arbitrate_intervention(
            intervention_id="INT-1",
            intervention={"kind": "KNOWN_ROUTE", "early_stop": True},
            response={
                "decision": "REJECT_DIFFERENT_ROUTE",
                "difference_certificate": {
                    "representation_difference": "dynamic rather than fixed weight",
                    "decisive_test": "derive the new cross-term",
                },
                "proposed_test": "derive the new cross-term",
            },
            current_epoch=1,
            novelty_deadline_epochs=1,
            require_certificate=True,
        )
        self.assertEqual(result.action, "CONTINUE_PROVISIONAL")
        self.assertEqual(result.route_status, RouteStatus.ACTIVE)
        self.assertEqual(result.novelty_obligation["deadline_epoch"], 2)

    def test_vague_rejection_is_stopped(self) -> None:
        result = arbitrate_intervention(
            intervention_id="INT-1",
            intervention={"kind": "KNOWN_ROUTE", "early_stop": True},
            response={
                "decision": "REJECT_DIFFERENT_ROUTE",
                "difference_certificate": {},
            },
            current_epoch=1,
            novelty_deadline_epochs=1,
            require_certificate=True,
        )
        self.assertEqual(result.action, "STOP_ROUTE")
        self.assertEqual(result.route_status, RouteStatus.EARLY_STOPPED_KNOWN_ROUTE)

    def test_inaccessible_or_concrete_lead_cannot_request_early_stop(self) -> None:
        concrete_lead = {
            "early_stop": True, "evidence_status": "CONCRETE_LEAD",
            "source_refs": ["doi:unavailable"], "applicability_conditions": ["suggested condition"],
        }
        inaccessible = {
            "early_stop": True, "evidence_status": "INACCESSIBLE",
            "source_refs": ["paywalled reference"], "applicability_conditions": ["unknown"],
        }
        verified = {
            "early_stop": True, "evidence_status": "EXACT_VERIFIED",
            "source_refs": ["SRC-local"], "applicability_conditions": ["all hypotheses checked"],
        }
        self.assertFalse(early_stop_has_exact_literature_evidence(concrete_lead))
        self.assertFalse(early_stop_has_exact_literature_evidence(inaccessible))
        self.assertTrue(early_stop_has_exact_literature_evidence(verified))

    def test_sentinel_can_withdraw_stop(self) -> None:
        result = arbitrate_intervention(
            intervention_id="INT-2",
            intervention={"kind": "DIFFERENCE_CONFIRMED", "early_stop": False},
            response={},
            current_epoch=2,
            novelty_deadline_epochs=1,
            require_certificate=True,
        )
        self.assertEqual(result.action, "CONTINUE")
        self.assertEqual(result.novelty_obligation, {})


if __name__ == "__main__":
    unittest.main()
