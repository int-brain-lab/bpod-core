# Contributing

## Setting Up the Development Environment

This project uses [UV](https://github.com/astral-sh/uv) as its package manager for
managing dependencies and ensuring consistent and reproducible environments. To install
UV:

**Linux and macOS:**

```console
$ curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell):**

```pwsh-session
PS> powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

See [UV's documentation](https://docs.astral.sh/uv/) for details.

Once UV is installed, synchronize your environment with the dependencies specified in
the `pyproject.toml` file, including development dependencies:

```console
$ uv sync
```

## Testing and Code Quality

We use [tox](https://tox.wiki/) to automate all testing and code quality checks.
Running tox will execute the full suite of checks across several Python versions:

- [pytest](https://docs.pytest.org/) — unit-tests (located in the `tests` directory)
- [mypy](https://mypy-lang.org/) — static type checking
- [ruff](https://docs.astral.sh/ruff/) — linting and formatting checks

To run all checks, execute:

```console
$ uv run tox -p
```

Tox will create isolated environments for each check and Python version. The terminal
output will indicate whether the checks passed or failed.

To run individual tools against your current environment:

```console
$ uv run pytest          # run unit-tests
$ uv run mypy            # run type checking
$ uv run ruff check      # check for linting issues
$ uv run ruff format     # auto-format code
```

Adding `--fix` to `ruff check` will automatically correct fixable issues.

After running `tox` or `pytest`, you can generate a coverage report to assess how
much of the code is covered by the unit-tests:

```console
$ uv run coverage report
```

For a more detailed representation, generate an HTML report:

```console
$ uv run coverage html
```

You'll find the HTML report in the folder `htmlcov`, where you can open `index.html`
in a web browser to view detailed coverage statistics.

## Pull Requests

All development work should be based on the `develop` branch. To contribute:

1. Create a new branch from `develop`:

   ```console
   $ git checkout develop
   $ git checkout -b your-branch-name
   ```

2. Make your changes, keeping each pull request focused on a single topic (feature,
   bugfix, refactor, etc.).

3. Before opening a pull request, ensure that all tests pass and the code is properly
   formatted (see [Testing and Code Quality](#testing-and-code-quality)).

4. Open your pull request against the `develop` branch. The `main` branch only
   receives merges from `develop` as part of the release process.

## Building the Documentation

We use [Sphinx](https://www.sphinx-doc.org/) to build our documentation and
API reference. To build the documentation, run the following command:

```console
$ uv run sphinx-build docs/source docs/build
```

After running this command, you can view the generated documentation in your
web browser by opening `docs/build/index.html`.

## Building the Package

To build bpod-core as a distributable Python package, execute the following command:

```console
$ uv build
```

This command will create a distributable package of bpod-core, in the form of a source
distribution (sdist) and a wheel (bdist_wheel). The generated package files will be
located in the `dist` directory.

## Versioning Scheme

bpod-core uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Its version
string is a combination of three fields, separated by dots:

**`MAJOR` . `MINOR` . `PATCH`**

- The `MAJOR` field is only incremented for breaking changes, i.e., changes that are
  not backward compatible with previous changes.
- The `MINOR` field will be incremented upon adding new, backward compatible features.
- The `PATCH` field will be incremented with each new, backward compatible bugfix
  release that does not implement a new feature.
- Optionally appended letters can be used to indicate an alpha release (`a`), a beta
  release (`b`) or a release candidate (`rc`).

On the developer side, these fields are controlled by both

1. adjusting the `version` field in `pyproject.toml`, and
2. adding the corresponding version string to a commit as a
   [git tag](https://git-scm.com/book/en/v2/Git-Basics-Tagging), for instance:

   ```console
   $ git tag 1.2.3
   $ git push origin --tags
   ```
