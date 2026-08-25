#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$ROOT/.demo}"
rm -rf "$DEST"
PYTHONPATH="$ROOT/src" python -m ariadne_math demo "$DEST"
PYTHONPATH="$ROOT/src" python -m ariadne_math campaign status "$DEST"
PYTHONPATH="$ROOT/src" python -m ariadne_math report "$DEST"
