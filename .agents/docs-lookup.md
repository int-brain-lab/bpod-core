# Documentation lookup

The project is documented extensively. Do not guess behavior when documentation is
available.

## Local files (current, possibly unreleased state)

For behavior questions, start at step 1 and only move down if the topic isn't
covered; don't skip ahead just because a search landed there first. Prefer
`docs/build` over `docs/source` even when both turn up — flatter Markdown, cheaper
to grep and read. `docs/build` and `docs/source/api` aren't tracked in git, so
build the docs (see "Commands" in the root AGENTS.md) if missing or stale, e.g. at
session start or right after editing `docs/source`.

1. `docs/build/llms.txt` — index of all pages; use it to find the right one.
2. `docs/build/llms-full.txt` — all pages concatenated; grep rather than read whole.
3. `docs/build/**/*.md` — per-page Markdown, incl. rendered API docstrings.
4. `docs/source/**/*.rst` — the authored sources; edit documentation here.
   Except `api/` and `state_machines/examples/`: generated, edit their generators.
5. Docstrings in `bpod_core/` — the final authority if docs and code disagree;
   they also hold the doctests.

## MCP server

`bpod-core-docs` — GitMCP-hosted documentation and source search for this repo,
checked in via `.mcp.json`. Mirrors the latest *released* docs, not the current
working tree; use it to check released behavior or as a fallback when local docs
are unclear or hard to search.
