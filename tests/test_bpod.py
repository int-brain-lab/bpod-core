import logging
import struct
from unittest.mock import MagicMock

import pytest
from serial import SerialException

from bpod_core.bpod import Bpod, BpodError, RemoteBpod
from bpod_core.bpod.constants import _REMOTE_CALL_METHODS, _REMOTE_DATA_METHODS
from bpod_core.bpod.structs import (
    BpodEventUnion,
    EventTrialStart,
    RequestCall,
    RequestData,
)
from bpod_core.com import ExtendedSerial
from bpod_core.constants import STRUCT_INT16_LE, STRUCT_UINT8
from bpod_core.fsm import StateMachine


class TestBpodIdentifyBpod:
    @pytest.fixture
    def mock_bpod(self, mock_bpod):
        mock_bpod.serial0.response_buffer = bytearray([222])
        return mock_bpod

    @pytest.mark.usefixtures('mock_comports')
    def test_automatic_success(self, mock_bpod):
        """Test successful identification of Bpod without specifying port or serial."""
        assert Bpod._identify_bpod() == ('COM3', '12345')

    def test_automatic_unsupported_vid(self, mock_bpod, mock_comports):
        """Test failure to auto identify Bpod when only device has unsupported VID."""
        mock_port_info = mock_comports.return_value
        mock_port_info[0].vid = 0x0000  # unsupported VID
        with pytest.raises(BpodError, match=r'No .* Bpod found'):
            Bpod._identify_bpod()

    def test_automatic_no_devices(self, mock_bpod, mock_comports):
        """Test failure to auto identify Bpod when no COM ports are available."""
        mock_comports.return_value = []
        with pytest.raises(BpodError, match=r'No .* Bpod found'):
            Bpod._identify_bpod()

    @pytest.mark.usefixtures('mock_comports')
    def test_automatic_no_discovery_byte(self, mock_bpod, mock_serial_discovery):
        """Test failure to auto identify Bpod when no discovery byte is received."""
        mock_serial_discovery.return_value = False
        with pytest.raises(BpodError, match=r'No .* Bpod found'):
            Bpod._identify_bpod()

    @pytest.mark.usefixtures('mock_comports')
    def test_serial_success(self, mock_bpod):
        """Test successful identification of Bpod when specifying serial."""
        port, serial_number = Bpod._identify_bpod(serial_number='12345')
        assert port == 'COM3'
        assert serial_number == '12345'  # existing serial

    @pytest.mark.usefixtures('mock_comports')
    def test_serial_incorrect_serial(self, mock_bpod):
        """Test failure to identify Bpod when specifying incorrect serial."""
        with pytest.raises(BpodError, match=r'No .* serial number'):
            Bpod._identify_bpod(serial_number='00000')

    def test_serial_unsupported_vid(self, mock_bpod, mock_comports):
        """Test failure to identify Bpod by serial if device has incompatible VID."""
        mock_port_info = mock_comports.return_value
        mock_port_info[0].vid = 0x0000  # unsupported VID
        with pytest.raises(BpodError, match=r'No .* Bpod found matching serial number'):
            Bpod._identify_bpod(serial_number='12345')

    @pytest.mark.usefixtures('mock_comports')
    def test_port_success(self, mock_bpod):
        """Test successful identification of Bpod when specifying port."""
        port, serial_number = Bpod._identify_bpod(port='COM3')
        assert port == 'COM3'
        assert serial_number == '12345'  # existing serial

    @pytest.mark.usefixtures('mock_comports')
    def test_port_incorrect_port(self, mock_bpod):
        """Test failure to identify Bpod when specifying incorrect port."""
        with pytest.raises(BpodError, match='Port not found'):
            Bpod._identify_bpod(port='incorrect_port')

    def test_port_unsupported_vid(self, mock_bpod, mock_comports):
        """Test failure to identify Bpod when specifying incorrect port."""
        mock_port_info = mock_comports.return_value
        mock_port_info[0].vid = 0x0000  # unsupported VID
        with pytest.raises(BpodError, match=r'not an .* Bpod'):
            Bpod._identify_bpod(port='COM3')


class TestGetVersionInfo:
    def test_get_version_info(self, mock_bpod):
        """Test retrieval of version info with supported firmware and hardware."""
        mock_bpod.serial0.mock_responses = {
            b'F': struct.pack('<2H', 23, 3),  # Firmware version 23, Bpod type 3
            b'f': STRUCT_INT16_LE.pack(1),  # Minor firmware version 1
            b'v': STRUCT_UINT8.pack(2),  # PCB revision 2
        }
        Bpod._get_version_info(mock_bpod)
        assert mock_bpod._version.firmware == (23, 1)
        assert mock_bpod._version.machine == 3
        assert mock_bpod._version.pcb == 2

    def test_get_version_info_unsupported_firmware(self, mock_bpod):
        """Test failure when firmware version is unsupported."""
        mock_bpod.serial0.mock_responses = {
            b'F': struct.pack('<2H', 20, 3),  # Firmware version 20, Bpod type 3
            b'f': STRUCT_INT16_LE.pack(1),  # Minor firmware version 1
        }
        with pytest.raises(BpodError, match=r'firmware .* is not supported'):
            Bpod._get_version_info(mock_bpod)

    def test_get_version_info_unsupported_hardware(self, mock_bpod):
        """Test failure when hardware version is unsupported."""
        mock_bpod.serial0.mock_responses = {
            b'F': struct.pack('<2H', 23, 2),  # Firmware version 23, Bpod type 2
            b'f': STRUCT_INT16_LE.pack(1),  # Minor firmware version 1
        }
        with pytest.raises(BpodError, match=r'hardware .* is not supported'):
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
        assert mock_bpod._hardware.cycle_period_us == 100
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
        assert mock_bpod._hardware.cycle_period_us == 100
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
        with pytest.raises(BpodError, match=r'Handshake .* failed'):
            Bpod._handshake(mock_bpod)
        mock_bpod.serial0.reset_input_buffer.assert_called_once()

    def test_handshake_failure_2(self, mock_bpod):
        """Test failure to complete handshake with Bpod due to exception."""
        mock_bpod.serial0 = MagicMock(spec=ExtendedSerial)
        mock_bpod.serial0.verify.side_effect = SerialException
        with pytest.raises(BpodError, match=r'Handshake .* failed'):
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
        mock_bpod.is_running = True
        with pytest.raises(BpodError, match=r'Cannot reset session clock'):
            Bpod.reset_session_clock(mock_bpod)


class TestRun:
    @pytest.fixture(autouse=True)
    def patch_run_state_machine(self, mocker):
        """Prevent _run_state_machine from starting threads in wire-format tests."""
        mocker.patch.object(Bpod, '_run_state_machine')

    @pytest.fixture
    def fsm_basic(self):
        fsm = StateMachine()
        fsm.add_state('a', 1, {'Tup': 'b'}, {'PWM1': 255})
        fsm.add_state('b', 1, {'Tup': 'a'})
        return fsm

    @pytest.fixture
    def fsm_global_timers(self):
        fsm = StateMachine()
        fsm.set_global_timer(
            index=2,
            duration=3,
            onset_delay=1.5,
            channel='PWM1',
            value_on=128,
            value_off=64,
            send_events=True,
            loop=1,
            loop_interval=3,
            onset_trigger=0,
        )
        fsm.add_state('a', 1, {'GlobalTimer2_Start': 'b'}, {'GlobalTimerTrig': 3})
        fsm.add_state('b', 1, {'GlobalTimer2_End': '>exit'})
        return fsm

    @pytest.fixture
    def fsm_global_counters(self):
        fsm = StateMachine()
        fsm.set_global_counter(2, 'Port1_High', 5)
        fsm.add_state('a', 2, {'Tup': 'b'}, {'PWM2': 255})
        fsm.add_state('b', 0, {'Tup': 'c'}, {'GlobalCounterReset': 2})
        fsm.add_state('c', 0, {'GlobalCounter2_End': '>exit'}, {'PWM1': 255})
        return fsm

    @pytest.fixture
    def fsm_conditions(self):
        fsm = StateMachine()
        fsm.set_condition(1, 'Port2', 1)
        fsm.add_state('a', 1, {'Tup': 'b'}, {'PWM1': 255})
        fsm.add_state('b', 1, {'Tup': '>exit', 'Condition1': '>exit'}, {'PWM2': 255})
        return fsm

    @pytest.fixture
    def fsm_softcodes(self):
        fsm = StateMachine()
        fsm.add_state('a', 5, {'SoftCode0': 'b', 'Tup': '>exit'})
        fsm.add_state('b', 0, {'Tup': '>exit'})
        return fsm

    def test_run_basic_25(self, fsm_basic, mock_bpod_25):
        """Test running a basic state machine on Bpod 2.5."""
        mock_bpod_25.run(fsm_basic)
        assert mock_bpod_25.serial0.last_write == (
            b'C\x01\x00&\x00\x02\x00\x00\x00\x01\x00\x00\x00\x01\t\xff\x00\x00\x00\x00'
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10'\x00\x00\x10"
            b"'\x00\x00\x00"
        )

    def test_run_basic_2p(self, fsm_basic, mock_bpod_2p):
        """Test running a basic state machine on Bpod 2+."""
        mock_bpod_2p.run(fsm_basic)
        assert mock_bpod_2p.serial0.last_write == (
            b'C\x01\x00,\x00\x02\x00\x00\x00\x01\x00\x00\x00\x01\x00\x0b\x00\xff\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b"\x00\x00\x00\x10'\x00\x00\x10'\x00\x00\x00"
        )

    def test_run_global_timers_25(self, fsm_global_timers, mock_bpod_25):
        """Test running a state machine with global timers on Bpod 2.5."""
        mock_bpod_25.run(fsm_global_timers)
        assert mock_bpod_25.serial0.last_write == (
            b'C\x01\x00\x61\x00\x02\x03\x00\x00\x00\x01\x00\x00\x00\x00\x01\x02\x01\x00'
            b'\x00\x01\x02\x02\x00\x00\x00\x00\xfe\xfe\x09\x00\x00\x80\x00\x00\x40\x00'
            b'\x00\x01\x01\x01\x01\x00\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x10\x27\x00\x00\x10\x27\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x30\x75\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x98\x3a\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x30\x75\x00\x00\x00'
        )

    def test_run_global_timers_2p(self, fsm_global_timers, mock_bpod_2p):
        """Test running a state machine with global timers on Bpod 2+."""
        mock_bpod_2p.run(fsm_global_timers)
        assert mock_bpod_2p.serial0.last_write == (
            b'C\x01\x00\x6b\x00\x02\x03\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x01\x02'
            b'\x01\x00\x00\x01\x02\x02\x00\x00\x00\x00\xfe\xfe\x0b\x00\x00\x00\x00\x80'
            b'\x00\x00\x00\x00\x00\x40\x00\x00\x00\x01\x01\x01\x01\x00\x00\x00\x04\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10\x27\x00\x00\x10\x27'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x30\x75\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x98\x3a\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x30\x75'
            b'\x00\x00\x00'
        )

    def test_run_global_counters_25(self, fsm_global_counters, mock_bpod_25):
        """Test running a state machine with global counters on Bpod 2.5."""
        mock_bpod_25.run(fsm_global_counters)
        assert mock_bpod_25.serial0.last_write == (
            b'C\x01\x00\x4a\x00\x03\x00\x03\x00\x01\x02\x02\x00\x00\x00\x01\x0a\xff\x00'
            b'\x01\x09\xff\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x00\x00\x00\xfe'
            b'\xfe\x6d\x01\x01\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x20'
            b'\x4e\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x05\x00\x00\x00\x00'
        )

    def test_run_global_counters_2p(self, fsm_global_counters, mock_bpod_2p):
        """Test running a state machine with global counters on Bpod 2+."""
        mock_bpod_2p.run(fsm_global_counters)
        assert mock_bpod_2p.serial0.last_write == (
            b'C\x01\x00\x53\x00\x03\x00\x03\x00\x01\x02\x02\x00\x00\x00\x01\x00\x0c\x00'
            b'\xff\x00\x00\x00\x01\x00\x0b\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x01\x02\x03\x00\x00\x00\xfe\xfe\x57\x01\x01\x03\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x20\x4e\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x05\x00\x00\x00\x00'
        )

    def test_run_conditions_25(self, fsm_conditions, mock_bpod_25):
        """Test running a state machine with conditions on Bpod 2.5."""
        mock_bpod_25.run(fsm_conditions)
        assert mock_bpod_25.serial0.last_write == (
            b'C\x01\x00\x2e\x00\x02\x00\x00\x02\x01\x02\x00\x00\x01\x09\xff\x01\x0a\xff'
            b'\x00\x00\x00\x00\x00\x00\x00\x01\x01\x02\x00\x0a\x00\x01\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x10\x27\x00\x00\x10\x27\x00\x00\x00'
        )

    def test_run_conditions_2p(self, fsm_conditions, mock_bpod_2p):
        """Test running a state machine with conditions on Bpod 2+."""
        mock_bpod_2p.run(fsm_conditions)
        assert mock_bpod_2p.serial0.last_write == (
            b'C\x01\x00\x36\x00\x02\x00\x00\x02\x01\x02\x00\x00\x01\x00\x0b\x00\xff\x00'
            b'\x01\x00\x0c\x00\xff\x00\x00\x00\x00\x00\x00\x00\x00\x01\x01\x02\x00\x0c'
            b'\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x10\x27\x00\x00\x10'
            b'\x27\x00\x00\x00'
        )

    def test_run_softcodes_25(self, fsm_softcodes, mock_bpod_25):
        """Test running a state machine with softcodes on Bpod 2.5."""
        mock_bpod_25.run(fsm_softcodes)
        assert mock_bpod_25.serial0.last_write == (
            b'C\x01\x00&\x00\x02\x00\x00\x00\x02\x02\x01K\x01\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00P\xc3\x00\x00\x00'
            b'\x00\x00\x00\x00'
        )

    def test_run_repeat(self, fsm_basic, mock_bpod_25):
        """Calling run() without sma re-sends the same compiled bytes from cache."""
        mock_bpod_25.run(fsm_basic)
        first_write = mock_bpod_25.serial0.last_write
        mock_bpod_25.run()
        assert mock_bpod_25.serial0.last_write == first_write

    def test_run_no_prior_sma(self, mock_bpod_25):
        """Calling run() without a prior run raises RuntimeError."""
        with pytest.raises(RuntimeError, match='No state machine has been run yet'):
            mock_bpod_25.run()


class TestRemoteCall:
    @pytest.mark.parametrize(
        'req',
        [
            pytest.param(RequestCall('very_illegal'), id='illegal call request'),
            pytest.param(RequestData('very_illegal'), id='illegal data request'),
        ],
    )
    def test_disallowed_method_raises(self, mock_bpod, req):
        """Method names outside the allowlist are rejected."""
        with pytest.raises(BpodError, match='cannot be called remotely'):
            Bpod._request_handler(mock_bpod, req)

    def test_allowed_method_is_called(self, mock_bpod):
        """Allowlisted method names resolve to the bound method."""
        Bpod._request_handler(mock_bpod, RequestCall('set_status_led', (False,)))
        mock_bpod.set_status_led.assert_called_once_with(False)

    @pytest.mark.parametrize(
        'method_set',
        [
            pytest.param(_REMOTE_CALL_METHODS, id='remote call'),
            pytest.param(_REMOTE_DATA_METHODS, id='remote data'),
        ],
    )
    def test_allowlist_names_are_methods(self, method_set):
        """Every allowlisted name refers to an existing Bpod method."""
        assert all(callable(getattr(Bpod, name)) for name in method_set)


class TestBpodSubscriberWait:
    def test_waits_for_subscribers_when_supervised(self, monkeypatch, request):
        """With BPOD_OVERRIDE_REMOTE set, init waits for a PUB/SUB subscriber."""
        monkeypatch.setenv('BPOD_OVERRIDE_REMOTE', '1')
        bpod = request.getfixturevalue('mock_bpod_25')
        bpod._zmq.wait_for_subscribers.assert_called_once_with(timeout=1.0)

    def test_no_wait_without_override(self, monkeypatch, request):
        """Without the environment override, init does not wait."""
        monkeypatch.delenv('BPOD_OVERRIDE_REMOTE', raising=False)
        bpod = request.getfixturevalue('mock_bpod_25')
        bpod._zmq.wait_for_subscribers.assert_not_called()


class TestRemoteBpodEventCallback:
    @pytest.fixture
    def mock_client(self, mocker):
        """Mock the ServiceClient used by RemoteBpod."""
        return mocker.patch('bpod_core.bpod.ServiceClient', autospec=True)

    def test_callback_subscribes_and_forwards(self, mock_client):
        """With an event_callback, the client subscribes and messages forward."""
        received = []
        remote = RemoteBpod(address='tcp://127.0.0.1:1', event_callback=received.append)
        kwargs = mock_client.call_args.kwargs
        assert kwargs['event_handler'] == remote._event_handler
        assert kwargs['event_type'] is BpodEventUnion

        message = EventTrialStart(time_us=0, trial=0, fsm_hash='00')
        remote._event_handler(message)
        assert received == [message]
