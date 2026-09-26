# AGENTS.md

Guidance for AI coding agents working on **bpod-core**.

## Purpose

`bpod-core` provides the core Python implementation for interacting with Bpod hardware.
Favor correctness, backwards compatibility, and readability over clever implementations.

## Repository Structure

- `bpod_core/` — library source code
- `tests/` — unit and integration tests
- `examples/` — runnable example scripts
- `docs/` — documentation
- `pyproject.toml` — project configuration

## Documentation

The project is documented extensively. Do not guess behavior when documentation is
available.

**Local files** (current, possibly unreleased state) — for behavior questions,
start at step 1 and only move down if the topic isn't covered; don't skip ahead
just because a search landed there first. Prefer `docs/build` over `docs/source`
even when both turn up — flatter Markdown, cheaper to grep and read. `docs/build`
and `docs/source/api` aren't tracked in git, so build the docs (see "Commands")
if missing or stale, e.g. at session start or right after editing `docs/source`.

1. `docs/build/llms.txt` — index of all pages; use it to find the right one.
2. `docs/build/llms-full.txt` — all pages concatenated; grep rather than read whole.
3. `docs/build/**/*.md` — per-page Markdown, incl. rendered API docstrings.
4. `docs/source/**/*.rst` — the authored sources; edit documentation here.
   Except `api/` and `state_machines/examples/`: generated, edit their generators.
5. Docstrings in `bpod_core/` — the final authority if docs and code disagree;
   they also hold the doctests.

**MCP server** `bpod-core-docs` — GitMCP-hosted documentation and source search for this
repo, checked in via `.mcp.json`. Mirrors the latest *released* docs, not the current
working tree; use it to check released behavior or as a fallback when local docs are
unclear or hard to search.

## Commands

- Install/sync dev environment: `uv sync`
- Unit tests, integration tests (pytest via tox): `uv run tox -p -m pytest`
- Lint: `uv run ruff check`
- Format check: `uv run ruff format --check`
- Type check (mypy via tox): `uv run tox -p -m mypy`
- Doctest (sphinx via tox): `uv run tox -e doctest`
- Build docs: `uv run sphinx-build -b dirhtml docs/source docs/build`
- Full matrix (lint, format, typing, doctest, tests): `uv run tox -p`

## Development Principles

- Keep public APIs backwards compatible unless explicitly requested.
- Prefer small, focused changes; keep diffs minimal and avoid unrelated refactoring.
- Avoid introducing new dependencies without justification.
- Maintain type hints throughout new code.
- Use descriptive variable and function names.
- Prefer composition over large inheritance hierarchies.

## Code Style

- Follow PEP 8 and match surrounding coding style.
- Add NumPy style docstrings for all public API in `bpod_core/`.
- Type annotations are mandatory.
- Line-length: 88 chars, also for docstrings.

## Testing

- Unit tests and integration tests are implemented in pytest.
- Tests should be concise, contain a one-line docstring and run in little time.
- Make use of pytest fixtures, parametrization and mocking where relevant.

## Hardware Considerations

Some functionality communicates with physical Bpod devices.

- Do not assume hardware is available.
- Prefer mockable interfaces.
- Hardware-dependent tests should be isolated.
- Avoid making changes that require hardware unless explicitly requested.

## Definition of Done

Before considering work complete:

1. Run the full test matrix.
2. Add tests for new functionality.
3. Update existing tests if behavior changes.
4. Ensure examples still work if affected.
5. Update docstrings and documentation where relevant.
6. Add a concise note under `## UNRELEASED` in `CHANGELOG.md` for user-facing changes.

## Pull Requests

Summaries should include:

- What changed.
- Why it changed.
- Any API changes.
- Any new dependencies.
- Any testing performed.
- Add a disclaimer about the use of LLM assisted coding, with model name and version.
