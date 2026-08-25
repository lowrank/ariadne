from __future__ import annotations

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from ariadne_math.config import load_config
from ariadne_math.enums import RouteMode
from ariadne_math.cli import main as cli_main
from ariadne_math.contracts import CONTRACT_TEMPLATE
from ariadne_math.controller import CampaignController
from ariadne_math.artifacts import ArtifactStore
from ariadne_math.reports import (
    LatexValidationError, build_report, validate_proof_latex,
    write_agent_audited_proof_report, write_continuation_brief,
    write_proof_candidate_note,
)
from ariadne_math.models import AgentCall
from ariadne_math.resources import assess_experiment_resources
from ariadne_math.setup_wizard import (
    SetupAnswers, _cache_open_literature_pdf, _open_pdf_urls,
    collect_setup_answers, generate_setup,
)
from ariadne_math.store import ResearchStore
from ariadne_math.activity import NullActivityReporter
from ariadne_math.tui import AriadneTUI, _prepare_existing_project_start, chat_intent_to_command, run_tui
from ariadne_math.util import content_hash, write_json


MOCK_BASE = """[budget]
max_epochs = 1
max_calls = 20
max_cost_usd = 1.0
stagnation_epochs = 2
duplicate_failure_limit = 2

[mode]
name = \"{mode}\"
offline_agents = {offline_agents}
research_agents = {research_agents}
parallel = true
literature_intervention = {literature_intervention}
require_route_difference_certificate = {literature_intervention}
novelty_deadline_epochs = 1
allow_experiments = false
route_similarity_threshold = 0.82

[providers.mock]
kind = \"mock\"
estimated_cost_usd = 0.0

[roles.offline_researcher]
provider = \"mock\"
network_policy = \"deny\"

[roles.literature_researcher]
provider = \"mock\"
network_policy = \"allow\"

[roles.contract_author]
provider = \"mock\"
network_policy = \"deny\"

[roles.contract_resolver]
provider = \"mock\"
network_policy = \"allow\"

[roles.literature_author]
provider = \"mock\"
network_policy = \"allow\"

[roles.intervention_responder]
provider = \"mock\"
network_policy = \"deny\"

[roles.literature_sentinel]
provider = \"mock\"
network_policy = \"allow\"

[roles.local_verifier]
provider = \"mock\"
network_policy = \"deny\"

[roles.global_verifier]
provider = \"mock\"
network_policy = \"deny\"

[roles.conceptual_pivot]
provider = \"mock\"
network_policy = \"deny\"

[roles.result_synthesizer]
provider = \"mock\"
network_policy = \"deny\"

[roles.instruction_interpreter]
provider = \"mock\"
network_policy = \"deny\"
"""


class ModeSetupTuiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.store = ResearchStore(self.root)
        contract = {
            **CONTRACT_TEMPLATE,
            "title": "Mode test",
            "statement": {
                "text": "For every admissible object X, prove or refute P(X).",
                "formal_quantifier_outline": "forall X, admissible(X) -> P(X)",
            },
        }
        write_json(self.store.paths.contract, contract)
        note = self.store.paths.literature / "dossier.md"
        note.write_text("# Dossier\n\nMock source route.", encoding="utf-8")
        self.store.add_literature_source(
            title="Mock dossier",
            citation="Mock",
            source_kind="local_note",
            exact_statement="Mock source theorem",
            assumptions=[],
            locator="dossier.md",
            relative_path=str(note.relative_to(self.root)),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _config(self, mode: str, offline: int = 2, research: int = 3) -> Path:
        contract = json.loads(self.store.paths.contract.read_text(encoding="utf-8"))
        contract["research_mode"] = mode
        write_json(self.store.paths.contract, contract)
        path = self.root / f"{mode}.toml"
        path.write_text(
            MOCK_BASE.format(
                mode=mode,
                offline_agents=offline,
                research_agents=research,
                literature_intervention=("true" if mode == "offline_sentinel" else "false"),
            ),
            encoding="utf-8",
        )
        return path

    def test_campaign_refuses_a_tampered_frozen_contract(self) -> None:
        config = load_config(self._config("offline_only", offline=1))
        first = CampaignController(self.root, config).run()
        contract = json.loads(self.store.paths.contract.read_text(encoding="utf-8"))
        contract["statement"]["text"] = "A silently weakened theorem."
        write_json(self.store.paths.contract, contract)
        with self.assertRaisesRegex(RuntimeError, "fingerprint mismatch"):
            CampaignController(self.root, config).run(new_campaign=False)
        campaign_id = str(first["campaign_id"])
        self.assertEqual(
            self.store.get_campaign(campaign_id)["status"], "CONTRACT_CHANGED"
        )
        self.assertTrue(any(
            event["event_type"] == "campaign_contract_changed"
            and event["payload"]["reason"] == "CONTRACT_CHANGED"
            for event in self.store.events.read_all()
        ))
        self.assertIn("CONTRACT_CHANGED", build_report(self.store))

    def test_literature_guided_uses_configured_researchers_and_no_sentinel(self) -> None:
        config = load_config(self._config("literature_guided", research=3))
        result = CampaignController(self.root, config).run()
        runs = self.store.list_agent_runs(str(result["campaign_id"]))
        roles = [row["role"] for row in runs]
        self.assertEqual(roles.count("literature_researcher"), 3)
        self.assertNotIn("literature_sentinel", roles)
        self.assertNotIn("offline_researcher", roles)
        tasks = self.store.list_tasks(str(result["campaign_id"]))
        # Three primary literature researchers plus one bounded archival synthesis.
        self.assertEqual(len(tasks), 4)
        self.assertTrue(all(task["status"] == "COMPLETED" for task in tasks))
        self.assertIn("result_synthesizer", roles)
        self.assertTrue(any(c["status"] == "SOURCE_REPORTED" for c in self.store.list_claims()))

    def test_offline_only_never_invokes_sentinel(self) -> None:
        config = load_config(self._config("offline_only", offline=2))
        result = CampaignController(self.root, config).run()
        roles = [
            row["role"] for row in self.store.list_agent_runs(str(result["campaign_id"]))
        ]
        self.assertEqual(roles.count("offline_researcher"), 2)
        self.assertNotIn("literature_sentinel", roles)
        self.assertEqual(result["status"], "COMPLETED_UNSOLVED")
        self.assertEqual(
            len(self.store.list_artifacts(kind="unsolved_campaign_note_latex")), 1
        )
        self.assertEqual(
            len(self.store.list_artifacts(kind="result_synthesis_no_proposal")), 1
        )
        self.assertEqual(
            len(self.store.list_artifacts(kind="strongest_partial_result")), 0
        )

    def test_refutation_candidate_triggers_independent_audits(self) -> None:
        config = load_config(self._config("offline_only", offline=1))
        controller = CampaignController(self.root, config)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())
        route_id = self.store.add_route(
            campaign_id=campaign_id,
            title="Exact obstruction",
            target_claim_id=root_claim_id,
            mode="REFUTATIONAL",
            method_family="explicit construction",
            representation="native variables",
            key_lemma="verify admissibility",
            central_mechanism="the conclusion is violated exactly",
            decisive_test="substitute the construction",
            difference_from_existing="new exact construction",
            fingerprint="exact obstruction",
            independence_cluster="counterexample",
            owner_slot="offline-1",
        )
        outcome = {
            "route": None,
            "status": "COUNTEREXAMPLE_CANDIDATE",
            "summary": "Submitted an alleged exact counterexample.",
            "claims": [],
            "failures": [],
            "decisive_events": [{"type": "COUNTEREXAMPLE", "description": "alleged violation"}],
            "novelty_evidence": [],
            "next_task": "Audit the alleged counterexample.",
            "proof_candidate": None,
            "counterexample_candidate": {
                "description": "An explicit admissible object violating the conclusion.",
                "verification": "All assumptions and the failed conclusion are checked exactly.",
                "scope": "The exact root claim.",
            },
        }
        _, proof_found, refutation_found = controller._process_research_outcome(
            campaign_id=campaign_id,
            epoch=1,
            call=AgentCall(
                role="offline_researcher",
                slot="offline-1",
                prompt="test",
                project_root=self.root,
                network_policy="deny",
                campaign_id=campaign_id,
                route_id=route_id,
                epoch=1,
            ),
            outcome=outcome,
            root_claim_id=root_claim_id,
        )
        self.assertFalse(proof_found)
        self.assertFalse(refutation_found)
        self.assertEqual(self.store.get_route(route_id)["status"], "ACTIVE")
        audits = self.store.list_audits(target_type="claim", target_id=root_claim_id)
        self.assertEqual(
            {audit["audit_type"] for audit in audits},
            {"LOCAL_COUNTEREXAMPLE_AUDIT", "GLOBAL_COUNTEREXAMPLE_AUDIT"},
        )
        runs = self.store.list_agent_runs(campaign_id)
        self.assertIn("local_verifier", [run["role"] for run in runs])
        self.assertIn("global_verifier", [run["role"] for run in runs])
        self.assertIn("Counterexample audits", build_report(self.store))

    def test_literature_access_failure_is_not_a_mathematical_obstruction(self) -> None:
        config = load_config(self._config("literature_guided", research=1))
        controller = CampaignController(self.root, config)
        campaign_id = self.store.create_campaign(
            mode="literature_guided", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())
        route_id = self.store.add_route(
            campaign_id=campaign_id, title="Concrete reformulation", target_claim_id=root_claim_id,
            mode="DEDUCTIVE", method_family="reformulation", representation="concrete finite form",
            key_lemma="derive equivalent statement", central_mechanism="exact reduction",
            decisive_test="check equivalence", difference_from_existing="first concrete form",
            fingerprint="concrete reformulation", independence_cluster="reformulation", owner_slot="literature-1",
        )
        outcome = {
            "route": None, "status": "PROGRESS", "summary": "A paywalled paper suggests a concrete formulation.",
            "claims": [],
            "failures": [{
                "failure_class": "SOURCE_MISMATCH", "signature": "paywalled old reference unavailable",
                "logical_scope": "literature access only; no mathematical conclusion",
                "revival_conditions": "obtain a copy or derive the concrete formulation independently",
            }],
            "decisive_events": [], "novelty_evidence": [],
            "next_task": "Derive the formulation directly from the contract.",
            "proof_candidate": None, "counterexample_candidate": None,
        }
        controller._process_research_outcome(
            campaign_id=campaign_id, epoch=1,
            call=AgentCall(role="literature_researcher", slot="literature-1", prompt="test",
                project_root=self.root, network_policy="allow", campaign_id=campaign_id,
                route_id=route_id, epoch=1),
            outcome=outcome, root_claim_id=root_claim_id,
        )
        self.assertEqual(self.store.list_failures(), [])
        self.assertEqual(self.store.get_route(route_id)["status"], "ACTIVE")
        self.assertTrue(any(
            event["event_type"] == "literature_access_uncertainty"
            for event in self.store.events.read_all()
        ))

    def test_counterexample_stops_only_after_explicit_independent_checks(self) -> None:
        config = load_config(self._config("offline_only", offline=1))
        controller = CampaignController(self.root, config)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())
        route_id = self.store.add_route(
            campaign_id=campaign_id, title="Exact witness", target_claim_id=root_claim_id,
            mode="REFUTATIONAL", method_family="explicit construction", representation="native",
            key_lemma="verify every hypothesis", central_mechanism="exact violation",
            decisive_test="substitute witness", difference_from_existing="first witness",
            fingerprint="exact witness", independence_cluster="counterexample", owner_slot="offline-1",
        )
        candidate = {
            "description": "A concrete admissible witness.",
            "verification": "The claimed violation is checked symbolically.",
            "scope": "The exact root claim.",
        }
        audit = {
            "verdict": "PASS", "failure_class": "", "minimal_failed_obligation": "",
            "local_repairable": False, "statement_drift": False,
            "recommended_transition": "PROMOTE_GLOBAL",
            "verified_object": "The submitted witness is exactly the stated object.",
            "verified_admissibility": "Each root hypothesis holds for the witness.",
            "verified_violation": "Substitution makes the exact required conclusion false.",
        }
        with mock.patch.object(
            controller.runner, "call", return_value=mock.Mock(text=json.dumps(audit))
        ):
            passed = controller._handle_counterexample_candidate(
                campaign_id=campaign_id, epoch=1, route_id=route_id,
                root_claim_id=root_claim_id, candidate=candidate,
            )
        self.assertTrue(passed)
        audits = self.store.list_audits(target_type="claim", target_id=root_claim_id)
        self.assertEqual(
            {item["audit_type"] for item in audits},
            {"LOCAL_COUNTEREXAMPLE_AUDIT", "GLOBAL_COUNTEREXAMPLE_AUDIT"},
        )
        for item in audits:
            artifact = self.store.get_artifact(item["artifact_id"])
            self.assertEqual(artifact["metadata"]["status"], "PASS")
            self.assertTrue(artifact["metadata"]["counterexample_artifact_id"])
        notes = self.store.list_artifacts(kind="agent_audited_counterexample_note_latex")
        self.assertEqual(len(notes), 1)
        note = (self.root / notes[0]["relative_path"]).read_text(encoding="utf-8")
        self.assertIn("Audited Counterexample Candidate", note)
        self.assertIn("Independent counterexample audits", note)
        self.assertTrue(any(
            event["event_type"] == "agent_audited_counterexample_note_created"
            for event in self.store.events.read_all()
        ))

    def test_reference_counterexample_without_independent_reconstruction_does_not_stop(self) -> None:
        config = load_config(self._config("offline_only", offline=1))
        controller = CampaignController(self.root, config)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())
        route_id = self.store.add_route(
            campaign_id=campaign_id, title="Incomplete witness", target_claim_id=root_claim_id,
            mode="REFUTATIONAL", method_family="construction", representation="native",
            key_lemma="check assumptions", central_mechanism="claimed violation",
            decisive_test="substitute", difference_from_existing="first witness",
            fingerprint="incomplete witness", independence_cluster="counterexample", owner_slot="offline-1",
        )
        incomplete_pass = {
            "verdict": "PASS", "failure_class": "", "minimal_failed_obligation": "",
            "local_repairable": False, "statement_drift": False,
            "recommended_transition": "PROMOTE_GLOBAL",
            "verified_object": "The stated witness is identified.",
            "verified_admissibility": "The written hypotheses hold.",
            "verified_violation": "The written conclusion fails.",
            "verified_source_independence": None,
        }
        with mock.patch.object(
            controller.runner, "call", return_value=mock.Mock(text=json.dumps(incomplete_pass))
        ):
            passed = controller._handle_counterexample_candidate(
                campaign_id=campaign_id, epoch=1, route_id=route_id, root_claim_id=root_claim_id,
                candidate={
                    "description": "x", "verification": "claimed", "scope": "root",
                    "origin": "REFERENCE_REPORTED", "source_reference": "Example 4 in a cited paper",
                    "independent_derivation": "The source has not yet been independently reconstructed.",
                },
            )
        self.assertFalse(passed)

    def test_conflicting_candidate_payload_never_stops_a_campaign(self) -> None:
        config = load_config(self._config("offline_only", offline=1))
        controller = CampaignController(self.root, config)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())
        route_id = self.store.add_route(
            campaign_id=campaign_id, title="Conflicting response", target_claim_id=root_claim_id,
            mode="DEDUCTIVE", method_family="algebra", representation="native",
            key_lemma="bridge", central_mechanism="identity", decisive_test="exact check",
            difference_from_existing="first route", fingerprint="conflict", independence_cluster="algebra",
            owner_slot="offline-1",
        )
        outcome = {
            "route": None, "status": "CANDIDATE_PROOF", "summary": "Contradictory payload.",
            "claims": [], "failures": [], "decisive_events": [], "novelty_evidence": [],
            "next_task": "Resolve the contradiction.",
            "proof_candidate": {"proof_latex": "x" * 500, "assumptions": [], "open_obligations": []},
            "counterexample_candidate": {
                "description": "x", "verification": "exact", "scope": "root claim"
            },
        }
        _, proof_found, refutation_found = controller._process_research_outcome(
            campaign_id=campaign_id, epoch=1,
            call=AgentCall(role="offline_researcher", slot="offline-1", prompt="test",
                project_root=self.root, network_policy="deny", campaign_id=campaign_id,
                route_id=route_id, epoch=1),
            outcome=outcome, root_claim_id=root_claim_id,
        )
        self.assertFalse(proof_found)
        self.assertFalse(refutation_found)
        self.assertEqual(self.store.get_route(route_id)["status"], "ACTIVE")
        self.assertTrue(any(
            event["event_type"] == "conflicting_candidate_payload"
            for event in self.store.events.read_all()
        ))

    def test_resume_records_redacted_operational_config_revision(self) -> None:
        config_path = self._config("offline_only", offline=1)
        config_path.write_text(
            config_path.read_text(encoding="utf-8")
            + '\n[providers.mock.env]\nTEST_API_KEY = "never-store-this-secret"\n',
            encoding="utf-8",
        )
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        self.store.update_campaign(campaign_id, status="PAUSED_HUMAN")
        self.store.set_meta(
            "problem_contract_sha256", content_hash(self.store.paths.contract.read_bytes())
        )
        CampaignController(
            self.root, load_config(config_path), config_path=config_path
        ).run(new_campaign=False)
        self.store.update_campaign(campaign_id, status="PAUSED_HUMAN", epoch=1)
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "estimated_cost_usd = 0.0", "estimated_cost_usd = 0.25", 1
            ),
            encoding="utf-8",
        )
        CampaignController(
            self.root, load_config(config_path), config_path=config_path
        ).run(new_campaign=False)
        revisions = self.store.list_campaign_config_revisions(campaign_id)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(
            revisions[0]["snapshot"]["providers"]["mock"]["env"]["TEST_API_KEY"],
            "<redacted>",
        )
        self.assertIn("providers.mock.estimated_cost_usd", revisions[1]["changes"])
        self.assertNotIn("never-store-this-secret", json.dumps(revisions))
        self.assertTrue(any(
            item["kind"] == "HUMAN_CONFIGURATION_REVISION"
            for item in self.store.list_decisions(campaign_id)
        ))
        report = build_report(self.store)
        self.assertIn("Operational configuration revisions", report)
        self.assertNotIn("never-store-this-secret", report)

    def test_budget_command_and_resume_use_stored_revised_limit(self) -> None:
        config_path = self._config("offline_only", offline=1)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        self.store.update_campaign(campaign_id, status="BUDGET_EXHAUSTED", epoch=1)
        with mock.patch("sys.stdout") as stdout:
            result = cli_main([
                "campaign", "budget", str(self.root), "--max-epochs", "2",
                "--max-calls", "25", "--max-cost-usd", "2.0",
                "--reason", "Continue one independently bounded epoch",
            ])
        self.assertEqual(result, 0)
        self.assertIn("Adjusted budget", "".join(str(call) for call in stdout.write.call_args_list))
        self.assertEqual(self.store.get_campaign(campaign_id)["status"], "PAUSED_HUMAN")
        self.store.set_meta(
            "problem_contract_sha256", content_hash(self.store.paths.contract.read_bytes())
        )
        result = CampaignController(self.root, load_config(config_path)).run(new_campaign=False)
        self.assertEqual(result["epoch"], 2)
        self.assertEqual(result["max_epochs"], 2)

    def test_cli_rejects_resume_when_contract_changed(self) -> None:
        config_path = self._config("offline_only", offline=1)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        self.store.update_campaign(campaign_id, status="CONTRACT_CHANGED")
        with mock.patch("sys.stderr") as stderr:
            result = cli_main([
                "campaign", "resume", str(self.root), "--config", str(config_path),
            ])
        self.assertEqual(result, 2)
        self.assertIn(
            "cannot resume because its frozen problem contract changed",
            "".join(str(call) for call in stderr.write.call_args_list),
        )

    def test_budget_exhausted_campaign_writes_unresolved_handoff(self) -> None:
        config_path = self._config("offline_only", offline=1)
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace("max_cost_usd = 1.0", "max_cost_usd = 0.0", 1),
            encoding="utf-8",
        )
        result = CampaignController(self.root, load_config(config_path)).run()
        self.assertEqual(result["status"], "BUDGET_EXHAUSTED")
        self.assertEqual(len(self.store.list_artifacts(kind="unsolved_campaign_note_latex")), 1)

    def test_final_unsolved_campaign_writes_journal_research_record(self) -> None:
        config = load_config(self._config("offline_only", offline=1))
        controller = CampaignController(self.root, config)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())
        route_id = self.store.add_route(
            campaign_id=campaign_id, title="Unclosed route", target_claim_id=root_claim_id,
            mode="DEDUCTIVE", method_family="reduction", representation="native",
            key_lemma="missing bridge", central_mechanism="reduce the target",
            decisive_test="prove the bridge", difference_from_existing="new reduction",
            fingerprint="unclosed", independence_cluster="reduction", owner_slot="offline-1",
        )
        attempt_id = self.store.add_attempt(
            campaign_id=campaign_id, route_id=route_id, epoch=1, agent_slot="offline-1",
            task="Establish the missing bridge", result_kind="BLOCKED",
            summary="The stated bridge was not derived.", artifact_id=None,
            decisive_event=False, cost_usd=0.0, usage={},
        )
        self.store.upsert_failure(
            canonical_key="TEST|missing bridge", failure_class="UNRESOLVED_OBLIGATION",
            signature="missing bridge", logical_scope="route only",
            revival_conditions="supply the missing bridge", attempt_id=attempt_id, cost_usd=0.0,
        )
        self.store.update_campaign(campaign_id, status="COMPLETED_UNSOLVED", epoch=1)
        controller._record_unsolved_campaign_note(campaign_id=campaign_id)
        notes = self.store.list_artifacts(kind="unsolved_campaign_note_latex")
        self.assertEqual(len(notes), 1)
        note = (self.root / notes[0]["relative_path"]).read_text(encoding="utf-8")
        self.assertIn("Unresolved Campaign Research Record", note)
        self.assertIn("Unclosed route", note)
        self.assertIn("supply the missing bridge", note)
        self.assertTrue(any(
            event["event_type"] == "unsolved_campaign_note_created"
            for event in self.store.events.read_all()
        ))
        report = build_report(self.store)
        self.assertIn("## Continuation handoff", report)
        self.assertIn("Frozen contract SHA-256", report)
        self.assertIn("supply the missing bridge", report)
        brief_path = write_continuation_brief(self.store)
        brief = brief_path.read_text(encoding="utf-8")
        self.assertIn("# Ariadne Continuation Brief", brief)
        self.assertIn("Unclosed route", brief)
        self.assertIn("Safe restart rule", brief)

    def test_agent_interpreted_instruction_can_be_added_and_cancelled(self) -> None:
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        config_path = self._config("offline_only", offline=1)
        tui = AriadneTUI(self.root, config_path)
        added = tui._apply_interpreted_chat_instruction(
            campaign_id=campaign_id,
            owner_message="Add numerical evidence and a plot to the report.",
            interpretation={
                "action": "ADD",
                "purpose": "REPORT_REQUIREMENTS",
                "instruction": "Run only bounded exact checks; retain reproducible data and a labelled plot.",
                "audience": "researchers",
                "route_id": "",
                "target_instruction_ids": [],
                "required_artifacts": ["CSV data", "plot PDF", "plotting code"],
            },
        )
        self.assertIn("saved as HIN-", added)
        active = self.store.list_human_instructions(campaign_id, active_only=True)
        self.assertEqual(len(active), 1)
        self.assertIn("plot PDF", active[0]["instruction_text"])
        cancelled = tui._apply_interpreted_chat_instruction(
            campaign_id=campaign_id,
            owner_message="Cancel the numerical-evidence instruction.",
            interpretation={
                "action": "CANCEL",
                "purpose": "REPORT_REQUIREMENTS",
                "instruction": "",
                "audience": "researchers",
                "route_id": "",
                "target_instruction_ids": [active[0]["instruction_id"]],
                "required_artifacts": [],
            },
        )
        self.assertIn(active[0]["instruction_id"], cancelled)
        self.assertEqual(self.store.list_human_instructions(campaign_id, active_only=True), [])
        self.assertEqual(
            self.store.instructions_for_agent(
                campaign_id=campaign_id, role="offline_researcher", route_id=None
            ),
            [],
        )

    def test_chat_intent_parser_maps_only_unambiguous_controls(self) -> None:
        self.assertEqual(chat_intent_to_command("please pause the campaign"), "/pause")
        self.assertEqual(chat_intent_to_command("generate report"), "/report")
        self.assertEqual(chat_intent_to_command("set reasoning strength max"), "/model max")
        self.assertEqual(chat_intent_to_command("next artifact"), "/artifact next")
        self.assertEqual(chat_intent_to_command("Develop a dual variational route"), None)

    def test_tui_interactive_commands_do_not_nest_prompt_toolkit_prompt(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "ariadne_math" / "tui.py").read_text(encoding="utf-8")
        setup_source = (Path(__file__).resolve().parents[1] / "src" / "ariadne_math" / "setup_wizard.py").read_text(encoding="utf-8")
        self.assertNotIn("from prompt_toolkit import prompt", source)
        self.assertNotIn("from prompt_toolkit import prompt", setup_source)
        self.assertIn('instruction(event, args)', source)
        self.assertIn('if args:\n                save(" ".join(args).strip())', source)

    def test_existing_project_resumes_only_unrequested_pause(self) -> None:
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        self.store.update_campaign(campaign_id, status="PAUSED_HUMAN")
        self.store.set_meta(
            "problem_contract_sha256", content_hash(self.store.paths.contract.read_bytes())
        )
        resume, message = _prepare_existing_project_start(self.store)
        self.assertTrue(resume)
        self.assertIn("Resuming", message)
        self.store.request_campaign_pause(
            campaign_id, reason="operator review", requested_by="tester"
        )
        resume, message = _prepare_existing_project_start(self.store)
        self.assertFalse(resume)
        self.assertIn("active human pause", message)

    def test_terminal_startup_asks_before_publishing_handoff_as_literature(self) -> None:
        config = self._config("offline_only", offline=1)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        self.store.update_campaign(campaign_id, status="COMPLETED_UNSOLVED", epoch=1)
        self.store.set_meta(
            "problem_contract_sha256", content_hash(self.store.paths.contract.read_bytes())
        )
        with mock.patch("ariadne_math.tui.project_has_git_repository", return_value=True), mock.patch(
            "ariadne_math.tui.AriadneTUI.run"
        ) as run, mock.patch("builtins.input", return_value="n") as prompt:
            run_tui(self.root, config)
        run.assert_called_once()
        prompt.assert_called_once()
        self.assertFalse(any(
            source["source_kind"] == "campaign_continuation_handoff"
            for source in self.store.list_literature_sources()
        ))
        with mock.patch("ariadne_math.tui.project_has_git_repository", return_value=True), mock.patch(
            "ariadne_math.tui.AriadneTUI.run"
        ), mock.patch("builtins.input", return_value="y"):
            run_tui(self.root, config)
        handoffs = [
            source for source in self.store.list_literature_sources()
            if source["source_kind"] == "campaign_continuation_handoff"
        ]
        self.assertEqual(len(handoffs), 1)
        self.assertTrue((self.root / handoffs[0]["relative_path"]).is_file())
        self.assertIn("Campaign-local handoff only", handoffs[0]["exact_statement"])

    def test_tui_prompts_for_git_when_restarting_an_existing_project(self) -> None:
        config = self._config("offline_only", offline=1)
        with mock.patch("ariadne_math.tui.project_has_git_repository", return_value=False), mock.patch(
            "ariadne_math.tui.enable_project_git", return_value=(True, "0123456789abcdef")
        ) as enable_git, mock.patch("ariadne_math.tui.AriadneTUI.run") as run, mock.patch(
            "builtins.input", return_value="y"
        ) as prompt:
            run_tui(self.root, config)
        prompt.assert_called_once()
        enable_git.assert_called_once_with(self.root.resolve())
        run.assert_called_once()

    def test_tui_does_not_prompt_for_git_when_project_is_already_versioned(self) -> None:
        config = self._config("offline_only", offline=1)
        with mock.patch("ariadne_math.tui.project_has_git_repository", return_value=True), mock.patch(
            "ariadne_math.tui.AriadneTUI.run"
        ) as run, mock.patch("builtins.input") as prompt:
            run_tui(self.root, config)
        prompt.assert_not_called()
        run.assert_called_once()

    def test_numerical_runs_are_retained_as_non_deductive_artifacts(self) -> None:
        config = load_config(self._config("offline_only", offline=1))
        controller = CampaignController(self.root, config)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())
        route_id = self.store.add_route(
            campaign_id=campaign_id, title="Bounded computation", target_claim_id=root_claim_id,
            mode="EMPIRICAL", method_family="exact finite check", representation="integers",
            key_lemma="check a finite model", central_mechanism="bounded enumeration",
            decisive_test="reproduce the listed cases", difference_from_existing="first computation",
            fingerprint="bounded computation", independence_cluster="computation", owner_slot="offline-1",
        )
        outcome = {
            "route": None, "status": "PROGRESS", "summary": "A small check completed.",
            "claims": [], "failures": [], "decisive_events": [], "novelty_evidence": [],
            "next_task": "Use an exact argument instead.", "proof_candidate": None,
            "counterexample_candidate": None,
            "experiment_request": {
                "mathematical_question": "Check the first five cases.",
                "competing_hypotheses": ["identity holds", "identity fails"],
                "possible_outcomes": ["supporting data", "counterexample candidate"],
                "domain": "n=1,...,5", "arithmetic": "exact integers",
                "estimated_runtime_seconds": 1, "requires_human_approval": False,
                "long_run_justification": "", "stopping_rule": "stop after five cases",
                "logical_force": "planning only", "scale": "small",
                "minimum_cpu_cores": 1, "minimum_memory_gb": 0.1,
                "requires_cuda": False, "minimum_gpu_memory_gb": 0.0,
                "hpc_code": "", "hpc_run_instructions": "",
            },
            "numerical_evidence": {
                "kind": "EXHAUSTIVE_FINITE_SEARCH", "summary": "Five cases checked.",
                "method": "direct exact enumeration", "output": "all five passed",
                "runtime_seconds": 0.01, "reproducibility": "python check.py --limit 5",
                "logical_force": "finite supporting evidence only",
            },
        }
        controller._process_research_outcome(
            campaign_id=campaign_id, epoch=1,
            call=AgentCall(role="offline_researcher", slot="offline-1", prompt="test",
                project_root=self.root, network_policy="deny", campaign_id=campaign_id,
                route_id=route_id, epoch=1),
            outcome=outcome, root_claim_id=root_claim_id,
        )
        kinds = {item["kind"] for item in self.store.list_artifacts()}
        self.assertIn("numerical_experiment_plan", kinds)
        self.assertIn("numerical_evidence", kinds)
        evidence = self.store.list_evidence()
        self.assertTrue(any(item["evidence_type"] == "EXHAUSTIVE_FINITE_SEARCH" for item in evidence))
        self.assertTrue(all("does not prove" in item["logical_force"] for item in evidence))

    def test_large_numerical_work_becomes_hpc_resource_request_when_local_machine_is_insufficient(self) -> None:
        config = load_config(self._config("offline_only", offline=1))
        controller = CampaignController(self.root, config)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())
        route_id = self.store.add_route(
            campaign_id=campaign_id, title="Ramsey search", target_claim_id=root_claim_id,
            mode="EMPIRICAL", method_family="exact graph search", representation="bitsets",
            key_lemma="enumerate all admissible graphs", central_mechanism="canonical augmentation",
            decisive_test="reproduce the frontier", difference_from_existing="first computation",
            fingerprint="ramsey-search", independence_cluster="computation", owner_slot="offline-1",
        )
        request = {
            "mathematical_question": "Search the next Ramsey frontier.",
            "competing_hypotheses": ["a witness exists", "no witness exists"],
            "possible_outcomes": ["candidate witness", "bounded nonexistence result"],
            "domain": "labelled graphs", "arithmetic": "exact bit operations",
            "estimated_runtime_seconds": 7200, "requires_human_approval": True,
            "long_run_justification": "The frontier is too large for a bounded local run.",
            "stopping_rule": "stop at the declared graph order", "logical_force": "planning only",
            "scale": "large", "minimum_cpu_cores": 24, "minimum_memory_gb": 64.0,
            "requires_cuda": False, "minimum_gpu_memory_gb": 0.0,
            "hpc_code": "python ramsey_search.py --n 43 --checkpoint out/",
            "hpc_run_instructions": "sbatch --cpus-per-task=24 --mem=64G ramsey.sbatch",
        }
        insufficient = {
            "is_large": True, "local_adequate": False, "needs_hpc": True,
            "reason": "Local machine does not satisfy the declared resource request.",
            "requested": {
                "minimum_cpu_cores": 24, "minimum_memory_gb": 64.0,
                "requires_cuda": False, "minimum_gpu_memory_gb": 0.0,
                "estimated_runtime_seconds": 7200,
            },
            "local_profile": {"cpu_cores": 8, "memory_gb": 16.0, "cuda_available": False, "gpu_memory_gb": 0.0},
        }
        with mock.patch("ariadne_math.controller.assess_experiment_resources", return_value=insufficient):
            controller._record_numerical_artifacts(
                campaign_id=campaign_id, epoch=1, route_id=route_id,
                root_claim_id=root_claim_id, slot="offline-1",
                outcome={"experiment_request": request, "numerical_evidence": {
                    "kind": "EXHAUSTIVE_FINITE_SEARCH", "summary": "untrusted output",
                    "method": "search", "output": "claimed", "runtime_seconds": 1.0,
                    "reproducibility": "python ramsey_search.py", "logical_force": "none",
                }},
            )
        kinds = {item["kind"] for item in self.store.list_artifacts()}
        self.assertIn("hpc_resource_request", kinds)
        self.assertNotIn("numerical_evidence", kinds)
        self.assertFalse(self.store.list_evidence())
        self.assertIn("hpc run instructions", AriadneTUI(self.root, self._config("offline_only", offline=1))._artifact_preview_text().lower())

    def test_large_numerical_resource_gate_accepts_declared_adequate_cpu_or_cuda(self) -> None:
        request = {
            "scale": "large", "estimated_runtime_seconds": 3600,
            "minimum_cpu_cores": 16, "minimum_memory_gb": 32.0,
            "requires_cuda": False, "minimum_gpu_memory_gb": 0.0,
            "requires_human_approval": True,
        }
        cpu = assess_experiment_resources(
            request, {"cpu_cores": 16, "memory_gb": 64.0, "cuda_available": False, "gpu_memory_gb": 0.0}
        )
        self.assertTrue(cpu["local_adequate"])
        self.assertFalse(cpu["needs_hpc"])
        cuda_request = {**request, "requires_cuda": True, "minimum_gpu_memory_gb": 24.0}
        cuda = assess_experiment_resources(
            cuda_request, {"cpu_cores": 4, "memory_gb": 64.0, "cuda_available": True, "gpu_memory_gb": 24.0}
        )
        self.assertTrue(cuda["local_adequate"])
        self.assertFalse(cuda["needs_hpc"])

    def test_open_literature_pdf_is_cached_as_reusable_markdown(self) -> None:
        class Response:
            headers = {"Content-Type": "application/pdf"}

            def read(self, _limit=None):
                return b"%PDF-1.7\nmock source"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        url = "https://arxiv.org/abs/2606.12270"
        self.assertEqual(_open_pdf_urls(url), ["https://arxiv.org/pdf/2606.12270.pdf"])
        with mock.patch("ariadne_math.setup_wizard.urlopen", return_value=Response()) as download, mock.patch(
            "ariadne_math.setup_wizard._read_source_excerpt",
            return_value="[pypdf extraction]\n# Parsed source\nExact theorem statement.",
        ) as extract:
            first = _cache_open_literature_pdf(
                self.store, url="https://arxiv.org/pdf/2606.12270.pdf",
                citation="Example preprint", reporter=NullActivityReporter(),
            )
            second = _cache_open_literature_pdf(
                self.store, url="https://arxiv.org/pdf/2606.12270.pdf",
                citation="Example preprint", reporter=NullActivityReporter(),
            )
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(extract.call_count, 1)
        source = next(item for item in self.store.list_literature_sources() if item["source_id"] == first)
        cached_markdown = self.root / source["relative_path"]
        self.assertTrue(cached_markdown.exists())
        self.assertIn("Exact theorem statement", cached_markdown.read_text(encoding="utf-8"))

    def test_agent_audited_journal_report_requires_both_bound_passes(self) -> None:
        controller = CampaignController(self.root, load_config(self._config("offline_only", offline=1)))
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=2, max_cost_usd=1.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())
        route_id = self.store.add_route(
            campaign_id=campaign_id, title="Squares", target_claim_id=root_claim_id,
            mode="DEDUCTIVE", method_family="algebra", representation="real numbers",
            key_lemma="a real square is nonnegative", central_mechanism="x times x",
            decisive_test="expand the square", difference_from_existing="first route",
            fingerprint="squares", independence_cluster="algebra", owner_slot="offline-1",
        )
        artifacts = ArtifactStore(self.store.paths)
        proof = artifacts.put_text(
            r"For $x\in\mathbb{R}$, $x^2=x\cdot x\geq 0$.",
            kind="proof_candidate_latex", suffix=".tex",
            metadata={"route_id": route_id, "root_claim_id": root_claim_id},
        )
        self.store.record_artifact(proof)
        record = artifacts.put_text(
            json.dumps({
                "assumptions": ["x is real"], "open_obligations": [],
                "literature_review": "No external result is used.", "sources": [],
            }),
            kind="proof_candidate_record", suffix=".json",
            metadata={"proof_artifact_id": proof.artifact_id},
        )
        self.store.record_artifact(record)
        local = artifacts.put_text("{}", kind="local_audit", suffix=".json",
            metadata={"proof_artifact_id": proof.artifact_id})
        self.store.record_artifact(local)
        self.store.add_audit(target_type="claim", target_id=root_claim_id,
            audit_type="LOCAL_PROOF_AUDIT", verdict="PASS", failure_class="",
            minimal_obligation="", local_repairable=False, artifact_id=local.artifact_id,
            auditor_profile="local")
        self.assertEqual(write_agent_audited_proof_report(self.store), [])
        global_audit = artifacts.put_text("{}", kind="global_audit", suffix=".json",
            metadata={"proof_artifact_id": proof.artifact_id})
        self.store.record_artifact(global_audit)
        self.store.add_audit(target_type="claim", target_id=root_claim_id,
            audit_type="GLOBAL_PROOF_AUDIT", verdict="PASS", failure_class="",
            minimal_obligation="", local_repairable=False, artifact_id=global_audit.artifact_id,
            auditor_profile="global")
        reports = write_agent_audited_proof_report(self.store)
        self.assertEqual(len(reports), 1)
        tex = reports[0][0].read_text(encoding="utf-8")
        self.assertIn("Complete proof", tex)
        self.assertIn(r"x^2=x\cdot x\geq 0", tex)
        self.assertIn("Literature review and provenance", tex)

    def test_artifact_browser_and_preview_are_separate_and_json_is_readable(self) -> None:
        config_path = self._config("offline_only", offline=1)
        source = ArtifactStore(self.store.paths).put_text(
            "The retained exact identity.", kind="partial_result"
        )
        self.store.record_artifact(source)
        artifact = ArtifactStore(self.store.paths).put_text(
            json.dumps({
                "summary": "complete numerical check",
                "tail": "VISIBLE-AFTER-ONE-KIB" + "x" * 1200,
                "verification": {"exact": True, "steps": ["derive identity", "check endpoints"]},
            }),
            kind="numerical_evidence", suffix=".json",
            metadata={"status": "OBSERVED", "source_artifact_id": source.artifact_id},
        )
        self.store.record_artifact(artifact)
        tui = AriadneTUI(self.root, config_path)
        tui.selected_artifact = 0
        listing = tui._artifacts_text()
        preview = tui._artifact_preview_text()
        self.assertIn("> [OBSERVED] numerical evidence", listing)
        self.assertIn("j preview", listing)
        self.assertNotIn("complete numerical check", listing)
        self.assertIn("numerical evidence [OBSERVED]", preview)
        self.assertIn("Summary: complete numerical check", preview)
        self.assertIn("VISIBLE-AFTER-ONE-KIB", preview)
        self.assertIn("Stored at: .ariadne/artifacts/", preview)
        self.assertIn("Artifact graph · one-hop provenance", preview)
        self.assertIn(source.artifact_id, preview)
        self.assertIn("metadata:source_artifact_id", preview)
        self.assertIn("Verification:", preview)
        self.assertIn("Exact: yes", preview)
        self.assertIn("- derive identity", preview)
        self.assertNotIn('"summary"', preview)

    def test_proof_candidate_note_preserves_safe_latex_as_proof(self) -> None:
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=2, max_cost_usd=1.0
        )
        claim_id = self.store.add_claim(statement="For every real x, x^2 >= 0.")
        route_id = self.store.add_route(
            campaign_id=campaign_id,
            title="Squares", target_claim_id=claim_id, mode="DEDUCTIVE",
            method_family="algebra", representation="real numbers",
            key_lemma="a real square is nonnegative", central_mechanism="x times x",
            decisive_test="expand the square", difference_from_existing="first route",
            fingerprint="squares", independence_cluster="algebra", owner_slot="offline-1",
        )
        tex_path, pdf_path = write_proof_candidate_note(
            self.store,
            proof_candidate={
                "proof_latex": r"For $x\in\mathbb{R}$, $x^2=x\cdot x\geq 0$.",
                "assumptions": ["x is real"], "open_obligations": [],
            },
            route_id=route_id, artifact_id="ART-proof-note",
        )
        self.assertIsNotNone(tex_path)
        self.assertIsNotNone(pdf_path)
        assert tex_path is not None and pdf_path is not None
        tex = tex_path.read_text(encoding="utf-8")
        self.assertIn(r"\begin{proof}", tex)
        self.assertIn(r"x^2=x\cdot x\geq 0", tex)
        self.assertNotIn(r"\textbackslash{}cdot", tex)
        self.assertTrue(pdf_path.exists())

    def test_proof_note_rejects_unicode_math_and_does_not_publish_tex(self) -> None:
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=2, max_cost_usd=1.0
        )
        claim_id = self.store.add_claim(statement="For every real x, x^2 >= 0.")
        route_id = self.store.add_route(
            campaign_id=campaign_id,
            title="Unicode squares", target_claim_id=claim_id, mode="DEDUCTIVE",
            method_family="algebra", representation="real numbers",
            key_lemma="a real square is nonnegative", central_mechanism="x times x",
            decisive_test="expand the square", difference_from_existing="first route",
            fingerprint="unicode-squares", independence_cluster="algebra", owner_slot="offline-1",
        )
        tex_path, pdf_path = write_proof_candidate_note(
            self.store,
            proof_candidate={
                "proof_latex": "For all x ∈ ℝ, x² ≥ 0.",
                "assumptions": [], "open_obligations": [],
            },
            route_id=route_id, artifact_id="ART-unicode-proof",
        )
        self.assertIsNone(tex_path)
        self.assertIsNone(pdf_path)
        with self.assertRaises(LatexValidationError):
            validate_proof_latex("x² ≥ 0")

    def test_controller_retains_malformed_proof_only_as_rejected_source(self) -> None:
        controller = CampaignController(self.root, load_config(self._config("offline_only", offline=1)))
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=2, max_cost_usd=1.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())
        route_id = self.store.add_route(
            campaign_id=campaign_id, title="Bad source", target_claim_id=root_claim_id,
            mode="DEDUCTIVE", method_family="algebra", representation="real numbers",
            key_lemma="check the expression", central_mechanism="direct algebra",
            decisive_test="expand it", difference_from_existing="first route",
            fingerprint="bad-source", independence_cluster="algebra", owner_slot="offline-1",
        )
        controller._handle_proof_candidate(
            campaign_id=campaign_id, epoch=1, route_id=route_id,
            root_claim_id=root_claim_id,
            proof_candidate={"proof_latex": "For all x ∈ ℝ, x² ≥ 0."},
        )
        rejected = self.store.list_artifacts(kind="proof_candidate_render_rejected")
        self.assertEqual(len(rejected), 1)
        self.assertIn("non-ASCII", rejected[0]["metadata"]["latex_error"])
        self.assertEqual(self.store.list_artifacts(kind="proof_candidate_latex"), [])

    def test_interactive_setup_asks_whether_to_enable_git(self) -> None:
        responses = [
            "Prove or refute Q.", "n", "2", "1", "y", "n", "",
            "3", "24", "30.0",
        ]
        with mock.patch("ariadne_math.setup_wizard._ask", side_effect=responses):
            answers = collect_setup_answers()
        self.assertFalse(answers.use_git)
        self.assertEqual(answers.research_mode, "offline_only")
        self.assertEqual(answers.researcher_count, 1)

    def test_interactive_setup_reuses_existing_git_without_prompting(self) -> None:
        responses = ["Prove or refute Q.", "2", "1", "y", "n", "", "3", "24", "30.0"]
        with mock.patch(
            "ariadne_math.setup_wizard.project_has_git_repository", return_value=True
        ), mock.patch("ariadne_math.setup_wizard._ask", side_effect=responses) as ask:
            answers = collect_setup_answers(project_root=self.root)
        self.assertTrue(answers.use_git)
        self.assertFalse(any("Git version control" in str(call.args[0]) for call in ask.call_args_list))

    def test_setup_uses_separate_contract_and_literature_agents(self) -> None:
        config_path = self._config("offline_sentinel", offline=2)
        base_note = self.root / "base-note.md"
        base_note.write_text("# Base note\n\nExact route-neutral identity.", encoding="utf-8")
        later_note = self.root / "later-note.md"
        later_note.write_text("# Later note\n\nKnown literature route.", encoding="utf-8")
        answers = SetupAnswers(
            title="Interactive setup test",
            statement="Prove or refute Q.",
            objective="Prove Q exactly.",
            hypotheses_and_domains="All quantified data are admissible.",
            uniformity_and_endpoints="Uniform in all data.",
            exclusions_and_statement_drift="No special cases.",
            proof_success="Complete proof.",
            refutation_success="Exact counterexample.",
            base_source_references="Mock source",
            source_files=(str(base_note),),
            literature_source_files=(str(later_note),),
            research_mode="literature_guided",
            researcher_count=2,
            parallel=True,
            allow_live_literature=False,
            literature_instructions="Audit all source hypotheses.",
        )
        with mock.patch(
            "ariadne_math.setup_wizard.enable_project_git", return_value=(True, "setup-commit")
        ) as enable_git:
            result = generate_setup(
                project_root=self.root,
                config_path=config_path,
                answers=answers,
            )
        enable_git.assert_called_once_with(self.root.resolve())
        self.assertTrue(result["git_versioning_enabled"])
        self.assertEqual(result["git_commit"], "setup-commit")
        self.assertEqual(result["mode"], "literature_guided")
        self.assertTrue(Path(result["contract"]).is_file())
        self.assertTrue(Path(result["literature_document"]).is_file())
        roles = [row["role"] for row in self.store.list_agent_runs()]
        self.assertIn("contract_author", roles)
        self.assertIn("literature_author", roles)
        rewritten = load_config(config_path)
        self.assertEqual(rewritten.mode.name, "literature_guided")
        self.assertEqual(rewritten.mode.research_agents, 2)
        self.assertEqual(rewritten.mode.offline_agents, 0)
        preserved = list((self.store.paths.literature / "source-materials").iterdir())
        self.assertEqual(len(preserved), 2)
        self.assertEqual(len(result["preserved_base_source_ids"]), 1)
        self.assertEqual(len(result["preserved_literature_source_ids"]), 1)

    def test_setup_uses_contract_resolver_only_after_offline_author_requests_it(self) -> None:
        config_path = self._config("literature_guided", research=1)
        answers = SetupAnswers(
            title="Agent-generated title",
            statement="Improve the named classical result.",
            objective="Fix the exact target before research.",
            hypotheses_and_domains="Use the cited source formulation.",
            uniformity_and_endpoints="Keep every endpoint from the source.",
            exclusions_and_statement_drift="Do not guess a nearby theorem.",
            proof_success="A complete proof of the resolved target.",
            refutation_success="An exact counterexample to the resolved target.",
            base_source_references="A named but underspecified result",
            source_files=(),
            research_mode="literature_guided",
            researcher_count=1,
            parallel=True,
            allow_live_literature=True,
            literature_instructions="Use exact locators.",
            use_git=False,
        )
        contract = json.loads(self.store.paths.contract.read_text(encoding="utf-8"))
        contract["title"] = "Resolved result"
        contract["statement"] = {
            "text": "For every admissible X, the resolved conclusion holds.",
            "formal_quantifier_outline": "forall X, admissible(X) -> conclusion(X)",
        }
        unresolved = mock.Mock(text=json.dumps({
            "problem_contract": None,
            "validation_notes": ["CONTRACT_RESOLUTION_REQUIRED: named result is ambiguous"],
        }))
        resolver = mock.Mock(text=json.dumps({"resolution": {
            "status": "RESOLVED", "title": "Resolved result", "citation": "Author, paper v1",
            "version": "v1", "locator": "Theorem 2.1", "exact_statement": contract["statement"]["text"],
            "hypotheses": ["X is admissible"], "endpoints": ["all admissible X"], "warnings": [],
        }}))
        retry = mock.Mock(text=json.dumps({"problem_contract": contract, "validation_notes": []}))
        literature = mock.Mock(text=json.dumps({
            "document_type": "shared_literature_dossier", "markdown": "# Dossier\n\nResolved source.",
            "sources": [], "warnings": [],
        }))
        with mock.patch("ariadne_math.setup_wizard._cache_cited_open_pdfs", return_value=[]), mock.patch(
            "ariadne_math.setup_wizard.AgentRunner.call",
            side_effect=[unresolved, resolver, retry, literature],
        ) as call:
            generate_setup(project_root=self.root, config_path=config_path, answers=answers)
        self.assertEqual([item.args[0].role for item in call.call_args_list], [
            "contract_author", "contract_resolver", "contract_author", "literature_author",
        ])
        resolutions = self.store.list_artifacts(kind="contract_resolution")
        self.assertEqual(len(resolutions), 1)
        self.assertEqual(json.loads((self.root / resolutions[0]["relative_path"]).read_text())["locator"], "Theorem 2.1")

    def test_tutorial_and_requirements_cover_supported_environment(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        requirements = (repository_root / "requirements.txt").read_text(encoding="utf-8")
        tutorial = (repository_root / "docs" / "TUTORIAL.md").read_text(encoding="utf-8")
        task = (repository_root / "examples" / "tutorial_task.md").read_text(encoding="utf-8")
        self.assertIn("sympy>=1.13", requirements)
        self.assertIn("pypdf>=5.0", requirements)
        self.assertIn("LLAMAPARSE_API_KEY", tutorial)
        self.assertIn("ARIADNE_PDF_BACKENDS", tutorial)
        self.assertIn("continuation brief", tutorial.casefold())
        self.assertIn("a^2+b^2", task)

    def test_all_packaged_mode_examples_load(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        guided = load_config(
            repository_root / "examples" / "config.codex.literature_guided.toml"
        )
        offline_only = load_config(
            repository_root / "examples" / "config.codex.offline_only.toml"
        )
        self.assertEqual(guided.mode.name, "literature_guided")
        self.assertEqual(guided.mode.researcher_count, 2)
        self.assertFalse(guided.mode.sentinel_enabled)
        self.assertEqual(offline_only.mode.name, "offline_only")
        self.assertEqual(offline_only.mode.researcher_count, 2)
        self.assertFalse(offline_only.mode.sentinel_enabled)

    def test_failed_method_cannot_be_recreated_by_a_renamed_route(self) -> None:
        config = load_config(self._config("literature_guided", offline=0, research=1))
        controller = CampaignController(self.root, config)
        campaign_id = self.store.create_campaign(
            mode="literature_guided", max_epochs=2, max_calls=10, max_cost_usd=5.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())
        failed_id = self.store.add_route(
            campaign_id=campaign_id,
            title="BMOA–Hardy duality for the binomial functional",
            target_claim_id=root_claim_id,
            mode="DEDUCTIVE",
            method_family="Hardy-space duality and Carleson-measure estimates",
            representation="analytic Hardy spaces, BMOA, and Carleson measures",
            key_lemma="uniform BMOA estimate",
            central_mechanism="pair U directly with the binomial kernel",
            decisive_test="construct a divergent Carleson quotient",
            difference_from_existing="first route",
            fingerprint="hardy bmoa duality carleson binomial functional",
            independence_cluster="hardy-bmoa",
            owner_slot="literature-1",
            status="METHOD_FAILED",
        )
        renamed = {
            "title": "Hardy–BMOA duality for the binomial functional",
            "mode": "DEDUCTIVE",
            "method_family": "analytic Hardy spaces, BMOA, and Carleson measures",
            "representation": "Hardy-space duality and BMOA",
            "key_lemma": "a uniform single-radius BMOA variance estimate",
            "central_mechanism": "pair U directly with the binomial kernel",
            "decisive_test": "bound the corresponding Carleson variance",
            "difference_from_existing": "This narrows the BMOA lemma to one radius and uses a long explanation instead of a new representation.",
            "independence_cluster": "hardy-bmoa",
            "revives_route_id": "",
            "revival_certificate": "",
        }
        blocked_id = controller._create_route_from_payload(
            campaign_id=campaign_id, owner_slot="literature-2",
            target_claim_id=root_claim_id, route=renamed,
        )
        self.assertEqual(self.store.get_route(blocked_id)["status"], "SUBSUMED")
        self.assertTrue(any(
            event["event_type"] == "route_blocked_failed_method"
            and event["payload"]["blocked_route_id"] == failed_id
            for event in self.store.events.read_all()
        ))
        packet = controller._research_context_packet(
            campaign_id=campaign_id, literature_guided=True
        )
        self.assertIn(failed_id, [item["route_id"] for item in packet["route_ledger"]])

        renamed["revives_route_id"] = failed_id
        renamed["revival_certificate"] = "A distinct harmonic extension produces an exact new coercive identity with an independently checked boundary term; this replaces, rather than merely narrows, the failed Carleson estimate. "
        review_id = controller._create_route_from_payload(
            campaign_id=campaign_id, owner_slot="literature-3",
            target_claim_id=root_claim_id, route=renamed,
        )
        self.assertEqual(self.store.get_route(review_id)["status"], "NEEDS_HUMAN_IDEA")

    def test_literature_guided_defaults_to_no_intervention_when_omitted(self) -> None:
        path = self.root / "minimal-guided.toml"
        path.write_text(
            MOCK_BASE.format(
                mode="literature_guided",
                offline_agents=0,
                research_agents=1,
                literature_intervention="false",
            ).replace("literature_intervention = false\n", "")
            .replace("require_route_difference_certificate = false\n", ""),
            encoding="utf-8",
        )
        config = load_config(path)
        self.assertFalse(config.mode.literature_intervention)
        self.assertFalse(config.mode.require_route_difference_certificate)

    def test_tui_surfaces_campaign_subprocess_failure_diagnostic(self) -> None:
        config_path = self._config("offline_only", offline=1)
        tui = AriadneTUI(self.root, config_path)

        class FailedProcess:
            pid = 4321
            returncode = 2

            def poll(self):
                return None

            def communicate(self):
                return None, "error: --config points to an invalid file\n"

        with mock.patch("ariadne_math.tui.subprocess.Popen", return_value=FailedProcess()) as popen:
            tui._launch_campaign()
            tui._worker_threads[-1].join(timeout=3)
        command = popen.call_args.args[0]
        self.assertIn("--record-activity", command)
        self.assertIn("failed (2): error: --config points to an invalid file", tui.message)
        self.assertTrue(any(
            event["event_type"] == "campaign_process_failed"
            for event in self.store.events.read_all()
        ))

    def test_tui_exit_pauses_and_reconciles_its_campaign_process(self) -> None:
        config_path = self._config("offline_only", offline=1)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        self.store.update_campaign(campaign_id, status="RUNNING")
        tui = AriadneTUI(self.root, config_path)
        process = mock.Mock()
        process.poll.return_value = None
        process.pid = 4321
        tui._campaign_process = process

        with mock.patch("ariadne_math.tui.os.killpg") as killpg:
            tui._stop_campaign_process_on_exit()

        killpg.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        self.assertEqual(self.store.get_campaign(campaign_id)["status"], "PAUSED_HUMAN")
        control = self.store.get_campaign_control(campaign_id)
        self.assertTrue(control["pause_requested"])

    def test_tui_does_not_launch_a_campaign_with_a_changed_contract(self) -> None:
        config_path = self._config("offline_only", offline=1)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=20, max_cost_usd=1.0
        )
        self.store.update_campaign(campaign_id, status="CONTRACT_CHANGED")
        tui = AriadneTUI(self.root, config_path)
        with mock.patch("ariadne_math.tui.subprocess.Popen") as popen:
            tui._launch_campaign()
        popen.assert_not_called()
        self.assertIn("frozen problem contract changed", tui.message)

    def test_setup_starts_campaign_automatically_unless_manual(self) -> None:
        config_path = self._config("offline_only", offline=1)
        answers = mock.Mock()
        result = {"mode": "offline_only", "researcher_count": 1}
        tui = AriadneTUI(self.root, config_path)
        with mock.patch("ariadne_math.tui.generate_setup", return_value=result), mock.patch.object(
            tui, "_launch_campaign"
        ) as launch:
            tui._start_setup_generation(answers)
            tui._worker_threads[-1].join(timeout=3)
        self.assertFalse(tui._worker_threads[-1].is_alive())
        launch.assert_called_once()

        manual_tui = AriadneTUI(self.root, config_path)
        with mock.patch("ariadne_math.tui.generate_setup", return_value=result), mock.patch.object(
            manual_tui, "_launch_campaign"
        ) as launch:
            manual_tui._start_setup_generation(answers, auto_start=False)
            manual_tui._worker_threads[-1].join(timeout=3)
        launch.assert_not_called()
        self.assertIn("manual mode", manual_tui.message)

    def test_tui_panels_read_shared_state(self) -> None:
        config_path = self._config("offline_only", offline=1)
        config = load_config(config_path)
        CampaignController(self.root, config).run()
        tui = AriadneTUI(self.root, config_path)
        self.assertIn("Mode test", tui._campaign_text())
        self.assertIn("✓", tui._tasks_text())
        self.assertIn("Weighted coercivity route", tui._routes_text())
        self.assertIn("Calls:", tui._budget_text())
        self.assertIn(f"Folder: {self.root.name}", tui._footer_text())
        self.assertTrue(tui._artifacts_text())

    def test_tui_compact_status_indicators_replace_lifecycle_words(self) -> None:
        tui = AriadneTUI(self.root, self._config("offline_only", offline=1))
        self.assertEqual(tui._status_indicator("RUNNING"), "●")
        self.assertEqual(tui._status_indicator("ACTIVE"), "●")
        self.assertEqual(tui._status_indicator("QUEUED"), "○")
        self.assertEqual(tui._status_indicator("COMPLETED"), "✓")
        self.assertEqual(tui._status_indicator("FAILED"), "×")
        self.assertEqual(tui._status_indicator("PAUSED_HUMAN"), "Ⅱ")
        self.assertEqual(tui._status_indicator("BUDGET_EXHAUSTED"), "!")
        self.assertIn("● work", tui._footer_text())
        self.assertIn("! blocked/budget", tui._footer_text())
        self.assertIn("Ⅱ paused", tui._footer_text())

    def test_partial_result_claim_is_retained_and_browsable_as_an_artifact(self) -> None:
        config_path = self._config("offline_only", offline=1)
        config = load_config(config_path)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=4, max_cost_usd=1.0
        )
        root_claim_id = self.store.add_claim(statement="Prove P for every admissible X.")
        route_id = self.store.add_route(
            campaign_id=campaign_id, title="Algebraic route", target_claim_id=root_claim_id,
            mode="DEDUCTIVE", method_family="algebra", representation="symbols",
            key_lemma="derive the bridge", central_mechanism="exact identity",
            decisive_test="check every term", difference_from_existing="first route",
            fingerprint="algebraic-route", independence_cluster="algebra", owner_slot="offline-1",
        )
        call = AgentCall(
            role="offline_researcher", slot="offline-1", prompt="test",
            project_root=self.root, network_policy="deny", campaign_id=campaign_id,
            route_id=route_id, epoch=1,
        )
        CampaignController(self.root, config)._process_research_outcome(
            campaign_id=campaign_id, epoch=1, call=call, root_claim_id=root_claim_id,
            outcome={
                "status": "PROGRESS", "summary": "Derived a route-local identity.",
                "claims": [{
                    "statement": "The exact identity holds under the stated assumptions.",
                    "assumptions": ["X is admissible"], "scope": "route-local",
                    "criticality": "supporting",
                }],
                "source_claims": [], "failures": [], "decisive_events": [],
                "novelty_evidence": [], "next_task": "Check the remaining bound.",
                "proof_candidate": None, "counterexample_candidate": None,
                "experiment_request": None, "numerical_evidence": None,
            },
        )
        partial_result = self.store.list_artifacts(kind="partial_result")
        self.assertEqual(len(partial_result), 1)
        tui = AriadneTUI(self.root, config_path)
        artifact_list = tui._artifacts_text()
        self.assertIn("partial result", artifact_list)
        tui.selected_artifact = next(
            i for i, item in enumerate(tui._visible_artifacts())
            if item["artifact_id"] == partial_result[0]["artifact_id"]
        )
        self.assertIn("Statement: The exact identity holds", tui._artifact_preview_text())
        claim_graph = tui._claims_text()
        self.assertIn("Logical propositions and dependencies only", claim_graph)
        self.assertIn("→ supports", claim_graph)
        self.assertNotIn(partial_result[0]["artifact_id"], claim_graph)
        claim_detail = tui._claim_preview_text()
        self.assertIn("Selected: Logical claim", claim_detail)
        self.assertIn("Supports:", claim_detail)
        self.assertIn("Underlying evidence and full research notes are in the Artifacts panel.", claim_detail)

    def test_route_roundtable_shares_only_complementary_route_state(self) -> None:
        config = load_config(self._config("offline_only", offline=1))
        controller = CampaignController(self.root, config)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=2, max_calls=10, max_cost_usd=2.0
        )
        root_claim_id = controller._ensure_root_claim(controller._load_contract())

        def add_route(title: str, representation: str, lemma: str) -> str:
            return self.store.add_route(
                campaign_id=campaign_id,
                title=title,
                target_claim_id=root_claim_id,
                mode=RouteMode.DEDUCTIVE,
                method_family="harmonic analysis",
                representation=representation,
                key_lemma=lemma,
                central_mechanism=(
                    "frequency localization"
                    if representation == "Fourier frequency space"
                    else "geometric separation"
                ),
                decisive_test=(
                    "prove the transfer estimate"
                    if representation == "Fourier frequency space"
                    else "prove the separation bound"
                ),
                difference_from_existing=title,
                fingerprint=title,
                independence_cluster=title,
                owner_slot=title,
            )

        route_a = add_route("Frequency decomposition", "Fourier frequency space", "localized multiplier estimate")
        route_b = add_route("Transfer bridge", "Fourier frequency space", "transfer multiplier estimate")
        add_route("Geometric alternative", "convex geometry", "separation estimate")
        self.store.add_attempt(
            campaign_id=campaign_id,
            route_id=route_b,
            epoch=1,
            agent_slot="route-b",
            task="establish transfer",
            result_kind="PROGRESS",
            summary="A partial multiplier estimate is retained for the bridge.",
            artifact_id=None,
            decisive_event=False,
            cost_usd=0.0,
            usage={},
        )
        packet = controller._route_roundtable(
            campaign_id=campaign_id, route=self.store.get_route(route_a)
        )
        self.assertEqual([item["route_id"] for item in packet], [route_b])
        self.assertEqual(packet[0]["roundtable_basis"], "shared representation")
        self.assertEqual(packet[0]["recent_results"][0]["result_kind"], "PROGRESS")

    def test_task_route_and_failure_panels_use_compact_lists_and_detail_previews(self) -> None:
        config_path = self._config("offline_only", offline=1)
        campaign_id = self.store.create_campaign(
            mode="offline_only", max_epochs=1, max_calls=4, max_cost_usd=1.0
        )
        root_claim_id = self.store.add_claim(statement="Prove P for every admissible X.")
        route_id = self.store.add_route(
            campaign_id=campaign_id, title="Algebraic route", target_claim_id=root_claim_id,
            mode="DEDUCTIVE", method_family="algebra", representation="symbols",
            key_lemma="derive the exact bridge", central_mechanism="factor the expression",
            decisive_test="check every term", difference_from_existing="first route",
            fingerprint="algebraic-route", independence_cluster="algebra", owner_slot="offline-1",
        )
        self.store.add_task(
            campaign_id=campaign_id, epoch=1, slot="offline-1", role="offline_researcher",
            route_id=route_id, summary="Derive the full exact bridge under all assumptions.",
        )
        attempt_id = self.store.add_attempt(
            campaign_id=campaign_id, route_id=route_id, epoch=1, agent_slot="offline-1",
            task="derive bridge", result_kind="BLOCKED", summary="The current representation loses a sign.",
            artifact_id=None, decisive_event=False, cost_usd=0.0, usage={},
        )
        self.store.upsert_failure(
            canonical_key="TEST|sign-loss", failure_class="SIGN_LOSS", signature="sign-loss",
            logical_scope="this representation", revival_conditions="use a symmetric form",
            attempt_id=attempt_id, cost_usd=0.0,
        )
        tui = AriadneTUI(self.root, config_path)
        task_list = tui._tasks_text()
        self.assertIn("j route/claim preview", task_list)
        self.assertIn("offline-1", task_list)
        self.assertIn("Algebraic route", task_list)
        self.assertNotIn("Derive the full exact bridge", task_list)
        task_preview = tui._task_preview_text()
        self.assertIn("Summary: Derive the full exact bridge", task_preview)
        self.assertIn("Title: Algebraic route", task_preview)
        self.assertIn("Statement: Prove P for every admissible X.", task_preview)
        route_list = tui._routes_text()
        self.assertIn("j route/claim preview", route_list)
        self.assertIn("Algebraic route", route_list)
        self.assertNotIn("derive the exact bridge", route_list)
        route_preview = tui._route_preview_text()
        self.assertIn("Key lemma: derive the exact bridge", route_preview)
        self.assertIn("Owner agent: offline-1", route_preview)
        self.assertIn("Statement: Prove P for every admissible X.", route_preview)
        failure_list = tui._failures_text()
        self.assertIn("j preview", failure_list)
        self.assertIn("• FAIL-", failure_list)
        self.assertFalse(any(marker in failure_list for marker in ("◐", "◓", "◑", "◒")))
        self.assertNotIn("use a symmetric form", failure_list)
        self.assertIn("Revival conditions: use a symmetric form", tui._failure_preview_text())

    def test_tui_run_builds_prompt_toolkit_layout(self) -> None:
        try:
            import prompt_toolkit.application
        except ImportError:
            self.skipTest("prompt_toolkit is not installed")
        config_path = self._config("offline_only", offline=1)
        with mock.patch(
            "prompt_toolkit.application.Application.run", return_value=None
        ):
            tui = AriadneTUI(self.root, config_path)
            tui.run()
        self.assertIsNotNone(tui._app)
        self.assertIsNone(tui._app.refresh_interval)

    def test_setup_document_type_matches_each_offline_mode(self) -> None:
        for mode, filename in (
            ("offline_sentinel", "literature_sentinel_note.md"),
            ("offline_only", "parked_literature_dossier.md"),
        ):
            with self.subTest(mode=mode):
                root = Path(self.temp.name) / f"setup-{mode}"
                store = ResearchStore(root)
                config_path = root / "config.toml"
                config_path.write_text(
                    MOCK_BASE.format(
                        mode="offline_sentinel",
                        offline_agents=2,
                        research_agents=0,
                        literature_intervention="true",
                    ),
                    encoding="utf-8",
                )
                answers = SetupAnswers(
                    title=f"{mode} setup",
                    statement="Prove or refute Q.",
                    objective="Resolve Q exactly.",
                    hypotheses_and_domains="All data are admissible.",
                    uniformity_and_endpoints="Uniform in all data.",
                    exclusions_and_statement_drift="No weakened target.",
                    proof_success="Complete proof.",
                    refutation_success="Exact counterexample.",
                    base_source_references="Mock source",
                    source_files=(),
                    literature_source_files=(),
                    research_mode=mode,
                    researcher_count=2,
                    parallel=True,
                    allow_live_literature=False,
                    literature_instructions="Record exact source hypotheses.",
                )
                result = generate_setup(
                    project_root=root,
                    config_path=config_path,
                    answers=answers,
                )
                self.assertEqual(Path(result["literature_document"]).name, filename)
                self.assertEqual(load_config(config_path).mode.name, mode)

    def test_setup_refuses_to_replace_contract_after_campaign_creation(self) -> None:
        config_path = self._config("offline_sentinel", offline=1)
        self.store.create_campaign(
            mode="offline_sentinel", max_epochs=1, max_calls=1, max_cost_usd=0.0
        )
        answers = SetupAnswers(
            title="Immutable target",
            statement="Prove or refute R.",
            objective="Resolve R.",
            hypotheses_and_domains="Exact hypotheses.",
            uniformity_and_endpoints="Exact uniformity.",
            exclusions_and_statement_drift="No drift.",
            proof_success="Complete proof.",
            refutation_success="Exact counterexample.",
            base_source_references="",
            source_files=(),
            literature_source_files=(),
            research_mode="offline_sentinel",
            researcher_count=1,
            parallel=False,
            allow_live_literature=False,
            literature_instructions="",
        )
        with self.assertRaisesRegex(ValueError, "cannot replace"):
            generate_setup(
                project_root=self.root,
                config_path=config_path,
                answers=answers,
            )


if __name__ == "__main__":
    unittest.main()
