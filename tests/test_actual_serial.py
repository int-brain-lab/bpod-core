import pytest

from bpod_core.bpod import Bpod

bpod_port = Bpod._identify_bpod()[0]


@pytest.mark.skipif(bpod_port is None, reason='No Bpod device found')
class TestSerial:
    @pytest.fixture(scope='class')
    def bpod(self):
        bpod = Bpod(bpod_port)
        yield bpod
        bpod.close()

    def test_serial_number(self, bpod):
        sn = bpod.serial_number
        bpod.close()
        Bpod(serial_number=sn)
