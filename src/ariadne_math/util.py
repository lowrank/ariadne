from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def canonical_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def short_id(prefix: str, payload: Any, length: int = 12) -> str:
    digest = content_hash(canonical_json(payload))[:length]
    return f"{prefix}-{digest}"


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_signature(text: str) -> str:
    text = normalize_text(text).lower()
    text = re.sub(r"[^a-z0-9_+*/<>=]+", " ", text)
    return " ".join(sorted(set(text.split())))


def token_set(text: str) -> set[str]:
    return set(normalize_signature(text).split())


def jaccard_similarity(a: str, b: str) -> float:
    sa, sb = token_set(a), token_set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def write_json(path: Path, data: Any) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract a structured agent response.

    Preferred format is an object between <ARIADNE_JSON> tags. As a fallback,
    the first balanced JSON object is parsed. Raises ValueError on failure.
    """
    tagged = re.search(r"<ARIADNE_JSON>\s*(\{.*?\})\s*</ARIADNE_JSON>", text, re.S)
    if tagged:
        value = json.loads(tagged.group(1))
        if not isinstance(value, dict):
            raise ValueError("Structured response must be a JSON object")
        return value

    starts = [m.start() for m in re.finditer(r"\{", text)]
    for start in starts:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(value, dict):
                        return value
                    break
    raise ValueError("No valid JSON object found in agent response")


def redact_environment(env: dict[str, str], extra_names: Iterable[str] = ()) -> dict[str, str]:
    sensitive = re.compile(r"(API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE[_-]?KEY)", re.I)
    blocked = {name.upper() for name in extra_names}
    result: dict[str, str] = {}
    for key, value in env.items():
        if sensitive.search(key) or key.upper() in blocked:
            continue
        if key.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}:
            continue
        result[key] = value
    return result
