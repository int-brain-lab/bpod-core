import logging
import struct
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from serial import SerialException

from bpod_core.bpod import Bpod, BpodError
from bpod_core.com import ExtendedSerial


@pytest.fixture
def mock_ext_serial():
    """Mock base class methods for ExtendedSerial."""
    extended_serial = ExtendedSerial()
    extended_serial.response_buffer = bytearray()
    extended_serial.mock_responses = dict()

    def write(data):
        assert data in extended_serial.mock_responses
        extended_serial.response_buffer.extend(
            extended_serial.mock_responses.get(data, b'')
        )

    def read(size: int = 1) -> bytes:
        response = bytes(extended_serial.response_buffer[:size])
        del extended_serial.response_buffer[:size]
        return response

    def in_waiting() -> int:
        return len(extended_serial.response_buffer)

    patched_obj_base = 'bpod_core.com.Serial'
    with (
        patch(f'{patched_obj_base}.__init__', return_value=None),
        patch(f'{patched_obj_base}.__enter__', return_value=extended_serial),
        patch(f'{patched_obj_base}.open'),
        patch(f'{patched_obj_base}.close'),
        patch(f'{patched_obj_base}.write', side_effect=write),
        patch(f'{patched_obj_base}.read', side_effect=read),
        patch(f'{patched_obj_base}.reset_input_buffer'),
        patch(
            f'{patched_obj_base}.in_waiting',
            new_callable=PropertyMock,
            side_effect=in_waiting,
        ),
    ):
        yield extended_serial


@pytest.fixture
def mock_bpod(mock_ext_serial):
    mock_bpod = MagicMock(spec=Bpod)
    mock_bpod.serial0 = mock_ext_serial
    mock_bpod._identify_bpod.side_effect = lambda *args, **kwargs: Bpod._identify_bpod(
        mock_bpod, *args, **kwargs
    )
    mock_bpod._sends_discovery_byte.side_effect = (
        lambda *args, **kwargs: Bpod._sends_discovery_byte(mock_bpod, *args, **kwargs)
    )
    yield mock_bpod


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
    def mock_bpod(self, mock_bpod):
        mock_bpod.serial0.response_buffer = bytearray([222])
        yield mock_bpod

    def test_automatic_success(self, mock_bpod, mock_comports):
        """Test successful identification of Bpod without specifying port or serial."""
        assert Bpod._identify_bpod(mock_bpod) == ('COM3', '12345')
        mock_bpod.serial0.__init__.assert_called_once_with('COM3', timeout=0.11)

    def test_automatic_unsupported_vid(self, mock_bpod, mock_comports):
        """Test failure to auto identify Bpod when only device has unsupported VID."""
        mock_port_info = mock_comports.return_value
        mock_port_info[0].vid = 0x0000  # unsupported VID
        with pytest.raises(BpodError, match=r'No .* Bpod found'):
            Bpod._identify_bpod(mock_bpod)
        mock_bpod.serial0.__init__.assert_not_called()

    def test_automatic_no_devices(self, mock_bpod, mock_comports):
        """Test failure to auto identify Bpod when no COM ports are available."""
        mock_comports.return_value = []
        with pytest.raises(BpodError, match=r'No .* Bpod found'):
            Bpod._identify_bpod(mock_bpod)
        mock_bpod.serial0.__init__.assert_not_called()

    def test_automatic_no_discovery_byte(self, mock_bpod, mock_comports):
        """Test failure to auto identify Bpod when no discovery byte is received."""
        mock_bpod.serial0.response_buffer = bytearray()
        with pytest.raises(BpodError, match='No .* Bpod found'):
            Bpod._identify_bpod(mock_bpod)
        mock_bpod.serial0.__init__.assert_called_once_with('COM3', timeout=0.11)

    def test_automatic_serial_exception(self, mock_bpod, mock_comports):
        """Test failure to auto identify Bpod when serial read raises exception."""
        mock_bpod.serial0.read.side_effect = SerialException
        with pytest.raises(BpodError, match='No .* Bpod found'):
            Bpod._identify_bpod(mock_bpod)
        mock_bpod.serial0.__init__.assert_called_once_with('COM3', timeout=0.11)

    def test_serial_success(self, mock_bpod, mock_comports):
        """Test successful identification of Bpod when specifying serial."""
        port, serial_number = Bpod._identify_bpod(mock_bpod, serial_number='12345')
        assert port == 'COM3'
        assert serial_number == '12345'  # existing serial
        mock_bpod.serial0.__init__.assert_called_once_with('COM3', timeout=0.11)

    def test_serial_incorrect_serial(self, mock_bpod, mock_comports):
        """Test failure to identify Bpod when specifying incorrect serial."""
        with pytest.raises(BpodError, match='No .* serial number'):
            Bpod._identify_bpod(mock_bpod, serial_number='00000')
        mock_bpod.serial0.__init__.assert_not_called()

    def test_serial_unsupported_vid(self, mock_bpod, mock_comports):
        """Test failure to identify Bpod by serial if device has incompatible VID."""
        mock_port_info = mock_comports.return_value
        mock_port_info[0].vid = 0x0000  # unsupported VID
        with pytest.raises(BpodError, match='.* not a supported Bpod'):
            Bpod._identify_bpod(mock_bpod, serial_number='12345')
        mock_bpod.serial0.__init__.assert_called_once_with('COM3', timeout=0.11)

    def test_port_success(self, mock_bpod, mock_comports):
        """Test successful identification of Bpod when specifying port."""
        port, serial_number = Bpod._identify_bpod(mock_bpod, port='COM3')
        assert port == 'COM3'
        assert serial_number == '12345'  # existing serial
        mock_bpod.serial0.__init__.assert_not_called()

    def test_port_incorrect_port(self, mock_bpod, mock_comports):
        """Test failure to identify Bpod when specifying incorrect port."""
        with pytest.raises(BpodError, match='Port not found'):
            Bpod._identify_bpod(mock_bpod, port='incorrect_port')
        mock_bpod.serial0.__init__.assert_not_called()

    def test_port_unsupported_vid(self, mock_bpod, mock_comports):
        """Test failure to identify Bpod when specifying incorrect port."""
        mock_port_info = mock_comports.return_value
        mock_port_info[0].vid = 0x0000  # unsupported VID
        with pytest.raises(BpodError, match='.* not .* supported Bpod'):
            Bpod._identify_bpod(mock_bpod, port='COM3')
        mock_bpod.serial0.__init__.assert_not_called()


class TestGetVersionInfo:
    def test_get_version_info(self, mock_bpod):
        """Test retrieval of version info with supported firmware and hardware."""
        mock_bpod.serial0.mock_responses = {
            b'F': struct.pack('<2H', 23, 3),  # Firmware version 23, Bpod type 3
            b'f': struct.pack('<H', 1),  # Minor firmware version 1
            b'v': struct.pack('<B', 2),  # PCB revision 2
        }
        Bpod._get_version_info(mock_bpod)
        assert mock_bpod._version.firmware == (23, 1)
        assert mock_bpod._version.machine == 3
        assert mock_bpod._version.pcb == 2

    def test_get_version_info_unsupported_firmware(self, mock_bpod):
        """Test failure when firmware version is unsupported."""
        mock_bpod.serial0.mock_responses = {
            b'F': struct.pack('<2H', 20, 3),  # Firmware version 20, Bpod type 3
            b'f': struct.pack('<H', 1),  # Minor firmware version 1
        }
        with pytest.raises(BpodError, match='firmware .* is not supported'):
            Bpod._get_version_info(mock_bpod)

    def test_get_version_info_unsupported_hardware(self, mock_bpod):
        """Test failure when hardware version is unsupported."""
        mock_bpod.serial0.mock_responses = {
            b'F': struct.pack('<2H', 23, 2),  # Firmware version 23, Bpod type 2
            b'f': struct.pack('<H', 1),  # Minor firmware version 1
        }
        with pytest.raises(BpodError, match='hardware .* is not supported'):
            Bpod._get_version_info(mock_bpod)


class TestGetHardwareConfiguration:
    def test_get_version_info_v23(self, mock_bpod):
        """Test retrieval of hardware configuration (firmware version 23)."""
        mock_bpod.serial0.mock_responses = {
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
        mock_bpod.version.firmware = (23, 0)
        Bpod._get_hardware_configuration(mock_bpod)
        assert mock_bpod._hardware.max_states == 256
        assert mock_bpod._hardware.cycle_period == 100
        assert mock_bpod._hardware.cycle_frequency == 1e6 // 100
        assert mock_bpod._hardware.max_serial_events == 75
        assert mock_bpod._hardware.max_bytes_per_serial_message == 5
        assert mock_bpod._hardware.n_global_timers == 16
        assert mock_bpod._hardware.n_global_counters == 8
        assert mock_bpod._hardware.n_conditions == 16
        assert mock_bpod._hardware.n_inputs == 16
        assert mock_bpod._hardware.input_description == b'UUUXZFFFFBBPPPPP'
        assert mock_bpod._hardware.n_outputs == 21
        assert mock_bpod._hardware.output_description == b'UUUXZFFFFBBPPPPPVVVVV'
        assert mock_bpod.serial0.in_waiting == 0

    def test_get_version_info_v22(self, mock_bpod):
        """Test retrieval of hardware configuration (firmware version 22)."""
        mock_bpod.serial0.mock_responses = {
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
        mock_bpod.version.firmware = (22, 0)
        Bpod._get_hardware_configuration(mock_bpod)
        assert mock_bpod._hardware.max_states == 256
        assert mock_bpod._hardware.cycle_period == 100
        assert mock_bpod._hardware.cycle_frequency == 10000
        assert mock_bpod._hardware.max_serial_events == 75
        assert mock_bpod._hardware.max_bytes_per_serial_message == 3
        assert mock_bpod._hardware.n_global_timers == 16
        assert mock_bpod._hardware.n_global_counters == 8
        assert mock_bpod._hardware.n_conditions == 16
        assert mock_bpod._hardware.n_inputs == 16
        assert mock_bpod._hardware.input_description == b'UUUXZFFFFBBPPPPP'
        assert mock_bpod._hardware.n_outputs == 21
        assert mock_bpod._hardware.output_description == b'UUUXZFFFFBBPPPPPVVVVV'
        assert mock_bpod.serial0.in_waiting == 0


class TestBpodHandshake:
    def test_handshake_success(self, mock_bpod, caplog):
        caplog.set_level(logging.DEBUG)
        mock_bpod.serial0.mock_responses = {b'6': b'5'}
        Bpod._handshake(mock_bpod)
        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == 'DEBUG'
        assert 'successful' in caplog.records[0].message

    def test_handshake_failure_1(self, mock_bpod):
        mock_bpod.serial0.mock_responses = {b'6': b''}
        with pytest.raises(BpodError, match='Handshake .* failed'):
            Bpod._handshake(mock_bpod)
        mock_bpod.serial0.reset_input_buffer.assert_called_once()

    def test_handshake_failure_2(self, mock_bpod):
        mock_bpod.serial0 = MagicMock(spec=ExtendedSerial)
        mock_bpod.serial0.verify.side_effect = SerialException
        with pytest.raises(BpodError, match='Handshake .* failed'):
            Bpod._handshake(mock_bpod)
        mock_bpod.serial0.reset_input_buffer.assert_called_once()


class TestResetSessionClock:
    def test_reset_session_clock(self, mock_bpod, caplog):
        caplog.set_level(logging.DEBUG)
        mock_bpod.serial0.mock_responses = {b'*': b'\x01'}
        assert Bpod._reset_session_clock(mock_bpod) is True
        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == 'DEBUG'
        assert 'Resetting' in caplog.records[0].message
