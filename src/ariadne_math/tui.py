from __future__ import annotations

import json
import os
import re
import signal
from datetime import datetime, timezone
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .activity import EventLogActivityReporter, NullActivityReporter
from .agent import AgentRunner
from .artifacts import ArtifactStore
from .config import load_config
from .contracts import validate_contract
from .models import AgentCall
from .prompt_loader import render_prompt
from .setup_wizard import collect_setup_answers, generate_setup
from .successor import (
    create_contract_variant_successor,
    enable_project_git,
    project_has_git_repository,
)
from .reports import (
    publish_continuation_brief_as_literature,
    write_agent_audited_proof_report, write_continuation_brief, write_report,
)
from .store import ResearchStore
from .util import canonical_json, content_hash, extract_json_object, read_json


def chat_intent_to_command(text: str) -> str | None:
    """Translate unambiguous conversational controls without a model call.

    A ``None`` result deliberately means “research instruction”, not an error.
    This keeps chat useful while ensuring that an ambiguous mathematical sentence
    can never trigger an operational action.
    """
    normalized = " ".join(text.casefold().strip().split())
    normalized = re.sub(r"^(please|could you|can you|would you)\s+", "", normalized)
    if normalized in {"run", "start", "start campaign", "run campaign", "continue", "resume", "resume campaign"}:
        return "/run"
    if normalized in {"pause", "pause campaign", "pause the campaign", "stop", "stop campaign", "stop the campaign"}:
        return "/pause"
    if normalized in {"refresh", "refresh panels", "refresh status", "update", "update status", "status"}:
        return "/refresh"
    if normalized in {"recover", "recover campaign", "recover interrupted work"}:
        return "/recover"
    if normalized in {"report", "make report", "generate report", "write report", "create report"}:
        return "/report"
    if normalized in {"help", "what can i do", "show help", "show commands"}:
        return "/help"
    if normalized in {"quit", "exit", "close"}:
        return "/quit"
    if normalized in {"setup", "set up", "new task", "new project", "start a new task"}:
        return "/setup"
    if normalized in {"manage routes", "change route", "change route status", "route status"}:
        return "/route"
    artifact_actions = {
        "next artifact": "next", "previous artifact": "prev", "prior artifact": "prev",
        "open artifact": "open", "show artifact": "open", "close artifact": "close",
    }
    if normalized in artifact_actions:
        return f"/artifact {artifact_actions[normalized]}"
    for strength in ("low", "medium", "high", "xhigh", "max"):
        if normalized in {
            f"model {strength}", f"set model {strength}",
            f"reasoning {strength}", f"set reasoning {strength}",
            f"set reasoning strength {strength}",
        }:
            return f"/model {strength}"
    return None


class AriadneTUI:
    def __init__(
        self,
        project_root: Path,
        config_path: Path,
        *,
        resume_on_start: bool = False,
        setup_answers: Any | None = None,
    ):
        self.project_root = project_root.resolve()
        self.config_path = config_path.resolve()
        self.resume_on_start = resume_on_start
        self.setup_answers = setup_answers
        self._setup_phase: tuple[str, str] | None = None
        self._setup_started_at: float | None = None
        self.store = ResearchStore(self.project_root)
        self.selected_artifact: int | None = None
        self.selected_task: int | None = None
        self.selected_route: int | None = None
        self.selected_claim: int | None = None
        self.selected_failure: int | None = None
        self.preview_panel: str | None = None
        self.message = "Ready"
        self._campaign_process: subprocess.Popen[str] | None = None
        self._worker_threads: list[threading.Thread] = []
        self._panel_refresh_thread: threading.Thread | None = None
        self._panel_refresh_stop = threading.Event()
        self._panel_cache: dict[str, str] = {}
        self._panel_cache_lock = threading.Lock()
        self._app = None

    def _apply_interpreted_chat_instruction(
        self, *, campaign_id: str, owner_message: str, interpretation: dict[str, Any]
    ) -> str:
        """Apply a bounded, auditable chat interpretation without contract drift."""
        action = str(interpretation.get("action", "ADD")).upper()
        if action == "PROPOSE_CONTRACT_VARIANT":
            variant = str(interpretation.get("target_variant", "")).strip()
            if not variant:
                raise ValueError("Interpreter did not supply a successor target variant")
            record = {
                "campaign_id": campaign_id,
                "owner_message": owner_message,
                "target_variant": variant,
                "interpretation": interpretation,
                "contract_unchanged": True,
            }
            artifacts = ArtifactStore(self.store.paths)
            artifact = artifacts.put_bytes(
                canonical_json(record).encode("utf-8"),
                kind="contract_variant_request",
                suffix=".json",
                media_type="application/json",
                metadata={"campaign_id": campaign_id, "status": "SUCCESSOR_BRANCH_REQUEST"},
            )
            self.store.record_artifact(artifact)
            successor = create_contract_variant_successor(
                parent_root=self.project_root,
                config_path=self.config_path,
                target_variant=variant,
                request_artifact_id=artifact.artifact_id,
            )
            provenance = read_json(successor / "SUCCESSOR_PROVENANCE.json")
            self.store.events.append(
                "contract_variant_branch_created",
                {
                    "campaign_id": campaign_id,
                    "artifact_id": artifact.artifact_id,
                    "branch_id": provenance.get("branch_id"),
                    "branch_name": provenance.get("branch_name"),
                    "successor_project": str(successor),
                },
            )
            return (
                f"Created successor branch {provenance.get('branch_name')}: {successor}. "
                "The parent contract and artifacts are unchanged; start Ariadne in the successor and use SUCCESSOR_TASK.md."
            )

        if action == "CHANGE_BUDGET":
            budget = interpretation.get("budget")
            if not isinstance(budget, dict):
                raise ValueError("Interpreter did not supply complete budget limits")
            updated = self.store.adjust_campaign_budget(
                campaign_id,
                max_epochs=int(budget["max_epochs"]),
                max_calls=int(budget["max_calls"]),
                max_cost_usd=float(budget["max_cost_usd"]),
                adjusted_by="tui-chat-interpreter",
                reason=str(budget.get("reason", "")).strip() or owner_message,
            )
            if updated.get("scheduled_action"):
                action = updated["scheduled_action"]
                limits = updated["requested_limits"]
                return (
                    f"Budget change is scheduled before epoch {action['apply_before_epoch']}: "
                    f"epochs {limits['max_epochs']}, calls {limits['max_calls']}, "
                    f"cost ${float(limits['max_cost_usd']):.2f}."
                )
            return (
                f"Budget updated; campaign is {updated['status']}. "
                f"epochs {updated['epoch']}/{updated['max_epochs']}, "
                f"calls {updated['calls_used']}/{updated['max_calls']}, "
                f"cost ${float(updated['cost_used']):.4f}/${float(updated['max_cost_usd']):.2f}."
            )

        # A report-continuation request is durable reporting guidance. The TUI
        # regenerates the report after storing it, but no mathematical evidence
        # is invented: requested figures/data appear only after agents retain them.
        if action == "CONTINUE_REPORT":
            action = "ADD"
            interpretation = {**interpretation, "action": action, "purpose": "REPORT_REQUIREMENTS"}

        audience = str(interpretation.get("audience", "researchers"))
        route_id = str(interpretation.get("route_id", "")).strip()
        instruction = str(interpretation.get("instruction", "")).strip()
        targets = [str(value) for value in interpretation.get("target_instruction_ids", []) if str(value)]
        active = {
            str(item["instruction_id"]): item
            for item in self.store.list_human_instructions(campaign_id, active_only=True)
        }
        if action not in {"ADD", "CANCEL", "REPLACE"}:
            raise ValueError("Interpreter returned an invalid instruction action")
        if action in {"CANCEL", "REPLACE"} and not targets:
            raise ValueError("Interpreter did not identify an active instruction to cancel")
        unknown = [item for item in targets if item not in active]
        if unknown:
            raise ValueError("Interpreter named non-active instruction IDs: " + ", ".join(unknown))
        if route_id:
            self.store.get_route(route_id)
        if audience not in {"all", "researchers", "sentinel", "verifiers"}:
            raise ValueError("Interpreter returned an invalid instruction audience")
        for instruction_id in targets:
            self.store.retire_human_instruction(instruction_id, retired_by="tui-chat-owner")
        if action == "CANCEL":
            self.store.events.append(
                "chat_instruction_cancelled",
                {"campaign_id": campaign_id, "instruction_ids": targets, "owner_message": owner_message},
            )
            return "Cancelled instruction" + ("s " if len(targets) > 1 else " ") + ", ".join(targets)
        if not instruction:
            raise ValueError("Interpreter returned an empty replacement instruction")
        artifacts = [str(item).strip() for item in interpretation.get("required_artifacts", []) if str(item).strip()]
        purpose = str(interpretation.get("purpose", "RESEARCH_GUIDANCE"))
        durable = (
            f"Owner chat request: {owner_message.strip()}\n\n"
            f"Interpreted {purpose.lower().replace('_', ' ')}: {instruction}"
        )
        if artifacts:
            durable += "\nRequired retained artifacts: " + "; ".join(artifacts)
        instruction_id = self.store.add_human_instruction(
            campaign_id=campaign_id,
            instruction_text=durable,
            audience=audience,
            route_id=route_id or None,
            author="tui-chat-interpreter",
        )
        self.store.events.append(
            "chat_instruction_interpreted",
            {
                "campaign_id": campaign_id,
                "instruction_id": instruction_id,
                "action": action,
                "replaced_instruction_ids": targets,
                "purpose": purpose,
            },
        )
        prefix = "Replaced " + ", ".join(targets) + "; " if action == "REPLACE" else ""
        return prefix + f"Agent-interpreted instruction saved as {instruction_id}"

    def run(self) -> None:
        try:
            from prompt_toolkit.application import Application, run_in_terminal
            from prompt_toolkit.filters import Condition, has_focus
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.buffer import Buffer
            from prompt_toolkit.document import Document
            from prompt_toolkit.layout import (
                Float,
                FloatContainer,
                HSplit,
                Layout,
                ScrollablePane,
                VSplit,
                Window,
                Dimension,
            )
            from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
            from prompt_toolkit.layout.containers import ConditionalContainer
            from prompt_toolkit.lexers import Lexer
            from prompt_toolkit.layout.menus import CompletionsMenu
            from prompt_toolkit.application import get_app
            from prompt_toolkit.mouse_events import MouseEventType
            from prompt_toolkit.styles import Style
            from prompt_toolkit.widgets import Label
        except ImportError as exc:
            raise RuntimeError(
                "The TUI requires prompt_toolkit. Install this package with its dependencies."
            ) from exc

        panel_cache = self._panel_cache
        panel_cache_lock = self._panel_cache_lock

        class PanelLexer(Lexer):
            def lex_document(self, document):
                def get_line(line_no: int):
                    line = document.lines[line_no]
                    upper = line.upper()
                    selected = line.startswith("> ")
                    marker = line.removeprefix("> ").lstrip()[:1]
                    if marker == "×" or any(token in upper for token in ("FAILED", "ERROR", "CRASHED", "EXCEPTION")):
                        return [("class:panel-failure", line)]
                    if marker in {"◐", "◓", "◑", "◒", "○"} or any(token in upper for token in ("RUNNING", "ACTIVE", "QUEUED", "PENDING")):
                        return AriadneTUI._active_shimmer_fragments(line, selected=selected)
                    if selected:
                        return [("class:panel-selected", line)]
                    if marker == "✓" or any(token in upper for token in ("COMPLETED", "VERIFIED", "PROVEN", "SUCCEEDED")):
                        return [("class:panel-success", line)]
                    if any(token in upper for token in ("PAUSED", "NEEDS_HUMAN", "WARNING")):
                        return [("class:panel-warning", line)]
                    if line.startswith("Message:") or line.startswith("Selected:"):
                        return [("class:panel-label", line)]
                    return [("", line)]

                return get_line

        class SelectablePanelControl(BufferControl):
            def __init__(self, panel_key: str):
                self._panel_key = panel_key
                self._last_text = ""
                super().__init__(
                    buffer=Buffer(
                        document=Document(text="Loading panel…"),
                        read_only=True,
                        multiline=True,
                    ),
                    focusable=True,
                    focus_on_click=True,
                    lexer=PanelLexer(),
                )
                self._last_text = self.buffer.document.text

            def create_content(self, width: int, height: int):
                with panel_cache_lock:
                    text = panel_cache.get(self._panel_key, self._last_text)
                if text != self._last_text and self.buffer.selection_state is None:
                    self.buffer.set_document(
                        Document(text=text),
                        bypass_readonly=True,
                    )
                    self._last_text = text
                return super().create_content(width, height)

            def mouse_handler(self, mouse_event):
                result = super().mouse_handler(mouse_event)
                if (
                    mouse_event.event_type == MouseEventType.MOUSE_UP
                    and self.buffer.selection_state is not None
                ):
                    # cut_selection() returns the selected text without changing
                    # the buffer when its returned document is not assigned.
                    _, clipboard_data = self.buffer.document.cut_selection()
                    get_app().clipboard.set_data(clipboard_data)
                return result

        panel_callbacks: dict[str, Callable[[], str]] = {}
        panel_focus_controls: list[Any] = []

        def panel(
            title: str,
            callback: Callable[[], str],
            *,
            height=None,
            control_holder: list[Any] | None = None,
            include_in_focus_cycle: bool = True,
        ):
            panel_callbacks[title] = callback
            control = SelectablePanelControl(title)
            if control_holder is not None:
                control_holder.append(control)
            if include_in_focus_cycle:
                panel_focus_controls.append(control)
            content = Window(
                control,
                wrap_lines=True,
                height=height,
                always_hide_cursor=True,
            )
            pane = ScrollablePane(
                content,
                show_scrollbar=True,
                display_arrows=True,
                keep_cursor_visible=True,
            )

            def border_style() -> str:
                focused = (
                    self._app is not None
                    and self._app.layout.current_control is control
                )
                return (
                    "class:panel-border-focused"
                    if focused else "class:panel-border"
                )

            # Keep the focus signal on the box itself. In particular, do not
            # put a foreground colour on a parent container: that would tint
            # ordinary panel text and make the semantic colours noisy.
            edge = lambda char, **kwargs: Window(
                char=char, style=border_style, **kwargs
            )
            top = VSplit(
                [
                    edge("┌", width=1, height=1),
                    edge("─", height=1),
                    Label(
                        f" {title} ", style="class:panel-title",
                        dont_extend_width=True, wrap_lines=False,
                    ),
                    edge("─", height=1),
                    edge("┐", width=1, height=1),
                ],
                height=1,
            )
            middle = VSplit(
                [edge("│", width=1), pane, edge("│", width=1)], padding=0
            )
            bottom = VSplit(
                [
                    edge("└", width=1, height=1),
                    edge("─", height=1),
                    edge("┘", width=1, height=1),
                ],
                height=1,
            )
            return HSplit(
                [top, middle, bottom],
                # Give ordinary panels a weighted, stable viewport. Their
                # content may grow or shrink, but preview changes must not
                # resize neighboring panels.
                height=height if height is not None else Dimension(min=4, weight=1),
            )

        header = panel("Campaign / current plan", self._campaign_text, height=5)
        task_controls: list[Any] = []
        tasks = panel(
            "Active and queued tasks",
            self._tasks_text,
            control_holder=task_controls,
        )
        route_controls: list[Any] = []
        routes = panel("Routes", self._routes_text, control_holder=route_controls)
        claim_controls: list[Any] = []
        claims = panel(
            "Logical claim graph", self._claims_text, control_holder=claim_controls
        )
        failure_controls: list[Any] = []
        failures = panel(
            "Failure clusters", self._failures_text,
            control_holder=failure_controls,
        )
        budget = panel("Budget and human controls", self._budget_text)
        artifact_controls: list[Any] = []
        artifacts = panel(
            "Artifacts",
            self._artifacts_text,
            control_holder=artifact_controls,
        )
        task_preview_controls: list[Any] = []
        task_preview = panel(
            "Task preview (k: return)", self._task_preview_text,
            control_holder=task_preview_controls, include_in_focus_cycle=False,
        )
        route_preview_controls: list[Any] = []
        route_preview = panel(
            "Route preview (k: return)", self._route_preview_text,
            control_holder=route_preview_controls, include_in_focus_cycle=False,
        )
        claim_preview_controls: list[Any] = []
        claim_preview = panel(
            "Claim graph detail (k: return)", self._claim_preview_text,
            control_holder=claim_preview_controls, include_in_focus_cycle=False,
        )
        failure_preview_controls: list[Any] = []
        failure_preview = panel(
            "Failure-cluster preview (k: return)", self._failure_preview_text,
            control_holder=failure_preview_controls, include_in_focus_cycle=False,
        )
        artifact_preview_controls: list[Any] = []
        artifact_preview = panel(
            "Artifact preview (k: return)", self._artifact_preview_text,
            control_holder=artifact_preview_controls, include_in_focus_cycle=False,
        )
        activity = panel("Recent activity", self._activity_text, height=9)
        from prompt_toolkit.completion import Completion, Completer
        from prompt_toolkit.widgets import TextArea
        from shlex import split as shell_split

        command_descriptions = {
            "/run": "start a campaign or resume a paused campaign",
            "/pause": "request a safe pause at the next checkpoint",
            "/refresh": "refresh the panels",
            "/instruct": "save a researcher instruction; type /instruct TEXT for the quick path",
            "/route": "change a route status; live changes apply before the next epoch",
            "/setup": "run setup and start automatically; /setup manual waits",
            "/artifact": "browse artifacts: /artifact next|prev|open|close",
            "/help": "show slash-command help",
            "/model": "choose Codex reasoning strength: low to max",
            "/report": "generate the report and a continuation handoff brief",
            "/recover": "recover stale interrupted work and pause safely",
            "/budget": "adjust limits now, or schedule them before the next running epoch",
            "/quit": "exit the TUI",
        }
        class SlashCommandCompleter(Completer):
            def get_completions(self, document, complete_event):
                text = document.text_before_cursor
                if not text.startswith("/") or " " in text or "\t" in text:
                    return
                needle = text.casefold()
                for command, description in command_descriptions.items():
                    if command.casefold().startswith(needle):
                        yield Completion(
                            command,
                            start_position=-len(text),
                            display=command,
                            display_meta=description,
                        )

        command_completer = SlashCommandCompleter()
        command_input = TextArea(
            height=3,
            prompt="Chat > ",
            multiline=True,
            wrap_lines=True,
            scrollbar=False,
            completer=command_completer,
            complete_while_typing=True,
        )
        def complete_slash_commands(buffer) -> None:
            if buffer.text.startswith("/") and buffer.complete_state is None:
                buffer.start_completion(select_first=False)

        command_input.buffer.on_text_changed += complete_slash_commands
        footer = Window(
            FormattedTextControl(text=self._footer_text),
            height=1,
            style="class:footer",
            always_hide_cursor=True,
        )

        main_body = HSplit(
            [
                header,
                VSplit(
                    [
                        HSplit([tasks, routes], padding=0),
                        HSplit([claims, failures], padding=0),
                        HSplit([budget, artifacts], padding=0),
                    ],
                    padding=1,
                ),
                activity,
                footer,
                command_input,
            ]
        )
        kb = KeyBindings()

        def pause() -> None:
            campaign = self.store.latest_campaign()
            if not campaign:
                self.message = "No campaign exists"
            else:
                self.store.request_campaign_pause(
                    str(campaign["campaign_id"]),
                    reason="Pause requested from Ariadne TUI",
                    requested_by="tui-operator",
                )
                self.message = "Pause requested; waiting for a safe checkpoint"

        def instruction(event, args: list[str]) -> None:
            def save(text: str, route_id: str = "", audience: str = "researchers") -> None:
                if not text:
                    self.message = "Instruction was empty; nothing saved"
                    return
                campaign = self.store.latest_campaign()
                if not campaign:
                    self.message = "No campaign exists; instruction not saved"
                    return
                if audience not in {"all", "researchers", "sentinel", "verifiers"}:
                    self.message = "Invalid instruction audience"
                    return
                if route_id:
                    try:
                        self.store.get_route(route_id)
                    except KeyError:
                        self.message = f"Unknown route {route_id}"
                        return
                instruction_id = self.store.add_human_instruction(
                    campaign_id=str(campaign["campaign_id"]), instruction_text=text,
                    audience=audience, route_id=route_id or None, author="tui-operator",
                )
                self.message = f"Saved instruction {instruction_id}"

            # The common path never leaves the active application. Quotes in the
            # command composer are handled by shell_split before this point.
            if args:
                save(" ".join(args).strip())
                event.app.invalidate()
                return

            async def ask() -> None:
                def dialog() -> tuple[str, str, str]:
                    text = input("Human instruction: ").strip()
                    route_id = input("Optional route ID (blank = campaign-wide): ").strip()
                    audience = (input("Audience all/researchers/sentinel/verifiers [researchers]: ").strip().lower() or "researchers")
                    return text, route_id, audience
                text, route_id, audience = await run_in_terminal(dialog)
                save(text, route_id, audience)
                event.app.invalidate()
            event.app.create_background_task(ask())

        def interpret_chat_instruction(event, owner_message: str) -> None:
            campaign = self.store.latest_campaign()
            if not campaign:
                self.message = "No campaign exists; say ‘set up a new task’ first"
                event.app.invalidate()
                return
            campaign_id = str(campaign["campaign_id"])
            try:
                config = load_config(self.config_path)
            except (OSError, ValueError) as exc:
                self.message = f"Could not load interpreter configuration: {exc}"
                event.app.invalidate()
                return
            if "instruction_interpreter" not in config.roles:
                # Backward-compatible configurations preserve the unmodified
                # owner message rather than silently dropping it.
                instruction(event, [owner_message])
                return
            self.message = "Agent is interpreting your instruction"
            self.store.events.append(
                "chat_instruction_interpretation_started",
                {"campaign_id": campaign_id, "owner_message": owner_message},
            )

            def worker() -> None:
                try:
                    contract = read_json(self.store.paths.contract)
                    anchor = {
                        "title": contract.get("title", ""),
                        "statement": contract.get("statement", {}),
                        "success_criteria": contract.get("success_criteria", {}),
                    }
                    routes = self.store.list_routes(campaign_id)[-12:]
                    active = self.store.list_human_instructions(
                        campaign_id, active_only=True
                    )[-16:]
                    prompt = render_prompt(
                        "instruction_interpreter.md",
                        owner_message=owner_message,
                        problem_anchor=json.dumps(anchor, indent=2),
                        routes=json.dumps(routes, indent=2),
                        active_instructions=json.dumps(active, indent=2),
                    )
                    runner = AgentRunner(self.store, config, reporter=NullActivityReporter())
                    response = runner.call(AgentCall(
                        role="instruction_interpreter",
                        slot="chat-instruction-interpreter",
                        prompt=prompt,
                        project_root=self.project_root,
                        network_policy=config.roles["instruction_interpreter"].network_policy,
                        campaign_id=campaign_id,
                        epoch=int(campaign.get("epoch", 0)),
                        metadata={"task_summary": "Interpret an owner chat instruction without executing it"},
                    ))
                    interpretation = extract_json_object(response.text)
                    if bool(interpretation.get("clarification_needed", False)):
                        question = str(interpretation.get("clarifying_question", "")).strip()
                        self.message = "Agent needs clarification: " + (question or "please restate the request")
                        self.store.events.append(
                            "chat_instruction_clarification_needed",
                            {"campaign_id": campaign_id, "question": question},
                        )
                    else:
                        self.message = self._apply_interpreted_chat_instruction(
                            campaign_id=campaign_id,
                            owner_message=owner_message,
                            interpretation=interpretation,
                        )
                        if str(interpretation.get("action", "")).upper() == "CONTINUE_REPORT":
                            self._start_report_generation()
                except Exception as exc:
                    self.message = f"Instruction interpretation failed: {exc}"
                    self.store.events.append(
                        "chat_instruction_interpretation_failed",
                        {"campaign_id": campaign_id, "error": str(exc)},
                    )
                finally:
                    event.app.invalidate()

            thread = threading.Thread(target=worker, daemon=True)
            self._worker_threads.append(thread)
            thread.start()

        def setup(event, args: list[str]) -> None:
            if args and args != ["manual"]:
                self.message = "Usage: /setup [manual]"
                event.app.invalidate()
                return
            auto_start = args != ["manual"]

            async def ask() -> None:
                answers = await run_in_terminal(lambda: collect_setup_answers(project_root=self.project_root))
                self._start_setup_generation(answers, auto_start=auto_start)
                event.app.invalidate()
            event.app.create_background_task(ask())

        def route_control(event) -> None:
            async def ask() -> None:
                routes = self.store.list_routes()
                if not routes:
                    self.message = "No routes exist"
                    return
                listing = "\n".join(f"{r['route_id']}  {r['status']}  {r['title']}" for r in routes)
                def dialog() -> tuple[str, str, str]:
                    print("\n" + listing)
                    route_id = input("Route ID: ").strip()
                    status = input("Status ACTIVE/PARKED/OBSOLETE/NEEDS_HUMAN_IDEA/NEEDS_REPRESENTATION_CHANGE: ").strip().upper()
                    note = input("Decision note: ").strip() or "TUI route-control decision"
                    return route_id, status, note
                route_id, status, note = await run_in_terminal(dialog)
                allowed = {"ACTIVE", "PARKED", "OBSOLETE", "NEEDS_HUMAN_IDEA", "NEEDS_REPRESENTATION_CHANGE"}
                if status not in allowed:
                    self.message = "Invalid route status"
                    return
                try:
                    route = self.store.get_route(route_id)
                except KeyError:
                    self.message = f"Unknown route {route_id}"
                    return
                campaign = self.store.latest_campaign()
                if not campaign:
                    self.message = "No campaign exists for route control"
                    return
                try:
                    result = self.store.set_human_route_status(
                        campaign_id=str(campaign["campaign_id"]),
                        route_id=route_id,
                        status=status,
                        requested_by="tui-operator",
                        rationale=note,
                    )
                except ValueError as exc:
                    self.message = f"Route unchanged: {exc}"
                    return
                if result["application"] == "NEXT_EPOCH":
                    action = result["scheduled_action"]
                    self.message = (
                        f"Route change scheduled before epoch {action['apply_before_epoch']}: "
                        f"{route_id} -> {status}"
                    )
                else:
                    self.message = f"Route {route_id} set to {status}"
                event.app.invalidate()
            event.app.create_background_task(ask())

        def set_model_settings(model: str | None, strength: str | None) -> None:
            allowed = {"low", "medium", "high", "xhigh", "max"}
            if strength is not None:
                strength = strength.strip().lower()
                if strength not in allowed:
                    self.message = (
                        "Model strength must be low, medium, high, xhigh, or max"
                    )
                    return
            if model is not None:
                model = model.strip()
                if model.casefold() in {"default", "none"}:
                    model = ""
            try:
                text = self.config_path.read_text(encoding="utf-8")
                changed = 0
                if strength is not None:
                    pattern = re.compile(
                        r'(?m)^(\s*ARIADNE_CODEX_REASONING_EFFORT\s*=\s*)(["\']).*?\2\s*$'
                    )
                    text, count = pattern.subn(r'\1"' + strength + '"', text)
                    changed += count

                if model is not None:
                    model_pattern = re.compile(
                        r'(?m)^(\s*ARIADNE_CODEX_MODEL\s*=\s*)(["\']).*?\2\s*$'
                    )
                    text, count = model_pattern.subn(
                        lambda match: match.group(1) + json.dumps(model), text
                    )
                    changed += count
                    if count == 0:
                        env_table = re.compile(
                            r'(?ms)^(\[providers\.[^.]+\.env\]\n)(.*?)(?=^\[|\Z)'
                        )

                        def add_model(match):
                            body = match.group(2)
                            if "ARIADNE_CODEX_MODEL" in body:
                                return match.group(0)
                            return match.group(1) + body.rstrip() + (
                                "\nARIADNE_CODEX_MODEL = "
                                + json.dumps(model)
                                + "\n"
                            )

                        text, tables = env_table.subn(add_model, text)
                        changed += tables

                if changed == 0:
                    self.message = "No Codex provider settings found in the config"
                    return
                self.config_path.write_text(text, encoding="utf-8")
                parts = []
                if model is not None:
                    parts.append(f"model={model or 'default'}")
                if strength is not None:
                    parts.append(f"strength={strength}")
                self.message = "Codex settings updated: " + ", ".join(parts)
            except OSError as exc:
                self.message = f"Could not update Codex model settings: {exc}"

        def model_command(event, args: list[str]) -> None:
            allowed = {"low", "medium", "high", "xhigh", "max"}
            if len(args) > 2:
                self.message = "Usage: /model [MODEL [low|medium|high|xhigh|max]]"
                return
            if args:
                if len(args) == 1 and args[0].casefold() in allowed:
                    set_model_settings(None, args[0])
                else:
                    set_model_settings(args[0], args[1] if len(args) == 2 else None)
                event.app.invalidate()
                return

            async def ask() -> None:
                config_text = self.config_path.read_text(encoding="utf-8")
                model_match = re.search(
                    r'(?m)^\s*ARIADNE_CODEX_MODEL\s*=\s*["\'](.*?)["\']\s*$',
                    config_text,
                )
                strength_match = re.search(
                    r'(?m)^\s*ARIADNE_CODEX_REASONING_EFFORT\s*=\s*["\'](.*?)["\']\s*$',
                    config_text,
                )
                current_model = model_match.group(1) if model_match else ""
                current_strength = strength_match.group(1) if strength_match else "xhigh"
                model = await run_in_terminal(
                    lambda: input(
                        f"Codex model name [default={current_model or 'default'}]: "
                    ).strip()
                )
                strength = await run_in_terminal(
                    lambda: input(
                        "Reasoning strength low/medium/high/xhigh/max "
                        f"[{current_strength}]: "
                    ).strip() or current_strength
                )
                set_model_settings(model or "default", strength)
                event.app.invalidate()

            event.app.create_background_task(ask())

        def _apply_budget_limits(
            *, max_epochs: int, max_calls: int, max_cost_usd: float, reason: str
        ) -> None:
            campaign = self.store.latest_campaign()
            if not campaign:
                self.message = "No campaign exists to adjust"
                return
            try:
                updated = self.store.adjust_campaign_budget(
                    str(campaign["campaign_id"]),
                    max_epochs=max_epochs,
                    max_calls=max_calls,
                    max_cost_usd=max_cost_usd,
                    adjusted_by="tui-operator",
                    reason=reason.strip() or "TUI budget adjustment",
                )
            except (KeyError, ValueError) as exc:
                self.message = f"Budget unchanged: {exc}"
                return
            if updated.get("scheduled_action"):
                action = updated["scheduled_action"]
                limits = updated["requested_limits"]
                self.message = (
                    f"Budget scheduled before epoch {action['apply_before_epoch']}: "
                    f"epochs {limits['max_epochs']}, calls {limits['max_calls']}, "
                    f"cost ${float(limits['max_cost_usd']):.2f}"
                )
            else:
                self.message = (
                    f"Budget updated: epochs {updated['epoch']}/{updated['max_epochs']}, "
                    f"calls {updated['calls_used']}/{updated['max_calls']}, "
                    f"cost ${float(updated['cost_used']):.4f}/${float(updated['max_cost_usd']):.2f}"
                )

        def budget_command(event, args: list[str]) -> None:
            if args:
                if len(args) < 3:
                    self.message = "Usage: /budget MAX_EPOCHS MAX_CALLS MAX_COST_USD [REASON]"
                    return
                try:
                    _apply_budget_limits(
                        max_epochs=int(args[0]), max_calls=int(args[1]),
                        max_cost_usd=float(args[2]), reason=" ".join(args[3:]) or "TUI budget adjustment",
                    )
                except ValueError:
                    self.message = "Budget values must be: integer epochs, integer calls, numeric USD cost"
                event.app.invalidate()
                return

            async def ask() -> None:
                campaign = self.store.latest_campaign()
                if not campaign:
                    self.message = "No campaign exists to adjust"
                    event.app.invalidate()
                    return
                values = await run_in_terminal(
                    lambda: (
                        input(f"Maximum epochs [{campaign['max_epochs']}]: ").strip(),
                        input(f"Maximum provider calls [{campaign['max_calls']}]: ").strip(),
                        input(f"Maximum campaign cost in USD [{campaign['max_cost_usd']}]: ").strip(),
                        input("Reason for this budget adjustment: ").strip(),
                    )
                )
                try:
                    _apply_budget_limits(
                        max_epochs=int(values[0] or campaign["max_epochs"]),
                        max_calls=int(values[1] or campaign["max_calls"]),
                        max_cost_usd=float(values[2] or campaign["max_cost_usd"]),
                        reason=values[3] or "TUI budget adjustment",
                    )
                except ValueError:
                    self.message = "Budget values must be: integer epochs, integer calls, numeric USD cost"
                event.app.invalidate()

            event.app.create_background_task(ask())

        def execute(command: str, event) -> None:
            command = command.strip()
            if not command:
                return
            if not command.startswith("/"):
                translated = chat_intent_to_command(command)
                if translated is None:
                    interpret_chat_instruction(event, command)
                    return
                command = translated
            try:
                parts = shell_split(command)
            except ValueError as exc:
                self.message = f"Invalid command: {exc}"
                return
            name = parts[0].lower()
            args = parts[1:]
            if name in {"/quit", "/exit"}:
                event.app.exit(); return
            if name in {"/help", "/?"}:
                self.message = "Commands: /run /pause /recover /budget /refresh /instruct TEXT /route /setup /artifact next|prev|open|close /model /report /quit"
            elif name == "/refresh":
                self.message = "Refreshed"
            elif name == "/pause":
                pause()
            elif name == "/recover":
                campaign = self.store.latest_campaign()
                if not campaign:
                    self.message = "No campaign exists to recover"
                elif str(campaign.get("status")) != "RUNNING":
                    self.message = "Recovery is only needed for a stale RUNNING campaign"
                else:
                    recovered = self.store.recover_interrupted_campaign(
                        str(campaign["campaign_id"]), recovered_by="tui-operator"
                    )
                    self.message = (
                        f"Recovered stale work: {recovered['agent_runs']} agent run(s), "
                        f"{recovered['tasks']} task(s); campaign paused"
                    )
            elif name == "/budget":
                budget_command(event, args)
            elif name in {"/run", "/resume"}:
                self._launch_campaign()
            elif name == "/instruct":
                instruction(event, args)
            elif name == "/setup":
                setup(event, args)
            elif name == "/route":
                route_control(event)
            elif name == "/model":
                model_command(event, args)
            elif name == "/report":
                if len(args) > 1:
                    self.message = "Usage: /report [output-path]"
                else:
                    output = None
                    if args:
                        output = Path(args[0])
                        if not output.is_absolute():
                            output = self.project_root / output
                    self._start_report_generation(output)
            elif name == "/artifact":
                items = self._visible_artifacts()
                action = args[0].lower() if len(args) == 1 else ""
                if not items or action not in {"next", "prev", "previous", "open", "close", "list"}:
                    self.message = "Usage: /artifact next|prev|open|close"
                elif action in {"open"}:
                    _open_artifact_preview(event)
                elif action in {"close", "list"}:
                    _close_artifact_preview(event)
                elif action == "next":
                    _move_artifact(1, event)
                else:
                    _move_artifact(-1, event)
            else:
                self.message = f"Unknown command {name}; try /help"
            event.app.invalidate()

        def _task_items() -> list[dict[str, Any]]:
            campaign = self.store.latest_campaign()
            return self.store.list_tasks(str(campaign["campaign_id"])) if campaign else []

        def _focused(control_holder: list[Any]) -> bool:
            return (
                bool(control_holder)
                and self.preview_panel is None
                and self._app is not None
                and self._app.layout.current_control is control_holder[0]
            )

        def _move_selected(
            delta: int, items: list[dict[str, Any]], attribute: str,
            panel_title: str, control_holder: list[Any], render: Callable[[], str], event,
        ) -> None:
            if not items:
                return
            selected = getattr(self, attribute)
            current = len(items) - 1 if selected is None else selected
            selected = max(0, min(current + delta, len(items) - 1))
            setattr(self, attribute, selected)
            listing = render()
            with self._panel_cache_lock:
                self._panel_cache[panel_title] = listing
            control = control_holder[0]
            control.buffer.set_document(Document(text=listing), bypass_readonly=True)
            control._last_text = listing
            control.buffer.cursor_position = control.buffer.document.translate_row_col_to_index(selected, 0)
            event.app.invalidate()

        def _open_preview(kind: str, items: list[dict[str, Any]], attribute: str, controls: list[Any], event) -> None:
            if not items:
                self.message = f"No {kind} records available to preview"
                return
            if getattr(self, attribute) is None:
                setattr(self, attribute, len(items) - 1)
            self.preview_panel = kind
            preview_titles = {
                "task": "Task preview (k: return)",
                "route": "Route preview (k: return)",
                "claim": "Claim graph detail (k: return)",
                "failure": "Failure-cluster preview (k: return)",
                "artifact": "Artifact preview (k: return)",
            }
            if controls:
                # Prime the preview synchronously and reset the cursor before
                # focusing it. This prevents a previous deep scroll position
                # from carrying over when a new record is opened.
                preview_text = panel_callbacks[preview_titles[kind]]()
                with self._panel_cache_lock:
                    self._panel_cache[preview_titles[kind]] = preview_text
                controls[0].buffer.set_document(
                    Document(text=preview_text, cursor_position=0),
                    bypass_readonly=True,
                )
                controls[0]._last_text = preview_text
                event.app.layout.focus(controls[0])
            event.app.invalidate()

        def _close_preview(event) -> None:
            kind = self.preview_panel
            self.preview_panel = None
            source_controls = {
                "task": task_controls,
                "route": route_controls,
                "claim": claim_controls,
                "failure": failure_controls,
                "artifact": artifact_controls,
            }
            if kind in source_controls and source_controls[kind]:
                event.app.layout.focus(source_controls[kind][0])
            event.app.invalidate()

        def _task_panel_focused() -> bool:
            return _focused(task_controls)

        @kb.add("up", filter=Condition(_task_panel_focused))
        def _task_up(event) -> None:
            _move_selected(-1, _task_items(), "selected_task", "Active and queued tasks", task_controls, self._tasks_text, event)

        @kb.add("down", filter=Condition(_task_panel_focused))
        def _task_down(event) -> None:
            _move_selected(1, _task_items(), "selected_task", "Active and queued tasks", task_controls, self._tasks_text, event)

        @kb.add("j", filter=Condition(_task_panel_focused))
        def _task_open(event) -> None:
            _open_preview("task", _task_items(), "selected_task", task_preview_controls, event)

        def _route_panel_focused() -> bool:
            return _focused(route_controls)

        @kb.add("up", filter=Condition(_route_panel_focused))
        def _route_up(event) -> None:
            _move_selected(-1, self.store.list_routes(), "selected_route", "Routes", route_controls, self._routes_text, event)

        @kb.add("down", filter=Condition(_route_panel_focused))
        def _route_down(event) -> None:
            _move_selected(1, self.store.list_routes(), "selected_route", "Routes", route_controls, self._routes_text, event)

        @kb.add("j", filter=Condition(_route_panel_focused))
        def _route_open(event) -> None:
            _open_preview("route", self.store.list_routes(), "selected_route", route_preview_controls, event)

        def _claim_panel_focused() -> bool:
            return _focused(claim_controls)

        @kb.add("up", filter=Condition(_claim_panel_focused))
        def _claim_up(event) -> None:
            _move_selected(
                -1, self.store.list_claims(), "selected_claim", "Logical claim graph",
                claim_controls, self._claims_text, event,
            )

        @kb.add("down", filter=Condition(_claim_panel_focused))
        def _claim_down(event) -> None:
            _move_selected(
                1, self.store.list_claims(), "selected_claim", "Logical claim graph",
                claim_controls, self._claims_text, event,
            )

        @kb.add("j", filter=Condition(_claim_panel_focused))
        def _claim_open(event) -> None:
            _open_preview(
                "claim", self.store.list_claims(), "selected_claim",
                claim_preview_controls, event,
            )

        def _failure_panel_focused() -> bool:
            return _focused(failure_controls)

        @kb.add("up", filter=Condition(_failure_panel_focused))
        def _failure_up(event) -> None:
            _move_selected(-1, self.store.list_failures(), "selected_failure", "Failure clusters", failure_controls, self._failures_text, event)

        @kb.add("down", filter=Condition(_failure_panel_focused))
        def _failure_down(event) -> None:
            _move_selected(1, self.store.list_failures(), "selected_failure", "Failure clusters", failure_controls, self._failures_text, event)

        @kb.add("j", filter=Condition(_failure_panel_focused))
        def _failure_open(event) -> None:
            _open_preview("failure", self.store.list_failures(), "selected_failure", failure_preview_controls, event)

        def _artifact_panel_focused() -> bool:
            return _focused(artifact_controls)

        def _move_artifact(delta: int, event) -> None:
            _move_selected(delta, self._visible_artifacts(), "selected_artifact", "Artifacts", artifact_controls, self._artifacts_text, event)

        def _open_artifact_preview(event) -> None:
            _open_preview("artifact", self._visible_artifacts(), "selected_artifact", artifact_preview_controls, event)

        def _close_artifact_preview(event) -> None:
            _close_preview(event)

        @kb.add("up", filter=Condition(_artifact_panel_focused))
        def _artifact_up(event) -> None:
            _move_artifact(-1, event)

        @kb.add("down", filter=Condition(_artifact_panel_focused))
        def _artifact_down(event) -> None:
            _move_artifact(1, event)

        @kb.add("pageup", filter=Condition(_artifact_panel_focused))
        def _artifact_page_up(event) -> None:
            artifact_controls[0].buffer.cursor_up(count=20)
            event.app.invalidate()

        @kb.add("pagedown", filter=Condition(_artifact_panel_focused))
        def _artifact_page_down(event) -> None:
            artifact_controls[0].buffer.cursor_down(count=20)
            event.app.invalidate()

        @kb.add("j", filter=Condition(_artifact_panel_focused))
        def _artifact_open(event) -> None:
            _open_artifact_preview(event)

        @kb.add("k", filter=Condition(lambda: self.preview_panel is not None))
        @kb.add("escape", filter=Condition(lambda: self.preview_panel is not None))
        def _preview_close(event) -> None:
            _close_preview(event)

        def _can_cycle_panels() -> bool:
            return (
                self.preview_panel is None
                and self._app is not None
                and self._app.layout.current_control is not command_input.control
            )

        def _cycle_panel(delta: int, event) -> None:
            if not panel_focus_controls:
                return
            current = event.app.layout.current_control
            try:
                index = panel_focus_controls.index(current)
            except ValueError:
                index = -1 if delta > 0 else 0
            event.app.layout.focus(
                panel_focus_controls[(index + delta) % len(panel_focus_controls)]
            )
            event.app.invalidate()

        @kb.add("right", filter=Condition(_can_cycle_panels))
        @kb.add("]", filter=Condition(_can_cycle_panels))
        def _focus_next_panel(event) -> None:
            _cycle_panel(1, event)

        @kb.add("left", filter=Condition(_can_cycle_panels))
        @kb.add("[", filter=Condition(_can_cycle_panels))
        def _focus_previous_panel(event) -> None:
            _cycle_panel(-1, event)

        @kb.add("/", eager=True)
        def _start_command_menu(event) -> None:
            # Panel clicks intentionally move focus for scrolling/selection.
            # A slash always returns to the command composer so typing cannot
            # disappear into a read-only panel buffer.
            event.app.layout.focus(command_input)
            command_input.buffer.insert_text("/")
            command_input.buffer.start_completion(select_first=False)

        @kb.add("tab")
        def _focus_command_input(event) -> None:
            event.app.layout.focus(command_input)

        @kb.add("c-c")
        def _quit(event) -> None:
            event.app.exit()

        @kb.add("enter")
        def _submit(event) -> None:
            command = command_input.text
            command_input.text = ""
            execute(command, event)

        style = Style.from_dict(
            {
                "panel-border": "fg:#5f6670",
                "panel-border-focused": "fg:#c2c7cf",
                "panel-title": "bold fg:#a8adb7",
                "footer": "reverse",
                # Codex-like semantic terminal palette.
                "panel-selected": "bold fg:#ffffff bg:#243447",
                "panel-active": "fg:#8b929a",
                "panel-active-wave-0": "fg:#62676d",
                "panel-active-wave-1": "fg:#747a81",
                "panel-active-wave-2": "fg:#899099",
                "panel-active-wave-3": "fg:#a2aab3",
                "panel-active-wave-4": "fg:#c6cdd5",
                "panel-selected-wave-0": "fg:#7d858e bg:#243447",
                "panel-selected-wave-1": "fg:#929aa4 bg:#243447",
                "panel-selected-wave-2": "fg:#aab2bb bg:#243447",
                "panel-selected-wave-3": "fg:#c3cad1 bg:#243447",
                "panel-selected-wave-4": "fg:#f0f3f5 bg:#243447",
                "panel-success": "fg:#10a37f",
                "panel-warning": "fg:#f5c242",
                "panel-failure": "fg:#ff8a8a",
                "panel-label": "bold fg:#8e8ea0",
                "completion-fallback": "bg:#243447",
                "completion-command": "bold fg:#d7af5f bg:#243447",
                "completion-meta": "fg:#d7d7d7 bg:#243447",
            }
        )
        def slash_menu_text():
            text = command_input.text
            if not text.startswith("/") or " " in text or "\t" in text:
                return []
            # Native completions provide keyboard navigation. This fallback
            # keeps the menu visible immediately after the slash, even while
            # the asynchronous completer is still starting.
            matches = [
                (command, description)
                for command, description in command_descriptions.items()
                if command.casefold().startswith(text.casefold())
            ]
            if not matches:
                return []
            fragments = [("class:completion-fallback", "Commands\n")]
            for command, description in matches:
                fragments.append(("class:completion-command", f"  {command:<12} "))
                fragments.append(("class:completion-meta", f"{description}\n"))
            return fragments

        body = FloatContainer(
            main_body,
            [
                Float(
                    left=1, right=1, top=1, bottom=4,
                    content=ConditionalContainer(
                        content=task_preview,
                        filter=Condition(lambda: self.preview_panel == "task"),
                    ),
                ),
                Float(
                    left=1, right=1, top=1, bottom=4,
                    content=ConditionalContainer(
                        content=route_preview,
                        filter=Condition(lambda: self.preview_panel == "route"),
                    ),
                ),
                Float(
                    left=1, right=1, top=1, bottom=4,
                    content=ConditionalContainer(
                        content=claim_preview,
                        filter=Condition(lambda: self.preview_panel == "claim"),
                    ),
                ),
                Float(
                    left=1, right=1, top=1, bottom=4,
                    content=ConditionalContainer(
                        content=failure_preview,
                        filter=Condition(lambda: self.preview_panel == "failure"),
                    ),
                ),
                Float(
                    left=1, right=1, top=1, bottom=4,
                    content=ConditionalContainer(
                        content=artifact_preview,
                        filter=Condition(lambda: self.preview_panel == "artifact"),
                    ),
                ),
                Float(
                    xcursor=True,
                    ycursor=True,
                    transparent=True,
                    content=Window(
                        FormattedTextControl(text=slash_menu_text),
                        height=Dimension(max=12),
                        style="class:completion-fallback",
                    ),
                ),
                Float(
                    xcursor=True,
                    ycursor=True,
                    transparent=True,
                    content=CompletionsMenu(
                        max_height=12,
                        scroll_offset=1,
                        extra_filter=has_focus(command_input.buffer),
                    ),
                )
            ],
        )
        app = Application(
            layout=Layout(body, focused_element=command_input),
            key_bindings=kb,
            full_screen=True,
            mouse_support=True,
            # This redraw clock drives only the first-eight-character
            # activity shimmer. Store polling remains on the one-second
            # panel-refresh thread.
            refresh_interval=1 / 24,
            style=style,
        )
        self._app = app

        def start_panel_refresh() -> None:
            if self._panel_refresh_thread and self._panel_refresh_thread.is_alive():
                return

            def refresh() -> None:
                while not self._panel_refresh_stop.is_set():
                    ordered_callbacks = sorted(
                        panel_callbacks.items(),
                        key=lambda item: (item[0] != "Recent activity", item[0]),
                    )
                    for key, callback in ordered_callbacks:
                        try:
                            value = callback()
                        except Exception as exc:
                            value = f"Panel unavailable: {type(exc).__name__}: {exc}"
                        with self._panel_cache_lock:
                            self._panel_cache[key] = value
                    if self._app is not None:
                        self._app.invalidate()
                    self._panel_refresh_stop.wait(1.0)

            self._panel_refresh_thread = threading.Thread(
                target=refresh, name="ariadne-tui-refresh", daemon=True
            )
            self._panel_refresh_thread.start()

        app.pre_run_callables.append(start_panel_refresh)
        if self.setup_answers is not None:
            app.pre_run_callables.append(
                lambda: self._start_setup_generation(self.setup_answers)
            )
        elif self.resume_on_start:
            app.pre_run_callables.append(self._launch_campaign)
        try:
            app.run()
        finally:
            self._panel_refresh_stop.set()
            self._stop_campaign_process_on_exit()

    def _contract(self) -> dict[str, Any] | None:
        try:
            if self.store.paths.contract.exists():
                contract = read_json(self.store.paths.contract)
                validate_contract(contract)
                return contract
        except Exception:
            return None
        return None

    @classmethod
    def _display_value(cls, value: Any) -> str:
        if isinstance(value, dict):
            return ", ".join(
                f"{key}: {cls._display_value(item)}" for key, item in value.items()
            )
        if isinstance(value, (list, tuple, set)):
            return "; ".join(cls._display_value(item) for item in value)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value)

    @staticmethod
    def _clean_activity(value: Any) -> str:
        text = str(value)
        text = text.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")
        text = text.replace('\\"', '"').replace('"', "")
        text = " ".join(text.split())
        return text

    @staticmethod
    def _elapsed_label(started_at: Any) -> str:
        try:
            timestamp = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return f"{max(0, int((datetime.now(timezone.utc) - timestamp).total_seconds()))}s"
        except (TypeError, ValueError, OverflowError):
            return "?s"

    @classmethod
    def _clip(cls, value: Any, limit: int = 180) -> str:
        text = " ".join(cls._display_value(value).split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    @staticmethod
    def _active_shimmer_fragments(
        line: str, *, selected: bool = False, phase: int | None = None
    ) -> list[tuple[str, str]]:
        """Render a low-key grayscale pulse that visibly travels left to right."""
        if not line:
            return []
        phase = int(time.monotonic() * 24) if phase is None else phase
        cycle = 16
        animated_width = min(8, len(line))
        fragments: list[tuple[str, str]] = []
        prefix = "class:panel-selected-wave-" if selected else "class:panel-active-wave-"
        baseline = 3 if selected else 2
        for position in range(animated_width):
            distance = abs(((position - phase + cycle // 2) % cycle) - cycle // 2)
            wave_brightness = max(0, 4 - distance)
            # The amplitude falls to zero at the cutoff, so character eight
            # already has the static gray and the rest of the row is seamless.
            taper = (
                (animated_width - position - 1) / max(1, animated_width - 1)
            )
            brightness = int(round(baseline + (wave_brightness - baseline) * taper))
            fragments.append((prefix + str(brightness), line[position: position + 1]))
        if animated_width < len(line):
            fragments.append(
                (
                    "class:panel-selected-wave-3" if selected else "class:panel-active",
                    line[animated_width:],
                )
            )
        return fragments

    @staticmethod
    def _status_indicator(status: Any) -> str:
        normalized = str(status or "").upper()
        if normalized in {"RUNNING", "ACTIVE"}:
            # Panel data is refreshed at roughly one-second intervals. A
            # non-multiple of four prevents every refresh from landing on the
            # same spinner frame, and forces the shimmer lexer to redraw.
            return ("◐", "◓", "◑", "◒")[int(time.monotonic() * 3) % 4]
        if normalized in {"QUEUED", "PENDING", "PROPOSED"}:
            return "○"
        if normalized in {"COMPLETED", "SUCCEEDED", "VERIFIED", "PROVEN", "COMPLETE_PROOF_CANDIDATE"}:
            return "✓"
        if normalized in {"FAILED", "ERROR", "CRASHED", "OBSOLETE", "METHOD_FAILED"}:
            return "×"
        if normalized in {"PAUSED_HUMAN", "NEEDS_HUMAN_IDEA", "NEEDS_REPRESENTATION_CHANGE"}:
            return "Ⅱ"
        if normalized in {"BLOCKED", "STAGNANT", "BUDGET_EXHAUSTED"}:
            return "!"
        return "·"

    _VISIBLE_ARTIFACT_KINDS = frozenset(
        {
            "structured_research_outcome",
            "partial_result",
            "agent_failure",
            "proof_candidate",
            "proof_candidate_latex",
            "counterexample_candidate",
            "local_audit",
            "global_audit",
            "local_counterexample_audit",
            "global_counterexample_audit",
            "CONCEPTUAL_PIVOT",
            "LITERATURE_EARLY_STOP_ARBITRATION",
            "STAGNATION_FREEZE",
            "CONCEPTUAL_STALL_HUMAN",
            "formal_verification_manifest",
            "numerical_experiment_plan",
            "hpc_resource_request",
            "numerical_evidence",
            "agent_audited_proof_report",
            "strongest_partial_result",
            "result_synthesis_no_proposal",
        }
    )

    @staticmethod
    def _clip_multiline(value: Any, limit: int = 1000) -> str:
        text = str(value)
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _setup_runs(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        roles = {"contract_author", "literature_author"}
        runs = [
            run
            for run in self.store.list_agent_runs(active_only=active_only)
            if run.get("campaign_id") is None and run.get("role") in roles
        ]
        if not runs and active_only and self._setup_phase:
            role, summary = self._setup_phase
            elapsed = (
                f" ({int(time.monotonic() - self._setup_started_at)}s)"
                if self._setup_started_at is not None
                else ""
            )
            runs = [{
                "role": role,
                "status": "RUNNING",
                "task_summary": summary + elapsed,
            }]
        return runs

    def _campaign_text(self) -> str:
        campaign = self.store.latest_campaign()
        contract = self._contract()
        target = "No contract. Use /setup to run the interactive setup wizard."
        if contract:
            statement = contract.get("statement", {})
            target = statement.get("text", statement) if isinstance(statement, dict) else statement
        title = str(contract.get("title", self.project_root.name)) if contract else self.project_root.name
        tags = contract.get("tags", []) if contract else []
        tag_text = ", ".join(str(tag) for tag in tags if str(tag).strip())
        project_label = f"{title} [{tag_text}]" if tag_text else title
        if not campaign:
            setup_runs = self._setup_runs(active_only=True)
            if setup_runs:
                current = "; ".join(
                    f"{self._status_indicator(run.get('status'))} {run['role']}: {self._clip(run['task_summary'], 110)}"
                    for run in setup_runs
                )
                return (
                    f"Project: {project_label} | setup in progress\n"
                    f"Target: {self._clip(target, 300)}\n"
                    f"Current setup agents: {current}"
                )
            return f"Project: {project_label}\nTarget: {self._clip(target, 300)}\nCampaign: none"
        active = self.store.list_agent_runs(str(campaign["campaign_id"]), active_only=True)
        current = "; ".join(
            self._active_run_label(run)
            for run in active
        ) or "No agent call active"
        return (
            f"Project: {project_label} | {campaign['campaign_id']} | mode={campaign['mode']} | "
            f"state={self._status_indicator(campaign.get('status'))} | epoch={campaign['epoch']}\n"
            f"Target: {self._clip(target, 300)}\nCurrent: {current}"
        )

    def _route_and_claim(
        self, route_id: str | None
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if not route_id:
            return None, None
        try:
            route = self.store.get_route(route_id)
        except KeyError:
            return None, None
        claim_id = route.get("target_claim_id")
        if not claim_id:
            return route, None
        try:
            return route, self.store.get_claim(str(claim_id))
        except KeyError:
            return route, None

    def _route_label(self, route_id: str | None, limit: int = 34) -> str:
        route, _ = self._route_and_claim(route_id)
        if route is None:
            return "unassigned route"
        return self._clip(route.get("title") or route.get("route_id"), limit)

    def _claim_label(self, route_id: str | None, limit: int = 64) -> str:
        _, claim = self._route_and_claim(route_id)
        if claim is None:
            return "no target claim"
        return self._clip(claim.get("statement") or claim.get("claim_id"), limit)

    def _active_run_label(self, run: dict[str, Any]) -> str:
        route = self._route_label(run.get("route_id"), 28)
        claim = self._claim_label(run.get("route_id"), 56)
        return (
            f"{run['slot']} [{run['role']}] → {route} → {claim} "
            f"{self._elapsed_label(run.get('started_at'))}"
        )

    def _tasks_text(self) -> str:
        campaign = self.store.latest_campaign()
        if not campaign:
            setup_runs = self._setup_runs(active_only=True)
            if setup_runs:
                return "\n".join(
                    f"{self._status_indicator(run.get('status')):<2} {run['role']}"
                    for run in setup_runs
                )
            return "No campaign task queue."
        tasks = self.store.list_tasks(str(campaign["campaign_id"]))
        if not tasks:
            return "No queued tasks yet."
        if self.selected_task is None:
            self.selected_task = len(tasks) - 1
        self.selected_task = min(self.selected_task, len(tasks) - 1)
        listing = "\n".join(
            ("> " if i == self.selected_task else "  ")
            + f"{self._status_indicator(t.get('status')):<2} {t['slot']:<14} → {self._route_label(t.get('route_id'), 30)}"
            for i, t in enumerate(tasks)
        )
        return listing + "\n\n↑/↓ browse  •  j route/claim preview"

    def _task_preview_text(self) -> str:
        campaign = self.store.latest_campaign()
        tasks = self.store.list_tasks(str(campaign["campaign_id"])) if campaign else []
        if not tasks:
            return "No campaign task is available. Press k to return."
        if self.selected_task is None:
            self.selected_task = len(tasks) - 1
        self.selected_task = min(self.selected_task, len(tasks) - 1)
        task = tasks[self.selected_task]
        route, claim = self._route_and_claim(task.get("route_id"))
        lines = [
            "Selected: Task",
            "",
            f"Status: {task.get('status', '')}",
            f"Agent: {task.get('slot', '')} [{task.get('role', '')}]",
            f"Epoch: {task.get('epoch', '')}",
            f"Summary: {task.get('summary', '')}",
            "",
            "Route:",
        ]
        if route is None:
            lines.append("  No route assigned.")
        else:
            lines.extend(
                [
                    f"  ID: {route.get('route_id', '')}",
                    f"  Title: {route.get('title', '')}",
                    f"  Status: {route.get('status', '')}",
                    f"  Method: {route.get('method_family', '')}",
                    f"  Representation: {route.get('representation', '')}",
                    f"  Key lemma: {route.get('key_lemma', '')}",
                ]
            )
        lines.extend(["", "Target claim:"])
        if claim is None:
            lines.append("  No target claim recorded.")
        else:
            lines.extend(
                [
                    f"  ID: {claim.get('claim_id', '')}",
                    f"  Status: {claim.get('status', '')}",
                    f"  Statement: {claim.get('statement', '')}",
                ]
            )
        lines.append("\nPress k or Esc to return to the list.")
        return "\n".join(lines)

    def _routes_text(self) -> str:
        routes = self.store.list_routes()
        if not routes:
            return "No routes declared."
        if self.selected_route is None:
            self.selected_route = len(routes) - 1
        self.selected_route = min(self.selected_route, len(routes) - 1)
        listing = "\n".join(
            ("> " if i == self.selected_route else "  ")
            + f"{self._status_indicator(r.get('status')):<2} {self._clip(r['title'], 38)}"
            for i, r in enumerate(routes)
        )
        return listing + "\n\n↑/↓ browse  •  j route/claim preview"

    def _route_preview_text(self) -> str:
        routes = self.store.list_routes()
        if not routes:
            return "No route is available. Press k to return."
        if self.selected_route is None:
            self.selected_route = len(routes) - 1
        self.selected_route = min(self.selected_route, len(routes) - 1)
        route = routes[self.selected_route]
        _, claim = self._route_and_claim(route.get("route_id"))
        lines = [
            "Selected: Route",
            "",
            f"ID: {route.get('route_id', '')}",
            f"Title: {route.get('title', '')}",
            f"Status: {route.get('status', '')}",
            f"Owner agent: {route.get('owner_slot', '')}",
            f"Method: {route.get('method_family', '')}",
            f"Representation: {route.get('representation', '')}",
            f"Key lemma: {route.get('key_lemma', '')}",
            f"Central mechanism: {route.get('central_mechanism', '')}",
            f"Decisive test: {route.get('decisive_test', '')}",
            "",
            "Target claim:",
        ]
        if claim is None:
            lines.append("  No target claim recorded.")
        else:
            lines.extend(
                [
                    f"  ID: {claim.get('claim_id', '')}",
                    f"  Status: {claim.get('status', '')}",
                    f"  Statement: {claim.get('statement', '')}",
                ]
            )
        lines.append("\nPress k or Esc to return to the list.")
        return "\n".join(lines)

    def _claims_text(self) -> str:
        """A compact logical index, deliberately separate from artifact files."""
        claims = self.store.list_claims()
        if not claims:
            return "No logical claims recorded."
        if self.selected_claim is None:
            self.selected_claim = len(claims) - 1
        self.selected_claim = min(max(0, self.selected_claim), len(claims) - 1)
        window_start = max(0, min(self.selected_claim - 6, len(claims) - 7))
        visible = claims[window_start: window_start + 7]
        incoming: dict[str, list[dict[str, Any]]] = {}
        outgoing: dict[str, list[dict[str, Any]]] = {}
        for edge in self.store.list_claim_edges():
            outgoing.setdefault(str(edge["predecessor_id"]), []).append(edge)
            incoming.setdefault(str(edge["successor_id"]), []).append(edge)

        lines: list[str] = []
        for index, claim in enumerate(visible):
            claim_id = str(claim["claim_id"])
            supports = outgoing.get(claim_id, [])
            supported_by = incoming.get(claim_id, [])
            if supports:
                relation = " → " + ", ".join(
                    str(edge["edge_type"]).replace("_", " ")
                    for edge in supports[:2]
                )
            elif supported_by:
                relation = f" ← {len(supported_by)} support"
                if len(supported_by) != 1:
                    relation += "s"
            else:
                relation = ""
            prefix = "> " if window_start + index == self.selected_claim else "  "
            category = str(claim.get("criticality", "supporting"))
            lines.append(
                prefix
                + f"• {category:<10} {self._clip(claim.get('statement') or claim_id, 50)}{relation}"
            )
        nl = chr(10)
        return (
            nl.join(lines)
            + nl + nl + "Logical propositions and dependencies only; evidence files are in Artifacts."
            + nl + "↑/↓ browse  •  j graph detail"
        )

    def _claim_preview_text(self) -> str:
        claims = self.store.list_claims()
        if not claims:
            return "No logical claim is available. Press k to return."
        if self.selected_claim is None:
            self.selected_claim = len(claims) - 1
        self.selected_claim = min(max(0, self.selected_claim), len(claims) - 1)
        claim = claims[self.selected_claim]
        by_id = {str(item["claim_id"]): item for item in claims}
        claim_id = str(claim["claim_id"])
        edges = self.store.list_claim_edges()
        incoming = [
            edge for edge in edges if str(edge["successor_id"]) == claim_id
        ]
        outgoing = [
            edge for edge in edges if str(edge["predecessor_id"]) == claim_id
        ]
        lines = [
            "Selected: Logical claim",
            "",
            f"ID: {claim_id}",
            f"Status: {claim.get('status', '')}",
            f"Criticality: {claim.get('criticality', '')}",
            f"Scope: {claim.get('scope', '')}",
            f"Source: {claim.get('source', '')}",
            f"Statement: {claim.get('statement', '')}",
            "Assumptions: " + (
                "; ".join(str(item) for item in claim.get("assumptions", []))
                or "none recorded"
            ),
            "",
            "Claim graph:",
        ]
        if incoming:
            lines.append("  Supported by:")
            lines.extend(
                f"    ← [{edge['edge_type']}] {edge['predecessor_id']}: "
                f"{self._clip(by_id.get(str(edge['predecessor_id']), {}).get('statement', ''), 120)}"
                for edge in incoming
            )
        if outgoing:
            lines.append("  Supports:")
            lines.extend(
                f"    → [{edge['edge_type']}] {edge['successor_id']}: "
                f"{self._clip(by_id.get(str(edge['successor_id']), {}).get('statement', ''), 120)}"
                for edge in outgoing
            )
        if not incoming and not outgoing:
            lines.append("  No recorded logical dependencies.")
        lines.append("")
        lines.append("Underlying evidence and full research notes are in the Artifacts panel.")
        lines.append("")
        lines.append("Press k or Esc to return to the list.")
        return chr(10).join(lines)

    def _failures_text(self) -> str:
        failures = self.store.list_failures()
        if not failures:
            return "No compressed failure clusters."
        if self.selected_failure is None:
            self.selected_failure = 0
        self.selected_failure = min(self.selected_failure, len(failures) - 1)
        listing = "\n".join(
            ("> " if i == self.selected_failure else "  ")
            + f"• {f['failure_id']} / {f['failure_class']}"
            for i, f in enumerate(failures)
        )
        return listing + "\n\n↑/↓ browse  •  j preview"

    def _failure_preview_text(self) -> str:
        failures = self.store.list_failures()
        if not failures:
            return "No failure cluster is available. Press k to return."
        if self.selected_failure is None:
            self.selected_failure = 0
        self.selected_failure = min(self.selected_failure, len(failures) - 1)
        return self._record_preview("Failure cluster", failures[self.selected_failure])

    def _record_preview(self, title: str, record: dict[str, Any]) -> str:
        """Render a stored operational record as a readable, scrollable detail view."""
        return (
            f"Selected: {title}\n\n"
            + self._format_json_preview(json.dumps(record, default=str))
            + "\n\nPress k or Esc to return to the list."
        )

    def _model_settings(self) -> tuple[str, str]:
        model = "gpt-5.6-sol"
        strength = "xhigh"
        try:
            config = load_config(self.config_path)
            for provider in config.providers.values():
                if provider.kind != "command" or not provider.command:
                    continue
                if provider.command[0] != "ariadne-codex-provider":
                    continue
                model = provider.env.get("ARIADNE_CODEX_MODEL", model) or model
                strength = (
                    provider.env.get("ARIADNE_CODEX_REASONING_EFFORT", strength)
                    or strength
                )
                break
        except (OSError, KeyError, TypeError, ValueError):
            pass
        return model, strength

    def _budget_limits(self) -> tuple[int, int, float]:
        # During first-run setup the campaign does not exist yet, but the
        # operator has already selected these limits. Prefer those answers so
        # the panel reflects the values currently being used by setup.
        if self.setup_answers is not None:
            return (
                int(self.setup_answers.max_epochs),
                int(self.setup_answers.max_calls),
                float(self.setup_answers.max_cost_usd),
            )
        try:
            budget = load_config(self.config_path).budget
            return (budget.max_epochs, budget.max_calls, budget.max_cost_usd)
        except (OSError, KeyError, TypeError, ValueError):
            return (3, 30, 25.0)

    def _budget_text(self) -> str:
        campaign = self.store.latest_campaign()
        if not campaign:
            max_epochs, max_calls, max_cost = self._budget_limits()
            return (
                f"Configured budget (campaign not started)\n"
                f"Calls: 0/{max_calls} | Cost: $0.0000/${max_cost:.2f} | "
                f"Epoch: 0/{max_epochs}\n"
                f"Message: {self._clip(self.message, 120)}"
            )
        control = self.store.get_campaign_control(str(campaign["campaign_id"]))
        instructions = self.store.list_human_instructions(
            str(campaign["campaign_id"]), active_only=True
        )
        pause = "YES" if control.get("pause_requested") else "no"
        scheduled = self.store.list_scheduled_campaign_actions(
            str(campaign["campaign_id"]), pending_only=True
        )
        return (
            f"Calls: {campaign['calls_used']}/{campaign['max_calls']}\n"
            f"Cost: ${float(campaign['cost_used']):.4f}/${float(campaign['max_cost_usd']):.2f}\n"
            f"Epoch: {campaign['epoch']}/{campaign['max_epochs']} | pause pending: {pause}\n"
            f"Scheduled next-epoch controls: {len(scheduled)} | Active instructions: {len(instructions)}\n"
            f"Message: {self._clip(self.message, 120)}"
        )

    def _visible_artifacts(self, limit: int = 200) -> list[dict[str, Any]]:
        return [
            item
            for item in self.store.list_artifacts(limit=limit)
            if item.get("kind") in self._VISIBLE_ARTIFACT_KINDS
        ]

    def _artifacts_text(self) -> str:
        """Return the compact, scrollable artifact browser."""
        items = self._visible_artifacts()
        if not items:
            return "No artifacts."
        if self.selected_artifact is None:
            self.selected_artifact = len(items) - 1
        self.selected_artifact = min(self.selected_artifact, len(items) - 1)
        listing = "\n".join(
            ("> " if i == self.selected_artifact else "  ")
            + self._artifact_list_label(item)
            for i, item in enumerate(items)
        )
        return listing + "\n\n↑/↓ browse  •  j preview  •  ←/→ or [/] focus panel  •  selected item is marked >"

    def _artifact_preview_text(self) -> str:
        """Return the complete selected artifact for the dedicated preview overlay."""
        items = self._visible_artifacts()
        if not items:
            return "No artifacts. Press k to return."
        if self.selected_artifact is None:
            self.selected_artifact = len(items) - 1
        self.selected_artifact = min(self.selected_artifact, len(items) - 1)
        selected = items[self.selected_artifact]
        raw_preview = ""
        path = self.store.paths.root / str(selected["relative_path"])
        if path.exists() and path.suffix.lower() in {".md", ".txt", ".json", ".tex"}:
            try:
                raw_preview = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw_preview = "[preview unavailable]"
        else:
            raw_preview = "[This artifact has no text preview. Open the artifact file to inspect it.]"

        structured_json = self._extract_fenced_json(raw_preview)
        if path.suffix.lower() == ".json" or structured_json is not None:
            preview = self._format_json_preview(structured_json or raw_preview)
        else:
            preview = self._clip_multiline(raw_preview, 262_144)
            if len(raw_preview) > len(preview):
                preview += "\n\n[Preview limited to 256 KiB; open the artifact file for the remainder.]"
        graph = self._artifact_graph_text(selected)
        return (
            "Selected: " + self._artifact_summary(selected, raw_preview)
            + "\nStored at: " + str(selected["relative_path"])
            + "\n\n" + graph
            + "\n\n" + preview + "\n\nPress k or Esc to return to the artifact list."
        )

    def _artifact_graph_text(self, selected: dict[str, Any]) -> str:
        """Render a compact Obsidian-style one-hop provenance graph for a preview."""
        selected_id = str(selected.get("artifact_id", ""))
        neighbors = self.store.list_artifact_neighbors([selected_id], limit=24)
        center = f"[{selected_id}] {self._clip(selected.get('kind', 'artifact'), 36)}"
        lines = ["Artifact graph · one-hop provenance", ""]
        incoming: list[str] = []
        outgoing: list[str] = []
        for neighbor in neighbors:
            node = (
                f"[{neighbor.get('artifact_id', '')}] "
                f"{self._clip(neighbor.get('kind', 'artifact'), 26)}"
            )
            relation_labels = [str(item) for item in neighbor.get("relations", [])]
            incoming_labels = [
                item.removeprefix("incoming:")
                for item in relation_labels
                if item.startswith("incoming:")
            ]
            outgoing_labels = [
                item.removeprefix("outgoing:")
                for item in relation_labels
                if item.startswith("outgoing:")
            ]
            if incoming_labels:
                incoming.append(f"  {node} ──{', '.join(incoming_labels)}──▶")
            if outgoing_labels:
                outgoing.append(f"  ◀──{', '.join(outgoing_labels)}── {node}")
        if incoming:
            lines.extend(incoming)
            lines.append("          │")
        lines.append(f"          {center}")
        if outgoing:
            lines.append("          │")
            lines.extend(outgoing)
        if not incoming and not outgoing:
            lines.append("          · no recorded artifact edges yet")
        lines.append("")
        lines.append(
            "Edges are recorded from agent context/use and artifact metadata. "
            "The graph updates when you select another artifact."
        )
        return "\n".join(lines)

    @staticmethod
    def _extract_fenced_json(raw: str) -> str | None:
        """Recognize legacy structured-outcome Markdown without showing its fence."""
        stripped = raw.strip()
        if not stripped.startswith("```json") or not stripped.endswith("```"):
            return None
        return stripped[len("```json"): -3].strip()

    @classmethod
    def _format_json_preview(cls, raw: str) -> str:
        """Render JSON as a readable outline without JSON punctuation or escapes."""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return cls._clip_multiline(raw, 262_144)

        lines: list[str] = []
        priority = {
            "title": 0, "status": 1, "summary": 2, "verdict": 3,
            "description": 4, "mathematical_question": 5, "statement": 6,
            "proof_latex": 7, "counterexample": 8, "verification": 9,
            "open_obligations": 10, "next_task": 11,
        }

        def label(key: Any) -> str:
            return str(key).replace("_", " ").strip().capitalize() or "Value"

        def scalar(value: Any) -> str:
            if value is None:
                return "not supplied"
            if isinstance(value, bool):
                return "yes" if value else "no"
            text = str(value).replace("\x00", "").strip()
            return text if len(text) <= 4_000 else text[:3_999] + "…"

        def render(value: Any, indent: int = 0, heading: str | None = None, depth: int = 0) -> None:
            prefix = "  " * indent
            if depth >= 7:
                lines.append(f"{prefix}{heading + ': ' if heading else ''}[nested content omitted]")
                return
            if isinstance(value, dict):
                if heading is not None:
                    lines.append(f"{prefix}{heading}:")
                    prefix += "  "
                if not value:
                    lines.append(f"{prefix}(empty)")
                    return
                keys = sorted(value, key=lambda key: (priority.get(str(key), 99), str(key)))
                for key in keys[:60]:
                    render(value[key], indent + (1 if heading is not None else 0), label(key), depth + 1)
                omitted = len(value) - 60
                if omitted > 0:
                    lines.append(f"{prefix}[{omitted} more fields omitted]")
                return
            if isinstance(value, list):
                if heading is not None:
                    lines.append(f"{prefix}{heading}:")
                    prefix += "  "
                if not value:
                    lines.append(f"{prefix}(none)")
                    return
                for item in value[:60]:
                    if isinstance(item, (dict, list)):
                        lines.append(f"{prefix}-")
                        render(item, indent + (2 if heading is not None else 1), None, depth + 1)
                    else:
                        lines.append(f"{prefix}- {scalar(item)}")
                omitted = len(value) - 60
                if omitted > 0:
                    lines.append(f"{prefix}[{omitted} more items omitted]")
                return
            value_text = scalar(value)
            if "\n" in value_text:
                lines.append(f"{prefix}{heading or 'Value'}:")
                lines.extend(f"{prefix}  {line}" for line in value_text.splitlines())
            elif heading is None:
                lines.append(f"{prefix}{value_text}")
            else:
                lines.append(f"{prefix}{heading}: {value_text}")

        render(data)
        return "\n".join(lines) or "(empty JSON artifact)"

    def _artifact_list_label(self, artifact: dict[str, Any]) -> str:
        metadata = artifact.get("metadata", {})
        status = str(metadata.get("status", "RECORDED"))
        kind_key = str(artifact.get("kind", "artifact"))
        kind = kind_key.replace("_", " ")
        if kind_key == "partial_result":
            statement = self._clip(metadata.get("statement", "unnamed lemma"), 54)
            return f"[{status}] partial result / {statement}"
        if kind_key == "structured_research_outcome":
            slot = str(metadata.get("slot", "agent"))
            epoch = metadata.get("epoch", "?")
            return f"[{status}] research outcome / {slot} E{epoch}"
        return f"[{status}] {kind}"

    def _artifact_summary(self, artifact: dict[str, Any], preview: str) -> str:
        kind = str(artifact.get("kind", "artifact")).replace("_", " ")
        metadata = artifact.get("metadata", {})
        status = str(metadata.get("status", "RECORDED"))
        if artifact.get("relative_path", "").endswith(".json"):
            try:
                data = json.loads(preview)
                if isinstance(data, dict):
                    detail = (
                        data.get("summary") or data.get("mathematical_question")
                        or data.get("verdict") or data.get("description") or ""
                    )
                    return f"{kind} [{status}] — {self._clip(detail, 180)}"
            except (TypeError, ValueError):
                pass
        return f"{kind} [{status}]"

    def _activity_text(self) -> str:
        # Keep a useful rolling history; the focused panel's scrollbar exposes
        # older activity without flooding the normal compact view.
        events = self.store.events.read_tail(1000)
        lines = []
        for event in events:
            payload = event.get("payload", {})
            summary = payload.get("summary") or payload.get("message") or payload.get("status") or payload
            lines.append(
                self._clean_activity(
                    f"{event.get('created_at', '')[11:19]} {event.get('event_type')}: {self._clip(summary, 240)}"
                )
            )
        if self._setup_phase:
            role, _ = self._setup_phase
            elapsed = (
                int(time.monotonic() - self._setup_started_at)
                if self._setup_started_at is not None
                else 0
            )
            lines.append(f"setup: {role} still running ({elapsed}s; live, not written to disk)")
        return "\n".join(lines[-1000:]) or "No activity yet."

    def _footer_text(self) -> str:
        model, strength = self._model_settings()
        status = self._clip(self.message, 90)
        if self._setup_phase:
            role, _ = self._setup_phase
            elapsed = (
                f" ({int(time.monotonic() - self._setup_started_at)}s)"
                if self._setup_started_at is not None
                else ""
            )
            status = f"Setup running: {role}{elapsed}"
        legend = "◐ work  ○ queued  ✓ done  × failed  Ⅱ paused  ! blocked/budget"
        return (
            f" {legend} "
            f"• {status} "
            f"• Folder: {self.project_root.name} "
            f"• Model: {model}/{strength} "
            "• Chat for instructions • Type / for exact commands • Enter submit • Ctrl-C quit "
        )

    def _start_report_generation(self, output: Path | None = None) -> None:
        self.message = "Generating Markdown research report"

        def worker() -> None:
            try:
                path = write_report(self.store, output)
                continuation = write_continuation_brief(self.store)
                journals = write_agent_audited_proof_report(self.store)
                suffix = f"; {len(journals)} audited LaTeX note(s)" if journals else ""
                self.message = (
                    f"Report generated: {path.relative_to(self.project_root)}; "
                    f"handoff: {continuation.relative_to(self.project_root)}{suffix}"
                )
            except Exception as exc:
                self.message = f"Report failed: {type(exc).__name__}: {exc}"
            finally:
                if self._app is not None:
                    self._app.invalidate()

        thread = threading.Thread(target=worker, name="ariadne-report", daemon=True)
        self._worker_threads.append(thread)
        thread.start()

    def _stop_campaign_process_on_exit(self) -> None:
        """Pause and interrupt the TUI-owned controller before this TUI exits.

        A TUI used to exit while its ``Popen`` child kept running in the
        background. This created an orphaned controller that a later TUI could
        not see, causing duplicate research slots. The child gets its own
        process group, so an interruption also reaches its bounded providers.
        """
        process = self._campaign_process
        if process is None or process.poll() is not None:
            return
        campaign = self.store.latest_campaign()
        if campaign and str(campaign.get("status")) == "RUNNING":
            self.store.request_campaign_pause(
                str(campaign["campaign_id"]),
                reason="Ariadne TUI exited while the campaign was running",
                requested_by="tui-exit",
            )
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGINT)
            else:  # pragma: no cover - Windows does not support process groups here.
                process.send_signal(signal.SIGINT)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:  # pragma: no cover - Windows does not support process groups here.
                process.terminate()
            process.wait(timeout=5)
        except ProcessLookupError:
            pass
        finally:
            # A killed controller might not execute its CLI KeyboardInterrupt
            # handler. Recover only after its process group has exited.
            latest = self.store.latest_campaign()
            if latest and str(latest.get("status")) == "RUNNING":
                self.store.recover_interrupted_campaign(
                    str(latest["campaign_id"]), recovered_by="tui-exit"
                )

    def _launch_campaign(self) -> None:
        if self._setup_phase or self._setup_runs(active_only=True):
            self.message = "Setup is still running; wait for setup complete before /run"
            return
        if self._campaign_process and self._campaign_process.poll() is None:
            self.message = "Campaign process is already running"
            return
        if not self.store.paths.contract.exists():
            self.message = "No problem contract; use /setup"
            return
        latest = self.store.latest_campaign()
        if latest and str(latest.get("status")) == "RUNNING":
            self.message = (
                "Campaign state is already RUNNING; use pause/status before "
                "starting another controller"
            )
            return
        if latest and str(latest.get("status")) == "CONTRACT_CHANGED":
            self.message = (
                "Campaign cannot resume: the frozen problem contract changed. "
                "Restore it exactly or create a new project."
            )
            return
        action = (
            "resume"
            if latest and str(latest.get("status")) == "PAUSED_HUMAN"
            else "run"
        )
        cmd = [
            sys.executable,
            "-m",
            "ariadne_math",
            "campaign",
            action,
            str(self.project_root),
            "--config",
            str(self.config_path),
            "--record-activity",
        ]
        self._campaign_process = subprocess.Popen(
            cmd,
            text=True,
            # Give the controller and its bounded providers a private process
            # group so TUI exit can interrupt exactly this campaign.
            start_new_session=(os.name == "posix"),
            stdout=subprocess.DEVNULL,
            # Keep stderr only until the child exits. This avoids continuous
            # disk writes while preserving the actionable CLI/provider error.
            stderr=subprocess.PIPE,
            cwd=self.project_root,
        )
        self.message = f"Started campaign {action} process pid={self._campaign_process.pid}"

        process = self._campaign_process

        def watch() -> None:
            _, stderr = process.communicate()
            returncode = int(process.returncode or 0)
            if returncode == 0:
                self.message = "Campaign process completed"
            else:
                detail = self._clean_activity(stderr or "no diagnostic was produced")
                if len(detail) > 900:
                    detail = detail[-900:]
                self.store.events.append(
                    "campaign_process_failed",
                    {"returncode": returncode, "diagnostic": detail},
                )
                self.message = f"Campaign process failed ({returncode}): {detail}"
            if self._app is not None:
                self._app.invalidate()

        watcher = threading.Thread(target=watch, name="ariadne-campaign-watch", daemon=True)
        self._worker_threads.append(watcher)
        watcher.start()

    def _start_setup_generation(self, answers, *, auto_start: bool = True) -> None:
        if any(thread.is_alive() for thread in self._worker_threads):
            self.message = "A background setup task is already running"
            return

        def worker() -> None:
            reporter = EventLogActivityReporter(self.store.events.append)

            class SetupReporterProxy:
                def emit(proxy, event_type, message, **data):
                    if event_type == "contract_author_stage":
                        self._setup_phase = (
                            "contract_author",
                            "Create and self-audit the exact mathematical problem contract",
                        )
                        self._setup_started_at = time.monotonic()
                    elif event_type == "literature_author_stage":
                        self._setup_phase = (
                            "literature_author",
                            "Create and validate the shared literature dossier",
                        )
                        self._setup_started_at = time.monotonic()
                    reporter.emit(event_type, message, **data)

                def __getattr__(proxy, name):
                    return getattr(reporter, name)

            setup_succeeded = False
            try:
                self.message = "Generating problem contract with Codex"
                result = generate_setup(
                    project_root=self.project_root,
                    config_path=self.config_path,
                    answers=answers,
                    reporter=SetupReporterProxy(),
                )
                setup_succeeded = True
                self.message = (
                    f"Setup complete: {result['mode']} with "
                    f"{result['researcher_count']} researchers"
                )
                reporter.emit("setup_completed", self.message, auto_start=auto_start)
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                reporter.emit("setup_failed", detail)
                self.message = "Setup failed; inspect Recent activity"
            finally:
                self._setup_phase = None
                self._setup_started_at = None
                if setup_succeeded and auto_start:
                    self._launch_campaign()
                elif setup_succeeded:
                    self.message += "; manual mode: use /run when ready"
                if self._app is not None:
                    self._app.invalidate()

        self._setup_phase = (
            "contract_author",
            "Create and self-audit the exact mathematical problem contract",
        )
        self._setup_started_at = time.monotonic()
        thread = threading.Thread(target=worker, name="ariadne-setup", daemon=True)
        self._worker_threads.append(thread)
        thread.start()


_TERMINAL_CAMPAIGN_STATUSES = frozenset({
    "COMPLETED_UNSOLVED",
    "COMPLETE_PROOF_CANDIDATE",
    "REFUTATION_CANDIDATE",
    "HUMAN_CHECKED",
    "FORMALLY_CERTIFIED",
})


def _contract_is_unchanged(store: ResearchStore) -> bool:
    if not store.paths.contract.exists():
        return False
    expected = store.get_meta("problem_contract_sha256")
    return expected is not None and expected == content_hash(store.paths.contract.read_bytes())


def _prepare_existing_project_start(
    store: ResearchStore, *, use_terminal_handoff: bool = False
) -> tuple[bool, str]:
    """Return whether an interrupted campaign may resume, plus a startup message."""
    latest = store.latest_campaign()
    if latest is None:
        return False, "Existing project has no campaign; use /setup."
    if not _contract_is_unchanged(store):
        return False, "Frozen contract differs from the campaign seal; campaign startup is blocked."
    status = str(latest.get("status", ""))
    campaign_id = str(latest["campaign_id"])
    if status == "PAUSED_HUMAN":
        control = store.get_campaign_control(campaign_id)
        if bool(control.get("pause_requested")):
            return False, "Campaign remains paused by an active human pause request."
        return True, "Resuming the interrupted unchanged-contract campaign."
    if status == "RUNNING":
        return False, "Campaign is marked RUNNING; wait for it or use /recover if it is stale."
    if status == "BUDGET_EXHAUSTED":
        return False, "Campaign needs a budget adjustment before it can continue; use /budget."
    if status in _TERMINAL_CAMPAIGN_STATUSES:
        if not use_terminal_handoff:
            return False, (
                "Previous campaign is terminal; starting fresh leaves its handoff out of "
                "the literature dossier. Use /run for a new campaign."
            )
        try:
            handoff = publish_continuation_brief_as_literature(store)
        except OSError as exc:
            return False, f"Terminal campaign found, but continuation handoff could not be published: {exc}"
        location = str(handoff.relative_to(store.paths.root)) if handoff else "unavailable"
        return False, (
            "Previous campaign is terminal; its opt-in handoff is available to "
            f"literature-aware roles at {location}. Use /run for a new campaign."
        )
    return False, f"Campaign status is {status}; inspect /report before continuing."


def run_tui(project_root: Path, config_path: Path | None = None) -> None:
    root = project_root.resolve()
    first_run = not (root / ".ariadne").exists()
    selected_config = (config_path or (root / "ariadne.codex.toml")).resolve()
    config_missing = not selected_config.exists()
    if config_missing:
        from importlib.resources import files

        selected_config.parent.mkdir(parents=True, exist_ok=True)
        selected_config.write_text(
            files("ariadne_math.integrations.codex")
            .joinpath("config.toml")
            .read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    load_config(selected_config)

    setup_needed = first_run or config_missing
    if setup_needed:
        # Run the owner interview before prompt_toolkit starts its event loop.
        # collect_setup_answers uses synchronous prompt calls, so doing this in
        # the launcher avoids asyncio.run() being nested inside the TUI loop.
        answers = collect_setup_answers(project_root=root)
        setup_answers = answers
        resume_on_start = False
    else:
        setup_answers = None
        store = ResearchStore(root)
        git_startup_message = ""
        if not project_has_git_repository(root):
            answer = input(
                "This Ariadne project is not a Git repository. Enable Git version control now? [Y/n]: "
            ).strip().lower()
            if answer in {"", "y", "yes"}:
                git_enabled, git_commit = enable_project_git(root)
                if git_enabled:
                    git_startup_message = "Git versioning enabled"
                    if git_commit:
                        git_startup_message += f" at {git_commit[:12]}"
                else:
                    git_startup_message = "Git was requested but could not be initialized"
            else:
                git_startup_message = "Git versioning was not enabled"
        use_terminal_handoff = False
        latest = store.latest_campaign()
        if (
            latest is not None
            and str(latest.get("status", "")) in _TERMINAL_CAMPAIGN_STATUSES
            and _contract_is_unchanged(store)
        ):
            handoff_path = write_continuation_brief(store)
            answer = input(
                "Previous campaign is terminal. Use its continuation handoff as local "
                f"literature for a fresh campaign? [{handoff_path}] [y/N]: "
            ).strip().lower()
            use_terminal_handoff = answer in {"y", "yes"}
        resume_on_start, startup_message = _prepare_existing_project_start(
            store, use_terminal_handoff=use_terminal_handoff
        )

    app = AriadneTUI(
        root,
        selected_config,
        resume_on_start=resume_on_start,
        setup_answers=setup_answers if setup_needed else None,
    )
    if not setup_needed:
        app.message = "; ".join(
            part for part in (git_startup_message, startup_message) if part
        )
    app.run()
