bpod-core — Development Guidelines (project-specific)

Audience: Experienced Python developers contributing to this repository. This document captures the repo’s concrete build, test, and workflow details so you can be productive quickly.

1. Environment, Build, and Configuration
- Python versions: The project targets Python >= 3.10. Tox is configured to test on 3.10–3.13.
- Package/build backend: uv_build (configured in pyproject.toml). Builds and venvs are managed via uv.
- Dependency groups (pyproject.toml):
  - dev: includes doc, lint, test, typing groups.
  - test: pytest, pytest-cov, pytest-mock, tox, tox-uv, etc.
  - lint: ruff.
  - typing: mypy and types stubs.
  - doc: Sphinx + extensions.
- Recommended workflow using uv:
  - Sync full dev toolchain: uv sync --group dev
    - Alternatively, fine-grained: uv sync --group test --group lint --group typing
  - Use tools with uv run: uv run <command>
    - Example: uv run pytest -q
- Build package artifacts:
  - uv build  (uses uv_build backend; module-name bpod_core, module-root "")
  - Sources included for sdist: tests/*.py, schema/*.json, docs/source/**/* (see [tool.uv.build-backend]).
- Cross-platform: [tool.uv.required-environments] lists Linux, macOS, Windows; development is expected to be cross-platform.

2. Testing
2.1 Test runner and defaults
- Test runner: pytest≥8 is used.
- Configuration is in pyproject.toml:
  - [tool.pytest.ini_options] addopts = "--cov=bpod_core" (coverage on by default for package code).
  - testpaths = ["tests/"]
- Coverage configuration in [tool.coverage.run]: source_pkgs = ["bpod_core"], relative_files = true.

2.2 Running tests directly (no tox)
- Run entire suite with coverage:
  - uv run pytest
- Run a subset by node id:
  - uv run pytest tests/test_com.py::TestToBytes::test_to_bytes_with_numpy_scalar -q
- Run with verbose output and stop on first failure:
  - uv run pytest -vv -x

2.3 Running tests via tox (matrix and tooling)
- tox env list (see tox.ini): ruff, typing, clean, py310–py313, combine
- Typical flows:
  - Run linter: uv run tox -e ruff
  - Type-check: uv run tox -e typing
  - Clean coverage files (per-interpreter coverage split): uv run tox -e clean
  - Run tests for all configured interpreters: uv run tox -e py310,py311,py312,py313
  - Combine coverage files produced by parallel tox runs: uv run tox -e combine
- Notes:
  - Tox uses runner = uv-venv-lock-runner; dependencies come from the test group (and ci group for -ci envs).
  - Coverage files are split by Python version via COVERAGE_FILE = .coverage.{py_dot_ver}.
  - coveralls integration exists (ci group) but is typically invoked only in CI (-ci envs).

2.4 Adding tests
- Location and naming: place new tests in tests/ using pytest discovery conventions (files named test_*.py or *_test.py). Keep tests small, deterministic, and independent from hardware.
- Common test patterns in this repo:
  - Use pytest-mock’s mocker fixture to patch external IO (e.g., pyserial). Example from tests/test_com.py patches bpod_core.com.Serial.
  - Numpy is available; prefer dtype=np.uint8 when dealing with byte conversions (see to_bytes behavior below) to avoid implicit widening.
  - For code that reads from sockets/serial/IPC, design injectable interfaces so tests can provide fakes or use MagicMock.
- Running just the new test module: uv run pytest tests/test_my_feature.py -q
- Coverage: no extra args are needed; --cov=bpod_core is already in addopts.

2.5 Example: a minimal test (verified)
We created a simple demonstration test locally to validate the workflow:

File: tests/test_guidelines_demo.py
- Content validated two utilities:
  - convert_to_snake_case('MyVar42-Value') -> 'my_var_42_value'
  - set_nested/get_nested for nested dict manipulation.
Execution:
- uv run pytest -q
- Result: all tests passed; coverage reported as configured.
Cleanup:
- The demo test file was removed after validation to keep the repository clean, as this document is the permanent artifact.

3. Linting, Formatting, and Typing
- Ruff (lint + isort + many rules): configured under [tool.ruff], [tool.ruff.lint], [tool.ruff.format]. Target Python 3.10; quote-style 'single'.
  - Run checks: uv run ruff check
  - Auto-fix (where safe): uv run ruff check --fix
  - Format check: uv run ruff format --check
  - Format apply: uv run ruff format
  - Ignored rules include: D100–D105, D401, PLR0912/13/15, PLR2004 (see pyproject).
- Docstring style: pydocstyle configured with convention = "numpy" (but many D* checks are disabled as noted above).
- Mypy: configured to check package bpod_core at Python 3.10; warn_return_any and warn_unused_configs are enabled.
  - Run: uv run mypy

4. Project Structure and Notable Internals
- bpod_core/com.py:
  - ExtendedSerial wraps pyserial’s Serial and adds helpers: write_struct/read_struct/query/query_struct/verify, and to_bytes for robust byte conversion.
  - ChunkedSerialReader accumulates bytes and calls process() per fixed-size chunk; tests demonstrate expected buffering semantics.
  - Testing tip: patch bpod_core.com.Serial rather than serial.Serial, because imports are localized in module.
- bpod_core/misc.py:
  - String utilities: sanitize_string, convert_to_snake_case.
  - Nested dict helpers: set_nested, get_nested.
  - Networking: get_local_ipv4 falls back to 127.0.0.1 for common unreachable errors.
- bpod_core/fsm.py, bpod_core/bpod.py, bpod_core/ipc.py:
  - Larger modules with finite-state-machine and IPC logic. Tests exercise numerous code paths; prefer mocking IO and using provided data structures when extending functionality.

5. Practical Notes and Pitfalls
- Byte conversion (to_bytes): Accepts bytes-like, int (0–255), numpy arrays/scalars (uint8), lists of ints (each 0–255), and str (ASCII/UTF-8 bytes). Floats are rejected (TypeError). Values >255 raise ValueError. If you add APIs that accept “bytes-like” inputs, delegate normalization to to_bytes to ensure consistency and reuse test coverage.
- Serial/IPC in tests: Keep unit tests pure by mocking the boundary; see tests/test_com.py for patterns using pytest-mock and MagicMock call assertions.
- Coverage combination: If you test across multiple interpreters using tox, run tox -e combine at the end to consolidate coverage files.
- Documentation build: To work on docs locally:
  - uv sync --no-default-groups --group doc
  - uv run sphinx-build docs/source docs/build

6. Common Commands Cheat Sheet
- Full dev environment: uv sync --group dev
- Run tests: uv run pytest
- Single test: uv run pytest tests/test_com.py::TestToBytes::test_to_bytes_with_numpy_scalar -q
- Lint: uv run ruff check && uv run ruff format --check
- Type-check: uv run mypy
- Tox (all py envs): uv run tox -e py310,py311,py312,py313
- Build sdist/wheel: uv build

7. Contribution Workflow Suggestions
- Prefer small, focused PRs with added/updated tests.
- Keep style consistent by running ruff format/check before committing.
- For public APIs, include or update docstrings in NumPy style; for large features, add or update docs under docs/source/.
- When working with IO/hardware, design abstraction boundaries to enable pure unit tests and fast CI.

Appendix: Verified Test Run
- Baseline suite (pre-demo): 112 passed locally with Python 3.10; coverage ~76% (bpod_core package measured).
- Demo test: added 2 passing tests; total rose to 114; then demo file removed; suite returned to 112.
