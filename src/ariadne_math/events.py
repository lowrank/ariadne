from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ProjectPaths
from .util import ensure_dir, short_id, utc_now


class EventLog:
    def __init__(self, paths: ProjectPaths):
        self.paths = paths
        ensure_dir(paths.state)

    def append(self, event_type: str, payload: dict[str, Any]) -> str:
        event = {
            "event_type": event_type,
            "created_at": utc_now(),
            "payload": payload,
        }
        event_id = short_id("EVT", event)
        event["event_id"] = event_id
        with self.paths.events.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        return event_id

    def read_tail(self, limit: int = 200, *, max_bytes: int = 2_000_000) -> list[dict[str, Any]]:
        """Read a bounded recent event window without repeatedly loading the log."""
        if limit <= 0 or not self.paths.events.exists():
            return []
        chunks: list[bytes] = []
        newline_count = 0
        with self.paths.events.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            remaining = max(1, int(max_bytes))
            while position and newline_count <= limit and remaining:
                size = min(65_536, position, remaining)
                position -= size
                handle.seek(position)
                block = handle.read(size)
                chunks.append(block)
                newline_count += block.count(b"\n")
                remaining -= size
        lines = b"".join(reversed(chunks)).splitlines()[-limit:]
        items: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except (TypeError, ValueError, UnicodeDecodeError):
                continue
            if isinstance(value, dict):
                items.append(value)
        return items

    def read_all(self) -> list[dict[str, Any]]:
        if not self.paths.events.exists():
            return []
        items: list[dict[str, Any]] = []
        with self.paths.events.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                value = json.loads(line)
                if isinstance(value, dict):
                    items.append(value)
        return items
