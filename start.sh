#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if ! command -v uv >/dev/null 2>&1; then
  echo 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' >&2
  exit 1
fi
uv sync --frozen --inexact
if [[ ! -f data/cmd_config.json ]]; then
  uv run --no-sync python tools/setup.py
fi
uv run --no-sync python tools/setup.py --check
if [[ ! -f .venv/.share-plugins-installed ]]; then
  uv pip install --python .venv/bin/python -r requirements-plugins.txt --override local-dependency-overrides.txt
  uv run --no-sync python -m playwright install --with-deps chromium
  touch .venv/.share-plugins-installed
fi
exec uv run --no-sync python main.py
