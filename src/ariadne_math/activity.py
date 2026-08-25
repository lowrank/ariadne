from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TextIO


ControlProvider = Callable[[], dict[str, Any] | None]
BudgetProvider = Callable[[], dict[str, Any] | None]


class ActivityReporter:
    """Public, high-level campaign activity reporting.

    Reporters deliberately expose tasks, plans, role transitions, verdicts, and
    costs—not hidden model reasoning or private scratch work.
    """

    def start(
        self,
        *,
        campaign_id: str,
        control_provider: ControlProvider | None = None,
        budget_provider: BudgetProvider | None = None,
    ) -> None:
        del campaign_id, control_provider, budget_provider

    def stop(self) -> None:
        return None

    def emit(self, event_type: str, message: str, **data: Any) -> None:
        del event_type, message, data

    def call_started(
        self,
        *,
        run_id: str,
        role: str,
        slot: str,
        route_id: str | None,
        provider: str,
        task: str,
        instruction_count: int = 0,
    ) -> None:
        del run_id, role, slot, route_id, provider, task, instruction_count

    def call_finished(
        self,
        *,
        run_id: str,
        role: str,
        slot: str,
        route_id: str | None,
        elapsed_seconds: float,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        del (
            run_id,
            role,
            slot,
            route_id,
            elapsed_seconds,
            input_tokens,
            output_tokens,
            cost_usd,
        )

    def call_failed(
        self,
        *,
        run_id: str,
        role: str,
        slot: str,
        route_id: str | None,
        elapsed_seconds: float,
        error: str,
    ) -> None:
        del run_id, role, slot, route_id, elapsed_seconds, error

    def interactive_checkpoint(
        self, *, epoch: int, summary: str
    ) -> tuple[str, str | None]:
        del epoch, summary
        return "continue", None


class NullActivityReporter(ActivityReporter):
    pass


@dataclass
class _ActiveCall:
    role: str
    slot: str
    route_id: str | None
    provider: str
    task: str
    started_monotonic: float


class ConsoleActivityReporter(ActivityReporter):
    def __init__(
        self,
        *,
        heartbeat_seconds: float = 15.0,
        stream: TextIO | None = None,
        json_events: bool = False,
        interactive: bool = False,
    ) -> None:
        self.heartbeat_seconds = max(0.0, float(heartbeat_seconds))
        self.stream = stream or sys.stderr
        self.json_events = json_events
        self.interactive = interactive
        self._lock = threading.RLock()
        self._active: dict[str, _ActiveCall] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._campaign_id = ""
        self._control_provider: ControlProvider | None = None
        self._budget_provider: BudgetProvider | None = None
        self._pause_announced = False

    def start(
        self,
        *,
        campaign_id: str,
        control_provider: ControlProvider | None = None,
        budget_provider: BudgetProvider | None = None,
    ) -> None:
        self._campaign_id = campaign_id
        self._control_provider = control_provider
        self._budget_provider = budget_provider
        self._stop_event.clear()
        if self.heartbeat_seconds <= 0:
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name="ariadne-activity-heartbeat",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=min(2.0, self.heartbeat_seconds))
        self._thread = None

    def emit(self, event_type: str, message: str, **data: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "campaign_id": self._campaign_id or data.pop("campaign_id", ""),
            "event_type": event_type,
            "message": message,
            **data,
        }
        with self._lock:
            if self.json_events:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=self.stream, flush=True)
            else:
                stamp = datetime.now().strftime("%H:%M:%S")
                label = event_type.replace("_", " ").upper()
                print(f"[{stamp}] {label}: {message}", file=self.stream, flush=True)

    def call_started(
        self,
        *,
        run_id: str,
        role: str,
        slot: str,
        route_id: str | None,
        provider: str,
        task: str,
        instruction_count: int = 0,
    ) -> None:
        with self._lock:
            self._active[run_id] = _ActiveCall(
                role=role,
                slot=slot,
                route_id=route_id,
                provider=provider,
                task=task,
                started_monotonic=time.monotonic(),
            )
        suffix = f"; {instruction_count} active human instruction(s)" if instruction_count else ""
        route = f" on {route_id}" if route_id else ""
        self.emit(
            "agent_started",
            f"{slot} [{role}] via {provider}{route}: {task}{suffix}",
            run_id=run_id,
            role=role,
            slot=slot,
            route_id=route_id,
            provider=provider,
            task=task,
            instruction_count=instruction_count,
        )

    def call_finished(
        self,
        *,
        run_id: str,
        role: str,
        slot: str,
        route_id: str | None,
        elapsed_seconds: float,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        with self._lock:
            self._active.pop(run_id, None)
        route = f" on {route_id}" if route_id else ""
        usage = ""
        if input_tokens or output_tokens:
            usage = f", tokens {input_tokens}/{output_tokens}"
        self.emit(
            "agent_finished",
            f"{slot} [{role}]{route} finished in {elapsed_seconds:.1f}s{usage}, cost ${cost_usd:.4f}",
            run_id=run_id,
            role=role,
            slot=slot,
            route_id=route_id,
            elapsed_seconds=round(elapsed_seconds, 3),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
        )

    def call_failed(
        self,
        *,
        run_id: str,
        role: str,
        slot: str,
        route_id: str | None,
        elapsed_seconds: float,
        error: str,
    ) -> None:
        with self._lock:
            self._active.pop(run_id, None)
        route = f" on {route_id}" if route_id else ""
        self.emit(
            "agent_failed",
            f"{slot} [{role}]{route} failed after {elapsed_seconds:.1f}s: {error}",
            run_id=run_id,
            role=role,
            slot=slot,
            route_id=route_id,
            elapsed_seconds=round(elapsed_seconds, 3),
            error=error,
        )

    def interactive_checkpoint(
        self, *, epoch: int, summary: str
    ) -> tuple[str, str | None]:
        if not self.interactive or not sys.stdin.isatty():
            return "continue", None
        with self._lock:
            print("", file=self.stream)
            print(f"Epoch {epoch} checkpoint: {summary}", file=self.stream, flush=True)
            print(
                "Press Enter to continue; [i] add a campaign-wide researcher instruction; [p] pause.",
                file=self.stream,
                flush=True,
            )
        try:
            choice = input("ariadne> ").strip().lower()
        except EOFError:
            return "continue", None
        if choice in {"p", "pause"}:
            try:
                reason = input("Pause reason (optional): ").strip()
            except EOFError:
                reason = ""
            return "pause", reason or "Interactive pause requested"
        if choice in {"i", "instruction", "instruct"}:
            try:
                text = input("Instruction for the next research epoch: ").strip()
            except EOFError:
                text = ""
            if text:
                return "instruction", text
        return "continue", None

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.heartbeat_seconds):
            with self._lock:
                active = list(self._active.values())
            control = self._safe_call(self._control_provider)
            budget = self._safe_call(self._budget_provider)
            pause_requested = bool(control and control.get("pause_requested"))
            if pause_requested and not self._pause_announced:
                reason = str(control.get("reason", "")).strip()
                detail = f": {reason}" if reason else ""
                self.emit(
                    "pause_requested",
                    "Human pause requested; the controller will pause at the next safe stage boundary after active work and any already-triggered atomic audit chain finish" + detail,
                )
                self._pause_announced = True
            if not active:
                continue
            now = time.monotonic()
            descriptions = []
            for item in active:
                elapsed = int(now - item.started_monotonic)
                route = f"/{item.route_id}" if item.route_id else ""
                descriptions.append(f"{item.slot}:{item.role}{route} ({elapsed}s)")
            budget_text = ""
            if budget:
                budget_text = (
                    f"; budget calls {budget.get('calls_used', 0)}/{budget.get('max_calls', '?')}, "
                    f"cost ${float(budget.get('cost_used', 0.0)):.4f}/${float(budget.get('max_cost_usd', 0.0)):.2f}"
                )
            pause_text = "; pause pending" if pause_requested else ""
            self.emit(
                "heartbeat",
                "Still working: " + ", ".join(descriptions) + budget_text + pause_text,
                active_calls=len(active),
            )

    @staticmethod
    def _safe_call(provider: Callable[[], Any] | None) -> Any:
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            return None


class EventLogActivityReporter(ConsoleActivityReporter):
    """Persist only meaningful activity events for a live UI or later audit.

    This reporter deliberately disables ConsoleActivityReporter heartbeats: a
    TUI can render elapsed time from its own in-memory clock without turning a
    long provider call into continuous SQLite or filesystem churn.
    """

    def __init__(self, append_event: Callable[[str, dict[str, Any]], str]) -> None:
        super().__init__(heartbeat_seconds=0.0)
        self._append_event = append_event

    def emit(self, event_type: str, message: str, **data: Any) -> None:
        payload = {
            "campaign_id": self._campaign_id or str(data.get("campaign_id", "")),
            "message": message,
            **data,
        }
        with self._lock:
            self._append_event(event_type, payload)


def make_activity_reporter(
    *,
    quiet: bool = False,
    json_events: bool = False,
    heartbeat_seconds: float = 15.0,
    interactive: bool = False,
    stream: TextIO | None = None,
) -> ActivityReporter:
    if quiet:
        return NullActivityReporter()
    return ConsoleActivityReporter(
        heartbeat_seconds=heartbeat_seconds,
        stream=stream,
        json_events=json_events,
        interactive=interactive,
    )
