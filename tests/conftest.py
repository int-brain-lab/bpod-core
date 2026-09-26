import re
import struct
from unittest.mock import PropertyMock

import pytest

from bpod_core import ipc
from bpod_core.bpod import Bpod, TimeReferences
from bpod_core.com import ExtendedSerial
from bpod_core.constants import VID_TEENSY, TeensyPID

fixture_bpod_all = {
    b'6': b'5',
    b'f': b'\x00\x00',
    b'v': b'\x01',
    b'C[\\x00\\x01]{2}.*': b'',
    rb'\*': b'\x01',
}

# Bpod 2.0 with firmware version 22
fixture_bpod_20 = {
    **fixture_bpod_all,
    # b'F': b'\x16\x00\x03\x00',
    b'F': b'\x17\x00\x03\x00',
    b'H': b'\x00\x01d\x00i\x05\x10\x08\x10\rUUUUUXBBPPPP\x11UUUUUXBBPPPPVVVV',
    b'M': b'\x00\x00\x00\x00\x00',
    b'E[\\x00\\x01]{13}': b'\x01',
}

# Bpod 2.5 with firmware version 23
fixture_bpod_25 = {
    **fixture_bpod_all,
    b'F': b'\x17\x00\x03\x00',
    b'H': b'\x00\x01d\x00i\x05\x10\x08\x10\rUUUUUXZBBPPPP\x11UUUUUXZBBPPPPVVVV',
    b'M': b'\x00\x00\x00\x00\x00',
    b'E[\\x00\\x01]{13}': b'\x01',
}

# FlexIO.reset() on connect sends a single combined write for all 4 channels
# (Q, ^, o, m, p, t, plus 8 per-threshold 'e' ops), expecting 14 acks in one go.
_flexio_reset_2p = (
    struct.pack('<c4B', b'Q', *(4,) * 4)
    + struct.pack('<cI', b'^', 10)
    + struct.pack('<cB', b'o', 3)
    + struct.pack('<c4B', b'm', *(0,) * 4)
    + struct.pack('<c8B', b'p', *(0,) * 8)
    + struct.pack('<c8H', b't', *(4095,) * 8)
    + b''.join(
        struct.pack('<cBB?', b'e', ch, th, False) for ch in range(4) for th in (0, 1)
    )
)

# Bpod 2+ with firmware version 23
fixture_bpod_2p = {
    **fixture_bpod_all,
    b'F': b'\x17\x00\x04\x00',
    b'H': (
        b'\x00\x01d\x00K\x05\x10\x08\x10\x10UUUXZFFFFBBPPPPP\x15UUUXZFFFFBBPPPPPVVVVV'
    ),
    b'M': b'\x00\x00\x00',
    b'E[\\x00\\x01]{16}': b'\x01',
    re.escape(_flexio_reset_2p): b'\x01' * 14,
    b'Q[\\x00-\\x04]{4}': b'\x01',
    b'\\^[\\x00-\\xff]{4}': b'\x01',
    b'o[\\x01-\\x04]': b'\x01',
    b'm[\\x00-\\xff]{4}': b'\x01',
    b'p[\\x00-\\xff]{8}': b'\x01',
    b't[\\x00-\\xff]{16}': b'\x01',
    b'e[\\x00-\\x03][\\x00-\\x01][\\x00-\\x01]': b'\x01',
}


@pytest.fixture
def mock_serial_discovery(mocker):
    return mocker.patch('bpod_core.bpod.verify_serial_discovery', return_value=True)


@pytest.fixture
def mock_comports(mocker, mock_serial_discovery):
    """Fixture to mock available COM ports."""
    mock_port_info = mocker.MagicMock()
    mock_port_info.device = 'COM3'
    mock_port_info.serial_number = '12345'
    mock_port_info.vid = VID_TEENSY
    mock_port_info.pid = TeensyPID.SERIAL
    mock_comports = mocker.patch('bpod_core.com.comports')
    mock_comports.return_value = [mock_port_info]
    return mock_comports


@pytest.fixture
def mock_ext_serial(mocker):
    """Mock base class methods for ExtendedSerial."""
    extended_serial = ExtendedSerial()
    extended_serial.response_buffer = bytearray()
    extended_serial.mock_responses = {}
    extended_serial.last_write = b''

    def write(data) -> None:
        if len(data) == 0:  # e.g., ExtendedSerial.verify() reading an acknowledgement
            return
        for pattern, value in extended_serial.mock_responses.items():
            if re.match(pattern, data):
                extended_serial.response_buffer.extend(value)
                extended_serial.last_write = data
                return
        raise AssertionError(f'No matching response for input {data}')

    def read(size: int = 1) -> bytes:
        response = bytes(extended_serial.response_buffer[:size])
        del extended_serial.response_buffer[:size]
        return response

    def in_waiting() -> int:
        return len(extended_serial.response_buffer)

    tmp = 'bpod_core.com.Serial'
    mocker.patch(f'{tmp}.__init__', return_value=None)
    mocker.patch(f'{tmp}.__enter__', return_value=extended_serial)
    mocker.patch(f'{tmp}.open')
    mocker.patch(f'{tmp}.close')
    mocker.patch(f'{tmp}.write', side_effect=write)
    mocker.patch(f'{tmp}.read', side_effect=read)

    def readinto(buf) -> int:
        n = len(buf)
        chunk = bytes(extended_serial.response_buffer[:n])
        del extended_serial.response_buffer[:n]
        buf[: len(chunk)] = chunk
        return len(chunk)

    mocker.patch(f'{tmp}.reset_input_buffer')
    mocker.patch(f'{tmp}.in_waiting', new_callable=PropertyMock, side_effect=in_waiting)
    mocker.patch(f'{tmp}.readinto', side_effect=readinto)
    return extended_serial


@pytest.fixture
def mock_zeroconf(mocker):
    """Mock Zeroconf class."""
    return mocker.patch('bpod_core.ipc.Zeroconf', spec=ipc.Zeroconf)


@pytest.fixture
def mock_local_discovery_dir(tmp_path, mocker):
    """Mock runtime directory for local advertisements."""
    mocker.patch.object(ipc.LocalServiceAdvertisement, '_runtime_directory', tmp_path)
    return tmp_path


@pytest.fixture
def mock_advertisement(mock_zeroconf, mock_local_discovery_dir):
    """Mock, both, zeroconf and local advertisement."""
    return {'zeroconf': mock_zeroconf, 'runtime_dir': mock_local_discovery_dir}


@pytest.fixture
def mock_ports_file(tmp_path, mocker):
    """Mock the state file storing per-UUID TCP ports."""
    ports_file = tmp_path / 'service_ports.json'
    mocker.patch.object(ipc.ServiceHost, '_ports_file', ports_file)
    return ports_file


@pytest.fixture
def mock_bpod(mocker, mock_ext_serial, mock_settings):
    mock_bpod = mocker.MagicMock(spec=Bpod)
    mock_bpod.is_running = False
    mock_bpod._time_reference = mocker.MagicMock(spec=TimeReferences)
    mock_bpod._softcode_thread = mocker.MagicMock()
    mock_bpod.serial0 = mock_ext_serial
    mock_bpod._identify_bpod.side_effect = lambda *args, **kwargs: Bpod._identify_bpod(
        mock_bpod,
        *args,
        **kwargs,
    )
    return mock_bpod


@pytest.fixture
def mock_settings(mocker):
    mock_settings = mocker.MagicMock()
    mocker.patch('bpod_core.bpod.SettingsDict', return_value=mock_settings)


@pytest.fixture
def mock_bpod_20(
    mock_comports, mock_ext_serial, mock_settings, mocker, mock_advertisement
):
    mock_ext_serial.mock_responses.update(fixture_bpod_20)
    mocker.patch('bpod_core.com.ExtendedSerial', return_value=mock_ext_serial)
    mocker.patch(
        'bpod_core.bpod.Bpod._detect_additional_serial_ports', return_value=(None, None)
    )
    mocker.patch('bpod_core.bpod.ServiceHost')
    return Bpod('COM3')


@pytest.fixture
def mock_bpod_25(
    mock_comports, mock_ext_serial, mock_settings, mocker, mock_advertisement
):
    mock_ext_serial.mock_responses.update(fixture_bpod_25)
    mocker.patch('bpod_core.com.ExtendedSerial', return_value=mock_ext_serial)
    mocker.patch(
        'bpod_core.bpod.Bpod._detect_additional_serial_ports', return_value=(None, None)
    )
    mocker.patch('bpod_core.bpod.ServiceHost')
    return Bpod('COM3')


@pytest.fixture
def mock_bpod_2p(
    mock_comports, mock_ext_serial, mock_settings, mocker, mock_advertisement
):
    mock_ext_serial.mock_responses.update(fixture_bpod_2p)
    mocker.patch('bpod_core.com.ExtendedSerial', return_value=mock_ext_serial)
    mocker.patch(
        'bpod_core.bpod.Bpod._detect_additional_serial_ports', return_value=(None, None)
    )
    mocker.patch('bpod_core.bpod.ServiceHost')
    return Bpod('COM3')
