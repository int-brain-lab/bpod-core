import re
from unittest.mock import MagicMock, PropertyMock

import pytest

from bpod_core.bpod import Bpod
from bpod_core.com import ExtendedSerial

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
def mock_comports(mocker):
    """Fixture to mock available COM ports."""
    mock_port_info = MagicMock()
    mock_port_info.device = 'COM3'
    mock_port_info.serial_number = '12345'
    mock_port_info.vid = 0x16C0  # supported VID
    mock_comports = mocker.patch('bpod_core.bpod.comports')
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
def mock_bpod(mock_ext_serial, mock_settings):
    mock_bpod = MagicMock(spec=Bpod)
    mock_bpod.serial0 = mock_ext_serial
    mock_bpod._identify_bpod.side_effect = lambda *args, **kwargs: Bpod._identify_bpod(
        mock_bpod,
        *args,
        **kwargs,
    )
    mock_bpod._sends_discovery_byte.side_effect = (
        lambda *args, **kwargs: Bpod._sends_discovery_byte(mock_bpod, *args, **kwargs)
    )
    return mock_bpod


@pytest.fixture
def mock_settings(mocker):
    mock_settings = MagicMock()
    mocker.patch('bpod_core.bpod.SettingsDict', return_value=mock_settings)


@pytest.fixture
def mock_bpod_20(mock_comports, mock_ext_serial, mock_settings, mocker):  # noqa: ARG001
    mock_ext_serial.mock_responses.update(fixture_bpod_20)
    mocker.patch('bpod_core.bpod.ExtendedSerial', return_value=mock_ext_serial)
    mocker.patch('bpod_core.bpod.Bpod._detect_additional_serial_ports')
    mocker.patch('bpod_core.bpod.DualChannelHost')
    return Bpod('COM3')


@pytest.fixture
def mock_bpod_25(mock_comports, mock_ext_serial, mock_settings, mocker):  # noqa: ARG001
    mock_ext_serial.mock_responses.update(fixture_bpod_25)
    mocker.patch('bpod_core.bpod.ExtendedSerial', return_value=mock_ext_serial)
    mocker.patch('bpod_core.bpod.Bpod._detect_additional_serial_ports')
    mocker.patch('bpod_core.bpod.DualChannelHost')
    return Bpod('COM3')


@pytest.fixture
def mock_bpod_2p(mock_comports, mock_ext_serial, mock_settings, mocker):  # noqa: ARG001
    mock_ext_serial.mock_responses.update(fixture_bpod_2p)
    mocker.patch('bpod_core.bpod.ExtendedSerial', return_value=mock_ext_serial)
    mocker.patch('bpod_core.bpod.Bpod._detect_additional_serial_ports')
    mocker.patch('bpod_core.bpod.DualChannelHost')
    return Bpod('COM3')
