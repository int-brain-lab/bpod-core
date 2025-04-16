import pytest

from bpod_core.bpod import Bpod, find_bpod_ports
from bpod_core.serial_extensions import SerialSingletonException

bpod_port = next(find_bpod_ports(), None)


@pytest.mark.skipif(bpod_port is None, reason='No Bpod device found')
class TestSerial:
    @pytest.fixture(scope='class')
    def bpod(self):
        bpod = Bpod(port=bpod_port)
        yield bpod
        bpod.close()

    def test_change_port(self, bpod):
        with pytest.raises(SerialSingletonException):
            bpod.port = 'some_port'

    def test_serial_number(self, bpod):
        sn = bpod.info.serial_number
        new_bpod = Bpod(serial_number=sn)
        assert new_bpod == bpod
