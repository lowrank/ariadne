from __future__ import annotations

from importlib.resources import files
from typing import Any


def load_prompt(name: str) -> str:
    resource = files("ariadne_math.prompts").joinpath(name)
    return resource.read_text(encoding="utf-8")


class _SafeDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_prompt(name: str, **values: Any) -> str:
    return load_prompt(name).format_map(_SafeDict({k: str(v) for k, v in values.items()}))
