from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .store import ResearchStore
from .util import content_hash, read_json, short_id, utc_now, write_json


_GITIGNORE = """# Ariadne runtime state is durable campaign data, not source control.
.ariadne/
__pycache__/
*.pyc
"""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], text=True,
        capture_output=True, check=False,
    )


def _ensure_git_snapshot(root: Path) -> tuple[bool, str]:
    """Initialize and snapshot a project after the owner opted into Git."""
    if shutil.which("git") is None:
        return False, ""
    probe = _git(root, "rev-parse", "--is-inside-work-tree")
    if probe.returncode != 0:
        created = subprocess.run(
            ["git", "init", str(root)], text=True, capture_output=True, check=False
        )
        if created.returncode != 0:
            return False, ""
    ignore = root / ".gitignore"
    existing = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if ".ariadne/" not in existing.splitlines():
        ignore.write_text(existing.rstrip() + "\n\n" + _GITIGNORE, encoding="utf-8")
    if _git(root, "config", "--get", "user.name").returncode != 0:
        _git(root, "config", "user.name", "Ariadne Harness")
    if _git(root, "config", "--get", "user.email").returncode != 0:
        _git(root, "config", "user.email", "ariadne@local")
    _git(root, "add", "-A")
    staged = _git(root, "diff", "--cached", "--quiet")
    has_head = _git(root, "rev-parse", "--verify", "HEAD").returncode == 0
    if staged.returncode != 0 or not has_head:
        committed = _git(root, "commit", "--allow-empty", "-m", "Ariadne project snapshot before contract branch")
        if committed.returncode != 0:
            return False, ""
    head = _git(root, "rev-parse", "HEAD")
    return head.returncode == 0, head.stdout.strip() if head.returncode == 0 else ""


def enable_project_git(root: Path) -> tuple[bool, str]:
    """Enable local project versioning and return whether a snapshot was made.

    This is intentionally called only from the explicit setup choice. A missing
    Git executable is non-fatal: Ariadne still records its immutable contract
    lineage and can make non-Git successor directories.
    """
    return _ensure_git_snapshot(root.resolve())


def project_has_git_repository(root: Path) -> bool:
    """Return whether ``root`` already belongs to a usable Git worktree."""
    return shutil.which("git") is not None and (
        _git(root.resolve(), "rev-parse", "--is-inside-work-tree").returncode == 0
    )


def record_campaign_epoch(
    root: Path,
    *,
    campaign_id: str,
    epoch: int,
    summary: str,
    attempt_count: int,
    decisive_events: int,
    status: str,
) -> dict[str, object]:
    """Commit a compact, auditable epoch record and tag meaningful progress.

    Git is deliberately best-effort operational provenance: a failed commit
    does not interrupt a mathematical campaign. Mutable `.ariadne` databases
    and large artifacts remain ignored; the committed record points back to the
    campaign and states only the public epoch summary.
    """
    project = root.resolve()
    if not project_has_git_repository(project):
        return {"enabled": False, "recorded": False, "tagged": False}
    try:
        log_path = project / "ARIADNE_EPOCHS.md"
        existing = log_path.read_text(encoding="utf-8") if log_path.exists() else (
            "# Ariadne campaign epoch log\n\n"
            "Compact Git-tracked provenance. Detailed mutable state and artifacts live under `.ariadne/`.\n"
        )
        record = (
            f"\n## {campaign_id} — epoch {epoch}\n\n"
            f"- Recorded: {utc_now()}\n"
            f"- Status at checkpoint: `{status}`\n"
            f"- Bounded attempts: {attempt_count}\n"
            f"- Decisive events: {decisive_events}\n"
            f"- Summary: {summary.strip()}\n"
        )
        if f"## {campaign_id} — epoch {epoch}\n" not in existing:
            log_path.write_text(existing.rstrip() + "\n" + record, encoding="utf-8")
        _git(project, "add", "ARIADNE_EPOCHS.md")
        staged = _git(project, "diff", "--cached", "--quiet")
        commit = ""
        if staged.returncode != 0:
            completed = _git(project, "commit", "-m", f"Record Ariadne {campaign_id} epoch {epoch}")
            if completed.returncode != 0:
                return {
                    "enabled": True, "recorded": False, "tagged": False,
                    "error": completed.stderr.strip() or completed.stdout.strip(),
                }
            head = _git(project, "rev-parse", "HEAD")
            commit = head.stdout.strip() if head.returncode == 0 else ""
        else:
            head = _git(project, "rev-parse", "HEAD")
            commit = head.stdout.strip() if head.returncode == 0 else ""

        progress = decisive_events > 0
        tag_name = f"ariadne/{campaign_id}/epoch-{epoch}-progress"
        tagged = False
        if progress and _git(project, "rev-parse", "-q", "--verify", f"refs/tags/{tag_name}").returncode != 0:
            tagged = _git(project, "tag", "-a", tag_name, "-m", f"Ariadne meaningful progress in {campaign_id} epoch {epoch}").returncode == 0
        return {
            "enabled": True, "recorded": True, "tagged": tagged,
            "tag": tag_name if progress else None, "commit": commit or None,
        }
    except OSError as exc:
        return {"enabled": True, "recorded": False, "tagged": False, "error": str(exc)}


def next_successor_path(parent_root: Path) -> Path:
    parent = parent_root.resolve()
    base = parent.with_name(parent.name + "-variant")
    candidate = base
    index = 2
    while candidate.exists():
        candidate = parent.with_name(f"{parent.name}-variant-{index}")
        index += 1
    return candidate


def _load_ledger(parent_store: ResearchStore) -> dict[str, object]:
    ledger_path = parent_store.paths.state / "contract_lineage.json"
    if ledger_path.exists():
        return read_json(ledger_path)
    contract_sha = content_hash(parent_store.paths.contract.read_bytes())
    root_branch = "VER-ROOT-" + contract_sha[:12]
    return {
        "format": 2,
        "root_project": str(parent_store.paths.root),
        "versions": [{
            "version_id": root_branch,
            "branch_id": root_branch,
            "branch_name": "main",
            "parent_branch_id": None,
            "kind": "ROOT",
            "contract_sha256": contract_sha,
            "contract_path": str(parent_store.paths.contract),
            "created_at": utc_now(),
        }],
    }


def _parent_branch_version(parent_root: Path, ledger: dict[str, object]) -> str:
    provenance = parent_root / "SUCCESSOR_PROVENANCE.json"
    if provenance.exists():
        try:
            return str(read_json(provenance).get("branch_id", ""))
        except (OSError, ValueError):
            pass
    versions = ledger.get("versions", [])
    if isinstance(versions, list) and versions:
        return str(versions[0].get("branch_id") or versions[0].get("version_id", ""))
    return ""


def _record_contract_variant(
    *, parent_store: ResearchStore, ledger: dict[str, object], target_variant: str,
    request_artifact_id: str, successor_root: Path, branch_id: str, branch_name: str,
    parent_branch_id: str, git_parent_commit: str,
) -> None:
    payload = {
        "version_id": branch_id,
        "branch_id": branch_id,
        "branch_name": branch_name,
        "parent_branch_id": parent_branch_id or None,
        "kind": "SUCCESSOR_BRANCH",
        "parent_project": str(parent_store.paths.root),
        "parent_contract_sha256": content_hash(parent_store.paths.contract.read_bytes()),
        "target_variant": target_variant.strip(),
        "request_artifact_id": request_artifact_id,
        "successor_project": str(successor_root),
        "git_parent_commit": git_parent_commit or None,
        "created_at": utc_now(),
    }
    ledger.setdefault("versions", []).append(payload)
    write_json(parent_store.paths.state / "contract_lineage.json", ledger)
    # A tracked mirror gives Git history a concise branch graph without adding
    # mutable databases or large artifact blobs to commits.
    write_json(parent_store.paths.root / "ARIADNE_BRANCHES.json", ledger)


def create_contract_variant_successor(
    *, parent_root: Path, config_path: Path, target_variant: str,
    request_artifact_id: str, successor_root: Path | None = None,
) -> Path:
    """Fork a new contract branch while leaving parent state/artifacts intact."""
    parent = parent_root.resolve()
    if not target_variant.strip():
        raise ValueError("A successor contract requires a nonempty target variant")
    if not (parent / ".ariadne" / "problem_contract.json").exists():
        raise FileNotFoundError("Parent project has no immutable problem contract")
    destination = (successor_root or next_successor_path(parent)).resolve()
    if destination.exists():
        raise FileExistsError(f"Successor directory already exists: {destination}")
    config = config_path.resolve()
    if not config.exists():
        raise FileNotFoundError(f"Configuration does not exist: {config}")

    parent_store = ResearchStore(parent)
    ledger = _load_ledger(parent_store)
    parent_branch_id = _parent_branch_version(parent, ledger)
    branch_seed = {
        "parent_project": str(parent),
        "parent_branch_id": parent_branch_id,
        "target_variant": target_variant.strip(),
        "request_artifact_id": request_artifact_id,
        "successor_project": str(destination),
    }
    branch_id = short_id("BRN", branch_seed)
    branch_name = "contract-variant-" + branch_id[4:].lower()
    # A pre-existing repository is itself an affirmative versioning choice.
    # Otherwise, do not create a repository merely because a chat instruction
    # asks for a revised theorem.
    git_enabled = _git(parent, "rev-parse", "--is-inside-work-tree").returncode == 0
    git_ready, git_parent_commit = (
        _ensure_git_snapshot(parent) if git_enabled else (False, "")
    )
    if git_ready:
        created = _git(parent, "worktree", "add", "-b", branch_name, str(destination))
        if created.returncode != 0:
            raise RuntimeError("Could not create Git contract branch: " + (created.stderr.strip() or created.stdout.strip()))
        # A successor must go through setup and mint its own immutable contract.
        # This exact, newly-created directory is the only deletion target here.
        copied_state = destination / ".ariadne"
        if copied_state.exists():
            shutil.rmtree(copied_state)
    else:
        destination.mkdir(parents=True)
        shutil.copy2(config, destination / "ariadne.codex.toml")

    copied_config = destination / "ariadne.codex.toml"
    if not copied_config.exists() or copied_config.resolve() != config:
        shutil.copy2(config, copied_config)

    handoff = parent_store.paths.reports / "continuation_brief.md"
    handoff_label = str(handoff) if handoff.exists() else "(not generated yet)"
    request = f"""# Successor contract request

This is branch `{branch_name}` derived from `{parent}`. The parent campaign and
its immutable contract remain unchanged. Do not interpret this request as a
change to the parent contract.

## Proposed target variant

{target_variant.strip()}

## Parent provenance

- Parent branch: `{parent_branch_id or 'main'}`
- Variant request artifact: `{request_artifact_id}`
- Parent continuation handoff: `{handoff_label}`

During setup, formulate a fresh exact problem contract for this target. Reuse a
parent artifact only after checking that it applies to the new contract.
"""
    (destination / "SUCCESSOR_TASK.md").write_text(request, encoding="utf-8")
    index_lines = [
        "# Parent artifact index", "",
        "The files below remain in the parent project. They are not proof of the",
        "successor target and must be rechecked before reuse.", "", f"Parent: `{parent}`", "",
    ]
    for artifact in parent_store.list_artifacts(limit=500):
        index_lines.append(f"- `{artifact['artifact_id']}` [{artifact['kind']}] `{parent / artifact['relative_path']}`")
    (destination / "PARENT_ARTIFACTS.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    _record_contract_variant(
        parent_store=parent_store, ledger=ledger, target_variant=target_variant,
        request_artifact_id=request_artifact_id, successor_root=destination,
        branch_id=branch_id, branch_name=branch_name,
        parent_branch_id=parent_branch_id, git_parent_commit=git_parent_commit,
    )
    provenance = {
        "branch_id": branch_id,
        "branch_name": branch_name,
        "parent_branch_id": parent_branch_id or None,
        "git_branch": branch_name if git_ready else None,
        "git_parent_commit": git_parent_commit or None,
        "created_at": utc_now(),
        "parent_project": str(parent),
        "parent_contract": str(parent_store.paths.contract),
        "variant_request_artifact_id": request_artifact_id,
        "target_variant": target_variant.strip(),
        "parent_artifact_index": "PARENT_ARTIFACTS.md",
        "setup_task": "SUCCESSOR_TASK.md",
    }
    write_json(destination / "SUCCESSOR_PROVENANCE.json", provenance)
    if git_ready:
        # Stage the fresh-setup reset too. This matters for legacy projects that
        # had accidentally tracked `.ariadne` before Ariadne began ignoring it.
        _git(destination, "add", "-A")
        committed = _git(destination, "commit", "-m", f"Create Ariadne contract branch {branch_name}")
        if committed.returncode != 0:
            raise RuntimeError("Could not commit successor branch metadata: " + committed.stderr.strip())
        _git(parent, "add", "ARIADNE_BRANCHES.json")
        _git(parent, "commit", "-m", f"Record Ariadne contract branch {branch_name}")
    return destination
