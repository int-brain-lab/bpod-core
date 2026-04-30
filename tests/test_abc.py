"""Tests for AbstractBpod concrete properties."""

from unittest.mock import patch

import pytest

from bpod_core.bpod.abc import AbstractBpod
from bpod_core.bpod.structs import VersionInfo


@pytest.fixture
def bpod():
    """Instantiate AbstractBpod directly by suppressing ABC enforcement."""
    with patch.object(AbstractBpod, '__abstractmethods__', frozenset()):
        return AbstractBpod()


class TestAbstractBpod:
    def test_version(self, bpod):
        """version property returns the assigned VersionInfo."""
        info = VersionInfo(
            firmware=(23, 0), machine=3, machine_str='Bpod 2+', pcb=None, bpod_core='0'
        )
        bpod._version = info
        assert bpod.version is info

    def test_serial_number(self, bpod):
        """serial_number property returns the assigned serial number string."""
        bpod._serial_number = 'ABC123'
        assert bpod.serial_number == 'ABC123'