import contextlib
import logging
import platform
import struct

import pytest

from bpod_core.bpod import Bpod

with contextlib.suppress(ImportError):
    from mock_serial import MockSerial

logging.basicConfig(level=logging.DEBUG)


@pytest.mark.skipif(platform.system() != 'Linux', reason='MockSerial depends on Linux')
class TestSerial:
    @pytest.fixture(scope='class')
    def device(self):
        device = MockSerial()
        device.open()
        device.stub(name='uint8', receive_bytes=b'A', send_bytes=b'B')
        device.stub(name='array', receive_bytes=b'Ba', send_bytes=b'OK')
        device.stub(name='handshake', receive_bytes=b'6', send_bytes=b'5')
        device.stub(name='disconnect', receive_bytes=b'Z', send_bytes=b'')
        device.stub(name='get_v_minor', receive_bytes=b'f', send_bytes=b'\x00\x00')
        device.stub(name='handshake_port1', receive_bytes=b'{', send_bytes=b'')
        device.stub(name='handshake_port2', receive_bytes=b'}', send_bytes=b'')
        device.stub(name='get_pcb_rev', receive_bytes=b'v', send_bytes=b'\x01')
        device.stub(
            name='get_v_firmware',
            receive_bytes=b'F',
            send_bytes=struct.pack('<HH', 23, 3),
        )
        device.stub(
            name='get_timestamp_transmission',
            receive_bytes=b'G',
            send_bytes=b'\x01',
        )
        device.stub(
            name='get_hw_description',
            receive_bytes=b'H',
            send_bytes=b'\x00\x01d\x00Z\x03\x10\x08\x10\x0cUUUUUXBBPPPP\x10UUUUUXBBPPPPVVVV',
        )
        device.stub(
            name='enable_ports',
            receive_bytes=b'E\x00\x00\x00\x00\x00\x00\x01\x01\x01\x01\x01\x01',
            send_bytes=b'\x01',
        )
        device.stub(
            name='set_sync_channel',
            receive_bytes=b'K\xff\x01',
            send_bytes=b'\x01',
        )
        device.stub(
            name='get_modules',
            receive_bytes=b'M',
            send_bytes=b'\x00\x00\x00\x00\x00',
        )
        device.stub(
            name='set_n_events_per_module',
            receive_bytes=b'%\x0f\x0f\x0f\x0f\x0f\x0f',
            send_bytes=b'\x01',
        )
        yield device

    @pytest.fixture(scope='class')
    def bpod(self, device):
        bpod = Bpod(port=device.port, timeout=0.1)
        yield bpod
        bpod.close()

    def test_datatypes(self, bpod):
        assert bpod.is_open
        assert bpod.query(b'A') == b'B'
        assert bpod.query(b'Ba', '<2s') == (b'OK',)

    def test_initialization(self, bpod):
        assert bpod.info.firmware_version == (23, 0)
        assert bpod.info.max_serial_events == 90
        assert bpod.info.max_bytes_per_serial_message == 3
        assert bpod.info.input_description_array == b'UUUUUXBBPPPP'
        assert bpod.info.output_description_array == b'UUUUUXBBPPPPVVVV'
