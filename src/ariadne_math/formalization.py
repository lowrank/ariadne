from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

from .artifacts import ArtifactStore
from .enums import ClaimStatus, EvidenceType
from .store import ResearchStore
from .transitions import transition_claim


class FormalizationGateClosed(RuntimeError):
    pass


def assert_formalization_gate(store: ResearchStore, claim_id: str) -> None:
    claim = store.get_claim(claim_id)
    if claim["status"] != ClaimStatus.HUMAN_CHECKED:
        raise FormalizationGateClosed(
            f"Claim {claim_id} is {claim['status']}; Lean is allowed only after HUMAN_CHECKED"
        )
    if not store.has_passing_human_review("claim", claim_id):
        raise FormalizationGateClosed(
            f"Claim {claim_id} lacks a passing human review artifact"
        )


def certify_formalization(
    store: ResearchStore,
    *,
    claim_id: str,
    toolchain: str,
    verify_command: Sequence[str],
    cwd: Path | None = None,
) -> str:
    assert_formalization_gate(store, claim_id)
    if not verify_command:
        raise ValueError("verify_command must not be empty")
    completed = subprocess.run(
        list(verify_command),
        cwd=cwd or store.paths.root,
        text=True,
        capture_output=True,
        check=False,
    )
    manifest = (
        f"Command: {list(verify_command)!r}\n"
        f"Working directory: {str((cwd or store.paths.root).resolve())}\n"
        f"Toolchain: {toolchain}\n"
        f"Return code: {completed.returncode}\n\n"
        f"## stdout\n\n{completed.stdout}\n\n"
        f"## stderr\n\n{completed.stderr}\n"
    )
    artifacts = ArtifactStore(store.paths)
    artifact = artifacts.put_text(
        manifest,
        kind="formal_verification_manifest",
        suffix=".md",
        metadata={
            "claim_id": claim_id,
            "toolchain": toolchain,
            "command": list(verify_command),
            "returncode": completed.returncode,
        },
    )
    store.record_artifact(artifact)
    if completed.returncode != 0:
        store.add_formalization(
            proof_claim_id=claim_id,
            status="FAILED",
            artifact_id=artifact.artifact_id,
            toolchain=toolchain,
        )
        raise RuntimeError(
            f"Formal verification command failed with exit code {completed.returncode}"
        )
    formalization_id = store.add_formalization(
        proof_claim_id=claim_id,
        status="PASSED",
        artifact_id=artifact.artifact_id,
        toolchain=toolchain,
    )
    store.add_evidence(
        claim_id=claim_id,
        evidence_type=EvidenceType.LEAN_PROOF,
        logical_force="Formal certification of the encoded statement under the recorded toolchain and verification command.",
        scope="encoded theorem and pinned project environment",
        artifact_id=artifact.artifact_id,
        status="ACCEPTED",
    )
    transition_claim(
        store,
        claim_id,
        ClaimStatus.FORMALLY_CERTIFIED,
        evidence_type=EvidenceType.LEAN_PROOF,
        artifact_present=True,
    )
    return formalization_id
