from __future__ import annotations

import argparse
import json
import shutil
import sys
from importlib.resources import files
from pathlib import Path
from typing import Sequence

from .activity import EventLogActivityReporter, make_activity_reporter
from .artifacts import ArtifactStore
from .config import load_config
from .controller import CampaignController
from .contracts import CONTRACT_TEMPLATE, validate_contract
from .enums import AuditVerdict, ClaimStatus, RouteStatus
from .formalization import FormalizationGateClosed, certify_formalization
from .reports import (
    problem_verdict, write_agent_audited_proof_report, write_continuation_brief, write_report,
)
from .store import CampaignAlreadyRunning, ResearchStore
from .setup_wizard import collect_setup_answers, generate_setup, load_setup_answers
from .tui import run_tui
from .transitions import InvalidTransition, transition_claim
from .util import content_hash, read_json, write_json


DEFAULT_MOCK_CONFIG = """[budget]
max_epochs = 3
max_calls = 30
max_cost_usd = 1.0
stagnation_epochs = 2
duplicate_failure_limit = 2

[mode]
name = "offline_sentinel"
offline_agents = 2
research_agents = 0
parallel = true
literature_intervention = true
require_route_difference_certificate = true
novelty_deadline_epochs = 1
allow_experiments = false
route_similarity_threshold = 0.82

[providers.mock]
kind = "mock"
estimated_cost_usd = 0.0

[roles.offline_researcher]
provider = "mock"
network_policy = "deny"

[roles.literature_researcher]
provider = "mock"
network_policy = "allow"

[roles.contract_author]
provider = "mock"
network_policy = "deny"

[roles.literature_author]
provider = "mock"
network_policy = "allow"

[roles.intervention_responder]
provider = "mock"
network_policy = "deny"

[roles.literature_sentinel]
provider = "mock"
network_policy = "allow"

[roles.local_verifier]
provider = "mock"
network_policy = "deny"

[roles.global_verifier]
provider = "mock"
network_policy = "deny"

[roles.conceptual_pivot]
provider = "mock"
network_policy = "deny"
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ariadne",
        description="Cost-aware, route-aware mathematical research harness",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize a project dossier")
    init.add_argument("project", type=Path)
    init.add_argument("--title", default="Untitled mathematical research project")
    init.add_argument(
        "--provider",
        choices=("mock", "codex"),
        default="mock",
        help="write a ready-to-use mock or Codex provider configuration",
    )

    setup = sub.add_parser(
        "setup",
        help="Interactive problem interview followed by separate contract and literature Codex agents",
    )
    setup.add_argument("project", type=Path)
    setup.add_argument("--config", required=True, type=Path)
    setup.add_argument(
        "--answers-file",
        type=Path,
        help="non-interactive JSON file containing setup interview answers",
    )
    _add_activity_options(setup)

    tui = sub.add_parser("tui", help="Open the live Ariadne terminal user interface")
    tui.add_argument("project", nargs="?", type=Path, default=Path("."))
    tui.add_argument("--config", type=Path, help="configuration TOML (defaults to PROJECT/ariadne.codex.toml)")

    contract = sub.add_parser("contract", help="Set or inspect the problem contract")
    contract_sub = contract.add_subparsers(dest="contract_command", required=True)
    contract_set = contract_sub.add_parser("set")
    contract_set.add_argument("project", type=Path)
    contract_set.add_argument("--file", required=True, type=Path)
    contract_show = contract_sub.add_parser("show")
    contract_show.add_argument("project", type=Path)

    literature = sub.add_parser("literature", help="Manage the literature sentinel dossier")
    literature_sub = literature.add_subparsers(dest="literature_command", required=True)
    lit_add = literature_sub.add_parser("add")
    lit_add.add_argument("project", type=Path)
    lit_add.add_argument("file", type=Path)
    lit_add.add_argument("--title", required=True)
    lit_add.add_argument("--citation", default="")
    lit_add.add_argument("--kind", default="local_note")
    lit_add.add_argument("--statement", default="")
    lit_add.add_argument("--locator", default="")
    lit_list = literature_sub.add_parser("list")
    lit_list.add_argument("project", type=Path)

    campaign = sub.add_parser("campaign", help="Run or inspect a campaign")
    campaign_sub = campaign.add_subparsers(dest="campaign_command", required=True)
    campaign_run = campaign_sub.add_parser("run")
    campaign_run.add_argument("project", type=Path)
    campaign_run.add_argument("--config", required=True, type=Path)
    campaign_run.add_argument("--resume", action="store_true")
    _add_activity_options(campaign_run)

    campaign_resume = campaign_sub.add_parser(
        "resume", help="Clear a human pause and resume the latest campaign"
    )
    campaign_resume.add_argument("project", type=Path)
    campaign_resume.add_argument("--config", required=True, type=Path)
    campaign_resume.add_argument("--by", default="operator")
    _add_activity_options(campaign_resume)

    campaign_recover = campaign_sub.add_parser(
        "recover", help="Mark stale running work interrupted and pause the campaign"
    )
    campaign_recover.add_argument("project", type=Path)
    campaign_recover.add_argument("--by", default="operator")

    campaign_budget = campaign_sub.add_parser(
        "budget", help="Adjust the latest paused campaign's limits without rewriting spent budget"
    )
    campaign_budget.add_argument("project", type=Path)
    campaign_budget.add_argument("--max-epochs", type=int)
    campaign_budget.add_argument("--max-calls", type=int)
    campaign_budget.add_argument("--max-cost-usd", type=float)
    campaign_budget.add_argument(
        "--reason", required=True, help="durable rationale for this human budget decision"
    )
    campaign_budget.add_argument("--by", default="operator")

    campaign_pause = campaign_sub.add_parser(
        "pause", help="Request a pause at the next safe checkpoint"
    )
    campaign_pause.add_argument("project", type=Path)
    campaign_pause.add_argument("--reason", default="Human intervention requested")
    campaign_pause.add_argument("--by", default="operator")

    campaign_instruct = campaign_sub.add_parser(
        "instruct", help="Add a persistent human instruction for future agent calls"
    )
    campaign_instruct.add_argument("project", type=Path)
    instruction_group = campaign_instruct.add_mutually_exclusive_group(required=True)
    instruction_group.add_argument("--text")
    instruction_group.add_argument("--file", type=Path)
    campaign_instruct.add_argument("--route")
    campaign_instruct.add_argument(
        "--audience",
        choices=["all", "researchers", "sentinel", "verifiers"],
        default="researchers",
    )
    campaign_instruct.add_argument("--by", default="operator")
    campaign_instruct.add_argument(
        "--pause",
        action="store_true",
        help="also request a pause so the instruction is guaranteed to apply before another research epoch",
    )

    campaign_instructions = campaign_sub.add_parser(
        "instructions", help="List persistent human instructions"
    )
    campaign_instructions.add_argument("project", type=Path)
    campaign_instructions.add_argument("--include-retired", action="store_true")

    campaign_retire = campaign_sub.add_parser(
        "retire-instruction", help="Retire a human instruction"
    )
    campaign_retire.add_argument("project", type=Path)
    campaign_retire.add_argument("--id", required=True)
    campaign_retire.add_argument("--by", default="operator")

    campaign_route_status = campaign_sub.add_parser(
        "route-status", help="Explicitly park, reactivate, or retire a route"
    )
    campaign_route_status.add_argument("project", type=Path)
    campaign_route_status.add_argument("--route", required=True)
    campaign_route_status.add_argument(
        "--status",
        required=True,
        choices=[
            RouteStatus.ACTIVE,
            RouteStatus.PARKED,
            RouteStatus.OBSOLETE,
            RouteStatus.NEEDS_HUMAN_IDEA,
            RouteStatus.NEEDS_REPRESENTATION_CHANGE,
        ],
    )
    campaign_route_status.add_argument("--note", default="Human route-control decision")
    campaign_route_status.add_argument("--by", default="operator")

    campaign_status = campaign_sub.add_parser("status")
    campaign_status.add_argument("project", type=Path)

    routes = sub.add_parser("routes", help="List research routes")
    routes.add_argument("project", type=Path)

    failures = sub.add_parser("failures", help="List compressed failure clusters")
    failures.add_argument("project", type=Path)

    report = sub.add_parser("report", help="Generate an honest Markdown report")
    report.add_argument("project", type=Path)
    report.add_argument("--output", type=Path)

    audit = sub.add_parser("audit", help="Record a local or global proof audit")
    audit.add_argument("project", type=Path)
    audit.add_argument("--claim", required=True)
    audit.add_argument("--type", choices=["local", "global"], required=True)
    audit.add_argument("--verdict", choices=[item.value for item in AuditVerdict], required=True)
    audit.add_argument("--report-file", type=Path)
    audit.add_argument("--failure-class", default="")
    audit.add_argument("--obligation", default="")
    audit.add_argument("--auditor", default="external-auditor")

    approve = sub.add_parser("approve", help="Record a complete human proof check")
    approve.add_argument("project", type=Path)
    approve.add_argument("--claim", required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--notes", default="Complete proof checked against the problem contract.")

    formalize = sub.add_parser("formalize", help="Run a late formal verification command")
    formalize.add_argument("project", type=Path)
    formalize.add_argument("--claim", required=True)
    formalize.add_argument("--toolchain", required=True)
    formalize.add_argument("--cwd", type=Path)
    formalize.add_argument("verify_command", nargs=argparse.REMAINDER)

    doctor = sub.add_parser("doctor", help="Inspect configuration and isolation policy")
    doctor.add_argument("project", type=Path)
    doctor.add_argument("--config", required=True, type=Path)

    demo = sub.add_parser("demo", help="Run the deterministic offline/sentinel demonstration")
    demo.add_argument("project", type=Path)
    demo.add_argument("--force", action="store_true")

    return parser


def _add_activity_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress live activity; final result is still printed",
    )
    parser.add_argument(
        "--json-events",
        action="store_true",
        help="write live activity as JSON Lines to stderr",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=15.0,
        help="heartbeat interval while bounded agent calls are active",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="offer continue/instruct/pause choices at epoch boundaries when stdin is a TTY",
    )
    parser.add_argument(
        "--record-activity",
        action="store_true",
        help=argparse.SUPPRESS,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except (ValueError, KeyError, FileNotFoundError, RuntimeError, InvalidTransition) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "init":
        return _cmd_init(args.project, args.title, args.provider)
    if args.command == "setup":
        answers = (
            load_setup_answers(args.answers_file)
            if args.answers_file
            else collect_setup_answers(project_root=Path(args.project))
        )
        setup_store = ResearchStore(args.project)
        reporter = (
            EventLogActivityReporter(setup_store.events.append)
            if bool(getattr(args, "record_activity", False))
            else make_activity_reporter(
                quiet=bool(getattr(args, "quiet", False)),
                json_events=bool(getattr(args, "json_events", False)),
                heartbeat_seconds=float(getattr(args, "heartbeat_seconds", 15.0)),
                interactive=False,
            )
        )
        result = generate_setup(
            project_root=args.project,
            config_path=args.config,
            answers=answers,
            reporter=reporter,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if args.command == "tui":
        run_tui(args.project, args.config)
        return 0
    if args.command == "contract":
        return _cmd_contract(args)
    if args.command == "literature":
        return _cmd_literature(args)
    if args.command == "campaign":
        return _cmd_campaign(args)
    if args.command == "routes":
        return _cmd_routes(args.project)
    if args.command == "failures":
        return _cmd_failures(args.project)
    if args.command == "report":
        return _cmd_report(args.project, args.output)
    if args.command == "audit":
        return _cmd_audit(args)
    if args.command == "approve":
        return _cmd_approve(args)
    if args.command == "formalize":
        return _cmd_formalize(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "demo":
        return _cmd_demo(args.project, args.force)
    raise ValueError(f"Unknown command {args.command}")


def _cmd_init(project: Path, title: str, provider: str = "mock") -> int:
    store = ResearchStore(project)
    store.set_meta("title", title)
    if provider == "codex":
        config_path = store.paths.root / "ariadne.codex.toml"
        config_text = files("ariadne_math.integrations.codex").joinpath(
            "config.toml"
        ).read_text(encoding="utf-8")
        config_label = "Codex configuration"
    else:
        config_path = store.paths.root / "ariadne.mock.toml"
        config_text = DEFAULT_MOCK_CONFIG
        config_label = "Mock configuration"
    if not config_path.exists():
        config_path.write_text(config_text, encoding="utf-8")
    template_path = store.paths.state / "problem_contract.template.json"
    if not template_path.exists():
        write_json(template_path, CONTRACT_TEMPLATE)
    print(f"Initialized Ariadne project: {store.paths.root}")
    print(f"{config_label}: {config_path}")
    print(f"Contract template: {template_path}")
    if provider == "codex":
        print("Next check: ariadne-codex-provider --check")
    return 0


def _cmd_contract(args: argparse.Namespace) -> int:
    store = ResearchStore(args.project)
    if args.contract_command == "show":
        print(json.dumps(read_json(store.paths.contract), indent=2, ensure_ascii=False))
        return 0
    if store.latest_campaign() is not None:
        raise ValueError(
            "The problem contract is immutable after a campaign is created. "
            "Initialize a new project directory for a revised statement."
        )
    data = read_json(args.file)
    validate_contract(data)
    write_json(store.paths.contract, data)
    store.set_meta(
        "problem_contract_sha256", content_hash(store.paths.contract.read_bytes())
    )
    store.events.append(
        "problem_contract_set",
        {
            "path": str(store.paths.contract.relative_to(store.paths.root)),
            "sha256": content_hash(store.paths.contract.read_bytes()),
        },
    )
    print(f"Problem contract set: {store.paths.contract}")
    return 0


def _cmd_literature(args: argparse.Namespace) -> int:
    store = ResearchStore(args.project)
    if args.literature_command == "list":
        print(json.dumps(store.list_literature_sources(), indent=2, ensure_ascii=False))
        return 0
    if not args.file.exists() or not args.file.is_file():
        raise FileNotFoundError(args.file)
    digest = content_hash(args.file.read_bytes())
    destination = store.paths.literature / f"{digest[:12]}-{args.file.name}"
    shutil.copy2(args.file, destination)
    source_id = store.add_literature_source(
        title=args.title,
        citation=args.citation,
        source_kind=args.kind,
        exact_statement=args.statement,
        assumptions=[],
        locator=args.locator,
        relative_path=str(destination.relative_to(store.paths.root)),
    )
    print(f"Added literature source {source_id}: {destination}")
    return 0


def _cmd_campaign(args: argparse.Namespace) -> int:
    store = ResearchStore(args.project)
    command = args.campaign_command

    if command == "status":
        campaign = store.latest_campaign()
        control = (
            store.get_campaign_control(str(campaign["campaign_id"]))
            if campaign
            else None
        )
        instructions = (
            store.list_human_instructions(
                str(campaign["campaign_id"]), active_only=True
            )
            if campaign
            else []
        )
        print(
            json.dumps(
                {
                    "campaign": campaign,
                    "control": control,
                    "active_human_instructions": instructions,
                    "active_agent_runs": (
                        store.list_agent_runs(str(campaign["campaign_id"]), active_only=True)
                        if campaign
                        else []
                    ),
                    "task_queue": (
                        store.list_tasks(str(campaign["campaign_id"]), limit=20)
                        if campaign
                        else []
                    ),
                    "recent_artifacts": store.list_artifacts(limit=10),
                    "recent_attempts": (
                        [
                            item
                            for item in store.list_attempts()
                            if item["campaign_id"] == str(campaign["campaign_id"])
                        ][-5:]
                        if campaign
                        else []
                    ),
                    "problem_verdict": problem_verdict(store),
                    "configuration_revisions": (
                        store.list_campaign_config_revisions(str(campaign["campaign_id"]))
                        if campaign
                        else []
                    ),
                    "counts": store.table_counts(),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    campaign = store.latest_campaign()
    if command in {"pause", "instruct", "instructions", "retire-instruction", "route-status", "resume", "recover", "budget"} and not campaign:
        raise ValueError("No campaign exists for this project")

    if command == "recover":
        campaign_id = str(campaign["campaign_id"])
        recovered = store.recover_interrupted_campaign(
            campaign_id, recovered_by=str(args.by)
        )
        print(
            f"Recovered {campaign_id}: {recovered['agent_runs']} stale agent run(s), "
            f"{recovered['tasks']} stale task(s); campaign is PAUSED_HUMAN."
        )
        return 0

    if command == "budget":
        if (
            args.max_epochs is None
            and args.max_calls is None
            and args.max_cost_usd is None
        ):
            raise ValueError("Set at least one of --max-epochs, --max-calls, or --max-cost-usd")
        updated = store.adjust_campaign_budget(
            str(campaign["campaign_id"]),
            max_epochs=args.max_epochs,
            max_calls=args.max_calls,
            max_cost_usd=args.max_cost_usd,
            adjusted_by=str(args.by),
            reason=str(args.reason),
        )
        print(
            f"Adjusted budget for {updated['campaign_id']}: "
            f"epochs {updated['epoch']}/{updated['max_epochs']}, "
            f"calls {updated['calls_used']}/{updated['max_calls']}, "
            f"cost ${float(updated['cost_used']):.4f}/${float(updated['max_cost_usd']):.2f}."
        )
        return 0

    if command == "pause":
        campaign_id = str(campaign["campaign_id"])
        store.request_campaign_pause(
            campaign_id, reason=args.reason, requested_by=args.by
        )
        print(
            f"Pause requested for {campaign_id}. The running controller will stop at the next safe stage boundary after active work and any already-triggered atomic proof-audit chain finish."
        )
        return 0

    if command == "instruct":
        campaign_id = str(campaign["campaign_id"])
        if args.file:
            if not args.file.exists() or not args.file.is_file():
                raise FileNotFoundError(args.file)
            text = args.file.read_text(encoding="utf-8")
        else:
            text = str(args.text or "")
        instruction_id = store.add_human_instruction(
            campaign_id=campaign_id,
            instruction_text=text,
            route_id=args.route,
            audience=args.audience,
            author=args.by,
        )
        if args.pause:
            store.request_campaign_pause(
                campaign_id,
                reason=f"Pause requested while adding instruction {instruction_id}",
                requested_by=args.by,
            )
        print(f"Added instruction {instruction_id} to {campaign_id}")
        if args.pause:
            print("A pause was also requested; resume after the controller reports PAUSED_HUMAN.")
        else:
            print("It will be injected into the next matching agent call; already-running calls are unchanged.")
        return 0

    if command == "instructions":
        campaign_id = str(campaign["campaign_id"])
        print(
            json.dumps(
                store.list_human_instructions(
                    campaign_id, active_only=not args.include_retired
                ),
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if command == "retire-instruction":
        store.retire_human_instruction(args.id, retired_by=args.by)
        print(f"Retired human instruction {args.id}")
        return 0

    if command == "route-status":
        campaign_id = str(campaign["campaign_id"])
        route = store.get_route(args.route)
        if str(route["campaign_id"]) != campaign_id:
            raise ValueError(
                f"Route {args.route} belongs to {route['campaign_id']}, not latest campaign {campaign_id}"
            )
        store.update_route(args.route, status=args.status)
        store.add_decision(
            campaign_id=campaign_id,
            epoch=int(campaign["epoch"]),
            kind="HUMAN_ROUTE_STATUS",
            available={"route": route},
            selected={"route_id": args.route, "status": args.status, "by": args.by},
            rationale=args.note,
            expected_event=(
                "Further bounded work under explicit human direction"
                if args.status == RouteStatus.ACTIVE
                else "No new work on this route until another explicit decision"
            ),
            stop_condition="Human changes the route status or the route reaches a terminal mathematical event",
            cost_cap=0.0,
        )
        print(f"Route {args.route} set to {args.status}")
        return 0

    config = load_config(args.config)
    resume = command == "resume" or bool(getattr(args, "resume", False))
    if resume:
        if str(campaign.get("status")) == "CONTRACT_CHANGED":
            raise ValueError(
                f"Campaign {campaign['campaign_id']} cannot resume because its frozen "
                "problem contract changed. Restore the exact sealed contract or create a new project."
            )
        if str(campaign.get("status")) != "PAUSED_HUMAN":
            raise ValueError(
                f"Campaign {campaign['campaign_id']} is {campaign.get('status')}; "
                "resume is only valid after a human pause"
            )
        campaign_id = str(campaign["campaign_id"])
        store.clear_campaign_pause(
            campaign_id, cleared_by=str(getattr(args, "by", "operator"))
        )

    reporter = (
        EventLogActivityReporter(store.events.append)
        if bool(getattr(args, "record_activity", False))
        else make_activity_reporter(
            quiet=bool(getattr(args, "quiet", False)),
            json_events=bool(getattr(args, "json_events", False)),
            heartbeat_seconds=float(getattr(args, "heartbeat_seconds", 15.0)),
            interactive=bool(getattr(args, "interactive", False)),
        )
    )
    try:
        result = CampaignController(
            args.project, config, reporter=reporter, config_path=args.config
        ).run(new_campaign=not resume)
    except CampaignAlreadyRunning as exc:
        print(f"Campaign not started: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        latest = store.latest_campaign()
        if latest:
            campaign_id = str(latest["campaign_id"])
            store.request_campaign_pause(
                campaign_id,
                reason="Campaign process interrupted from the terminal",
                requested_by="keyboard-interrupt",
            )
            store.update_campaign(campaign_id, status="PAUSED_HUMAN")
            print(
                f"\nCampaign {campaign_id} marked PAUSED_HUMAN. The active provider process may have been interrupted; inspect status before resuming.",
                file=sys.stderr,
            )
        return 130
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"Problem verdict: {problem_verdict(store)}")
    return 0


def _cmd_routes(project: Path) -> int:
    store = ResearchStore(project)
    print(json.dumps(store.list_routes(), indent=2, ensure_ascii=False))
    return 0


def _cmd_failures(project: Path) -> int:
    store = ResearchStore(project)
    print(json.dumps(store.list_failures(), indent=2, ensure_ascii=False))
    return 0


def _cmd_report(project: Path, output: Path | None) -> int:
    store = ResearchStore(project)
    path = write_report(store, output)
    print(path)
    continuation = write_continuation_brief(store)
    print(continuation)
    for tex_path, pdf_path in write_agent_audited_proof_report(store):
        print(tex_path)
        if pdf_path:
            print(pdf_path)
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    store = ResearchStore(args.project)
    artifact_id = None
    if args.report_file:
        data = args.report_file.read_bytes()
        artifact = ArtifactStore(store.paths).put_bytes(
            data,
            kind=f"{args.type}_audit_report",
            suffix=args.report_file.suffix,
        )
        store.record_artifact(artifact)
        artifact_id = artifact.artifact_id
    store.add_audit(
        target_type="claim",
        target_id=args.claim,
        audit_type=f"{args.type.upper()}_PROOF_AUDIT",
        verdict=args.verdict,
        failure_class=args.failure_class,
        minimal_obligation=args.obligation,
        local_repairable=False,
        artifact_id=artifact_id,
        auditor_profile=args.auditor,
    )
    if args.verdict == AuditVerdict.PASS:
        current = ClaimStatus(store.get_claim(args.claim)["status"])
        if args.type == "local":
            if current == ClaimStatus.PROPOSED:
                transition_claim(store, args.claim, ClaimStatus.CANDIDATE_LEMMA)
            transition_claim(store, args.claim, ClaimStatus.AGENT_AUDITED_LOCAL)
        else:
            transition_claim(store, args.claim, ClaimStatus.AGENT_AUDITED_GLOBAL)
    print(f"Recorded {args.type} audit for {args.claim}")
    return 0


def _cmd_approve(args: argparse.Namespace) -> int:
    store = ResearchStore(args.project)
    current = ClaimStatus(store.get_claim(args.claim)["status"])
    if current != ClaimStatus.AGENT_AUDITED_GLOBAL:
        raise InvalidTransition(
            f"Human approval requires AGENT_AUDITED_GLOBAL; current status is {current}"
        )
    store.add_human_review(
        target_type="claim",
        target_id=args.claim,
        reviewer=args.reviewer,
        verdict="PASS",
        notes=args.notes,
    )
    transition_claim(store, args.claim, ClaimStatus.HUMAN_CHECKED)
    print(f"Claim {args.claim} is now HUMAN_CHECKED")
    return 0


def _cmd_formalize(args: argparse.Namespace) -> int:
    command = list(args.verify_command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("Provide the verification command after `--`, e.g. `-- lake build`")
    store = ResearchStore(args.project)
    formalization_id = certify_formalization(
        store,
        claim_id=args.claim,
        toolchain=args.toolchain,
        verify_command=command,
        cwd=args.cwd,
    )
    print(f"Formal certification recorded: {formalization_id}")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    store = ResearchStore(args.project)
    config = load_config(args.config)
    rows = []
    for role, role_cfg in sorted(config.roles.items()):
        provider = config.provider_for_role(role)
        executable = provider.command[0] if provider.command else None
        command_available = (
            True
            if provider.kind == "mock"
            else bool(executable and shutil.which(executable))
        )
        is_codex_wrapper = executable == "ariadne-codex-provider"
        if provider.kind == "mock":
            isolation = "mock provider; no external process"
        elif role_cfg.network_policy == "deny" and provider.sandbox_prefix:
            isolation = "OS sandbox prefix configured"
        elif role_cfg.network_policy == "deny" and is_codex_wrapper:
            isolation = (
                "Codex bounded-role isolation: no literature in prompt, web disabled, "
                "empty read-only workspace; Codex client still requires model-service access"
            )
        elif role_cfg.network_policy == "deny":
            isolation = "protocol-only offline: NO OS network isolation"
        else:
            isolation = "network allowed by role"
        rows.append(
            {
                "role": role,
                "provider": provider.name,
                "kind": provider.kind,
                "command": executable,
                "command_available": command_available,
                "network_policy": role_cfg.network_policy,
                "isolation": isolation,
            }
        )
    print(
        json.dumps(
            {
                "project": str(store.paths.root),
                "contract_present": store.paths.contract.exists(),
                "roles": rows,
                "budget": config.budget.__dict__,
                "mode": config.mode.__dict__,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _cmd_demo(project: Path, force: bool) -> int:
    if project.exists() and any(project.iterdir()):
        if not force:
            raise ValueError(f"Demo destination {project} is not empty; use --force")
        shutil.rmtree(project)
    _cmd_init(project, "Offline researchers with literature sentinel demo", "mock")
    store = ResearchStore(project)
    contract = {
        **CONTRACT_TEMPLATE,
        "problem_id": "DEMO-001",
        "title": "Demonstration problem",
        "statement": {
            "text": "Prove or refute that a proposed uniform estimate can be obtained without the classical loss.",
            "formal_quantifier_outline": "forall admissible objects X, A(X) <= C B(X) with C uniform",
        },
        "hypotheses": ["admissibility assumptions are fixed in the contract"],
        "conclusion": ["uniform loss-free estimate"],
    }
    write_json(store.paths.contract, contract)
    literature_note = store.paths.root / "known_energy_route.md"
    literature_note.write_text(
        "# Known route note\n\nThe classical fixed-weight energy/Gronwall route retains the loss. "
        "This note does not cover a dynamic weight whose derivative enters the identity.\n",
        encoding="utf-8",
    )
    destination = store.paths.literature / literature_note.name
    shutil.copy2(literature_note, destination)
    store.add_literature_source(
        title="Known fixed-weight energy route",
        citation="Demonstration source",
        source_kind="local_note",
        exact_statement="Fixed-weight energy iteration retains the classical loss.",
        assumptions=["the weight is fixed"],
        locator="known_energy_route.md",
        relative_path=str(destination.relative_to(store.paths.root)),
    )
    config_path = store.paths.root / "ariadne.mock.toml"
    result = CampaignController(
        project,
        load_config(config_path),
        reporter=make_activity_reporter(heartbeat_seconds=5.0),
        config_path=config_path,
    ).run()
    report_path = write_report(store)
    print(json.dumps(result, indent=2))
    print(f"Report: {report_path}")
    print("The demo intentionally remains UNSOLVED; it demonstrates a proposed early stop, a structured rejection, and a literature withdrawal after novelty evidence.")
    return 0
