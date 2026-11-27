#!/usr/bin/env bash
set -euo pipefail

uv lock --check
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy src
uv run --locked pytest --cov=toolswap --cov-report=term-missing
uv run --locked python -c 'from hashlib import sha256; from pathlib import Path; print("uv.lock SHA-256: " + sha256(Path("uv.lock").read_bytes()).hexdigest())'
