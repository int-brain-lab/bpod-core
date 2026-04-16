"""A Python package for communicating with the Bpod Finite State Machine."""

import importlib.metadata

try:
    __version__ = importlib.metadata.version('bpod_core')
except importlib.metadata.PackageNotFoundError:
    from pathlib import Path

    from msgspec import toml
    from packaging.version import Version

    toml_file = Path(__file__).parents[1].joinpath('pyproject.toml')
    with toml_file.open('r', encoding='utf8') as f:
        toml_data = toml.decode(f.read())
    __version__ = str(Version(toml_data['project']['version']))
