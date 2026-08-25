from __future__ import annotations

import argparse
from pathlib import Path

from .tui import run_tui


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ariadne live terminal user interface")
    parser.add_argument("project", nargs="?", type=Path, default=Path("."))
    parser.add_argument("--config", type=Path, help="configuration TOML (defaults to PROJECT/ariadne.codex.toml)")
    args = parser.parse_args(argv)
    run_tui(args.project, args.config)
    return 0
