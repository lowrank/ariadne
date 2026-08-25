from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from ariadne_math.contracts import CONTRACT_TEMPLATE
from ariadne_math.successor import (
    create_contract_variant_successor,
    enable_project_git,
    record_campaign_epoch,
)
from ariadne_math.store import ResearchStore
from ariadne_math.util import read_json, write_json


class SuccessorTests(unittest.TestCase):
    def _parent(self, root: Path) -> tuple[ResearchStore, Path]:
        store = ResearchStore(root)
        contract = {
            **CONTRACT_TEMPLATE,
            "title": "Parent theorem",
            "statement": {
                "text": "Prove P.",
                "formal_quantifier_outline": "P",
            },
        }
        write_json(store.paths.contract, contract)
        config = root / "ariadne.codex.toml"
        config.write_text("[budget]\nmax_epochs = 1\n", encoding="utf-8")
        (root / "README.md").write_text("# Parent\n", encoding="utf-8")
        return store, config

    def test_successor_without_git_is_a_clean_sibling_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "parent"
            store, config = self._parent(parent)
            child = Path(tmp) / "child"
            result = create_contract_variant_successor(
                parent_root=parent,
                config_path=config,
                target_variant="Prove the strengthened statement P'.",
                request_artifact_id="ART-variant-request",
                successor_root=child,
            )
            self.assertEqual(result, child)
            self.assertFalse((child / ".ariadne").exists())
            self.assertTrue((child / "ariadne.codex.toml").is_file())
            self.assertIn("strengthened statement", (child / "SUCCESSOR_TASK.md").read_text(encoding="utf-8"))
            provenance = read_json(child / "SUCCESSOR_PROVENANCE.json")
            self.assertIsNone(provenance["git_branch"])
            ledger = read_json(store.paths.state / "contract_lineage.json")
            self.assertEqual(ledger["versions"][-1]["successor_project"], str(child))

    @unittest.skipUnless(shutil.which("git"), "Git is not installed")
    def test_opted_in_git_uses_a_clean_successor_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "parent"
            self._parent(parent)
            enabled, commit = enable_project_git(parent)
            self.assertTrue(enabled)
            self.assertTrue(commit)
            self.assertTrue((parent / ".git").exists())
            self.assertIn(".ariadne/", (parent / ".gitignore").read_text(encoding="utf-8"))

            child = Path(tmp) / "child"
            create_contract_variant_successor(
                parent_root=parent,
                config_path=parent / "ariadne.codex.toml",
                target_variant="Refine P with its sharp endpoint.",
                request_artifact_id="ART-variant-request",
                successor_root=child,
            )
            provenance = read_json(child / "SUCCESSOR_PROVENANCE.json")
            self.assertTrue(str(provenance["git_branch"]).startswith("contract-variant-"))
            self.assertFalse((child / ".ariadne").exists())
            self.assertTrue((child / "SUCCESSOR_TASK.md").is_file())

    @unittest.skipUnless(shutil.which("git"), "Git is not installed")
    def test_epoch_record_is_committed_and_decisive_epoch_is_tagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            self._parent(root)
            enabled, _ = enable_project_git(root)
            self.assertTrue(enabled)
            record = record_campaign_epoch(
                root,
                campaign_id="CMP-123",
                epoch=1,
                summary="A load-bearing exact reduction was retained.",
                attempt_count=2,
                decisive_events=1,
                status="RUNNING",
            )
            self.assertTrue(record["recorded"])
            self.assertTrue(record["tagged"])
            self.assertIn("CMP-123", (root / "ARIADNE_EPOCHS.md").read_text(encoding="utf-8"))
            self.assertEqual(record["tag"], "ariadne/CMP-123/epoch-1-progress")


if __name__ == "__main__":
    unittest.main()
