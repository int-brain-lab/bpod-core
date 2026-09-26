# AGENTS.md

Guidance for AI coding agents working on **bpod-core**.

`bpod-core` provides the core Python implementation for interacting with Bpod hardware.

## Package manager

This project uses [`uv`](https://docs.astral.sh/uv/)

## Commands

- Install/sync dev environment: `uv sync`
- Full check matrix (lint, format, typing, doctest, tests): `uv run tox -p`
- Tests only: `uv run tox -p -m pytest`
- Type check only: `uv run tox -p -m mypy`
- Lint: `uv run ruff check`
- Format check: `uv run ruff format --check`
- Doctest: `uv run tox -e doctest`
- Build docs: `uv run sphinx-build -b dirhtml docs/source docs/build`

## Repository structure

- `bpod_core/` — library source code
- `tests/` — unit and integration tests
- `examples/` — runnable example scripts
- `docs/` — documentation (Sphinx source + generated build)
- `pyproject.toml` — project configuration

## Guides

Load these only when relevant to the task at hand:

- [Documentation lookup](.agents/docs-lookup.md) — check before answering any
  "how does X behave" question; don't guess when docs exist.
- [References](.agents/references.md) — Bpod firmware sources and MATLAB implementation.
- [Development principles](.agents/development-principles.md)
- [Code style](.agents/code-style.md) / [docstrings](.agents/docstrings.md)
- [Testing](.agents/testing.md) / [hardware considerations](.agents/hardware.md)
- [Definition of done](.agents/definition-of-done.md) — checklist before finishing
  a code change.
- [Plan mode](.agents/plan-mode.md)
- [Pull requests](.agents/pull-requests.md)
