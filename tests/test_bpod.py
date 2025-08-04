import logging
import re
import struct
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from serial import SerialException

from bpod_core.bpod import Bpod, BpodError
from bpod_core.com import ExtendedSerial
from bpod_core.fsm import StateMachine

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
def mock_comports():
    """Fixture to mock available COM ports."""
    mock_port_info = MagicMock()
    mock_port_info.device = 'COM3'
    mock_port_info.serial_number = '12345'
    mock_port_info.vid = 0x16C0  # supported VID
    with patch('bpod_core.bpod.comports') as mock_comports:
        mock_comports.return_value = [mock_port_info]
        yield mock_comports


@pytest.fixture
def mock_ext_serial():
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
        mock_bpod,
        *args,
        **kwargs,
    )
    mock_bpod._sends_discovery_byte.side_effect = (
        lambda *args, **kwargs: Bpod._sends_discovery_byte(mock_bpod, *args, **kwargs)
    )
    return mock_bpod


@pytest.fixture
def mock_bpod_20(mock_comports, mock_ext_serial):  # noqa: ARG001
    mock_ext_serial.mock_responses.update(fixture_bpod_20)
    with (
        patch('bpod_core.bpod.ExtendedSerial', return_value=mock_ext_serial),
        patch('bpod_core.bpod.Bpod._detect_additional_serial_ports'),
        patch('bpod_core.bpod.ZMQService'),
        patch('bpod_core.bpod.json.load', return_value={}),
        patch('bpod_core.bpod.json.dump', return_value={}),
    ):
        yield Bpod


@pytest.fixture
def mock_bpod_25(mock_comports, mock_ext_serial):  # noqa: ARG001
    mock_ext_serial.mock_responses.update(fixture_bpod_25)
    with (
        patch('bpod_core.bpod.ExtendedSerial', return_value=mock_ext_serial),
        patch('bpod_core.bpod.Bpod._detect_additional_serial_ports'),
        patch('bpod_core.bpod.ZMQService'),
        patch('bpod_core.bpod.json.load', return_value={}),
        patch('bpod_core.bpod.json.dump', return_value={}),
    ):
        yield Bpod


@pytest.fixture
def mock_bpod_2p(mock_comports, mock_ext_serial):  # noqa: ARG001
    mock_ext_serial.mock_responses.update(fixture_bpod_2p)
    with (
        patch('bpod_core.bpod.ExtendedSerial', return_value=mock_ext_serial),
        patch('bpod_core.bpod.Bpod._detect_additional_serial_ports'),
        patch('bpod_core.bpod.ZMQService'),
        patch('bpod_core.bpod.json.load', return_value={}),
        patch('bpod_core.bpod.json.dump', return_value={}),
    ):
        yield Bpod


class TestBpodIdentifyBpod:
    @pytest.fixture
    def mock_bpod(self, mock_bpod):
        mock_bpod.serial0.response_buffer = bytearray([222])
        return mock_bpod

    @pytest.mark.usefixtures('mock_comports')
    def test_automatic_success(self, mock_bpod):
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

    @pytest.mark.usefixtures('mock_comports')
    def test_automatic_no_discovery_byte(self, mock_bpod):
        """Test failure to auto identify Bpod when no discovery byte is received."""
        mock_bpod.serial0.response_buffer = bytearray()
        with pytest.raises(BpodError, match='No .* Bpod found'):
            Bpod._identify_bpod(mock_bpod)
        mock_bpod.serial0.__init__.assert_called_once_with('COM3', timeout=0.11)

    @pytest.mark.usefixtures('mock_comports')
    def test_automatic_serial_exception(self, mock_bpod):
        """Test failure to auto identify Bpod when serial read raises exception."""
        mock_bpod.serial0.read.side_effect = SerialException
        with pytest.raises(BpodError, match='No .* Bpod found'):
            Bpod._identify_bpod(mock_bpod)
        mock_bpod.serial0.__init__.assert_called_once_with('COM3', timeout=0.11)

    @pytest.mark.usefixtures('mock_comports')
    def test_serial_success(self, mock_bpod):
        """Test successful identification of Bpod when specifying serial."""
        port, serial_number = Bpod._identify_bpod(mock_bpod, serial_number='12345')
        assert port == 'COM3'
        assert serial_number == '12345'  # existing serial
        mock_bpod.serial0.__init__.assert_called_once_with('COM3', timeout=0.11)

    @pytest.mark.usefixtures('mock_comports')
    def test_serial_incorrect_serial(self, mock_bpod):
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

    @pytest.mark.usefixtures('mock_comports')
    def test_port_success(self, mock_bpod):
        """Test successful identification of Bpod when specifying port."""
        port, serial_number = Bpod._identify_bpod(mock_bpod, port='COM3')
        assert port == 'COM3'
        assert serial_number == '12345'  # existing serial
        mock_bpod.serial0.__init__.assert_not_called()

    @pytest.mark.usefixtures('mock_comports')
    def test_port_incorrect_port(self, mock_bpod):
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
        assert mock_bpod._hardware.max_serial_events == 75
        assert mock_bpod._hardware.max_bytes_per_serial_message == 5
        assert mock_bpod._hardware.n_global_timers == 16
        assert mock_bpod._hardware.n_global_counters == 8
        assert mock_bpod._hardware.n_conditions == 16
        assert mock_bpod._hardware.n_inputs == 16
        assert mock_bpod._hardware.input_description == b'UUUXZFFFFBBPPPPP'
        assert mock_bpod._hardware.n_outputs == 21
        assert mock_bpod._hardware.output_description == b'UUUXZFFFFBBPPPPPVVVVV'
        assert mock_bpod._hardware.cycle_frequency == 10000
        assert mock_bpod._hardware.n_modules == 3
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
        assert mock_bpod._hardware.max_serial_events == 75
        assert mock_bpod._hardware.max_bytes_per_serial_message == 3
        assert mock_bpod._hardware.n_global_timers == 16
        assert mock_bpod._hardware.n_global_counters == 8
        assert mock_bpod._hardware.n_conditions == 16
        assert mock_bpod._hardware.n_inputs == 16
        assert mock_bpod._hardware.input_description == b'UUUXZFFFFBBPPPPP'
        assert mock_bpod._hardware.n_outputs == 21
        assert mock_bpod._hardware.output_description == b'UUUXZFFFFBBPPPPPVVVVV'
        assert mock_bpod._hardware.cycle_frequency == 10000
        assert mock_bpod._hardware.n_modules == 3
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
        mock_bpod.serial0.mock_responses = {rb'\*': b'\x01'}
        assert Bpod.reset_session_clock(mock_bpod) is True
        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == 'DEBUG'
        assert 'Resetting' in caplog.records[0].message


class TestSendStateMachine:
    @pytest.fixture
    def fsm_basic(self):
        fsm = StateMachine()
        fsm.add_state('a', 1, {'Tup': 'b'}, {'PWM1': 255})
        fsm.add_state('b', 1, {'Tup': 'a'})
        return fsm

    @pytest.fixture
    def fsm_global_timers(self):
        fsm = StateMachine()
        fsm.set_global_timer(2, 3, 1.5, 'PWM1', 128, 64, 1, 1, 3, 0)
        fsm.add_state('a', 1, {'GlobalTimer3_Start': 'b'}, {'GlobalTimerTrig': 4})
        fsm.add_state('b', 1, {'GlobalTimer3_End': '>exit'})
        return fsm

    @pytest.fixture
    def fsm_global_counters(self):
        fsm = StateMachine()
        fsm.set_global_counter(2, 'Port1_High', 5)
        fsm.add_state('a', 2, {'Tup': 'b'}, {'PWM2': 255})
        fsm.add_state('b', 0, {'Tup': 'c'}, {'GlobalCounterReset': 3})
        fsm.add_state('c', 0, {'GlobalCounter3_End': '>exit'}, {'PWM1': 255})
        return fsm

    @pytest.fixture
    def fsm_conditions(self):
        fsm = StateMachine()
        fsm.set_condition(1, 'Port2', 1)
        fsm.add_state('a', 1, {'Tup': 'b'}, {'PWM1': 255})
        fsm.add_state('b', 1, {'Tup': '>exit', 'Condition2': '>exit'}, {'PWM2': 255})
        return fsm

    def test_send_state_machine_basic_25(self, fsm_basic, mock_bpod_25):
        bpod = mock_bpod_25('COM3')
        bpod.send_state_machine(fsm_basic, run_asap=False)
        assert bpod.serial0.last_write == (
            b'C\x00\x00&\x00\x02\x00\x00\x00\x01\x00\x00\x00\x01\t\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b"\x00\x00\x00\x00\x00\x00\x10'\x00\x00\x10'\x00\x00\x00"
        )

    def test_send_state_machine_basic_2p(self, fsm_basic, mock_bpod_2p):
        bpod = mock_bpod_2p('COM3')
        bpod.send_state_machine(fsm_basic, run_asap=False)
        assert bpod.serial0.last_write == (
            b'C\x00\x00,\x00\x02\x00\x00\x00\x01\x00\x00\x00\x01\x00\x0b\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10'\x00\x00\x10'\x00\x00\x00"
        )

    def test_send_state_machine_global_timers_25(self, fsm_global_timers, mock_bpod_25):
        bpod = mock_bpod_25('COM3')
        bpod.send_state_machine(fsm_global_timers)
        assert bpod.serial0.last_write == (
            b'C\x00\x00\x61\x00\x02\x03\x00\x00\x00\x01\x00\x00\x00\x00\x01\x02\x01\x00\x00\x01\x02\x02\x00\x00\x00\x00'
            b'\xfe\xfe\x09\x00\x00\x80\x00\x00\x40\x00\x00\x01\x01\x01\x01\x00\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x10\x27\x00\x00\x10\x27\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x30\x75\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x98\x3a\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x30\x75\x00\x00\x00'
        )

    def test_send_state_machine_global_timers_2p(self, fsm_global_timers, mock_bpod_2p):
        bpod = mock_bpod_2p('COM3')
        bpod.send_state_machine(fsm_global_timers)
        assert bpod.serial0.last_write == (
            b'C\x00\x00\x6b\x00\x02\x03\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x01\x02\x01\x00\x00\x01\x02\x02\x00\x00'
            b'\x00\x00\xfe\xfe\x0b\x00\x00\x00\x00\x80\x00\x00\x00\x00\x00\x40\x00\x00\x00\x01\x01\x01\x01\x00\x00\x00'
            b'\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10\x27\x00\x00\x10\x27\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x30\x75\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x98\x3a\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x30\x75\x00\x00\x00'
        )

    def test_send_state_machine_global_counters_25(
        self,
        fsm_global_counters,
        mock_bpod_25,
    ):
        bpod = mock_bpod_25('COM3')
        bpod.send_state_machine(fsm_global_counters)
        assert bpod.serial0.last_write == (
            b'C\x00\x00\x4a\x00\x03\x00\x03\x00\x01\x02\x02\x00\x00\x00\x01\x0a\xff\x00\x01\x09\xff\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x01\x02\x03\x00\x00\x00\xfe\xfe\x6d\x01\x01\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x20\x4e\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x05\x00\x00\x00\x00'
        )

    def test_send_state_machine_global_counters_2p(
        self,
        fsm_global_counters,
        mock_bpod_2p,
    ):
        bpod = mock_bpod_2p('COM3')
        bpod.send_state_machine(fsm_global_counters)
        assert bpod.serial0.last_write == (
            b'C\x00\x00\x53\x00\x03\x00\x03\x00\x01\x02\x02\x00\x00\x00\x01\x00\x0c\x00\xff\x00\x00\x00\x01\x00\x0b\x00'
            b'\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x00\x00\x00\xfe\xfe\x57\x01\x01\x03\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x20\x4e\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x05\x00\x00\x00\x00'
        )

    def test_send_state_machine_conditions_25(self, fsm_conditions, mock_bpod_25):
        bpod = mock_bpod_25('COM3')
        bpod.send_state_machine(fsm_conditions)
        assert bpod.serial0.last_write == (
            b'C\x00\x00\x2e\x00\x02\x00\x00\x02\x01\x02\x00\x00\x01\x09\xff\x01\x0a\xff\x00\x00\x00\x00\x00\x00\x00\x01'
            b'\x01\x02\x00\x0a\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10\x27\x00\x00\x10\x27\x00\x00\x00'
        )

    def test_send_state_machine_conditions_2p(self, fsm_conditions, mock_bpod_2p):
        bpod = mock_bpod_2p('COM3')
        bpod.send_state_machine(fsm_conditions)
        assert bpod.serial0.last_write == (
            b'C\x00\x00\x36\x00\x02\x00\x00\x02\x01\x02\x00\x00\x01\x00\x0b\x00\xff\x00\x01\x00\x0c\x00\xff\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x01\x01\x02\x00\x0c\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10\x27\x00'
            b'\x00\x10\x27\x00\x00\x00'
        )
