from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ariadne_math.cli import DEFAULT_MOCK_CONFIG
from ariadne_math.config import load_config
from ariadne_math.controller import CampaignController
from ariadne_math.store import ResearchStore
from ariadne_math.util import write_json


class DemoTests(unittest.TestCase):
    def test_offline_sentinel_negotiation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ResearchStore(root)
            write_json(
                store.paths.contract,
                {
                    "problem_id": "T",
                    "statement": {"text": "Prove or refute P"},
                    "hypotheses": [],
                    "success_criteria": {"proof": "proof", "refutation": "counterexample"},
                    "formalization_policy": {
                        "lean_allowed_only_after_human_checked_proof": True
                    },
                },
            )
            literature_file = store.paths.literature / "known.md"
            literature_file.write_text("Known fixed-weight route.", encoding="utf-8")
            store.add_literature_source(
                title="Known route",
                citation="demo",
                source_kind="note",
                exact_statement="fixed-weight route has loss",
                assumptions=["fixed weight"],
                locator="known.md",
                relative_path=str(literature_file.relative_to(root)),
            )
            config_path = root / "config.toml"
            config_path.write_text(DEFAULT_MOCK_CONFIG, encoding="utf-8")
            with mock.patch(
                "ariadne_math.controller.record_campaign_epoch",
                return_value={"enabled": False, "recorded": False, "tagged": False},
            ) as record_epoch:
                result = CampaignController(root, load_config(config_path)).run()
            self.assertEqual(result["status"], "COMPLETED_UNSOLVED")
            self.assertTrue(record_epoch.called)
            self.assertEqual(record_epoch.call_args.kwargs["campaign_id"], result["campaign_id"])
            interventions = store.list_interventions(result["campaign_id"])
            self.assertTrue(any(i["kind"] == "KNOWN_ROUTE" for i in interventions))
            self.assertTrue(
                any(i["kind"] == "DIFFERENCE_CONFIRMED" for i in interventions)
            )
            known = next(i for i in interventions if i["kind"] == "KNOWN_ROUTE")
            self.assertEqual(known["status"], "RESOLVED_DIFFERENCE_CONFIRMED")
            routes = store.list_routes(result["campaign_id"])
            weighted = next(r for r in routes if r["title"] == "Weighted coercivity route")
            self.assertEqual(weighted["status"], "ACTIVE")
            self.assertEqual(weighted["novelty_obligation"], {})


if __name__ == "__main__":
    unittest.main()
