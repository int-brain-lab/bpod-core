import inspect
import logging
import re
import struct
import threading
import time
import weakref
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock
from uuid import uuid4

import polars as pl
import pytest
from serial import SerialException

from bpod_core.bpod import (
    Bpod,
    BpodError,
    Input,
    Module,
    Output,
    RemoteBpod,
    _allocate_module_events,
)
from bpod_core.bpod._threads import SoftcodeThread
from bpod_core.bpod.constants import _REMOTE_CALL_METHODS, _REMOTE_DATA_METHODS
from bpod_core.bpod.structs import (
    BpodEventUnion,
    EventTrialStart,
    RawSoftcode,
    RequestCall,
    RequestData,
)
from bpod_core.com import ExtendedSerial
from bpod_core.constants import STRUCT_INT16_LE, STRUCT_UINT8, UINT32_MAX
from bpod_core.fsm import StateMachine
from bpod_core.ipc import ErrorData, RemoteError, ServiceHost


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


class TestUpdateModules:
    @pytest.mark.parametrize(
        ('ports', 'expected'),
        [
            ([('A', 5), (None, 0), (None, 0)], [15, 15, 15]),  # surplus is kept
            ([('A', 45), (None, 0), (None, 0)], [45, 0, 0]),  # unused ports emptied
            ([('A', 20), ('B', 0), ('ValveModule1', 0)], [20, 15, 10]),  # valve module
            ([('A', 25), (None, 0), ('C', 10)], [25, 10, 10]),  # surplus of module C
        ],
    )
    def test_allocate_module_events(self, ports, expected):
        """Serial events are allocated to module ports as in Bpod's MATLAB software."""
        modules = [SimpleNamespace(name=n, is_connected=bool(n)) for n, _ in ports]
        n_requested = [n for _, n in ports]
        assert _allocate_module_events(modules, n_requested) == expected

    def test_allocate_module_events_exceeded(self):
        """Requests that cannot be met by unused ports or spare events raise."""
        modules = [SimpleNamespace(name=n, is_connected=True) for n in 'ABC']
        with pytest.raises(BpodError, match='A requests 24 serial events, but only 15'):
            _allocate_module_events(modules, [24, 0, 0])

    def test_serial_event_allocation_is_sent(self, mock_bpod_2p):
        """The allocation is sent with '%' and matches the event numbering."""
        mock_bpod_2p.serial0.mock_responses[b'M'] = (
            b'\x01'  # module port 1: connected
            + struct.pack('<IB', 1, 15)  # firmware version, length of name
            + b'DIO_12Ch_Stream\x01'  # name, more info follows
            + b'#\x18\x01'  # requests 24 events, more info follows
            + b'E\x01\x04Lick\x01'  # one custom event name, more info follows
            + b'V\x02\x01v\x00\x00'  # hardware version 2.0, no more info
            + b'\x00\x00'  # module ports 2 and 3: not connected
        )
        mock_bpod_2p.update_modules()
        assert mock_bpod_2p.serial0.last_write == b'%' + bytes((24, 15, 6, 15, 15))
        event_names = mock_bpod_2p.modules['DIO_12Ch_Stream1'].event_names
        assert event_names[:2] == ['DIO_12Ch_Stream1_Lick', 'DIO_12Ch_Stream1_1']
        assert mock_bpod_2p._event_indices['DIO_12Ch_Stream1_Lick'] == 0
        assert mock_bpod_2p._event_indices['Flex1_0'] == 75  # max_serial_events
        assert mock_bpod_2p._n_softcodes == 15

    def test_unacknowledged_allocation_raises(self, mock_bpod_2p):
        """A state machine that does not acknowledge the allocation is an error."""
        mock_bpod_2p.serial0.mock_responses[b'%'] = b'\x00'
        with pytest.raises(BpodError, match='did not acknowledge'):
            mock_bpod_2p.update_modules()


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


class TestRequestDisconnect:
    @pytest.fixture
    def open_serial(self, mock_ext_serial):
        mock_ext_serial.is_open = True
        yield mock_ext_serial
        mock_ext_serial.is_open = False  # else pyserial's close() runs on collection

    def test_confirmed(self, open_serial, caplog):
        """The firmware confirms the disconnect with ASCII '1'."""
        open_serial.mock_responses[b'Z'] = b'1'
        Bpod._request_disconnect(open_serial)
        assert open_serial.last_write == b'Z'
        assert len(open_serial.response_buffer) == 0
        assert 'did not confirm' not in caplog.text

    @pytest.mark.parametrize('reply', [b'', b'\x01'], ids=['no reply', 'unexpected'])
    def test_not_confirmed(self, open_serial, reply, caplog):
        """A missing or unexpected reply is logged."""
        open_serial.mock_responses[b'Z'] = reply
        Bpod._request_disconnect(open_serial)
        assert 'Bpod did not confirm the disconnect request' in caplog.text

    def test_skipped_on_closed_port(self, mock_ext_serial):
        """Nothing is sent over a closed port."""
        mock_ext_serial.is_open = False
        mock_ext_serial.mock_responses.clear()  # any write would raise AssertionError
        Bpod._request_disconnect(mock_ext_serial)


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
        with pytest.raises(BpodError, match=r'Cannot use Bpod.reset_session_clock'):
            Bpod.reset_session_clock(mock_bpod)


def _first(mapping):
    return next(iter(mapping.values()))


def _enable_first_input(bpod):
    _first(bpod.inputs).enabled = True


_IDLE_ONLY_CALLS = {
    'Bpod.open': lambda bpod: bpod.open(),
    'Bpod.reset_session_clock': lambda bpod: bpod.reset_session_clock(),
    'Bpod.set_status_led': lambda bpod: bpod.set_status_led(True),
    'Bpod.update_modules': lambda bpod: bpod.update_modules(),
    'Input.enable': lambda bpod: _first(bpod.inputs).enable(True),
    'Input.enabled': _enable_first_input,
    'Input.read': lambda bpod: _first(bpod.inputs).read(),
    'Module.load_serial_message': lambda bpod: _first(bpod.modules).load_serial_message(
        0, b'\x01'
    ),
    'Module.set_relay': lambda bpod: _first(bpod.modules).set_relay(True),
}
"""Calls to every member that must be refused while a state machine is running."""

_ALLOWED_WHILE_RUNNING = frozenset(
    {
        'Bpod.address',
        'Bpod.address_control',
        'Bpod.address_events',
        'Bpod.close',
        'Bpod.get_data',
        'Bpod.input_event_names',
        'Bpod.is_queued',
        'Bpod.is_ready',
        'Bpod.is_running',
        'Bpod.location',
        'Bpod.name',
        'Bpod.peek_data',
        'Bpod.port',
        'Bpod.run',
        'Bpod.send_softcode',
        'Bpod.serial0',
        'Bpod.serial_number',
        'Bpod.set_softcode_handler',
        'Bpod.stop_state_machine',
        'Bpod.validate_state_machine',
        'Bpod.version',
        'Bpod.wait',
        'Input.override',
        'Module.event_names',
        'Module.relay',
        'Output.override',
    }
)
"""Public members that are safe to use while a state machine is running."""


class TestRequiresIdle:
    """Members that query or reconfigure the Bpod are refused during a trial."""

    def test_every_public_member_is_classified(self):
        """Each public member is either refused during a trial or explicitly allowed.

        A new public method or property fails this test until it's either decorated
        with ``_requires_idle`` (and added to ``_IDLE_ONLY_CALLS``) or added to
        ``_ALLOWED_WHILE_RUNNING``.
        """
        requires_idle = {}
        for cls in (Bpod, Input, Output, Module):
            for name, member in inspect.getmembers(cls):
                if name.startswith('_'):
                    continue
                if isinstance(member, property):
                    function = member.fset  # only setters change state
                elif callable(member):
                    function = member
                else:
                    continue
                qualified_name = f'{cls.__name__}.{name}'
                requires_idle[qualified_name] = getattr(
                    function, 'requires_idle', False
                )
        idle_only = {name for name, flag in requires_idle.items() if flag}
        assert idle_only == _IDLE_ONLY_CALLS.keys()
        assert requires_idle.keys() - idle_only == _ALLOWED_WHILE_RUNNING

    @pytest.mark.parametrize('name', sorted(_IDLE_ONLY_CALLS))
    def test_refused_while_running(self, name, mock_bpod_25, mock_ext_serial, mocker):
        """Refused members raise and send nothing to the Bpod."""
        mocker.patch.object(
            Bpod, 'is_running', new_callable=PropertyMock, return_value=True
        )
        mock_ext_serial.mock_responses.clear()  # any write would raise AssertionError
        with pytest.raises(BpodError, match=f'Cannot use {re.escape(name)} while'):
            _IDLE_ONLY_CALLS[name](mock_bpod_25)


class TestInputOverride:
    @pytest.fixture
    def port(self, mock_bpod_25, mock_ext_serial):
        mock_ext_serial.mock_responses[rb'V'] = b''
        return mock_bpod_25.inputs['Port2']

    def test_sends_channel_index_and_state(self, port, mock_ext_serial, mocker, caplog):
        """The override message carries the input's index and the new state."""
        mocker.patch.object(
            Bpod, 'is_running', new_callable=PropertyMock, return_value=True
        )
        port.override(True)
        assert mock_ext_serial.last_write == bytes([ord('V'), port.index, 1])
        assert not caplog.records

    def test_not_sent_without_running_state_machine(
        self, port, mock_ext_serial, caplog
    ):
        """An override while idle isn't sent, since the Bpod would ignore it."""
        mock_ext_serial.mock_responses.clear()  # any write would raise AssertionError
        port.override(True)
        assert 'override of Port2 not sent' in caplog.text


class TestModuleRelay:
    def test_set_relay(self, mock_bpod_25, mock_ext_serial, caplog):
        """Switching a relay sends 'J' with the module's index and logs it."""
        caplog.set_level(logging.INFO)
        mock_ext_serial.mock_responses[rb'J'] = b''
        module = _first(mock_bpod_25.modules)
        module.set_relay(True)
        assert mock_ext_serial.last_write == bytes([ord('J'), module.index, 1])
        assert f'Enabling relay for module {module.name}' in caplog.text
        assert module.relay


class TestSendSoftcode:
    def test_sends_softcode(self, mock_bpod_25, mock_ext_serial, mocker, caplog):
        """A softcode is sent to a running state machine."""
        mocker.patch.object(
            Bpod, 'is_running', new_callable=PropertyMock, return_value=True
        )
        mock_ext_serial.mock_responses[rb'~'] = b''
        mock_bpod_25.send_softcode(3)
        assert mock_ext_serial.last_write == b'~\x03'
        assert not caplog.records

    def test_not_sent_without_running_state_machine(
        self, mock_bpod_25, mock_ext_serial, caplog
    ):
        """A softcode while idle isn't sent, since the Bpod would ignore it."""
        mock_ext_serial.mock_responses.clear()  # any write would raise AssertionError
        mock_bpod_25.send_softcode(3)
        assert 'softcode 3 not sent' in caplog.text


class TestIsRunning:
    @pytest.fixture
    def fsm(self):
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Tup': '>exit'})
        return fsm

    def test_true_while_handing_over(self, fsm, mock_bpod_25, mocker):
        """is_running stays true while a queued state machine's threads are set up.

        The Bpod starts a queued state machine as soon as the previous one ends, while
        run() still waits for the previous event thread to drain.
        """
        mocker.patch('bpod_core.bpod.ReadThread')
        mocker.patch('bpod_core.bpod.EventThread')
        observed = []
        mock_bpod_25._read_thread = MagicMock(**{'is_alive.return_value': False})
        mock_bpod_25._event_thread = MagicMock(**{'is_alive.return_value': True})
        mock_bpod_25._event_thread.join.side_effect = lambda: observed.append(
            mock_bpod_25.is_running
        )
        mock_bpod_25.run(fsm)
        assert observed == [True]

    def test_false_after_failed_send(self, fsm, mock_bpod_25, mock_ext_serial, mocker):
        """A state machine that couldn't be sent doesn't count as running."""
        mocker.patch.object(mock_ext_serial, 'write', side_effect=SerialException)
        with pytest.raises(SerialException):
            mock_bpod_25.run(fsm)
        assert not mock_bpod_25.is_running


class TestGetData:
    def test_waits_for_event_thread(self, mock_bpod_25):
        """Data the event thread is still processing is waited for."""

        def finish():
            time.sleep(0.05)
            mock_bpod_25._trial_data.put(pl.LazyFrame({'trial': [0]}))

        mock_bpod_25._read_thread = None
        mock_bpod_25._event_thread = threading.Thread(target=finish)
        mock_bpod_25._event_thread.start()
        assert mock_bpod_25.get_data()['trial'].to_list() == [0]

    def test_raises_without_data(self, mock_bpod_25):
        """Without a trial running or being processed, there's nothing to wait for."""
        with pytest.raises(BpodError, match='No trial data available'):
            mock_bpod_25.get_data()


class TestResettableActions:
    def test_outputs_reset_by_the_bpod(self, mock_bpod_2p):
        """Flex, BNC, PWM and valve outputs are reset at each state, nothing else."""
        assert mock_bpod_2p._resettable_actions == [
            *(f'Flex{i}' for i in range(1, 5)),
            'TTLOut1',
            'TTLOut2',
            *(f'PWM{i}' for i in range(1, 6)),
            *(f'Valve{i}' for i in range(1, 6)),
        ]


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
        fsm.add_state('a', 1, {'GlobalTimer2_Start': 'b'}, {'GlobalTimerTrig': 2})
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

    @pytest.mark.parametrize('action', ['GlobalTimerTrig', 'GlobalTimerCancel'])
    def test_run_global_timer_action_without_timer(
        self, fsm_global_timers, mock_bpod_25, action
    ):
        """Triggering or canceling a global timer that hasn't been set is rejected."""
        fsm_global_timers.states['b'].actions[action] = 1
        with pytest.raises(ValueError, match='Global Timer 1 has not been set'):
            mock_bpod_25.run(fsm_global_timers)

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

    def test_run_while_running(self, fsm_basic, mock_bpod_25, mocker):
        """A state machine can be queued while another one is running."""
        mocker.patch.object(
            Bpod, 'is_running', new_callable=PropertyMock, return_value=True
        )
        mock_bpod_25.run(fsm_basic)
        assert mock_bpod_25.serial0.last_write.startswith(b'C')

    @pytest.mark.parametrize('trial_number', [-1, UINT32_MAX + 1])
    def test_run_rejects_out_of_range_trial_number(
        self, fsm_basic, mock_bpod_25, trial_number
    ):
        """An out-of-range trial number is rejected before anything is sent."""
        mock_bpod_25.serial0.mock_responses.clear()  # any write would raise
        index = mock_bpod_25._next_fsm_index
        with pytest.raises(ValueError, match='trial_number must be between'):
            mock_bpod_25.run(fsm_basic, trial_number=trial_number)
        assert mock_bpod_25._next_fsm_index == index

    def test_run_accepts_largest_trial_number(self, fsm_basic, mock_bpod_25):
        """The largest trial number that fits the trial column is accepted."""
        mock_bpod_25.run(fsm_basic, trial_number=UINT32_MAX)
        assert mock_bpod_25._next_fsm_index == UINT32_MAX

    def test_run_rejects_incrementing_past_largest_trial_number(
        self, fsm_basic, mock_bpod_25
    ):
        """The automatic trial number is checked too."""
        mock_bpod_25.run(fsm_basic, trial_number=UINT32_MAX)
        with pytest.raises(ValueError, match='trial_number must be between'):
            mock_bpod_25.run(fsm_basic)
        assert mock_bpod_25._next_fsm_index == UINT32_MAX

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


def test_exit_cleans_up(mock_bpod_2p):
    """Leaving the context ends the IPC service and softcode thread."""
    with mock_bpod_2p:
        pass
    mock_bpod_2p._zmq.close.assert_called_once()
    assert not mock_bpod_2p._softcode_thread.is_alive()


def test_exit_cleans_up_if_close_fails(mock_bpod_2p, mocker):
    """Leaving the context ends the IPC service and softcode thread if close fails."""
    mocker.patch.object(mock_bpod_2p, 'close', side_effect=SerialException)
    with pytest.raises(SerialException), mock_bpod_2p:
        pass
    mock_bpod_2p._zmq.close.assert_called_once()
    assert not mock_bpod_2p._softcode_thread.is_alive()


@pytest.fixture
def mock_unsupported_firmware(mock_comports, mock_ext_serial, mock_settings, mocker):
    """Mock a Bpod with outdated firmware: connecting fails after opening the port."""
    mock_ext_serial.mock_responses.update(
        {
            b'6': b'5',
            rb'\*': b'\x01',
            b'F': struct.pack('<2H', 20, 3),  # Firmware version 20 is not supported
        }
    )
    mocker.patch('bpod_core.com.ExtendedSerial', return_value=mock_ext_serial)


def test_failed_init_cleans_up(mock_unsupported_firmware, mocker):
    """A failed connection is closed and leaves no softcode thread behind."""
    close = mocker.spy(Bpod, 'close')
    threads_before = set(threading.enumerate())
    with pytest.raises(BpodError, match=r'firmware .* is not supported'):
        Bpod('COM3')
    close.assert_called_once()
    assert close.spy_exception is None
    new_threads = set(threading.enumerate()) - threads_before
    assert not any(isinstance(t, SoftcodeThread) for t in new_threads)


def test_failed_init_raises_original_error(mock_unsupported_firmware, mocker):
    """An error while cleaning up doesn't hide why the connection failed."""
    mocker.patch.object(Bpod, 'close', side_effect=SerialException)
    with pytest.raises(BpodError, match=r'firmware .* is not supported'):
        Bpod('COM3')


@pytest.fixture
def real_ipc(mock_bpod_2p, mock_ports_file, fast_event_loop, mocker):
    """Connect further Bpods to the mocked device with a real IPC service."""
    mocker.patch('bpod_core.bpod.ServiceHost', ServiceHost)
    mocker.patch.object(Bpod, '_get_setting', return_value=None)  # no name, location
    mocker.patch('bpod_core.bpod.uuid5', return_value=uuid4())  # unique socket names


def test_dropped_task_cleans_up(real_ipc, assert_collected):
    """A Bpod, owned by a dropped object that handles its softcodes, is cleaned up."""

    class Task:
        def __init__(self):
            self.handled = threading.Event()
            self.bpod = Bpod('COM3')

            def on_softcode(code):  # a closure over self, not a bound method
                self.handled.set()

            self.bpod.set_softcode_handler(on_softcode)

    task = Task()
    task.bpod._softcode_thread.queue.put(RawSoftcode(1, 0, 0))
    assert task.handled.wait(timeout=2)
    with RemoteBpod(address=task.bpod.address):  # requests for the IPC service
        pass
    bpod_ref = weakref.ref(task.bpod)
    threads = task.bpod._zmq._event_thread, task.bpod._softcode_thread
    del task
    assert_collected(bpod_ref, *threads, close=lambda b: b.__exit__(None, None, None))


def test_dropped_remote_bpod_cleans_up(real_ipc, assert_collected):
    """A subscribed RemoteBpod dropped without leaving its context ends its thread."""
    with Bpod('COM3') as bpod:
        events = []
        remote = RemoteBpod(address=bpod.address, event_callback=events.append)
        deadline = time.monotonic() + 2
        while not events and time.monotonic() < deadline:  # slow-joiner safe
            bpod._zmq.publish([EventTrialStart(time_us=0, trial=0, fsm_hash='00')])
            time.sleep(0.02)
        assert events
        remote_ref = weakref.ref(remote)
        event_thread = remote._zmq._event_thread
        del remote
        assert_collected(remote_ref, event_thread, close=lambda r: r.close())


def test_cleanup_runs_softcodes_before_disconnecting(mocker):
    """The finalizer lets queued softcodes run before disconnecting the Bpod."""
    calls = MagicMock()
    mocker.patch.object(Bpod, '_request_disconnect', calls.disconnect)
    Bpod._bpod_cleanup(calls.serial, calls.softcode_thread)
    names = [name for name, *_ in calls.mock_calls]
    assert names.index('softcode_thread.stop') < names.index('disconnect')


def test_wait_after_lost_connection(mock_bpod_2p):
    """wait() raises a lost connection once, with the serial error as its cause."""
    error = SerialException('device disconnected')
    mock_bpod_2p._read_thread = MagicMock(
        error=error, trial_number=3, is_alive=lambda: False
    )
    with pytest.raises(
        BpodError, match='Lost connection to the Bpod during trial #3'
    ) as e:
        mock_bpod_2p.wait()
    assert e.value.__cause__ is error
    mock_bpod_2p.wait()  # reported already


def test_close_after_lost_connection(mock_bpod_2p, mock_ext_serial, mocker):
    """close() still closes the port if the Bpod can't be told to disconnect."""
    mocker.patch.object(mock_ext_serial, 'is_open', True)  # reset after the test
    mocker.patch.object(mock_ext_serial, 'write', side_effect=SerialException('gone'))
    close = mocker.spy(mock_ext_serial, 'close')  # this port only, not other mocks
    mock_bpod_2p.close()
    close.assert_called_once()


def test_next_reader_gets_leftover(mock_bpod_2p, mocker):
    """A trial's reader starts with the bytes its predecessor read beyond its trial."""
    read_thread_class = mocker.patch('bpod_core.bpod.ReadThread')
    mocker.patch('bpod_core.bpod.EventThread')
    mock_bpod_2p._read_thread = MagicMock(
        leftover=b'\x01next', leftover_received_ns=42, is_alive=lambda: False
    )
    mock_bpod_2p._fsm_annotations = MagicMock()
    mock_bpod_2p._run_state_machine()
    kwargs = read_thread_class.call_args.kwargs
    assert kwargs['initial_bytes'] == b'\x01next'
    assert kwargs['initial_received_ns'] == 42


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

    def test_failed_handshake_closes_client(self, mock_client):
        """A RemoteBpod whose handshake fails closes its connection."""
        error = RemoteError(ErrorData.from_exception(RuntimeError('Version mismatch')))
        mock_client.return_value.request.side_effect = error
        with pytest.raises(RemoteError, match='Version mismatch'):
            RemoteBpod(address='tcp://127.0.0.1:1', event_callback=print)
        mock_client.return_value.close.assert_called_once()

    def test_callback_subscribes_and_forwards(self, mock_client):
        """With an event_callback, the client subscribes and messages forward."""
        received = []
        remote = RemoteBpod(address='tcp://127.0.0.1:1', event_callback=received.append)
        kwargs = mock_client.call_args.kwargs
        assert kwargs['event_handler'] == remote._event_handler
        assert kwargs['event_type'] == list[BpodEventUnion]

        message = EventTrialStart(time_us=0, trial=0, fsm_hash='00')
        remote._event_handler([message])
        assert received == [message]
