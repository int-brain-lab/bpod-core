import re
from pathlib import Path

from packaging.version import Version

from bpod_core import __version__ as bpod_core_version


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
