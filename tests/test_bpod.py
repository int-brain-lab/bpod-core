import logging
import struct
from unittest.mock import MagicMock

import pytest
from serial import SerialException

from bpod_core.bpod import Bpod, BpodError
from bpod_core.com import ExtendedSerial
from bpod_core.fsm import StateMachine


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
        """Test successful handshake with Bpod."""
        caplog.set_level(logging.DEBUG)
        mock_bpod.serial0.mock_responses = {b'6': b'5'}
        Bpod._handshake(mock_bpod)
        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == 'DEBUG'
        assert 'successful' in caplog.records[0].message

    def test_handshake_failure_1(self, mock_bpod):
        """Test failure to complete handshake with Bpod due to incorrect response."""
        mock_bpod.serial0.mock_responses = {b'6': b'6'}
        with pytest.raises(BpodError, match='Handshake .* failed'):
            Bpod._handshake(mock_bpod)
        mock_bpod.serial0.reset_input_buffer.assert_called_once()

    def test_handshake_failure_2(self, mock_bpod):
        """Test failure to complete handshake with Bpod due to exception."""
        mock_bpod.serial0 = MagicMock(spec=ExtendedSerial)
        mock_bpod.serial0.verify.side_effect = SerialException
        with pytest.raises(BpodError, match='Handshake .* failed'):
            Bpod._handshake(mock_bpod)
        mock_bpod.serial0.reset_input_buffer.assert_called_once()


class TestResetSessionClock:
    def test_reset_session_clock(self, mock_bpod, caplog):
        """Test successful reset of session clock."""
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
        """Test sending a basic state machine to Bpod 2.5."""
        mock_bpod_25.send_state_machine(fsm_basic, run_asap=False)
        assert mock_bpod_25.serial0.last_write == (
            b'C\x00\x00&\x00\x02\x00\x00\x00\x01\x00\x00\x00\x01\t\xff\x00\x00\x00\x00'
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10'\x00\x00\x10"
            b"'\x00\x00\x00"
        )

    def test_send_state_machine_basic_2p(self, fsm_basic, mock_bpod_2p):
        """Test sending a basic state machine to Bpod 2+."""
        mock_bpod_2p.send_state_machine(fsm_basic, run_asap=False)
        assert mock_bpod_2p.serial0.last_write == (
            b'C\x00\x00,\x00\x02\x00\x00\x00\x01\x00\x00\x00\x01\x00\x0b\x00\xff\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b"\x00\x00\x00\x10'\x00\x00\x10'\x00\x00\x00"
        )

    def test_send_state_machine_global_timers_25(self, fsm_global_timers, mock_bpod_25):
        """Test sending a state machine with global timers to Bpod 2.5."""
        mock_bpod_25.send_state_machine(fsm_global_timers)
        assert mock_bpod_25.serial0.last_write == (
            b'C\x00\x00\x61\x00\x02\x03\x00\x00\x00\x01\x00\x00\x00\x00\x01\x02\x01\x00'
            b'\x00\x01\x02\x02\x00\x00\x00\x00\xfe\xfe\x09\x00\x00\x80\x00\x00\x40\x00'
            b'\x00\x01\x01\x01\x01\x00\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x10\x27\x00\x00\x10\x27\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x30\x75\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x98\x3a\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x30\x75\x00\x00\x00'
        )

    def test_send_state_machine_global_timers_2p(self, fsm_global_timers, mock_bpod_2p):
        """Test sending a state machine with global timers to Bpod 2+."""
        mock_bpod_2p.send_state_machine(fsm_global_timers)
        assert mock_bpod_2p.serial0.last_write == (
            b'C\x00\x00\x6b\x00\x02\x03\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x01\x02'
            b'\x01\x00\x00\x01\x02\x02\x00\x00\x00\x00\xfe\xfe\x0b\x00\x00\x00\x00\x80'
            b'\x00\x00\x00\x00\x00\x40\x00\x00\x00\x01\x01\x01\x01\x00\x00\x00\x04\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10\x27\x00\x00\x10\x27'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x30\x75\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x98\x3a\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x30\x75'
            b'\x00\x00\x00'
        )

    def test_send_state_machine_global_counters_25(
        self,
        fsm_global_counters,
        mock_bpod_25,
    ):
        """Test sending a state machine with global counters to Bpod 2.5."""
        mock_bpod_25.send_state_machine(fsm_global_counters)
        assert mock_bpod_25.serial0.last_write == (
            b'C\x00\x00\x4a\x00\x03\x00\x03\x00\x01\x02\x02\x00\x00\x00\x01\x0a\xff\x00'
            b'\x01\x09\xff\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x00\x00\x00\xfe'
            b'\xfe\x6d\x01\x01\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x20'
            b'\x4e\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x05\x00\x00\x00\x00'
        )

    def test_send_state_machine_global_counters_2p(
        self, fsm_global_counters, mock_bpod_2p
    ):
        """Test sending a state machine with global counters to Bpod 2+."""
        mock_bpod_2p.send_state_machine(fsm_global_counters)
        assert mock_bpod_2p.serial0.last_write == (
            b'C\x00\x00\x53\x00\x03\x00\x03\x00\x01\x02\x02\x00\x00\x00\x01\x00\x0c\x00'
            b'\xff\x00\x00\x00\x01\x00\x0b\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x01\x02\x03\x00\x00\x00\xfe\xfe\x57\x01\x01\x03\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x20\x4e\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x05\x00\x00\x00\x00'
        )

    def test_send_state_machine_conditions_25(self, fsm_conditions, mock_bpod_25):
        """Test sending a state machine with conditions to Bpod 2.5."""
        mock_bpod_25.send_state_machine(fsm_conditions)
        assert mock_bpod_25.serial0.last_write == (
            b'C\x00\x00\x2e\x00\x02\x00\x00\x02\x01\x02\x00\x00\x01\x09\xff\x01\x0a\xff'
            b'\x00\x00\x00\x00\x00\x00\x00\x01\x01\x02\x00\x0a\x00\x01\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x10\x27\x00\x00\x10\x27\x00\x00\x00'
        )

    def test_send_state_machine_conditions_2p(self, fsm_conditions, mock_bpod_2p):
        """Test sending a state machine with conditions to Bpod 2+."""
        mock_bpod_2p.send_state_machine(fsm_conditions)
        assert mock_bpod_2p.serial0.last_write == (
            b'C\x00\x00\x36\x00\x02\x00\x00\x02\x01\x02\x00\x00\x01\x00\x0b\x00\xff\x00'
            b'\x01\x00\x0c\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00\x01\x01\x02\x00\x0c'
            b'\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10\x27\x00\x00\x10'
            b'\x27\x00\x00\x00'
        )
