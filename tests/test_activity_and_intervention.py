from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ariadne_math.activity import ConsoleActivityReporter, EventLogActivityReporter
from ariadne_math.config import load_config
from ariadne_math.controller import CampaignController
from ariadne_math.store import CampaignAlreadyRunning, ResearchStore
from ariadne_math.util import write_json


SLOW_AGENT = r'''from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

prompt = sys.stdin.read()
log = Path(os.environ["PROMPT_LOG"])
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"epoch": os.environ.get("ARIADNE_EPOCH"), "prompt": prompt}) + "\n")
time.sleep(1.1)
epoch = int(os.environ.get("ARIADNE_EPOCH", "1"))
route = None
if epoch == 1:
    route = {
        "title": "Human-steerable route",
        "mode": "DEDUCTIVE",
        "method_family": "direct argument",
        "representation": "native variables",
        "key_lemma": "derive the bridge identity",
        "central_mechanism": "exact cancellation",
        "decisive_test": "prove the identity",
        "difference_from_existing": "first route with an exact cancellation target",
        "independence_cluster": "direct"
    }
result = {
    "route": route,
    "status": "PROGRESS" if epoch == 1 else "BLOCKED",
    "summary": "First bounded task completed" if epoch == 1 else "Second bounded task read the human instruction",
    "claims": [],
    "failures": [],
    "decisive_events": [{"type": "PRECISE_BRIDGE", "description": "bridge isolated"}] if epoch == 1 else [],
    "novelty_evidence": [],
    "next_task": "Use the owner instruction" if epoch == 1 else "Pause for review",
    "proof_candidate": None,
    "counterexample_candidate": None,
    "experiment_request": None,
        "numerical_evidence": None
}
print("<ARIADNE_JSON>")
print(json.dumps(result))
print("</ARIADNE_JSON>")
'''


class ActivityAndInterventionTests(unittest.TestCase):
    def _make_project(self, root: Path) -> tuple[ResearchStore, Path, Path]:
        store = ResearchStore(root)
        write_json(
            store.paths.contract,
            {
                "problem_id": "HUMAN-CTRL",
                "statement": {"text": "Prove or refute P"},
                "hypotheses": [],
                "success_criteria": {"proof": "proof", "refutation": "counterexample"},
                "formalization_policy": {
                    "lean_allowed_only_after_human_checked_proof": True
                },
            },
        )
        script = root / "slow_agent.py"
        script.write_text(SLOW_AGENT, encoding="utf-8")
        prompt_log = root / "prompts.jsonl"
        config = root / "config.toml"
        config.write_text(
            f'''[budget]
max_epochs = 2
max_calls = 5
max_cost_usd = 5.0
stagnation_epochs = 2
duplicate_failure_limit = 2

[mode]
name = "offline_sentinel"
offline_agents = 1
parallel = false
literature_intervention = false
require_route_difference_certificate = true
novelty_deadline_epochs = 1
allow_experiments = false
route_similarity_threshold = 0.82

[providers.slow]
kind = "command"
command = [{json.dumps(sys.executable)}, {json.dumps(str(script))}]
timeout_seconds = 10
estimated_cost_usd = 0.0

[providers.slow.env]
PROMPT_LOG = {json.dumps(str(prompt_log))}

[roles.offline_researcher]
provider = "slow"
network_policy = "deny"
''',
            encoding="utf-8",
        )
        return store, config, prompt_log

    def test_controller_refuses_admission_while_project_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, config_path, _ = self._make_project(root)
            with store.campaign_controller_lock():
                with self.assertRaises(CampaignAlreadyRunning):
                    CampaignController(root, load_config(config_path)).run()
            self.assertIsNone(store.latest_campaign())

    def test_live_activity_pause_instruction_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, config_path, prompt_log = self._make_project(root)
            stream = io.StringIO()
            reporter = ConsoleActivityReporter(
                heartbeat_seconds=1.0, stream=stream
            )
            result_holder: dict[str, object] = {}

            def run_first() -> None:
                result_holder["first"] = CampaignController(
                    root, load_config(config_path), reporter=reporter
                ).run()

            thread = threading.Thread(target=run_first)
            thread.start()
            campaign_id = None
            deadline = time.time() + 5
            while time.time() < deadline:
                latest = store.latest_campaign()
                if latest:
                    campaign_id = str(latest["campaign_id"])
                    active_runs = store.list_agent_runs(campaign_id, active_only=True)
                    if active_runs:
                        self.assertIn("structurally independent", active_runs[0]["task_summary"])
                        self.assertEqual(active_runs[0]["epoch"], 1)
                        break
                time.sleep(0.05)
            self.assertIsNotNone(campaign_id)
            assert campaign_id is not None
            store.request_campaign_pause(
                campaign_id,
                reason="Add a new structural instruction",
                requested_by="test-human",
            )
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            first = result_holder["first"]
            self.assertEqual(first["status"], "PAUSED_HUMAN")  # type: ignore[index]

            instruction_id = store.add_human_instruction(
                campaign_id=campaign_id,
                instruction_text="Use the dual identity and avoid the old absolute-value estimate.",
                audience="researchers",
                author="test-human",
            )
            self.assertTrue(instruction_id.startswith("HIN-"))
            store.clear_campaign_pause(campaign_id, cleared_by="test-human")

            second_stream = io.StringIO()
            second = CampaignController(
                root,
                load_config(config_path),
                reporter=ConsoleActivityReporter(
                    heartbeat_seconds=1.0, stream=second_stream
                ),
            ).run(new_campaign=False)
            self.assertEqual(second["epoch"], 2)
            prompts = [json.loads(line) for line in prompt_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(prompts), 2)
            self.assertNotIn("HUMAN_INTERVENTIONS", prompts[0]["prompt"])
            self.assertIn("<HUMAN_INTERVENTIONS>", prompts[1]["prompt"])
            self.assertIn("Use the dual identity", prompts[1]["prompt"])

            output = stream.getvalue()
            self.assertIn("CAMPAIGN STARTED", output)
            self.assertIn("EPOCH PLAN", output)
            self.assertIn("AGENT STARTED", output)
            self.assertIn("HEARTBEAT", output)
            self.assertIn("CAMPAIGN PAUSED", output)
            self.assertIn("pause pending", output)
            self.assertIn("CAMPAIGN RESUMED", second_stream.getvalue())


    def test_event_log_reporter_records_stage_events_without_heartbeats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ResearchStore(Path(tmp))
            campaign = store.create_campaign(
                mode="offline_only", max_epochs=1, max_calls=2, max_cost_usd=1.0
            )
            reporter = EventLogActivityReporter(store.events.append)
            reporter.start(campaign_id=campaign)
            reporter.call_started(
                run_id="RUN-test", role="offline_researcher", slot="offline-1",
                route_id=None, provider="mock", task="Check the exact bridge.",
            )
            reporter.call_finished(
                run_id="RUN-test", role="offline_researcher", slot="offline-1",
                route_id=None, elapsed_seconds=1.0, input_tokens=0,
                output_tokens=0, cost_usd=0.0,
            )
            reporter.stop()
            events = store.events.read_tail(2)
            self.assertEqual(
                [event["event_type"] for event in events],
                ["agent_started", "agent_finished"],
            )
            self.assertTrue(all(event["payload"]["campaign_id"] == campaign for event in events))
            self.assertNotIn("heartbeat", [event["event_type"] for event in store.events.read_all()])

    def test_existing_database_gets_activity_columns(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".ariadne"
            state.mkdir()
            database = state / "state.sqlite"
            conn = sqlite3.connect(database)
            conn.execute(
                "CREATE TABLE agent_runs ("
                "run_id TEXT PRIMARY KEY, campaign_id TEXT, role TEXT NOT NULL, "
                "slot TEXT NOT NULL, route_id TEXT, provider TEXT NOT NULL, "
                "network_policy TEXT NOT NULL, isolation_status TEXT NOT NULL, "
                "status TEXT NOT NULL, prompt_artifact_id TEXT, response_artifact_id TEXT, "
                "input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, "
                "cost_usd REAL NOT NULL DEFAULT 0.0, started_at TEXT NOT NULL, finished_at TEXT)"
            )
            conn.commit()
            conn.close()
            ResearchStore(root)
            conn = sqlite3.connect(database)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_runs)")}
            conn.close()
            self.assertIn("epoch", columns)
            self.assertIn("task_summary", columns)

    def test_route_specific_instruction_is_not_sent_to_new_route_calls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, _, _ = self._make_project(root)
            campaign = store.create_campaign(
                mode="offline_sentinel", max_epochs=2, max_calls=5, max_cost_usd=1.0
            )
            claim = store.add_claim(statement="P")
            route = store.add_route(
                campaign_id=campaign,
                title="R",
                target_claim_id=claim,
                mode="DEDUCTIVE",
                method_family="m",
                representation="r",
                key_lemma="l",
                central_mechanism="c",
                decisive_test="d",
                difference_from_existing="new route",
                fingerprint="m r l c",
                independence_cluster="m",
                owner_slot="offline-1",
            )
            store.add_human_instruction(
                campaign_id=campaign,
                route_id=route,
                instruction_text="Only this route",
                audience="researchers",
            )
            self.assertEqual(
                store.instructions_for_agent(
                    campaign_id=campaign,
                    role="offline_researcher",
                    route_id=None,
                ),
                [],
            )
            self.assertEqual(
                len(
                    store.instructions_for_agent(
                        campaign_id=campaign,
                        role="offline_researcher",
                        route_id=route,
                    )
                ),
                1,
            )


if __name__ == "__main__":
    unittest.main()
