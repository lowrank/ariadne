from __future__ import annotations

import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest import mock

from ariadne_math.agent import AgentRunner
from ariadne_math.artifacts import ArtifactStore
from ariadne_math.config import BudgetConfig, HarnessConfig, ModeConfig, ProviderConfig, RoleConfig
from ariadne_math.enums import ClaimStatus, RouteMode, RouteStatus
from ariadne_math.models import AgentCall, ProviderResponse, Usage
from ariadne_math.store import CampaignAlreadyRunning, ResearchStore


class StoreTests(unittest.TestCase):
    def test_campaign_controller_lock_excludes_parallel_controller_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            with store.campaign_controller_lock():
                with self.assertRaises(CampaignAlreadyRunning):
                    with ResearchStore(Path(tmp)).campaign_controller_lock():
                        pass

            # Leaving the context simulates normal completion or Ctrl+C cleanup.
            with ResearchStore(Path(tmp)).campaign_controller_lock():
                pass

    def test_claim_route_and_failure_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            campaign = store.create_campaign(
                mode="offline_sentinel", max_epochs=3, max_calls=10, max_cost_usd=5.0
            )
            claim = store.add_claim(
                statement="For all x, P(x)", status=ClaimStatus.PROPOSED
            )
            route = store.add_route(
                campaign_id=campaign,
                title="Direct route",
                target_claim_id=claim,
                mode=RouteMode.DEDUCTIVE,
                method_family="induction",
                representation="native",
                key_lemma="induction step",
                central_mechanism="reduce n+1 to n",
                decisive_test="prove step",
                difference_from_existing="first route",
                fingerprint="induction native",
                independence_cluster="induction",
                owner_slot="offline-1",
            )
            attempt = store.add_attempt(
                campaign_id=campaign,
                route_id=route,
                epoch=1,
                agent_slot="offline-1",
                task="prove step",
                result_kind="BLOCKED",
                summary="constant is nonuniform",
                artifact_id=None,
                decisive_event=False,
                cost_usd=0.1,
                usage={},
            )
            failure, count = store.upsert_failure(
                canonical_key="NONUNIFORM|constant n",
                failure_class="NONUNIFORM_CONSTANT",
                signature="constant depends on n",
                logical_scope="this induction estimate",
                revival_conditions="new invariant",
                attempt_id=attempt,
                cost_usd=0.1,
            )
            self.assertEqual(count, 1)
            self.assertEqual(store.get_route(route)["title"], "Direct route")
            self.assertEqual(store.list_failures()[0]["failure_id"], failure)
            self.assertGreaterEqual(len(store.events.read_all()), 4)


    def test_budget_reservation_is_atomic_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            campaign = store.create_campaign(
                mode="offline_only", max_epochs=1, max_calls=1, max_cost_usd=1.0
            )

            def reserve(index: int) -> bool:
                return store.reserve_budget(
                    campaign,
                    reservation_id=f"test-reservation-{index}",
                    estimated_cost_usd=0.5,
                )

            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(reserve, range(6)))
            self.assertEqual(sum(results), 1)
            state = store.get_campaign(campaign)
            self.assertEqual(state["calls_used"], 1)
            self.assertEqual(state["cost_used"], 0.5)

    def test_recover_interrupted_campaign_preserves_reserved_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ResearchStore(root)
            campaign = store.create_campaign(
                mode="offline_only", max_epochs=1, max_calls=2, max_cost_usd=5.0
            )
            prompt = ArtifactStore(store.paths).put_text("prompt", kind="agent_prompt")
            store.record_artifact(prompt)
            run_id = store.start_agent_run(
                campaign_id=campaign,
                role="offline_researcher",
                slot="offline-1",
                route_id=None,
                epoch=1,
                task_summary="stale task",
                provider="mock",
                network_policy="deny",
                isolation_status="MOCK_ISOLATED",
                prompt_artifact_id=prompt.artifact_id,
            )
            task_id = store.add_task(
                campaign_id=campaign,
                epoch=1,
                slot="offline-1",
                role="offline_researcher",
                route_id=None,
                summary="stale task",
            )
            store.start_task(task_id, run_id=run_id)
            self.assertTrue(
                store.reserve_budget(
                    campaign, reservation_id=run_id, estimated_cost_usd=1.0
                )
            )
            recovered = store.recover_interrupted_campaign(campaign, recovered_by="test")
            self.assertEqual(recovered, {"agent_runs": 1, "tasks": 1})
            self.assertEqual(store.get_campaign(campaign)["status"], "PAUSED_HUMAN")
            self.assertEqual(store.get_campaign(campaign)["calls_used"], 1)
            self.assertEqual(store.get_campaign(campaign)["cost_used"], 1.0)
            self.assertEqual(store.list_agent_runs(campaign)[0]["status"], "INTERRUPTED")
            self.assertEqual(store.list_tasks(campaign)[0]["status"], "CANCELLED")

    def test_budget_adjustment_requires_safe_pause_and_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            campaign = store.create_campaign(
                mode="offline_only", max_epochs=3, max_calls=4, max_cost_usd=5.0
            )
            with self.assertRaisesRegex(ValueError, "RUNNING, PAUSED_HUMAN, BUDGET_EXHAUSTED, or COMPLETED_UNSOLVED"):
                store.adjust_campaign_budget(
                    campaign, max_calls=6, adjusted_by="tester", reason="Need another route"
                )
            store.update_campaign(campaign, status="RUNNING", epoch=1)
            running = store.adjust_campaign_budget(
                campaign, max_epochs=4, max_calls=6, max_cost_usd=7.0,
                adjusted_by="tester", reason="Fund the next epoch without interrupting this one",
            )
            self.assertEqual(running["status"], "RUNNING")
            self.assertEqual(running["max_epochs"], 3)
            self.assertEqual(store.get_campaign(campaign)["max_calls"], 4)
            self.assertEqual(running["requested_limits"]["max_calls"], 6)
            self.assertEqual(running["scheduled_action"]["status"], "PENDING")
            running_decision = store.list_decisions(campaign)[-1]
            self.assertEqual(running_decision["selected"]["application"], "NEXT_EPOCH")
            replacement = store.adjust_campaign_budget(
                campaign, max_epochs=4, max_calls=5, max_cost_usd=6.0,
                adjusted_by="tester", reason="Lower the ceiling after the active epoch",
            )
            self.assertEqual(replacement["scheduled_action"]["status"], "PENDING")
            self.assertEqual(len(store.list_scheduled_campaign_actions(campaign, pending_only=True)), 2)
            applied = store.apply_scheduled_campaign_actions(campaign)
            self.assertEqual([item["status"] for item in applied], ["APPLIED", "APPLIED"])
            self.assertEqual(store.get_campaign(campaign)["max_calls"], 5)
            self.assertEqual(store.get_campaign(campaign)["max_cost_usd"], 6.0)

            store.update_campaign(campaign, status="PAUSED_HUMAN", epoch=1)
            updated = store.adjust_campaign_budget(
                campaign, max_epochs=5, max_calls=8, max_cost_usd=9.5,
                adjusted_by="tester", reason="Fund independent verification",
            )
            self.assertEqual(updated["max_epochs"], 5)
            self.assertEqual(updated["max_calls"], 8)
            self.assertEqual(updated["max_cost_usd"], 9.5)
            decisions = store.list_decisions(campaign)
            self.assertEqual(decisions[-1]["kind"], "HUMAN_BUDGET_ADJUSTMENT")
            self.assertTrue(any(
                event["event_type"] == "campaign_budget_adjusted"
                for event in store.events.read_all()
            ))
            store.update_campaign(campaign, status="BUDGET_EXHAUSTED")
            reopened = store.adjust_campaign_budget(
                campaign, max_calls=9, adjusted_by="tester", reason="Reopen exhausted budget"
            )
            self.assertEqual(reopened["status"], "PAUSED_HUMAN")

            with self.assertRaisesRegex(ValueError, "below calls already used"):
                store.adjust_campaign_budget(
                    campaign, max_calls=-1, adjusted_by="tester", reason="Invalid reduction"
                )

    def test_running_route_control_is_applied_before_next_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            campaign = store.create_campaign(
                mode="offline_only", max_epochs=3, max_calls=4, max_cost_usd=5.0
            )
            claim = store.add_claim(
                statement="For all x, P(x)", status=ClaimStatus.PROPOSED
            )
            route_id = store.add_route(
                campaign_id=campaign,
                title="Direct route",
                target_claim_id=claim,
                mode=RouteMode.DEDUCTIVE,
                method_family="induction",
                representation="native",
                key_lemma="induction step",
                central_mechanism="reduce n+1 to n",
                decisive_test="prove step",
                difference_from_existing="first route",
                fingerprint="induction native",
                independence_cluster="induction",
                owner_slot="offline-1",
            )
            store.update_campaign(campaign, status="RUNNING", epoch=1)
            result = store.set_human_route_status(
                campaign_id=campaign,
                route_id=route_id,
                status=RouteStatus.PARKED,
                requested_by="tester",
                rationale="Avoid duplicating the active epoch's assigned work",
            )
            self.assertEqual(result["application"], "NEXT_EPOCH")
            self.assertEqual(store.get_route(route_id)["status"], RouteStatus.ACTIVE)
            pending = store.list_scheduled_campaign_actions(campaign, pending_only=True)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["kind"], "ROUTE_STATUS")
            applied = store.apply_scheduled_campaign_actions(campaign)
            self.assertEqual(applied[0]["status"], "APPLIED")
            self.assertEqual(store.get_route(route_id)["status"], RouteStatus.PARKED)
            self.assertEqual(store.list_scheduled_campaign_actions(campaign)[0]["status"], "APPLIED")

    def test_agent_settles_usage_only_provider_at_configured_token_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ResearchStore(root)
            campaign = store.create_campaign(
                mode="offline_only", max_epochs=1, max_calls=2, max_cost_usd=10.0
            )
            config = HarnessConfig(
                providers={
                    "metered": ProviderConfig(
                        name="metered", kind="mock", estimated_cost_usd=1.0,
                        input_cost_per_million_usd=2.5,
                        cached_input_cost_per_million_usd=0.25,
                        output_cost_per_million_usd=15.0,
                    )
                },
                roles={"offline_researcher": RoleConfig(
                    name="offline_researcher", provider="metered", network_policy="deny"
                )},
                budget=BudgetConfig(max_epochs=1, max_calls=2, max_cost_usd=10.0),
                mode=ModeConfig(name="offline_only", offline_agents=1, literature_intervention=False),
            )

            class UsageOnlyProvider:
                def run(self, call):
                    return ProviderResponse(
                        text="usage-only response",
                        usage=Usage(
                            input_tokens=1_000_000,
                            cached_input_tokens=400_000,
                            output_tokens=200_000,
                        ),
                    )

            with mock.patch("ariadne_math.agent.create_provider", return_value=UsageOnlyProvider()):
                AgentRunner(store, config).call(AgentCall(
                    role="offline_researcher", slot="offline-1", prompt="test",
                    project_root=root, network_policy="deny", campaign_id=campaign, epoch=1,
                ))
            # 0.6M uncached*$2.50 + 0.4M cached*$0.25 + 0.2M output*$15 = $4.60.
            state = store.get_campaign(campaign)
            self.assertEqual(state["cost_used"], 4.6)
            run = store.list_agent_runs(campaign)[0]
            self.assertEqual(run["cost_usd"], 4.6)

    def test_literature_selection_keeps_small_dossier_and_ranks_large_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            for index in range(13):
                keyword = "Fourier multiplier oscillation" if index == 0 else f"unrelated combinatorics topic {index}"
                store.add_literature_source(
                    title=f"{keyword} source {index}",
                    citation=f"Ref {index}",
                    source_kind="article",
                    exact_statement=f"A theorem about {keyword}.",
                    assumptions=[],
                    locator=f"Thm {index}",
                    relative_path=f".ariadne/literature/source-{index}.md",
                )
            all_sources = store.select_literature_sources(
                query="anything", limit=20
            )
            self.assertEqual(len(all_sources), 13)
            selected = store.select_literature_sources(
                query="Fourier multiplier oscillation", limit=12
            )
            self.assertEqual(len(selected), 12)
            self.assertEqual(selected[0]["title"], "Fourier multiplier oscillation source 0")

    def test_agent_exposes_recent_artifacts_to_provider_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ResearchStore(root)
            campaign = store.create_campaign(
                mode="offline_only", max_epochs=1, max_calls=2, max_cost_usd=5.0
            )
            evidence = ArtifactStore(store.paths).put_text(
                "Exact retained evidence.", kind="partial_result"
            )
            store.record_artifact(evidence)
            config = HarnessConfig(
                providers={"mock": ProviderConfig(name="mock", kind="mock")},
                roles={"offline_researcher": RoleConfig(
                    name="offline_researcher", provider="mock", network_policy="deny"
                )},
                budget=BudgetConfig(max_epochs=1, max_calls=2, max_cost_usd=5.0),
                mode=ModeConfig(name="offline_only", offline_agents=1, literature_intervention=False),
            )
            captured = []

            class CapturingProvider:
                def run(self, call):
                    captured.append(call)
                    return ProviderResponse(text="{}", usage=Usage())

            with mock.patch("ariadne_math.agent.create_provider", return_value=CapturingProvider()):
                AgentRunner(store, config).call(AgentCall(
                    role="offline_researcher",
                    slot="offline-1",
                    prompt="Inspect retained evidence.",
                    project_root=root,
                    network_policy="deny",
                    campaign_id=campaign,
                    epoch=1,
                ))
            self.assertIn("Local artifact context", captured[0].prompt)
            context = captured[0].metadata["artifact_context"]
            self.assertEqual(context[0]["id"], evidence.artifact_id)
            self.assertEqual(context[0]["relative_path"], str(evidence.path.relative_to(root)))

    def test_agent_uses_estimated_cost_when_provider_omits_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ResearchStore(root)
            campaign = store.create_campaign(
                mode="offline_only", max_epochs=1, max_calls=2, max_cost_usd=5.0
            )
            config = HarnessConfig(
                providers={
                    "mock": ProviderConfig(
                        name="mock", kind="mock", estimated_cost_usd=1.25
                    )
                },
                roles={
                    "offline_researcher": RoleConfig(
                        name="offline_researcher", provider="mock", network_policy="deny"
                    )
                },
                budget=BudgetConfig(max_epochs=1, max_calls=2, max_cost_usd=5.0),
                mode=ModeConfig(
                    name="offline_only",
                    offline_agents=1,
                    literature_intervention=False,
                ),
            )
            AgentRunner(store, config).call(
                AgentCall(
                    role="offline_researcher",
                    slot="offline-1",
                    prompt="test",
                    project_root=root,
                    network_policy="deny",
                    campaign_id=campaign,
                    epoch=1,
                )
            )
            state = store.get_campaign(campaign)
            self.assertEqual(state["calls_used"], 1)
            self.assertEqual(state["cost_used"], 1.25)


if __name__ == "__main__":
    unittest.main()
