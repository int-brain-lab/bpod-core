import re
from unittest.mock import PropertyMock

import pytest

from bpod_core import ipc
from bpod_core.bpod import Bpod
from bpod_core.com import ExtendedSerial
from bpod_core.constants import VID_TEENSY, TeensyPID

fixture_bpod_all = {
    b'6': b'5',
    b'f': b'\x00\x00',
    b'v': b'\x01',
    b'C[\\x00\\x01]{2}.*': b'',
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

# Bpod 2+ with firmware version 23
fixture_bpod_2p = {
    **fixture_bpod_all,
    b'F': b'\x17\x00\x04\x00',
    b'H': (
        b'\x00\x01d\x00K\x05\x10\x08\x10\x10UUUXZFFFFBBPPPPP\x15UUUXZFFFFBBPPPPPVVVVV'
    ),
    b'M': b'\x00\x00\x00',
    b'E[\\x00\\x01]{16}': b'\x01',
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
    mocker.patch(f'{tmp}.reset_input_buffer')
    mocker.patch(f'{tmp}.in_waiting', new_callable=PropertyMock, side_effect=in_waiting)
    return extended_serial


@pytest.fixture
def mock_zeroconf(mocker):
    """Mock Zeroconf class."""
    return mocker.patch('bpod_core.ipc.Zeroconf', spec=ipc.Zeroconf)


@pytest.fixture
def mock_local_discovery_dir(tmp_path, mocker):
    """Mock runtime directory for local advertisements."""
    mocker.patch.object(ipc.LocalServiceAdvertisement, 'runtime_directory', tmp_path)
    return tmp_path


@pytest.fixture
def mock_advertisement(mock_zeroconf, mock_local_discovery_dir):
    """Mock, both, zeroconf and local advertisement."""
    return {'zeroconf': mock_zeroconf, 'runtime_dir': mock_local_discovery_dir}


@pytest.fixture
def mock_bpod(mocker, mock_ext_serial, mock_settings):
    mock_bpod = mocker.MagicMock(spec=Bpod)
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
    mocker.patch('bpod_core.bpod.Bpod._detect_additional_serial_ports')
    mocker.patch('bpod_core.bpod.ServiceHost')
    return Bpod('COM3')


@pytest.fixture
def mock_bpod_25(
    mock_comports, mock_ext_serial, mock_settings, mocker, mock_advertisement
):
    mock_ext_serial.mock_responses.update(fixture_bpod_25)
    mocker.patch('bpod_core.com.ExtendedSerial', return_value=mock_ext_serial)
    mocker.patch('bpod_core.bpod.Bpod._detect_additional_serial_ports')
    mocker.patch('bpod_core.bpod.ServiceHost')
    return Bpod('COM3')


@pytest.fixture
def mock_bpod_2p(
    mock_comports, mock_ext_serial, mock_settings, mocker, mock_advertisement
):
    mock_ext_serial.mock_responses.update(fixture_bpod_2p)
    mocker.patch('bpod_core.com.ExtendedSerial', return_value=mock_ext_serial)
    mocker.patch('bpod_core.bpod.Bpod._detect_additional_serial_ports')
    mocker.patch('bpod_core.bpod.ServiceHost')
    return Bpod('COM3')
