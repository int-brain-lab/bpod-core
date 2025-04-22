import re
from pathlib import Path

from bpod_core import __version__ as bpod_core_version


def test_semantic_versioning():
    """Test bpod_core version for correct syntax."""
    pattern = (
        r'^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)'
        r'(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)'
        r'(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?'
        r'(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$'
    )
    assert re.match(pattern, bpod_core_version) is not None


def test_changelog():
    """Test that the current version is mentioned in the changelog."""
    changelog_path = Path(__file__).parents[1].joinpath('CHANGELOG.md')
    assert changelog_path.exists(), 'changelog file does not exist'
    with open(changelog_path) as file:
        content = file.read()
    assert f'## [{bpod_core_version}]' in content, (
        f'version {bpod_core_version} is not contained in the CHANGELOG.md file'
    )
