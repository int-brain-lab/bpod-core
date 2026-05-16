# Contributing

Contributions to bpod-core's codebase are very welcome! Whether you're fixing a bug,
adding a feature, or improving the documentation, we appreciate your help.

Before starting work on a non-trivial contribution, please check the
[issue tracker](https://github.com/int-brain-lab/bpod-core/issues) to see if the topic
is already being discussed or worked on. If not, open a new issue to describe what you
have in mind. This helps avoid duplicate effort and ensures your contribution is aligned
with the project's direction before you invest significant time.

## Development Environment

This project uses [uv](https://github.com/astral-sh/uv) as its package manager for
managing dependencies and ensuring consistent and reproducible environments. See
[uv's documentation](https://docs.astral.sh/uv/getting-started/installation/) for
installation instructions.

Once uv is installed, clone the repository and check out the `develop` branch:

```console
$ git clone -b develop https://github.com/int-brain-lab/bpod-core.git
```

Then synchronize your environment with the project's dependencies:

```console
$ cd bpod-core
$ uv sync
```

## Making Changes

All development work should be based on the `develop` branch. Create a new branch for
your contribution:

```console
$ git checkout develop
$ git checkout -b your-branch-name
```

Keep each branch focused on a single topic (feature, bugfix, refactor, etc.).

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

## Opening a Pull Request

Before opening a pull request, ensure that all tests pass and the code is properly
formatted (see [Testing and Code Quality](#testing-and-code-quality)).

Open your pull request against the `develop` branch. The `main` branch only receives
merges from `develop` as part of the release process.

## For Maintainers

### Building the Documentation

We use [Sphinx](https://www.sphinx-doc.org/) to build our documentation and
API reference. To build the documentation, run the following command:

```console
$ uv run sphinx-build docs/source docs/build
```

After running this command, you can view the generated documentation in your
web browser by opening `docs/build/index.html`.

### Building the Package

To build bpod-core as a distributable Python package, execute the following command:

```console
$ uv build
```

This command will create a distributable package of bpod-core, in the form of a source
distribution (sdist) and a wheel (bdist_wheel). The generated package files will be
located in the `dist` directory.

### Versioning Scheme

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

Use `uv version` to increment bpod-core's version prior to release:

```console
$ uv version --bump patch  # 1.2.3 -> 1.2.4
$ uv version --bump minor  # 1.2.3 -> 1.3.0
$ uv version --bump major  # 1.2.3 -> 2.0.0
$ uv version --bump alpha  # 1.2.3a1 -> 1.2.3a2  (or 1.2.3a1 -> 1.2.3a2)
```

The same pattern applies to `beta` and `rc`. To start an alpha on a minor or major bump,
combine flags: `--bump minor --bump alpha` → `1.3.0a1`.

Then tag the commit accordingly and push the tag:

```console
$ git tag 1.2.4
$ git push origin --tags
```
