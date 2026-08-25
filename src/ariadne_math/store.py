from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # Ariadne currently targets Unix-like research workstations.
    import fcntl
except ImportError:  # pragma: no cover - guarded for non-Unix importability.
    fcntl = None  # type: ignore[assignment]

from .artifacts import ArtifactRecord
from .enums import CampaignStatus, ClaimStatus, RouteStatus
from .events import EventLog
from .models import ProjectPaths
from .util import canonical_json, ensure_dir, short_id, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    media_type TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    epoch INTEGER NOT NULL DEFAULT 0,
    max_epochs INTEGER NOT NULL,
    max_calls INTEGER NOT NULL,
    max_cost_usd REAL NOT NULL,
    calls_used INTEGER NOT NULL DEFAULT 0,
    cost_used REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_campaign_actions (
    action_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    rationale TEXT NOT NULL,
    apply_before_epoch INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT "PENDING",
    requested_at TEXT NOT NULL,
    applied_at TEXT,
    outcome_json TEXT NOT NULL DEFAULT "{}"
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    statement TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    criticality TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_edges (
    predecessor_id TEXT NOT NULL REFERENCES claims(claim_id),
    successor_id TEXT NOT NULL REFERENCES claims(claim_id),
    edge_type TEXT NOT NULL,
    PRIMARY KEY (predecessor_id, successor_id, edge_type)
);

CREATE TABLE IF NOT EXISTS routes (
    route_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    title TEXT NOT NULL,
    target_claim_id TEXT REFERENCES claims(claim_id),
    mode TEXT NOT NULL,
    method_family TEXT NOT NULL,
    representation TEXT NOT NULL,
    key_lemma TEXT NOT NULL,
    central_mechanism TEXT NOT NULL,
    decisive_test TEXT NOT NULL,
    difference_from_existing TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    independence_cluster TEXT NOT NULL,
    owner_slot TEXT NOT NULL,
    status TEXT NOT NULL,
    epochs_without_progress INTEGER NOT NULL DEFAULT 0,
    novelty_obligation_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    route_id TEXT REFERENCES routes(route_id),
    epoch INTEGER NOT NULL,
    agent_slot TEXT NOT NULL,
    task TEXT NOT NULL,
    result_kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    artifact_id TEXT REFERENCES artifacts(artifact_id),
    decisive_event INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    usage_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failure_clusters (
    failure_id TEXT PRIMARY KEY,
    canonical_key TEXT UNIQUE NOT NULL,
    failure_class TEXT NOT NULL,
    signature TEXT NOT NULL,
    logical_scope TEXT NOT NULL,
    revival_conditions TEXT NOT NULL,
    attempts_count INTEGER NOT NULL DEFAULT 0,
    total_cost REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failure_attempts (
    failure_id TEXT NOT NULL REFERENCES failure_clusters(failure_id),
    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
    PRIMARY KEY (failure_id, attempt_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    claim_id TEXT REFERENCES claims(claim_id),
    evidence_type TEXT NOT NULL,
    logical_force TEXT NOT NULL,
    scope TEXT NOT NULL,
    artifact_id TEXT REFERENCES artifacts(artifact_id),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS literature_sources (
    source_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    citation TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    exact_statement TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    locator TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    audit_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS interventions (
    intervention_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    route_id TEXT NOT NULL REFERENCES routes(route_id),
    kind TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    message TEXT NOT NULL,
    early_stop INTEGER NOT NULL,
    applicability_json TEXT NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT NOT NULL DEFAULT '{}',
    deadline_epoch INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audits (
    audit_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    audit_type TEXT NOT NULL,
    verdict TEXT NOT NULL,
    failure_class TEXT NOT NULL,
    minimal_obligation TEXT NOT NULL,
    local_repairable INTEGER NOT NULL,
    artifact_id TEXT REFERENCES artifacts(artifact_id),
    auditor_profile TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    epoch INTEGER NOT NULL,
    kind TEXT NOT NULL,
    available_json TEXT NOT NULL,
    selected_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    expected_event TEXT NOT NULL,
    stop_condition TEXT NOT NULL,
    cost_cap REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_config_revisions (
    revision_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    revision_number INTEGER NOT NULL,
    effective_sha256 TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_path TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(campaign_id, revision_number)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    campaign_id TEXT REFERENCES campaigns(campaign_id),
    role TEXT NOT NULL,
    slot TEXT NOT NULL,
    route_id TEXT REFERENCES routes(route_id),
    epoch INTEGER,
    task_summary TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL,
    network_policy TEXT NOT NULL,
    isolation_status TEXT NOT NULL,
    status TEXT NOT NULL,
    prompt_artifact_id TEXT REFERENCES artifacts(artifact_id),
    response_artifact_id TEXT REFERENCES artifacts(artifact_id),
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    started_at TEXT NOT NULL,
    finished_at TEXT
);


CREATE TABLE IF NOT EXISTS task_queue (
    task_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    epoch INTEGER NOT NULL,
    slot TEXT NOT NULL,
    role TEXT NOT NULL,
    route_id TEXT REFERENCES routes(route_id),
    summary TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'QUEUED',
    run_id TEXT REFERENCES agent_runs(run_id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS human_reviews (
    review_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    verdict TEXT NOT NULL,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_controls (
    campaign_id TEXT PRIMARY KEY REFERENCES campaigns(campaign_id),
    pause_requested INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    requested_by TEXT NOT NULL DEFAULT '',
    requested_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_instructions (
    instruction_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    route_id TEXT REFERENCES routes(route_id),
    audience TEXT NOT NULL,
    instruction_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    retired_at TEXT
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
    reserved_cost_usd REAL NOT NULL,
    settled_cost_usd REAL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    settled_at TEXT
);

CREATE TABLE IF NOT EXISTS formalizations (
    formalization_id TEXT PRIMARY KEY,
    proof_claim_id TEXT NOT NULL REFERENCES claims(claim_id),
    status TEXT NOT NULL,
    artifact_id TEXT REFERENCES artifacts(artifact_id),
    toolchain TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class CampaignAlreadyRunning(RuntimeError):
    """Raised when another controller owns the project-level run lock."""


class ResearchStore:
    def __init__(self, root: Path):
        self.paths = ProjectPaths(root.resolve())
        ensure_dir(self.paths.root)
        ensure_dir(self.paths.state)
        ensure_dir(self.paths.artifacts)
        ensure_dir(self.paths.literature)
        ensure_dir(self.paths.reports)
        ensure_dir(self.paths.formal)
        self.events = EventLog(self.paths)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.paths.database, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize(self) -> None:
        with self.transaction() as conn:
            # Use rollback journaling so Ariadne does not create recurring
            # state.sqlite-wal/state.sqlite-shm sidecar files. Routine TUI
            # heartbeat writes are disabled; durable state transitions remain
            # protected by SQLite transactions.
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.executescript(SCHEMA)
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(agent_runs)").fetchall()
            }
            if "epoch" not in columns:
                conn.execute("ALTER TABLE agent_runs ADD COLUMN epoch INTEGER")
            if "task_summary" not in columns:
                conn.execute(
                    "ALTER TABLE agent_runs ADD COLUMN task_summary TEXT NOT NULL DEFAULT ''"
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def campaign_controller_lock(self) -> Iterator[None]:
        """Exclusively admit one campaign controller for this project.

        The lock is advisory and process-scoped: unlike a database lease it has
        no heartbeat writes and is released automatically if the controller is
        interrupted or crashes. The empty lock file is retained so repeated
        launches do not create/delete filesystem objects during a campaign.
        """
        if fcntl is None:  # pragma: no cover - supported deployments are Unix.
            raise RuntimeError(
                "Ariadne requires Unix advisory file locking to run a campaign controller."
            )
        lock_path = self.paths.state / "campaign-controller.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CampaignAlreadyRunning(
                    "Another Ariadne campaign controller is already running for this project. "
                    "Use the existing TUI/session, or stop that controller before resuming."
                ) from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def set_meta(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        self.events.append("meta_set", {"key": key, "value": value})

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def _schedule_campaign_action(
        self,
        *,
        campaign_id: str,
        kind: str,
        payload: dict[str, Any],
        requested_by: str,
        rationale: str,
    ) -> dict[str, Any]:
        campaign = self.get_campaign(campaign_id)
        if str(campaign["status"]) != CampaignStatus.RUNNING:
            raise ValueError("Only a running campaign needs next-epoch scheduling")
        now = utc_now()
        record = {
            "campaign_id": campaign_id,
            "kind": kind,
            "payload": payload,
            "requested_by": requested_by,
            "rationale": rationale,
            "requested_at": now,
        }
        action_id = short_id("ACT", record)
        apply_before_epoch = int(campaign["epoch"]) + 1
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO scheduled_campaign_actions "
                "(action_id,campaign_id,kind,payload_json,requested_by,rationale,"
                "apply_before_epoch,status,requested_at,applied_at,outcome_json) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, NULL, '{}')",
                (
                    action_id,
                    campaign_id,
                    kind,
                    canonical_json(payload),
                    requested_by,
                    rationale,
                    apply_before_epoch,
                    now,
                ),
            )
        result = {
            "action_id": action_id,
            "campaign_id": campaign_id,
            "kind": kind,
            "payload": payload,
            "requested_by": requested_by,
            "rationale": rationale,
            "apply_before_epoch": apply_before_epoch,
            "status": "PENDING",
            "requested_at": now,
        }
        self.events.append("campaign_action_scheduled", result)
        return result

    def list_scheduled_campaign_actions(
        self, campaign_id: str, *, pending_only: bool = False
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM scheduled_campaign_actions WHERE campaign_id=?"
        if pending_only:
            query += " AND status='PENDING'"
        query += " ORDER BY requested_at, action_id"
        with self.transaction() as conn:
            rows = conn.execute(query, (campaign_id,)).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["outcome"] = json.loads(item.pop("outcome_json"))
            items.append(item)
        return items

    def apply_scheduled_campaign_actions(
        self, campaign_id: str
    ) -> list[dict[str, Any]]:
        # Apply queued human controls before a new epoch is planned.
        applied: list[dict[str, Any]] = []
        with self.transaction() as conn:
            campaign_row = conn.execute(
                "SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if not campaign_row:
                raise KeyError(f"Unknown campaign {campaign_id}")
            campaign = dict(campaign_row)
            rows = conn.execute(
                "SELECT * FROM scheduled_campaign_actions "
                "WHERE campaign_id=? AND status='PENDING' ORDER BY requested_at, action_id",
                (campaign_id,),
            ).fetchall()
            for row in rows:
                action = dict(row)
                payload = json.loads(str(action["payload_json"]))
                outcome: dict[str, Any]
                try:
                    if action["kind"] == "BUDGET":
                        max_epochs = int(payload["max_epochs"])
                        max_calls = int(payload["max_calls"])
                        max_cost_usd = float(payload["max_cost_usd"])
                        if max_epochs < max(1, int(campaign["epoch"])):
                            raise ValueError("max_epochs cannot be below the completed epoch")
                        if max_calls < int(campaign["calls_used"]):
                            raise ValueError("max_calls cannot be below calls already used")
                        if max_cost_usd < float(campaign["cost_used"]):
                            raise ValueError("max_cost_usd cannot be below recorded cost already used")
                        if max_cost_usd < 0 or not math.isfinite(max_cost_usd):
                            raise ValueError("max_cost_usd must be a finite nonnegative number")
                        conn.execute(
                            "UPDATE campaigns SET max_epochs=?, max_calls=?, max_cost_usd=?, updated_at=? "
                            "WHERE campaign_id=?",
                            (max_epochs, max_calls, max_cost_usd, utc_now(), campaign_id),
                        )
                        campaign.update(
                            max_epochs=max_epochs,
                            max_calls=max_calls,
                            max_cost_usd=max_cost_usd,
                        )
                        outcome = {
                            "application": "APPLIED",
                            "max_epochs": max_epochs,
                            "max_calls": max_calls,
                            "max_cost_usd": max_cost_usd,
                        }
                    elif action["kind"] == "ROUTE_STATUS":
                        route_id = str(payload["route_id"])
                        route = conn.execute(
                            "SELECT campaign_id FROM routes WHERE route_id=?", (route_id,)
                        ).fetchone()
                        if not route:
                            raise KeyError(f"Unknown route {route_id}")
                        if str(route["campaign_id"]) != campaign_id:
                            raise ValueError(f"Route {route_id} belongs to another campaign")
                        status = str(payload["status"])
                        conn.execute(
                            "UPDATE routes SET status=?, updated_at=? WHERE route_id=?",
                            (status, utc_now(), route_id),
                        )
                        outcome = {
                            "application": "APPLIED",
                            "route_id": route_id,
                            "status": status,
                        }
                    else:
                        raise ValueError(f"Unknown scheduled action kind {action['kind']}")
                    action_status = "APPLIED"
                except (KeyError, TypeError, ValueError) as exc:
                    action_status = "REJECTED"
                    outcome = {"application": "REJECTED", "error": str(exc)}
                conn.execute(
                    "UPDATE scheduled_campaign_actions SET status=?, applied_at=?, outcome_json=? "
                    "WHERE action_id=?",
                    (action_status, utc_now(), canonical_json(outcome), action["action_id"]),
                )
                applied.append(
                    {
                        "action_id": str(action["action_id"]),
                        "campaign_id": campaign_id,
                        "kind": str(action["kind"]),
                        "status": action_status,
                        "payload": payload,
                        "outcome": outcome,
                        "apply_before_epoch": int(action["apply_before_epoch"]),
                    }
                )
        for action in applied:
            self.events.append("campaign_action_applied", action)
        return applied

    def record_artifact(
        self, artifact: ArtifactRecord, metadata: dict[str, Any] | None = None
    ) -> None:
        relative = str(artifact.path.relative_to(self.paths.root))
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO artifacts VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.artifact_id,
                    artifact.kind,
                    artifact.media_type,
                    relative,
                    artifact.size,
                    canonical_json(metadata if metadata is not None else artifact.metadata),
                    utc_now(),
                ),
            )

    def create_campaign(
        self,
        *,
        mode: str,
        max_epochs: int,
        max_calls: int,
        max_cost_usd: float,
    ) -> str:
        payload = {
            "root": str(self.paths.root),
            "mode": mode,
            "created_at": utc_now(),
        }
        campaign_id = short_id("CAM", payload)
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO campaigns VALUES(?, ?, ?, 0, ?, ?, ?, 0, 0.0, ?, ?)",
                (
                    campaign_id,
                    mode,
                    CampaignStatus.CREATED,
                    max_epochs,
                    max_calls,
                    max_cost_usd,
                    now,
                    now,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO campaign_controls "
                "(campaign_id,pause_requested,reason,requested_by,requested_at,updated_at) "
                "VALUES(?, 0, '', '', NULL, ?)",
                (campaign_id, now),
            )
        self.events.append("campaign_created", {"campaign_id": campaign_id, **payload})
        return campaign_id

    def latest_campaign(self) -> dict[str, Any] | None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Unknown campaign {campaign_id}")
        return dict(row)

    def update_campaign(
        self,
        campaign_id: str,
        *,
        status: str | None = None,
        epoch: int | None = None,
    ) -> None:
        fields: list[str] = ["updated_at=?"]
        values: list[Any] = [utc_now()]
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if epoch is not None:
            fields.append("epoch=?")
            values.append(epoch)
        values.append(campaign_id)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE campaigns SET {', '.join(fields)} WHERE campaign_id=?",
                values,
            )
        self.events.append(
            "campaign_updated",
            {"campaign_id": campaign_id, "status": status, "epoch": epoch},
        )

    def adjust_campaign_budget(
        self,
        campaign_id: str,
        *,
        max_epochs: int | None = None,
        max_calls: int | None = None,
        max_cost_usd: float | None = None,
        adjusted_by: str,
        reason: str,
    ) -> dict[str, Any]:
        # A running campaign records an auditable request and applies it only
        # at the next epoch boundary. Paused campaigns change immediately.
        if not str(reason).strip():
            raise ValueError("Budget adjustment reason must not be empty")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Unknown campaign {campaign_id}")
            campaign = dict(row)
            prior_status = str(campaign["status"])
            if prior_status not in {
                CampaignStatus.RUNNING,
                CampaignStatus.PAUSED_HUMAN,
                CampaignStatus.BUDGET_EXHAUSTED,
                CampaignStatus.COMPLETED_UNSOLVED,
            }:
                raise ValueError(
                    "Budget changes require RUNNING, PAUSED_HUMAN, BUDGET_EXHAUSTED, or COMPLETED_UNSOLVED"
                )
            active_runs = conn.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE campaign_id=? AND status='RUNNING'",
                (campaign_id,),
            ).fetchone()[0]
            queued_tasks = conn.execute(
                "SELECT COUNT(*) FROM task_queue WHERE campaign_id=? AND status IN ('QUEUED','RUNNING')",
                (campaign_id,),
            ).fetchone()[0]
            if (active_runs or queued_tasks) and prior_status != CampaignStatus.RUNNING:
                raise ValueError(
                    "Budget changes require no active or queued work outside a running campaign; use campaign recover for stale work"
                )
            new_epochs = int(campaign["max_epochs"]) if max_epochs is None else int(max_epochs)
            new_calls = int(campaign["max_calls"]) if max_calls is None else int(max_calls)
            new_cost = float(campaign["max_cost_usd"]) if max_cost_usd is None else float(max_cost_usd)
            if new_epochs < max(1, int(campaign["epoch"])):
                raise ValueError("max_epochs cannot be below the current epoch")
            if new_calls < int(campaign["calls_used"]):
                raise ValueError("max_calls cannot be below calls already used")
            if new_cost < float(campaign["cost_used"]):
                raise ValueError("max_cost_usd cannot be below recorded cost already used")
            if new_cost < 0 or not math.isfinite(new_cost):
                raise ValueError("max_cost_usd must be a finite nonnegative number")
            if (
                prior_status == CampaignStatus.COMPLETED_UNSOLVED
                and new_epochs <= int(campaign["epoch"])
            ):
                raise ValueError(
                    "Reopening a completed unsolved campaign requires max_epochs above the completed epoch"
                )
            previous = {
                "max_epochs": int(campaign["max_epochs"]),
                "max_calls": int(campaign["max_calls"]),
                "max_cost_usd": float(campaign["max_cost_usd"]),
            }
            scheduled = prior_status == CampaignStatus.RUNNING
            if not scheduled:
                now = utc_now()
                conn.execute(
                    "UPDATE campaigns SET max_epochs=?, max_calls=?, max_cost_usd=?, status=?, updated_at=? "
                    "WHERE campaign_id=?",
                    (
                        new_epochs,
                        new_calls,
                        new_cost,
                        CampaignStatus.PAUSED_HUMAN
                        if prior_status in {
                            CampaignStatus.BUDGET_EXHAUSTED,
                            CampaignStatus.COMPLETED_UNSOLVED,
                        }
                        else prior_status,
                        now,
                        campaign_id,
                    ),
                )

        payload = {
            "max_epochs": new_epochs,
            "max_calls": new_calls,
            "max_cost_usd": new_cost,
        }
        action = None
        if scheduled:
            action = self._schedule_campaign_action(
                campaign_id=campaign_id,
                kind="BUDGET",
                payload=payload,
                requested_by=adjusted_by,
                rationale=reason.strip(),
            )
        selected = {
            **payload,
            "adjusted_by": adjusted_by,
            "prior_status": prior_status,
            "status": prior_status if scheduled else (
                CampaignStatus.PAUSED_HUMAN
                if prior_status in {
                    CampaignStatus.BUDGET_EXHAUSTED,
                    CampaignStatus.COMPLETED_UNSOLVED,
                }
                else prior_status
            ),
            "application": "NEXT_EPOCH" if scheduled else "IMMEDIATE",
            "scheduled_action_id": action["action_id"] if action else None,
        }
        self.add_decision(
            campaign_id=campaign_id,
            epoch=int(campaign["epoch"]),
            kind="HUMAN_BUDGET_ADJUSTMENT",
            available={
                "previous_limits": previous,
                "usage": {
                    "epoch": int(campaign["epoch"]),
                    "calls_used": int(campaign["calls_used"]),
                    "cost_used": float(campaign["cost_used"]),
                },
            },
            selected=selected,
            rationale=reason.strip(),
            expected_event=(
                "Revised limits are applied before the next epoch begins"
                if scheduled
                else (
                    "Campaign is reopened as PAUSED_HUMAN and future bounded work follows the revised limits"
                    if prior_status
                    in {
                        CampaignStatus.BUDGET_EXHAUSTED,
                        CampaignStatus.COMPLETED_UNSOLVED,
                    }
                    else "Future bounded work follows the revised campaign limits"
                )
            ),
            stop_condition="The campaign reaches a revised budget limit or another human budget adjustment is recorded",
            cost_cap=0.0,
        )
        self.events.append(
            "campaign_budget_scheduled" if scheduled else "campaign_budget_adjusted",
            {
                "campaign_id": campaign_id,
                "previous_limits": previous,
                "new_limits": selected,
                "reason": reason.strip(),
            },
        )
        result = self.get_campaign(campaign_id)
        if action is not None:
            result["scheduled_action"] = action
            result["requested_limits"] = payload
        return result

    def reserve_budget(
        self,
        campaign_id: str,
        *,
        reservation_id: str,
        estimated_cost_usd: float,
    ) -> bool:
        """Atomically reserve one provider call and its conservative cost estimate."""
        if estimated_cost_usd < 0:
            raise ValueError("estimated_cost_usd must be nonnegative")
        now = utc_now()
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE campaigns SET calls_used=calls_used+1, cost_used=cost_used+?, "
                "updated_at=? WHERE campaign_id=? AND calls_used < max_calls "
                "AND cost_used+? <= max_cost_usd",
                (estimated_cost_usd, now, campaign_id, estimated_cost_usd),
            )
            if cursor.rowcount != 1:
                return False
            conn.execute(
                "INSERT INTO budget_reservations "
                "(reservation_id,campaign_id,reserved_cost_usd,status,created_at) "
                "VALUES(?, ?, ?, 'RESERVED', ?)",
                (reservation_id, campaign_id, estimated_cost_usd, now),
            )
        self.events.append(
            "budget_reserved",
            {
                "campaign_id": campaign_id,
                "reservation_id": reservation_id,
                "estimated_cost_usd": estimated_cost_usd,
            },
        )
        return True

    def settle_budget(
        self, reservation_id: str, *, settled_cost_usd: float
    ) -> None:
        """Replace a reservation estimate with a reported or fallback final cost."""
        if settled_cost_usd < 0:
            raise ValueError("settled_cost_usd must be nonnegative")
        now = utc_now()
        with self.transaction() as conn:
            reservation = conn.execute(
                "SELECT campaign_id,reserved_cost_usd,status FROM budget_reservations "
                "WHERE reservation_id=?",
                (reservation_id,),
            ).fetchone()
            if not reservation:
                raise KeyError(f"Unknown budget reservation {reservation_id}")
            if str(reservation["status"]) != "RESERVED":
                return
            conn.execute(
                "UPDATE campaigns SET cost_used=cost_used-?+?, updated_at=? "
                "WHERE campaign_id=?",
                (
                    float(reservation["reserved_cost_usd"]),
                    settled_cost_usd,
                    now,
                    str(reservation["campaign_id"]),
                ),
            )
            conn.execute(
                "UPDATE budget_reservations SET settled_cost_usd=?, status='SETTLED', "
                "settled_at=? WHERE reservation_id=?",
                (settled_cost_usd, now, reservation_id),
            )
        self.events.append(
            "budget_settled",
            {"reservation_id": reservation_id, "settled_cost_usd": settled_cost_usd},
        )

    def budget_available(self, campaign_id: str, next_cost: float = 0.0) -> bool:
        campaign = self.get_campaign(campaign_id)
        return (
            int(campaign["calls_used"]) < int(campaign["max_calls"])
            and float(campaign["cost_used"]) + next_cost
            <= float(campaign["max_cost_usd"])
        )

    def add_claim(
        self,
        *,
        statement: str,
        assumptions: list[str] | None = None,
        scope: str = "general",
        status: str = ClaimStatus.PROPOSED,
        criticality: str = "supporting",
        source: str = "agent",
    ) -> str:
        payload = {
            "statement": statement,
            "assumptions": assumptions or [],
            "scope": scope,
            "source": source,
        }
        claim_id = short_id("CLM", payload)
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO claims VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    claim_id,
                    statement,
                    canonical_json(assumptions or []),
                    scope,
                    status,
                    criticality,
                    source,
                    now,
                    now,
                ),
            )
        self.events.append("claim_added", {"claim_id": claim_id, **payload, "status": status})
        return claim_id

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Unknown claim {claim_id}")
        item = dict(row)
        item["assumptions"] = json.loads(item.pop("assumptions_json"))
        return item

    def list_claims(self) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute("SELECT * FROM claims ORDER BY created_at").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["assumptions"] = json.loads(item.pop("assumptions_json"))
            result.append(item)
        return result

    def transition_claim(self, claim_id: str, status: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE claims SET status=?, updated_at=? WHERE claim_id=?",
                (status, utc_now(), claim_id),
            )
        self.events.append("claim_transition", {"claim_id": claim_id, "status": status})

    def add_claim_edge(self, predecessor: str, successor: str, edge_type: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO claim_edges VALUES(?, ?, ?)",
                (predecessor, successor, edge_type),
            )

    def add_route(
        self,
        *,
        campaign_id: str,
        title: str,
        target_claim_id: str | None,
        mode: str,
        method_family: str,
        representation: str,
        key_lemma: str,
        central_mechanism: str,
        decisive_test: str,
        difference_from_existing: str,
        fingerprint: str,
        independence_cluster: str,
        owner_slot: str,
        status: str = RouteStatus.ACTIVE,
    ) -> str:
        payload = {
            "campaign_id": campaign_id,
            "title": title,
            "mode": mode,
            "method_family": method_family,
            "representation": representation,
            "key_lemma": key_lemma,
            "owner_slot": owner_slot,
        }
        route_id = short_id("RTE", payload)
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO routes VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '{}', ?, ?)",
                (
                    route_id,
                    campaign_id,
                    title,
                    target_claim_id,
                    mode,
                    method_family,
                    representation,
                    key_lemma,
                    central_mechanism,
                    decisive_test,
                    difference_from_existing,
                    fingerprint,
                    independence_cluster,
                    owner_slot,
                    status,
                    now,
                    now,
                ),
            )
        self.events.append("route_added", {"route_id": route_id, **payload})
        return route_id

    def get_route(self, route_id: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM routes WHERE route_id=?", (route_id,)).fetchone()
        if not row:
            raise KeyError(f"Unknown route {route_id}")
        item = dict(row)
        item["novelty_obligation"] = json.loads(item.pop("novelty_obligation_json"))
        return item

    def list_routes(
        self, campaign_id: str | None = None, *, active_only: bool = False
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM routes"
        params: list[Any] = []
        clauses: list[str] = []
        if campaign_id:
            clauses.append("campaign_id=?")
            params.append(campaign_id)
        if active_only:
            clauses.append("status=?")
            params.append(RouteStatus.ACTIVE)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        with self.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["novelty_obligation"] = json.loads(item.pop("novelty_obligation_json"))
            result.append(item)
        return result

    def update_route(
        self,
        route_id: str,
        *,
        status: str | None = None,
        epochs_without_progress: int | None = None,
        novelty_obligation: dict[str, Any] | None = None,
    ) -> None:
        fields: list[str] = ["updated_at=?"]
        values: list[Any] = [utc_now()]
        if status is not None:
            fields.append("status=?")
            values.append(status)
        if epochs_without_progress is not None:
            fields.append("epochs_without_progress=?")
            values.append(epochs_without_progress)
        if novelty_obligation is not None:
            fields.append("novelty_obligation_json=?")
            values.append(canonical_json(novelty_obligation))
        values.append(route_id)
        with self.transaction() as conn:
            conn.execute(
                f"UPDATE routes SET {', '.join(fields)} WHERE route_id=?", values
            )
        self.events.append(
            "route_updated",
            {
                "route_id": route_id,
                "status": status,
                "epochs_without_progress": epochs_without_progress,
                "novelty_obligation": novelty_obligation,
            },
        )

    def set_human_route_status(
        self,
        *,
        campaign_id: str,
        route_id: str,
        status: str,
        requested_by: str,
        rationale: str,
    ) -> dict[str, Any]:
        # Queue route controls during a live epoch so they cannot race with
        # work that was already admitted for that epoch.
        route = self.get_route(route_id)
        if str(route["campaign_id"]) != campaign_id:
            raise ValueError(
                f"Route {route_id} belongs to {route['campaign_id']}, not {campaign_id}"
            )
        allowed = {
            RouteStatus.ACTIVE,
            RouteStatus.PARKED,
            RouteStatus.OBSOLETE,
            RouteStatus.NEEDS_HUMAN_IDEA,
            RouteStatus.NEEDS_REPRESENTATION_CHANGE,
        }
        if status not in allowed:
            raise ValueError(f"Unknown route status {status!r}")
        campaign = self.get_campaign(campaign_id)
        scheduled = str(campaign["status"]) == CampaignStatus.RUNNING
        action = None
        if scheduled:
            action = self._schedule_campaign_action(
                campaign_id=campaign_id,
                kind="ROUTE_STATUS",
                payload={"route_id": route_id, "status": status},
                requested_by=requested_by,
                rationale=rationale,
            )
        else:
            self.update_route(route_id, status=status)
        selected = {
            "route_id": route_id,
            "status": status,
            "by": requested_by,
            "application": "NEXT_EPOCH" if scheduled else "IMMEDIATE",
            "scheduled_action_id": action["action_id"] if action else None,
        }
        self.add_decision(
            campaign_id=campaign_id,
            epoch=int(campaign["epoch"]),
            kind="HUMAN_ROUTE_STATUS",
            available={"route": route},
            selected=selected,
            rationale=rationale,
            expected_event=(
                "Route status is applied before the next epoch begins"
                if scheduled
                else (
                    "Further bounded work under explicit human direction"
                    if status == RouteStatus.ACTIVE
                    else "No new work on this route until another explicit decision"
                )
            ),
            stop_condition="Human changes the route status or the route reaches a terminal mathematical event",
            cost_cap=0.0,
        )
        self.events.append(
            "route_status_scheduled" if scheduled else "route_status_set",
            {
                "campaign_id": campaign_id,
                "route_id": route_id,
                "status": status,
                "requested_by": requested_by,
                "rationale": rationale,
                "application": selected["application"],
                "scheduled_action_id": selected["scheduled_action_id"],
            },
        )
        result = {
            "route": self.get_route(route_id),
            "application": selected["application"],
            "scheduled_action": action,
        }
        return result

    def add_attempt(
        self,
        *,
        campaign_id: str,
        route_id: str | None,
        epoch: int,
        agent_slot: str,
        task: str,
        result_kind: str,
        summary: str,
        artifact_id: str | None,
        decisive_event: bool,
        cost_usd: float,
        usage: dict[str, Any],
    ) -> str:
        payload = {
            "campaign_id": campaign_id,
            "route_id": route_id,
            "epoch": epoch,
            "agent_slot": agent_slot,
            "task": task,
            "summary": summary,
            "created_at": utc_now(),
        }
        attempt_id = short_id("ATT", payload)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO attempts VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    campaign_id,
                    route_id,
                    epoch,
                    agent_slot,
                    task,
                    result_kind,
                    summary,
                    artifact_id,
                    int(decisive_event),
                    cost_usd,
                    canonical_json(usage),
                    payload["created_at"],
                ),
            )
        self.events.append("attempt_added", {"attempt_id": attempt_id, **payload})
        return attempt_id

    def list_attempts(self, route_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM attempts"
        params: tuple[Any, ...] = ()
        if route_id:
            query += " WHERE route_id=?"
            params = (route_id,)
        query += " ORDER BY created_at"
        with self.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def upsert_failure(
        self,
        *,
        canonical_key: str,
        failure_class: str,
        signature: str,
        logical_scope: str,
        revival_conditions: str,
        attempt_id: str,
        cost_usd: float,
    ) -> tuple[str, int]:
        now = utc_now()
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT failure_id, attempts_count FROM failure_clusters WHERE canonical_key=?",
                (canonical_key,),
            ).fetchone()
            if row:
                failure_id = str(row["failure_id"])
                conn.execute(
                    "UPDATE failure_clusters SET attempts_count=attempts_count+1, "
                    "total_cost=total_cost+?, updated_at=? WHERE failure_id=?",
                    (cost_usd, now, failure_id),
                )
                count = int(row["attempts_count"]) + 1
            else:
                failure_id = short_id("FAIL", {"canonical_key": canonical_key})
                conn.execute(
                    "INSERT INTO failure_clusters VALUES(?, ?, ?, ?, ?, ?, 1, ?, 'ACTIVE', ?, ?)",
                    (
                        failure_id,
                        canonical_key,
                        failure_class,
                        signature,
                        logical_scope,
                        revival_conditions,
                        cost_usd,
                        now,
                        now,
                    ),
                )
                count = 1
            conn.execute(
                "INSERT OR IGNORE INTO failure_attempts VALUES(?, ?)",
                (failure_id, attempt_id),
            )
        self.events.append(
            "failure_cluster_updated",
            {
                "failure_id": failure_id,
                "failure_class": failure_class,
                "canonical_key": canonical_key,
                "attempts_count": count,
            },
        )
        return failure_id, count

    def list_failures(self) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM failure_clusters ORDER BY attempts_count DESC, created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def add_evidence(
        self,
        *,
        claim_id: str | None,
        evidence_type: str,
        logical_force: str,
        scope: str,
        artifact_id: str | None,
        status: str = "RECORDED",
    ) -> str:
        payload = {
            "claim_id": claim_id,
            "evidence_type": evidence_type,
            "logical_force": logical_force,
            "scope": scope,
            "artifact_id": artifact_id,
        }
        evidence_id = short_id("EVD", payload)
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO evidence VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence_id,
                    claim_id,
                    evidence_type,
                    logical_force,
                    scope,
                    artifact_id,
                    status,
                    utc_now(),
                ),
            )
        self.events.append("evidence_added", {"evidence_id": evidence_id, **payload})
        return evidence_id

    def list_evidence(self, claim_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM evidence"
        params: tuple[Any, ...] = ()
        if claim_id:
            query += " WHERE claim_id=?"
            params = (claim_id,)
        query += " ORDER BY created_at"
        with self.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def add_literature_source(
        self,
        *,
        title: str,
        citation: str,
        source_kind: str,
        exact_statement: str,
        assumptions: list[str],
        locator: str,
        relative_path: str,
        audit_status: str = "UNREVIEWED",
    ) -> str:
        payload = {
            "title": title,
            "citation": citation,
            "locator": locator,
            "exact_statement": exact_statement,
        }
        source_id = short_id("SRC", payload)
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO literature_sources VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    title,
                    citation,
                    source_kind,
                    exact_statement,
                    canonical_json(assumptions),
                    locator,
                    relative_path,
                    audit_status,
                    utc_now(),
                ),
            )
        self.events.append("literature_source_added", {"source_id": source_id, **payload})
        return source_id

    def list_literature_sources(self) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM literature_sources ORDER BY created_at"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["assumptions"] = json.loads(item.pop("assumptions_json"))
            result.append(item)
        return result

    def select_literature_sources(
        self, *, query: str, limit: int = 12
    ) -> list[dict[str, Any]]:
        # Preserve complete access for small dossiers. Larger dossiers are
        # ranked per route, rather than blindly sharing the newest sources.
        sources = self.list_literature_sources()
        if limit <= 0:
            return []
        if len(sources) <= limit:
            return sources
        terms = {
            token
            for token in re.findall(r"[a-z0-9]{3,}", query.casefold())
            if token not in {"the", "and", "for", "with", "from", "that", "this"}
        }

        def score(source: dict[str, Any]) -> int:
            haystack = " ".join(
                [
                    str(source.get("title", "")),
                    str(source.get("citation", "")),
                    str(source.get("exact_statement", "")),
                    " ".join(str(item) for item in source.get("assumptions", [])),
                    str(source.get("locator", "")),
                ]
            ).casefold()
            counts = {
                token: len(re.findall(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", haystack))
                for token in terms
            }
            return sum(counts.values()) + 3 * sum(value > 0 for value in counts.values())

        ranked = sorted(
            enumerate(sources),
            key=lambda pair: (score(pair[1]), pair[0]),
            reverse=True,
        )
        return [source for _, source in ranked[:limit]]

    def add_intervention(
        self,
        *,
        campaign_id: str,
        route_id: str,
        kind: str,
        source_refs: list[str],
        message: str,
        early_stop: bool,
        applicability: list[str],
        deadline_epoch: int | None,
    ) -> str:
        payload = {
            "campaign_id": campaign_id,
            "route_id": route_id,
            "kind": kind,
            "source_refs": source_refs,
            "message": message,
            "early_stop": early_stop,
            "created_at": utc_now(),
        }
        intervention_id = short_id("INT", payload)
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO interventions VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'PROPOSED', '{}', ?, ?, ?)",
                (
                    intervention_id,
                    campaign_id,
                    route_id,
                    kind,
                    canonical_json(source_refs),
                    message,
                    int(early_stop),
                    canonical_json(applicability),
                    deadline_epoch,
                    payload["created_at"],
                    payload["created_at"],
                ),
            )
        self.events.append("intervention_added", {"intervention_id": intervention_id, **payload})
        return intervention_id

    def update_intervention(
        self,
        intervention_id: str,
        *,
        status: str,
        response: dict[str, Any] | None = None,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE interventions SET status=?, response_json=?, updated_at=? "
                "WHERE intervention_id=?",
                (status, canonical_json(response or {}), utc_now(), intervention_id),
            )
        self.events.append(
            "intervention_updated",
            {"intervention_id": intervention_id, "status": status, "response": response or {}},
        )

    def list_interventions(
        self, campaign_id: str | None = None, route_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM interventions"
        clauses: list[str] = []
        params: list[Any] = []
        if campaign_id:
            clauses.append("campaign_id=?")
            params.append(campaign_id)
        if route_id:
            clauses.append("route_id=?")
            params.append(route_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        with self.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["source_refs"] = json.loads(item.pop("source_refs_json"))
            item["applicability"] = json.loads(item.pop("applicability_json"))
            item["response"] = json.loads(item.pop("response_json"))
            result.append(item)
        return result

    def add_decision(
        self,
        *,
        campaign_id: str,
        epoch: int,
        kind: str,
        available: Any,
        selected: Any,
        rationale: str,
        expected_event: str,
        stop_condition: str,
        cost_cap: float,
    ) -> str:
        payload = {
            "campaign_id": campaign_id,
            "epoch": epoch,
            "kind": kind,
            "selected": selected,
            "created_at": utc_now(),
        }
        decision_id = short_id("DEC", payload)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO decisions VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    campaign_id,
                    epoch,
                    kind,
                    canonical_json(available),
                    canonical_json(selected),
                    rationale,
                    expected_event,
                    stop_condition,
                    cost_cap,
                    payload["created_at"],
                ),
            )
        self.events.append("decision_added", {"decision_id": decision_id, **payload})
        return decision_id

    def list_decisions(self, campaign_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM decisions"
        params: tuple[Any, ...] = ()
        if campaign_id is not None:
            query += " WHERE campaign_id=?"
            params = (campaign_id,)
        query += " ORDER BY created_at"
        with self.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["available"] = json.loads(item.pop("available_json"))
            item["selected"] = json.loads(item.pop("selected_json"))
            items.append(item)
        return items

    @staticmethod
    def _configuration_diff(before: Any, after: Any, prefix: str = "") -> dict[str, dict[str, Any]]:
        if isinstance(before, dict) and isinstance(after, dict):
            result: dict[str, dict[str, Any]] = {}
            for key in sorted(set(before) | set(after)):
                key_prefix = f"{prefix}.{key}" if prefix else str(key)
                result.update(ResearchStore._configuration_diff(
                    before.get(key), after.get(key), key_prefix
                ))
            return result
        if before != after:
            return {prefix: {"before": before, "after": after}}
        return {}

    def record_campaign_config_revision(
        self,
        campaign_id: str,
        *,
        snapshot: dict[str, Any],
        effective_sha256: str,
        source_sha256: str,
        source_path: str,
        reason: str,
        author: str,
    ) -> dict[str, Any]:
        """Persist a redacted operational configuration snapshot when it changes."""
        with self.transaction() as conn:
            campaign = conn.execute(
                "SELECT epoch FROM campaigns WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
            if not campaign:
                raise KeyError(f"Unknown campaign {campaign_id}")
            previous = conn.execute(
                "SELECT * FROM campaign_config_revisions WHERE campaign_id=? "
                "ORDER BY revision_number DESC LIMIT 1",
                (campaign_id,),
            ).fetchone()
            if previous:
                previous_item = dict(previous)
                previous_snapshot = json.loads(previous_item["snapshot_json"])
                if (
                    previous_item["effective_sha256"] == effective_sha256
                    and previous_item["source_sha256"] == source_sha256
                ):
                    return {"recorded": False, "revision": previous_item, "changes": {}}
                revision_number = int(previous_item["revision_number"]) + 1
                changes = self._configuration_diff(previous_snapshot, snapshot)
            else:
                previous_snapshot = None
                revision_number = 1
                changes = {}
            payload = {
                "campaign_id": campaign_id,
                "revision_number": revision_number,
                "effective_sha256": effective_sha256,
                "source_sha256": source_sha256,
                "created_at": utc_now(),
            }
            revision_id = short_id("CFG", payload)
            conn.execute(
                "INSERT INTO campaign_config_revisions "
                "(revision_id,campaign_id,revision_number,effective_sha256,source_sha256,source_path,"
                "snapshot_json,changes_json,reason,author,created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    revision_id, campaign_id, revision_number, effective_sha256,
                    source_sha256, source_path, canonical_json(snapshot),
                    canonical_json(changes), reason, author, payload["created_at"],
                ),
            )
        revision = {
            "revision_id": revision_id,
            "campaign_id": campaign_id,
            "revision_number": revision_number,
            "effective_sha256": effective_sha256,
            "source_sha256": source_sha256,
            "source_path": source_path,
            "snapshot": snapshot,
            "changes": changes,
            "reason": reason,
            "author": author,
            "created_at": payload["created_at"],
        }
        self.events.append(
            "campaign_config_revision_recorded",
            {"revision_id": revision_id, "campaign_id": campaign_id,
             "revision_number": revision_number, "changes": changes},
        )
        if previous_snapshot is not None:
            self.add_decision(
                campaign_id=campaign_id,
                epoch=int(campaign["epoch"]),
                kind="HUMAN_CONFIGURATION_REVISION",
                available={"previous_snapshot": previous_snapshot},
                selected={"revision_id": revision_id, "snapshot": snapshot, "changes": changes},
                rationale=reason,
                expected_event="Future bounded calls use the revised operational configuration",
                stop_condition="The operator records another configuration revision or the campaign ends",
                cost_cap=0.0,
            )
        return {"recorded": True, "revision": revision, "changes": changes}

    def list_campaign_config_revisions(self, campaign_id: str) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM campaign_config_revisions WHERE campaign_id=? "
                "ORDER BY revision_number", (campaign_id,)
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json"))
            item["changes"] = json.loads(item.pop("changes_json"))
            result.append(item)
        return result

    def start_agent_run(
        self,
        *,
        campaign_id: str | None,
        role: str,
        slot: str,
        route_id: str | None,
        epoch: int | None,
        task_summary: str,
        provider: str,
        network_policy: str,
        isolation_status: str,
        prompt_artifact_id: str,
    ) -> str:
        payload = {
            "campaign_id": campaign_id,
            "role": role,
            "slot": slot,
            "route_id": route_id,
            "epoch": epoch,
            "task_summary": task_summary,
            "started_at": utc_now(),
        }
        run_id = short_id("RUN", payload)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO agent_runs(run_id,campaign_id,role,slot,route_id,epoch,task_summary,provider,network_policy,isolation_status,status,prompt_artifact_id,started_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, ?)",
                (
                    run_id,
                    campaign_id,
                    role,
                    slot,
                    route_id,
                    epoch,
                    task_summary,
                    provider,
                    network_policy,
                    isolation_status,
                    prompt_artifact_id,
                    payload["started_at"],
                ),
            )
        return run_id

    def finish_agent_run(
        self,
        run_id: str,
        *,
        status: str,
        response_artifact_id: str | None,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE agent_runs SET status=?, response_artifact_id=?, input_tokens=?, "
                "output_tokens=?, cost_usd=?, finished_at=? WHERE run_id=?",
                (
                    status,
                    response_artifact_id,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    utc_now(),
                    run_id,
                ),
            )

    def list_agent_runs(
        self,
        campaign_id: str | None = None,
        *,
        active_only: bool = False,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM agent_runs"
        clauses: list[str] = []
        params: list[Any] = []
        if campaign_id:
            clauses.append("campaign_id=?")
            params.append(campaign_id)
        if active_only:
            clauses.append("status='RUNNING'")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at"
        with self.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def add_task(
        self,
        *,
        campaign_id: str,
        epoch: int,
        slot: str,
        role: str,
        route_id: str | None,
        summary: str,
        priority: int = 100,
    ) -> str:
        payload = {
            "campaign_id": campaign_id,
            "epoch": epoch,
            "slot": slot,
            "role": role,
            "route_id": route_id,
            "summary": summary,
            "created_at": utc_now(),
        }
        task_id = short_id("TSK", payload)
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO task_queue "
                "(task_id,campaign_id,epoch,slot,role,route_id,summary,priority,status,created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?)",
                (
                    task_id, campaign_id, epoch, slot, role, route_id, summary,
                    int(priority), payload["created_at"],
                ),
            )
        self.events.append("task_queued", {"task_id": task_id, **payload})
        return task_id

    def start_task(self, task_id: str, *, run_id: str | None = None) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE task_queue SET status='RUNNING', run_id=?, started_at=? WHERE task_id=?",
                (run_id, utc_now(), task_id),
            )
        self.events.append("task_started", {"task_id": task_id, "run_id": run_id})

    def finish_task(self, task_id: str, *, status: str) -> None:
        if status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise ValueError(f"Invalid task terminal status {status!r}")
        with self.transaction() as conn:
            conn.execute(
                "UPDATE task_queue SET status=?, finished_at=? WHERE task_id=?",
                (status, utc_now(), task_id),
            )
        self.events.append("task_finished", {"task_id": task_id, "status": status})

    def list_tasks(
        self,
        campaign_id: str | None = None,
        *,
        statuses: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM task_queue"
        clauses: list[str] = []
        params: list[Any] = []
        if campaign_id:
            clauses.append("campaign_id=?")
            params.append(campaign_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY CASE status WHEN 'RUNNING' THEN 0 WHEN 'QUEUED' THEN 1 ELSE 2 END, priority, created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def list_artifacts(
        self, *, kind: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM artifacts"
        params: list[Any] = []
        if kind is not None:
            query += " WHERE kind=?"
            params.append(kind)
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item.pop("metadata_json"))
            items.append(item)
        return items

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"Unknown artifact {artifact_id}")
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        return item

    def add_audit(
        self,
        *,
        target_type: str,
        target_id: str,
        audit_type: str,
        verdict: str,
        failure_class: str,
        minimal_obligation: str,
        local_repairable: bool,
        artifact_id: str | None,
        auditor_profile: str,
    ) -> str:
        payload = {
            "target_type": target_type,
            "target_id": target_id,
            "audit_type": audit_type,
            "verdict": verdict,
            "created_at": utc_now(),
        }
        audit_id = short_id("AUD", payload)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO audits VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    audit_id,
                    target_type,
                    target_id,
                    audit_type,
                    verdict,
                    failure_class,
                    minimal_obligation,
                    int(local_repairable),
                    artifact_id,
                    auditor_profile,
                    payload["created_at"],
                ),
            )
        self.events.append("audit_added", {"audit_id": audit_id, **payload})
        return audit_id

    def list_audits(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM audits"
        clauses: list[str] = []
        params: list[Any] = []
        if target_type is not None:
            clauses.append("target_type=?")
            params.append(target_type)
        if target_id is not None:
            clauses.append("target_id=?")
            params.append(target_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        with self.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def add_human_review(
        self,
        *,
        target_type: str,
        target_id: str,
        reviewer: str,
        verdict: str,
        notes: str,
    ) -> str:
        payload = {
            "target_type": target_type,
            "target_id": target_id,
            "reviewer": reviewer,
            "verdict": verdict,
            "created_at": utc_now(),
        }
        review_id = short_id("HUM", payload)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO human_reviews VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    review_id,
                    target_type,
                    target_id,
                    reviewer,
                    verdict,
                    notes,
                    payload["created_at"],
                ),
            )
        self.events.append("human_review_added", {"review_id": review_id, **payload})
        return review_id

    def has_passing_human_review(self, target_type: str, target_id: str) -> bool:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM human_reviews WHERE target_type=? AND target_id=? "
                "AND verdict='PASS' LIMIT 1",
                (target_type, target_id),
            ).fetchone()
        return bool(row)

    def request_campaign_pause(
        self,
        campaign_id: str,
        *,
        reason: str = "",
        requested_by: str = "operator",
    ) -> None:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO campaign_controls "
                "(campaign_id,pause_requested,reason,requested_by,requested_at,updated_at) "
                "VALUES(?, 1, ?, ?, ?, ?) "
                "ON CONFLICT(campaign_id) DO UPDATE SET "
                "pause_requested=1, reason=excluded.reason, "
                "requested_by=excluded.requested_by, requested_at=excluded.requested_at, "
                "updated_at=excluded.updated_at",
                (campaign_id, reason, requested_by, now, now),
            )
        self.events.append(
            "campaign_pause_requested",
            {
                "campaign_id": campaign_id,
                "reason": reason,
                "requested_by": requested_by,
            },
        )

    def clear_campaign_pause(
        self, campaign_id: str, *, cleared_by: str = "operator"
    ) -> None:
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO campaign_controls "
                "(campaign_id,pause_requested,reason,requested_by,requested_at,updated_at) "
                "VALUES(?, 0, '', ?, NULL, ?) "
                "ON CONFLICT(campaign_id) DO UPDATE SET "
                "pause_requested=0, reason='', requested_by=excluded.requested_by, "
                "requested_at=NULL, updated_at=excluded.updated_at",
                (campaign_id, cleared_by, now),
            )
        self.events.append(
            "campaign_pause_cleared",
            {"campaign_id": campaign_id, "cleared_by": cleared_by},
        )

    def recover_interrupted_campaign(
        self, campaign_id: str, *, recovered_by: str = "operator"
    ) -> dict[str, int]:
        """Reconcile a campaign only after an operator confirms its process is stale."""
        now = utc_now()
        with self.transaction() as conn:
            runs = conn.execute(
                "UPDATE agent_runs SET status='INTERRUPTED', finished_at=? "
                "WHERE campaign_id=? AND status='RUNNING'",
                (now, campaign_id),
            ).rowcount
            tasks = conn.execute(
                "UPDATE task_queue SET status='CANCELLED', finished_at=? "
                "WHERE campaign_id=? AND status IN ('RUNNING', 'QUEUED')",
                (now, campaign_id),
            ).rowcount
            reservations = conn.execute(
                "SELECT reservation_id,reserved_cost_usd FROM budget_reservations "
                "WHERE campaign_id=? AND status='RESERVED'",
                (campaign_id,),
            ).fetchall()
            for reservation in reservations:
                conn.execute(
                    "UPDATE budget_reservations SET settled_cost_usd=?, status='SETTLED', "
                    "settled_at=? WHERE reservation_id=?",
                    (
                        float(reservation["reserved_cost_usd"]),
                        now,
                        str(reservation["reservation_id"]),
                    ),
                )
            conn.execute(
                "UPDATE campaigns SET status=?, updated_at=? WHERE campaign_id=?",
                (CampaignStatus.PAUSED_HUMAN, now, campaign_id),
            )
            conn.execute(
                "INSERT INTO campaign_controls "
                "(campaign_id,pause_requested,reason,requested_by,requested_at,updated_at) "
                "VALUES(?, 1, ?, ?, ?, ?) "
                "ON CONFLICT(campaign_id) DO UPDATE SET pause_requested=1, "
                "reason=excluded.reason, requested_by=excluded.requested_by, "
                "requested_at=excluded.requested_at, updated_at=excluded.updated_at",
                (
                    campaign_id,
                    "Operator recovered stale running work",
                    recovered_by,
                    now,
                    now,
                ),
            )
        result = {"agent_runs": int(runs), "tasks": int(tasks)}
        self.events.append(
            "campaign_recovered",
            {"campaign_id": campaign_id, "recovered_by": recovered_by, **result},
        )
        return result

    def get_campaign_control(self, campaign_id: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM campaign_controls WHERE campaign_id=?", (campaign_id,)
            ).fetchone()
        if not row:
            return {
                "campaign_id": campaign_id,
                "pause_requested": 0,
                "reason": "",
                "requested_by": "",
                "requested_at": None,
                "updated_at": None,
            }
        return dict(row)

    def add_human_instruction(
        self,
        *,
        campaign_id: str,
        instruction_text: str,
        route_id: str | None = None,
        audience: str = "researchers",
        author: str = "operator",
    ) -> str:
        text = instruction_text.strip()
        if not text:
            raise ValueError("Human instruction cannot be empty")
        allowed = {"all", "researchers", "sentinel", "verifiers"}
        if audience not in allowed:
            raise ValueError(
                f"Unknown instruction audience {audience!r}; choose from {sorted(allowed)}"
            )
        if route_id is not None:
            route = self.get_route(route_id)
            if str(route["campaign_id"]) != campaign_id:
                raise ValueError(
                    f"Route {route_id} belongs to campaign {route['campaign_id']}, not {campaign_id}"
                )
        payload = {
            "campaign_id": campaign_id,
            "route_id": route_id,
            "audience": audience,
            "instruction_text": text,
            "author": author,
            "created_at": utc_now(),
        }
        instruction_id = short_id("HIN", payload)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO human_instructions "
                "(instruction_id,campaign_id,route_id,audience,instruction_text,status,author,created_at,retired_at) "
                "VALUES(?, ?, ?, ?, ?, 'ACTIVE', ?, ?, NULL)",
                (
                    instruction_id,
                    campaign_id,
                    route_id,
                    audience,
                    text,
                    author,
                    payload["created_at"],
                ),
            )
        self.events.append(
            "human_instruction_added",
            {"instruction_id": instruction_id, **payload},
        )
        return instruction_id

    def list_human_instructions(
        self,
        campaign_id: str,
        *,
        active_only: bool = True,
        route_id: str | None = None,
        audience: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["campaign_id=?"]
        params: list[Any] = [campaign_id]
        if active_only:
            clauses.append("status='ACTIVE'")
        if route_id is not None:
            clauses.append("(route_id IS NULL OR route_id=?)")
            params.append(route_id)
        if audience is not None:
            clauses.append("audience IN ('all', ?)")
            params.append(audience)
        query = (
            "SELECT * FROM human_instructions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at"
        )
        with self.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def instructions_for_agent(
        self,
        *,
        campaign_id: str | None,
        role: str,
        route_id: str | None,
    ) -> list[dict[str, Any]]:
        if not campaign_id:
            return []
        if role == "literature_sentinel":
            audience = "sentinel"
        elif role in {"local_verifier", "global_verifier"}:
            audience = "verifiers"
        else:
            audience = "researchers"
        items = self.list_human_instructions(
            campaign_id,
            active_only=True,
            audience=audience,
        )
        return [
            item
            for item in items
            if item.get("route_id") is None or item.get("route_id") == route_id
        ]

    def retire_human_instruction(
        self, instruction_id: str, *, retired_by: str = "operator"
    ) -> None:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE human_instructions SET status='RETIRED', retired_at=? "
                "WHERE instruction_id=?",
                (utc_now(), instruction_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Unknown human instruction {instruction_id}")
        self.events.append(
            "human_instruction_retired",
            {"instruction_id": instruction_id, "retired_by": retired_by},
        )

    def add_formalization(
        self,
        *,
        proof_claim_id: str,
        status: str,
        artifact_id: str | None,
        toolchain: str,
    ) -> str:
        payload = {
            "proof_claim_id": proof_claim_id,
            "toolchain": toolchain,
            "created_at": utc_now(),
        }
        formalization_id = short_id("FRM", payload)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO formalizations VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    formalization_id,
                    proof_claim_id,
                    status,
                    artifact_id,
                    toolchain,
                    payload["created_at"],
                    payload["created_at"],
                ),
            )
        self.events.append("formalization_added", {"formalization_id": formalization_id, **payload})
        return formalization_id

    def table_counts(self) -> dict[str, int]:
        tables = [
            "claims",
            "routes",
            "attempts",
            "failure_clusters",
            "literature_sources",
            "interventions",
            "evidence",
            "audits",
            "decisions",
            "agent_runs",
            "human_reviews",
            "campaign_controls",
            "human_instructions",
            "task_queue",
            "formalizations",
        ]
        with self.transaction() as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }
