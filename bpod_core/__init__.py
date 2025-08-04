"""A Python package for communicating with the Bpod Finite State Machine."""

import importlib.metadata

try:
    __version__ = importlib.metadata.version('bpod_core')
except importlib.metadata.PackageNotFoundError:
    __version__ = '0.0.0.dev0'  # Fallback for development mode
