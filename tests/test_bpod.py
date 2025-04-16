import logging

import pytest

from bpod_core.bpod import Bpod
from bpod_core.serial_extensions import SerialSingleton, SerialSingletonException

logging.basicConfig(level=logging.DEBUG)


class TestBpod:
    def test_unconnected(self):
        bpod = Bpod(connect=False)
        assert not bpod.is_open

    def test_singleton(self):
        # create a few instances
        bpod1 = Bpod(connect=False)
        bpod2 = Bpod(connect=False)
        bpod3 = Bpod('FakePort3', connect=False)
        bpod4 = Bpod('FakePort4', connect=False)
        bpod5 = Bpod(port='FakePort4', connect=False)

        # assert that a port blocked by *another* SerialSingleton child can't be used
        with pytest.raises(SerialSingletonException):
            SerialSingleton('FakePort4')

        # assert that port cannot be changed after initialization
        with pytest.raises(SerialSingletonException):
            bpod5.port = 'some_other_port'

        # assert singleton behavior
        assert bpod1 is bpod2
        assert bpod1 is not bpod3
        assert bpod3 is not bpod4
        assert bpod4 is bpod5

    def test_set_port(self):
        bpod = Bpod(connect=False)
        with pytest.raises(TypeError):
            bpod.setPort()
