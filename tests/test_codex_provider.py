from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ariadne_math.config import load_config
from ariadne_math.util import extract_json_object


FAKE_CODEX = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 999.0.0-test")
    raise SystemExit(0)
if args == ["login", "status"]:
    print("Logged in using test credentials")
    raise SystemExit(0)

if "exec" not in args:
    print("expected exec", file=sys.stderr)
    raise SystemExit(4)

role = os.environ.get("ARIADNE_ROLE", "")
output = {
    "offline_researcher": {
        "route": None,
        "status": "BLOCKED",
        "summary": "test",
        "claims": [],
        "failures": [],
        "decisive_events": [],
        "novelty_evidence": [],
        "next_task": "test",
        "proof_candidate": None,
        "counterexample_candidate": None,
        "experiment_request": None,
        "numerical_evidence": None
    },
    "literature_researcher": {
        "route": None,
        "status": "BLOCKED",
        "summary": "test",
        "claims": [],
        "source_claims": [],
        "failures": [],
        "decisive_events": [],
        "novelty_evidence": [],
        "next_task": "test",
        "proof_candidate": None,
        "counterexample_candidate": None,
        "experiment_request": None,
        "numerical_evidence": None
    },
    "contract_author": {
        "problem_contract": {
            "problem_id": "P-TEST",
            "title": "test",
            "research_mode": "offline_sentinel",
            "statement": {"text": "test"},
            "success_criteria": {"proof": "test", "refutation": "test"},
            "formalization_policy": {"lean_allowed_only_after_human_checked_proof": True}
        },
        "validation_notes": []
    },
    "literature_author": {
        "document_type": "literature_sentinel",
        "markdown": "# test",
        "sources": [],
        "warnings": []
    },
    "intervention_responder": {
        "decision": "ACCEPT_STOP",
        "reason": "test",
        "difference_certificate": {
            "assumptions_difference": "",
            "representation_difference": "",
            "key_lemma_difference": "",
            "outcome_difference": "",
            "decisive_test": ""
        },
        "proposed_test": ""
    },
    "literature_sentinel": {"interventions": []},
    "local_verifier": {
        "verdict": "UNCERTAIN",
        "failure_class": "VERIFIER_UNCERTAINTY",
        "minimal_failed_obligation": "test",
        "local_repairable": False,
        "statement_drift": False,
        "recommended_transition": "HUMAN_REVIEW",
        "verified_object": None,
        "verified_admissibility": None,
        "verified_violation": None,
        "verified_source_independence": None
    },
    "global_verifier": {
        "verdict": "UNCERTAIN",
        "failure_class": "VERIFIER_UNCERTAINTY",
        "minimal_failed_obligation": "test",
        "local_repairable": False,
        "statement_drift": False,
        "recommended_transition": "HUMAN_REVIEW",
        "verified_object": None,
        "verified_admissibility": None,
        "verified_violation": None,
        "verified_source_independence": None
    },
    "conceptual_pivot": {
        "new_representations": [],
        "needs_human": True,
        "human_question": "test"
    },
    "result_synthesizer": {
        "verdict": "NO_MEANINGFUL_RESULT",
        "proposal": None,
        "reason_no_proposal": "test",
        "used_artifact_ids": []
    },
    "instruction_interpreter": {
        "action": "ADD",
        "purpose": "RESEARCH_GUIDANCE",
        "instruction": "test",
        "audience": "researchers",
        "route_id": "",
        "target_instruction_ids": [],
        "required_artifacts": [],
        "budget": None,
        "target_variant": "",
        "clarification_needed": False,
        "clarifying_question": ""
    }
}[role]

out_path = Path(args[args.index("--output-last-message") + 1])
out_path.write_text(json.dumps(output), encoding="utf-8")
workspace = Path(args[args.index("--cd") + 1])
schema = Path(args[args.index("--output-schema") + 1])
log_path = os.environ.get("FAKE_CODEX_LOG")
if log_path:
    Path(log_path).write_text(json.dumps({
        "args": args,
        "prompt": sys.stdin.read(),
        "role": role,
        "network_policy": os.environ.get("ARIADNE_NETWORK_POLICY"),
        "has_openai_api_key": "OPENAI_API_KEY" in os.environ,
        "has_llamaparse_api_key": "LLAMAPARSE_API_KEY" in os.environ,
        "codex_home": os.environ.get("CODEX_HOME"),
        "workspace_exists": workspace.is_dir(),
        "workspace_entries": sorted(p.name for p in workspace.iterdir()),
        "context_manifest": (
            json.loads((workspace / "ariadne-context" / "MANIFEST.json").read_text(encoding="utf-8"))
            if (workspace / "ariadne-context" / "MANIFEST.json").is_file()
            else {}
        ),
        "schema_exists": schema.is_file()
    }), encoding="utf-8")
else:
    sys.stdin.read()
print(json.dumps({
    "type": "turn.completed",
    "usage": {"input_tokens": 123, "input_tokens_details": {"cached_tokens": 23}, "output_tokens": 45}
}))
'''


class CodexProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.codex = self.bin_dir / "codex"
        self.codex.write_text(FAKE_CODEX, encoding="utf-8")
        self.codex.chmod(self.codex.stat().st_mode | stat.S_IXUSR)
        self.log = self.root / "codex-log.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def env(self, role: str, policy: str = "deny") -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = str(self.bin_dir) + os.pathsep + env.get("PATH", "")
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        env["ARIADNE_ROLE"] = role
        env["ARIADNE_SLOT"] = "test-slot"
        env["ARIADNE_NETWORK_POLICY"] = policy
        env["FAKE_CODEX_LOG"] = str(self.log)
        env["OPENAI_API_KEY"] = "must-not-reach-offline-codex"
        env["LLAMAPARSE_API_KEY"] = "must-not-reach-offline-llamaparse"
        env["CODEX_HOME"] = str(self.root / "codex-home")
        return env

    def invoke(self, env: dict[str, str], prompt: str = "test prompt") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "ariadne_math.integrations.codex_provider"],
            input=prompt,
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def test_offline_research_role_is_ephemeral_coding_and_web_disabled(self) -> None:
        completed = self.invoke(self.env("offline_researcher", "deny"))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = extract_json_object(completed.stdout)
        self.assertEqual(
            result["usage"],
            {"input_tokens": 123, "cached_input_tokens": 23, "output_tokens": 45},
        )

        log = json.loads(self.log.read_text(encoding="utf-8"))
        args = log["args"]
        self.assertIn("--strict-config", args)
        self.assertIn("--ephemeral", args)
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ignore-rules", args)
        self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(args[args.index("--ask-for-approval") + 1], "never") if "--ask-for-approval" in args else None
        self.assertIn('web_search="disabled"', args)
        self.assertIn("agents.enabled=false", args)
        self.assertIn("memories.generate_memories=false", args)
        self.assertIn("features.shell_tool=true", args)
        self.assertIn("features.unified_exec=true", args)
        self.assertIn("features.apps=false", args)
        self.assertIn("features.skill_mcp_dependency_install=false", args)
        self.assertIn("check_for_update_on_startup=false", args)
        self.assertIn("feedback.enabled=false", args)
        self.assertIn("hide_agent_reasoning=true", args)
        self.assertIn("show_raw_agent_reasoning=false", args)
        self.assertTrue(log["workspace_exists"])
        self.assertEqual(log["workspace_entries"], [])
        self.assertTrue(log["schema_exists"])
        self.assertFalse(log["has_openai_api_key"])
        self.assertFalse(log["has_llamaparse_api_key"])
        self.assertEqual(log["codex_home"], str(self.root / "codex-home"))
        self.assertEqual(log["prompt"], "test prompt")

    def test_stages_curated_artifacts_in_an_isolated_workspace(self) -> None:
        project = self.root / "project"
        relative = ".ariadne/artifacts/aa/evidence.md"
        evidence = project / relative
        evidence.parent.mkdir(parents=True)
        evidence.write_text("exact retained lemma", encoding="utf-8")
        env = self.env("offline_researcher", "deny")
        env["ARIADNE_PROJECT_ROOT"] = str(project)
        env["ARIADNE_ARTIFACT_CONTEXT"] = json.dumps([
            {
                "id": "ART-test",
                "kind": "proof_candidate",
                "relative_path": relative,
            }
        ])
        completed = self.invoke(env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        log = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertIn("ariadne-context", log["workspace_entries"])
        manifest = log["context_manifest"]
        self.assertTrue(manifest["read_only"])
        self.assertEqual(manifest["available"][0]["id"], "ART-test")
        self.assertEqual(manifest["available"][0]["workspace_relative_path"], "ariadne-context/" + relative)
        self.assertEqual(manifest["available"][0]["context_reason"], "curated evidence")
        self.assertEqual(manifest["available"][0]["neighbor_of"], "")
        self.assertEqual(manifest["available"][0]["relations"], "")
        self.assertEqual(manifest["skipped"], [])

    def test_literature_research_role_has_coding_without_changing_web_policy(self) -> None:
        env = self.env("literature_researcher", "allow")
        env["ARIADNE_CODEX_WEB_SEARCH"] = "live"
        completed = self.invoke(env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        args = json.loads(self.log.read_text(encoding="utf-8"))["args"]
        self.assertEqual(args[args.index("--sandbox") + 1], "workspace-write")
        self.assertIn("features.shell_tool=true", args)
        self.assertIn("features.unified_exec=true", args)
        self.assertIn('web_search="live"', args)
        log = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertTrue(log["has_llamaparse_api_key"])

    def test_only_literature_roles_can_enable_live_web(self) -> None:
        env = self.env("literature_sentinel", "allow")
        env["ARIADNE_CODEX_WEB_SEARCH"] = "live"
        completed = self.invoke(env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        log = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertIn('web_search="live"', log["args"])

        for role in ("literature_researcher", "literature_author"):
            env = self.env(role, "allow")
            env["ARIADNE_CODEX_WEB_SEARCH"] = "live"
            completed = self.invoke(env)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            log = json.loads(self.log.read_text(encoding="utf-8"))
            self.assertIn('web_search="live"', log["args"])

        env = self.env("offline_researcher", "allow")
        env["ARIADNE_CODEX_WEB_SEARCH"] = "live"
        completed = self.invoke(env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        log = json.loads(self.log.read_text(encoding="utf-8"))
        self.assertIn('web_search="disabled"', log["args"])

    def test_check_command(self) -> None:
        env = self.env("offline_researcher", "deny")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ariadne_math.integrations.codex_provider",
                "--check",
            ],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Ariadne Codex provider: READY", completed.stdout)



    def test_init_can_write_codex_configuration(self) -> None:
        from ariadne_math.cli import _cmd_init
        from ariadne_math.config import load_config

        project = self.root / "initialized-project"
        self.assertEqual(_cmd_init(project, "test", "codex"), 0)
        config_path = project / "ariadne.codex.toml"
        self.assertTrue(config_path.is_file())
        config = load_config(config_path)
        self.assertIn("offline_researcher", config.roles)
        self.assertIn("literature_researcher", config.roles)
        self.assertIn("contract_author", config.roles)
        self.assertIn("literature_author", config.roles)
        self.assertTrue((project / ".ariadne" / "problem_contract.template.json").is_file())

    def test_legacy_gpt56_sol_provider_gets_default_token_pricing(self) -> None:
        config_path = self.root / "legacy.toml"
        config_path.write_text("""
[budget]
max_epochs = 1
max_calls = 1
max_cost_usd = 5.0

[mode]
name = "offline_only"
offline_agents = 1
literature_intervention = false

[providers.codex]
kind = "command"
command = ["ariadne-codex-provider"]
estimated_cost_usd = 1.0
[providers.codex.env]
ARIADNE_CODEX_MODEL = "gpt-5.6-sol"

[roles.offline_researcher]
provider = "codex"
network_policy = "deny"
""", encoding="utf-8")
        provider = load_config(config_path).providers["codex"]
        self.assertEqual(
            (provider.input_cost_per_million_usd, provider.cached_input_cost_per_million_usd, provider.output_cost_per_million_usd),
            (2.5, 0.25, 15.0),
        )

    def test_example_codex_config_loads(self) -> None:
        from ariadne_math.config import load_config

        config_path = Path(__file__).resolve().parents[1] / "examples" / "config.codex.toml"
        config = load_config(config_path)
        self.assertEqual(config.roles["offline_researcher"].network_policy, "deny")
        self.assertEqual(config.roles["literature_sentinel"].network_policy, "allow")
        self.assertEqual(
            config.providers["codex_offline"].command,
            ("ariadne-codex-provider",),
        )

    def test_research_proof_candidate_schema_required_keys_match_properties(self) -> None:
        from importlib.resources import files
        for filename in ("offline_researcher.json", "literature_researcher.json"):
            schema = json.loads(files("ariadne_math.integrations.codex").joinpath("schemas", filename).read_text())
            candidate = schema["properties"]["proof_candidate"]["anyOf"][1]
            self.assertEqual(set(candidate["required"]), set(candidate["properties"]))
            self.assertIn("proof_latex", candidate["required"])

    def test_all_role_schemas_are_packaged_and_loadable(self) -> None:
        from importlib.resources import files

        package_root = files("ariadne_math.integrations.codex")
        schema_dir = package_root.joinpath("schemas")
        names = {
            "offline_researcher.json",
            "literature_researcher.json",
            "contract_author.json",
            "contract_resolver.json",
            "literature_author.json",
            "intervention_responder.json",
            "literature_sentinel.json",
            "verifier.json",
            "conceptual_pivot.json",
            "proof_expander.json",
            "result_synthesizer.json",
            "instruction_interpreter.json",
        }
        def assert_openai_object_rules(node, path="$"):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIn("additionalProperties", node, path)
                    self.assertFalse(node["additionalProperties"], path)
                    properties = node.get("properties", {})
                    self.assertEqual(set(node.get("required", [])), set(properties), path)
                for key, value in node.items():
                    assert_openai_object_rules(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    assert_openai_object_rules(value, f"{path}[{index}]")

        for name in names:
            data = json.loads(schema_dir.joinpath(name).read_text(encoding="utf-8"))
            self.assertEqual(data["type"], "object")
            assert_openai_object_rules(data)

    def test_proof_expander_is_authorized_for_literature_tools_only_with_network_access(self) -> None:
        from unittest.mock import patch
        from ariadne_math.integrations.codex.research_tools import _network_guard
        from ariadne_math.integrations.codex_provider import _web_mode

        with patch.dict(os.environ, {
            "ARIADNE_ROLE": "proof_expander",
            "ARIADNE_NETWORK_POLICY": "allow",
            "ARIADNE_CODEX_WEB_SEARCH": "live",
        }, clear=False):
            self.assertEqual(_web_mode("proof_expander"), "live")
            self.assertIsNone(_network_guard())
        with patch.dict(os.environ, {
            "ARIADNE_ROLE": "proof_expander",
            "ARIADNE_NETWORK_POLICY": "deny",
        }, clear=False):
            with self.assertRaisesRegex(RuntimeError, "network tools require"):
                _network_guard()
        with patch.dict(os.environ, {
            "ARIADNE_ROLE": "contract_resolver",
            "ARIADNE_NETWORK_POLICY": "allow",
            "ARIADNE_CODEX_WEB_SEARCH": "live",
        }, clear=False):
            self.assertEqual(_web_mode("contract_resolver"), "live")
        with patch.dict(os.environ, {
            "ARIADNE_ROLE": "contract_author",
            "ARIADNE_NETWORK_POLICY": "allow",
            "ARIADNE_CODEX_WEB_SEARCH": "live",
        }, clear=False):
            self.assertEqual(_web_mode("contract_author"), "disabled")


if __name__ == "__main__":
    unittest.main()
