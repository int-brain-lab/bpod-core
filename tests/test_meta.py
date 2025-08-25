import importlib.metadata
import re
import sys
from pathlib import Path
from typing import cast

import pytest
from msgspec import toml
from packaging.version import Version

from bpod_core import __version__ as bpod_core_version


def test_version_found(monkeypatch):
    monkeypatch.setattr(importlib.metadata, 'version', lambda _: '1.2.3')
    sys.modules.pop('bpod_core', None)
    import bpod_core  # noqa: PLC0415

    assert bpod_core.__version__ == '1.2.3'


def test_version_not_found(monkeypatch):
    def raise_not_found(name):
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, 'version', raise_not_found)
    sys.modules.pop('bpod_core', None)
    import bpod_core  # noqa: PLC0415

    assert Version(bpod_core.__version__) > Version('0.0.0')


@pytest.fixture
def pyproject_version() -> str:
    toml_file = Path(__file__).parents[1].joinpath('pyproject.toml')
    with toml_file.open('r', encoding='utf8') as f:
        toml_data = toml.decode(f.read(), type=dict)
    return cast('str', toml_data.get('project', {}).get('version', ''))


def test_version_pep440(pyproject_version):
    """Test that the version in pyproject.toml is PEP 440 compliant."""
    assert (
        re.match(
            (
                r'^([1-9][0-9]*!)?(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))*((a|b|rc)'
                r'(0|[1-9][0-9]*))?(\.post(0|[1-9][0-9]*))?(\.dev(0|[1-9][0-9]*))?$'
            ),
            pyproject_version,
        )
        is not None
    ), 'Project version in pyproject.toml is not PEP 440 compliant'


def test_changelog():
    """Test that the current version is mentioned in the changelog."""
    changelog_path = Path(__file__).parents[1].joinpath('CHANGELOG.md')
    assert changelog_path.exists(), 'changelog file does not exist'
    pattern = re.compile(r'^## \[(\S+)\] - .*')
    with changelog_path.open() as f:
        for line in f:
            match = pattern.match(line)
            if match:
                changelog_version = match.group(1)
                if Version(changelog_version) == Version(bpod_core_version):
                    return  # Found the version, test passes
    raise AssertionError(
        f'version {bpod_core_version} is not contained in the CHANGELOG.md file'
    )
