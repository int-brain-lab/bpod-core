import logging
import struct
from unittest.mock import MagicMock, patch

import pytest
from serial import SerialException

from bpod_core.bpod import Bpod, BpodError
from bpod_core.serial_extensions import ExtendedSerial


class TestBpodIdentifyBpod:
    @pytest.fixture
    def mock_comports(self):
        """Fixture to mock available COM ports."""
        mock_port_info = MagicMock()
        mock_port_info.device = 'COM3'
        mock_port_info.serial_number = '12345'
        mock_port_info.vid = 0x16C0  # supported VID
        with patch('bpod_core.bpod.comports') as mock_comports:
            mock_comports.return_value = [mock_port_info]
            yield mock_comports

    @pytest.fixture
    def mock_serial(self):
        """Fixture to mock serial communication."""
        mock_serial_instance = MagicMock()
        mock_serial_instance.read.return_value = bytes([222])
        mock_serial_instance.validate_response.return_value = True
        mock_serial_instance.__enter__.return_value = mock_serial_instance
        with patch(
            'bpod_core.bpod.ExtendedSerial', return_value=mock_serial_instance
        ) as mock_serial:
            yield mock_serial

    def test_automatic_success(self, mock_serial, mock_comports):
        """Test successful identification of Bpod without specifying port or serial."""
        port, serial_number = Bpod._identify_bpod()
        assert port == 'COM3'
        assert serial_number == '12345'
        mock_serial.assert_called_once_with('COM3', timeout=0.15)

    def test_automatic_unsupported_vid(self, mock_serial, mock_comports):
        """Test failure to auto identify Bpod when only device has unsupported VID."""
        mock_port_info = mock_comports.return_value
        mock_port_info[0].vid = 0x0000  # unsupported VID
        with pytest.raises(BpodError, match=r'No .* Bpod found'):
            Bpod._identify_bpod()
        mock_serial.assert_not_called()

    def test_automatic_no_devices(self, mock_serial, mock_comports):
        """Test failure to auto identify Bpod when no COM ports are available."""
        mock_comports.return_value = []
        with pytest.raises(BpodError, match=r'No .* Bpod found'):
            Bpod._identify_bpod()
        mock_serial.assert_not_called()

    def test_automatic_no_discovery_byte(self, mock_serial, mock_comports):
        """Test failure to auto identify Bpod when no discovery byte is received."""
        mock_serial_instance = mock_serial.return_value
        mock_serial_instance.read.return_value = b''
        with pytest.raises(BpodError, match='No .* Bpod found'):
            Bpod._identify_bpod()
        mock_serial.assert_called_once_with('COM3', timeout=0.15)

    def test_automatic_serial_exception(self, mock_serial, mock_comports):
        """Test failure to auto identify Bpod when serial read raises exception."""
        mock_serial_instance = mock_serial.return_value
        mock_serial_instance.read.side_effect = SerialException
        with pytest.raises(BpodError, match='No .* Bpod found'):
            Bpod._identify_bpod()
        mock_serial.assert_called_once_with('COM3', timeout=0.15)

    def test_serial_success(self, mock_serial, mock_comports):
        """Test successful identification of Bpod when specifying serial (non-eager)."""
        port, serial_number = Bpod._identify_bpod(serial_number='12345')
        assert port == 'COM3'
        assert serial_number == '12345'  # existing serial
        mock_serial.assert_called_once_with('COM3', timeout=0.15)

    def test_serial_incorrect_serial(self, mock_serial, mock_comports):
        """Test failure to identify Bpod when specifying incorrect serial."""
        with pytest.raises(BpodError, match='No .* serial number'):
            Bpod._identify_bpod(serial_number='00000')
        mock_serial.assert_not_called()

    def test_serial_unsupported_vid(self, mock_serial, mock_comports):
        """Test failure to identify Bpod by serial if device has incompatible VID."""
        mock_port_info = mock_comports.return_value
        mock_port_info[0].vid = 0x0000  # unsupported VID
        with pytest.raises(BpodError, match='.* not .* supported Bpod'):
            Bpod._identify_bpod(serial_number='12345')
        mock_serial.assert_called_once_with('COM3', timeout=0.15)

    def test_port_success(self, mock_serial, mock_comports):
        """Test successful identification of Bpod when specifying port."""
        port, serial_number = Bpod._identify_bpod(port='COM3')
        assert port == 'COM3'
        assert serial_number == '12345'  # existing serial
        mock_serial.assert_not_called()

    def test_port_incorrect_port(self, mock_serial, mock_comports):
        """Test failure to identify Bpod when specifying incorrect port."""
        with pytest.raises(BpodError, match='Port not found'):
            Bpod._identify_bpod(port='incorrect_port')
        mock_serial.assert_not_called()

    def test_port_unsupported_vid(self, mock_serial, mock_comports):
        """Test failure to identify Bpod when specifying incorrect port."""
        mock_port_info = mock_comports.return_value
        mock_port_info[0].vid = 0x0000  # unsupported VID
        with pytest.raises(BpodError, match='.* not .* supported Bpod'):
            Bpod._identify_bpod(port='COM3')
        mock_serial.assert_not_called()


@pytest.fixture
def mock_serial():
    """Mock read and write methods for ExtendedSerial."""
    response_buffer = bytearray()
    extended_serial = ExtendedSerial()
    extended_serial.mock_responses = dict()

    def write(data):
        nonlocal response_buffer
        assert data in extended_serial.mock_responses
        response_buffer.extend(extended_serial.mock_responses.get(data, b''))

    def read(size) -> bytes:
        nonlocal response_buffer
        assert size <= len(response_buffer)
        response = bytes(response_buffer[:size])
        del response_buffer[:size]
        return response

    def in_waiting() -> int:
        nonlocal response_buffer
        return len(response_buffer)

    patched_object_base = 'bpod_core.serial_extensions.serial.Serial'
    with (
        patch(f'{patched_object_base}.write', side_effect=write),
        patch(f'{patched_object_base}.read', side_effect=read),
        patch(f'{patched_object_base}.in_waiting', side_effect=in_waiting),
    ):
        yield extended_serial


class TestGetVersionInfo:
    def test_get_version_info(self, mock_serial):
        """Test retrieval of version info with supported firmware and hardware."""
        mock_serial.mock_responses = {
            b'F': struct.pack('<2H', 23, 3),  # Firmware version 23, Bpod type 3
            b'f': struct.pack('<H', 1),  # Minor firmware version 1
            b'v': struct.pack('<B', 2),  # PCB revision 2
        }
        bpod = MagicMock(spec=Bpod)
        bpod.serial0 = mock_serial
        Bpod._get_version_info(bpod)
        assert bpod._version.firmware == (23, 1)
        assert bpod._version.machine == 3
        assert bpod._version.pcb == 2

    def test_get_version_info_unsupported_firmware(self, mock_serial):
        """Test failure when firmware version is unsupported."""
        mock_serial.mock_responses = {
            b'F': struct.pack('<2H', 20, 3),  # Firmware version 20, Bpod type 3
            b'f': struct.pack('<H', 1),  # Minor firmware version 1
        }
        bpod = MagicMock(spec=Bpod)
        bpod.serial0 = mock_serial
        with pytest.raises(BpodError, match='firmware .* is not supported'):
            Bpod._get_version_info(bpod)

    def test_get_version_info_unsupported_hardware(self, mock_serial):
        """Test failure when hardware version is unsupported."""
        mock_serial.mock_responses = {
            b'F': struct.pack('<2H', 23, 2),  # Firmware version 23, Bpod type 2
            b'f': struct.pack('<H', 1),  # Minor firmware version 1
        }
        bpod = MagicMock(spec=Bpod)
        bpod.serial0 = mock_serial
        with pytest.raises(BpodError, match='hardware .* is not supported'):
            Bpod._get_version_info(bpod)


class TestGetHardwareConfiguration:
    def test_get_version_info_v23(self, mock_serial):
        """Test retrieval of hardware configuration (firmware version 23)."""
        mock_serial.mock_responses = {
            b'H': struct.pack(
                '<2H6B16s1B21s',
                256,  # max_states
                100,  # timer_period
                75,  # max_serial_events
                5,  # max_bytes_per_serial_message
                16,  # n_global_timers
                8,  # n_global_counters
                16,  # n_conditions
                16,  # n_inputs
                b'UUUXZFFFFBBPPPPP',  # input_description
                21,  # n_outputs
                b'UUUXZFFFFBBPPPPPVVVVV',  # output_description
            ),
        }
        bpod = MagicMock(spec=Bpod)
        bpod.serial0 = mock_serial
        bpod.version = MagicMock()
        bpod.version.firmware = (23, 0)
        Bpod._get_hardware_configuration(bpod)
        assert bpod._hardware_config.max_states == 256
        assert bpod._hardware_config.timer_period == 100
        assert bpod._hardware_config.max_serial_events == 75
        assert bpod._hardware_config.max_bytes_per_serial_message == 5
        assert bpod._hardware_config.n_global_timers == 16
        assert bpod._hardware_config.n_global_counters == 8
        assert bpod._hardware_config.n_conditions == 16
        assert bpod._hardware_config.n_inputs == 16
        assert bpod._hardware_config.input_description == b'UUUXZFFFFBBPPPPP'
        assert bpod._hardware_config.n_outputs == 21
        assert bpod._hardware_config.output_description == b'UUUXZFFFFBBPPPPPVVVVV'
        assert mock_serial.in_waiting() == 0

    def test_get_version_info_v22(self, mock_serial):
        """Test retrieval of hardware configuration (firmware version 22)."""
        mock_serial.mock_responses = {
            b'H': struct.pack(
                '<2H5B16s1B21s',
                256,  # max_states
                100,  # timer_period
                75,  # max_serial_events
                16,  # n_global_timers
                8,  # n_global_counters
                16,  # n_conditions
                16,  # n_inputs
                b'UUUXZFFFFBBPPPPP',  # input_description
                21,  # n_outputs
                b'UUUXZFFFFBBPPPPPVVVVV',  # output_description
            ),
        }
        bpod = MagicMock(spec=Bpod)
        bpod.serial0 = mock_serial
        bpod.version = MagicMock()
        bpod.version.firmware = (22, 0)
        Bpod._get_hardware_configuration(bpod)
        assert bpod._hardware_config.max_states == 256
        assert bpod._hardware_config.timer_period == 100
        assert bpod._hardware_config.max_serial_events == 75
        assert bpod._hardware_config.max_bytes_per_serial_message == 3
        assert bpod._hardware_config.n_global_timers == 16
        assert bpod._hardware_config.n_global_counters == 8
        assert bpod._hardware_config.n_conditions == 16
        assert bpod._hardware_config.n_inputs == 16
        assert bpod._hardware_config.input_description == b'UUUXZFFFFBBPPPPP'
        assert bpod._hardware_config.n_outputs == 21
        assert bpod._hardware_config.output_description == b'UUUXZFFFFBBPPPPPVVVVV'
        assert mock_serial.in_waiting() == 0


class TestBpodHandshake:
    @pytest.fixture
    def mock_bpod(self):
        mock_bpod = MagicMock(spec=Bpod)
        mock_bpod.serial0 = MagicMock()
        mock_bpod.port0 = 'COM3'
        yield mock_bpod

    def test_handshake_success(self, mock_bpod, caplog):
        caplog.set_level(logging.DEBUG)
        mock_bpod.serial0.validate_response.return_value = True
        Bpod._handshake(mock_bpod)
        mock_bpod.serial0.validate_response.assert_called_once_with(b'6', b'5')
        mock_bpod.serial0.reset_input_buffer.assert_called_once()
        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == 'DEBUG'
        assert 'successful' in caplog.records[0].message

    def test_handshake_failure_1(self, mock_bpod):
        mock_bpod.serial0.validate_response.return_value = False
        with pytest.raises(BpodError, match='Handshake .* failed'):
            Bpod._handshake(mock_bpod)
        mock_bpod.serial0.reset_input_buffer.assert_called_once()

    def test_handshake_failure_2(self, mock_bpod):
        mock_bpod.serial0.validate_response.side_effect = SerialException
        with pytest.raises(BpodError, match='Handshake .* failed'):
            Bpod._handshake(mock_bpod)
        mock_bpod.serial0.reset_input_buffer.assert_called_once()
