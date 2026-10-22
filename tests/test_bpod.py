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
from pydantic import ValidationError
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
from bpod_core.bpod.constants import (
    _REMOTE_CALL_METHODS,
    _REMOTE_DATA_METHODS,
    FlexIOChannelType,
    FlexIOThresholdMode,
    FlexIOThresholdPolarity,
)
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

# the Bpod fixtures patch this method; keep the original for testing it directly
_DETECT_ADDITIONAL_SERIAL_PORTS = Bpod._detect_additional_serial_ports


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


class TestInitCleanup:
    """Tests for cleanup when Bpod.__init__ fails partway through connecting."""

    def test_failure_calls_close(
        self, mock_comports, mock_ext_serial, mock_settings, mocker, mock_advertisement
    ):
        """A mid-connect failure closes the connection instead of leaving it stuck."""
        mock_ext_serial.mock_responses.update(
            {
                b'6': b'5',
                b'f': b'\x00\x00',
                b'v': b'\x01',
                b'F': b'\x17\x00\x03\x00',
                b'H': (
                    b'\x00\x01d\x00i\x05\x10\x08\x10\rUUUUUXZBBPPPP\x11UUUUUXZBBPPPPVVVV'
                ),
                b'M': b'\x00\x00\x00\x00\x00',
                rb'\*': b'\x01',
            }
        )
        mocker.patch('bpod_core.com.ExtendedSerial', return_value=mock_ext_serial)
        mocker.patch(
            'bpod_core.bpod.Bpod._detect_additional_serial_ports',
            return_value=(None, None),
        )
        mocker.patch('bpod_core.bpod.ServiceHost')
        mocker.patch(
            'bpod_core.bpod.Bpod._configure_io', side_effect=RuntimeError('boom')
        )
        close_spy = mocker.spy(Bpod, 'close')
        with pytest.raises(RuntimeError, match='boom'):
            Bpod('COM3')
        close_spy.assert_called_once()

    def test_handshake_failure_calls_close(
        self, mock_comports, mock_ext_serial, mock_settings, mocker, mock_advertisement
    ):
        """A handshake failure inside super().__init__() is also covered by cleanup."""
        mocker.patch('bpod_core.com.ExtendedSerial', return_value=mock_ext_serial)
        mocker.patch('bpod_core.bpod.ServiceHost')
        mocker.patch(
            'bpod_core.bpod.Bpod._handshake', side_effect=BpodError('handshake boom')
        )
        close_spy = mocker.spy(Bpod, 'close')
        with pytest.raises(BpodError, match='handshake boom'):
            Bpod('COM3')
        close_spy.assert_called_once()


class TestRequestDisconnect:
    @pytest.fixture
    def open_serial(self, mock_ext_serial):
        mock_ext_serial.is_open = True
        return mock_ext_serial

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
        'Bpod.action_names',
        'Bpod.address',
        'Bpod.address_control',
        'Bpod.address_events',
        'Bpod.close',
        'Bpod.flex_io',
        'Bpod.get_analog_data',
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


class TestFlexIOChannelsNotInInputsOutputs:
    """FlexIO channels are accessed through flex_io, not inputs and outputs."""

    def test_not_listed(self, mock_bpod_2p):
        assert not [n for n in mock_bpod_2p.inputs if n.startswith('Flex')]
        assert not [n for n in mock_bpod_2p.outputs if n.startswith('Flex')]

    def test_other_channels_keep_their_hardware_index(self, mock_bpod_2p):
        """Indices follow the hardware description, which still has FlexIO channels."""
        assert mock_bpod_2p.inputs['TTLIn1'].index == 9  # after U U U X Z F F F F
        assert mock_bpod_2p.outputs['TTLOut1'].index == 9

    def test_flexio_inputs_stay_enabled(self, mock_bpod_2p, mock_ext_serial):
        """The enable message still covers every input; FlexIO inputs are enabled."""
        mock_bpod_2p._set_enable_inputs()
        # U U U X Z disabled, F F F F enabled, then B B P P P P P enabled
        assert mock_ext_serial.last_write == b'E' + bytes(5) + b'\x01' * 11


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

    @pytest.fixture
    def fsm_flexio(self):
        fsm = StateMachine()
        fsm.add_state(
            'a', 0, {'Flex3_High': 'b'}, {'Flex2': 1, 'AnalogThreshDisable': 1}
        )
        fsm.add_state(
            'b', 0, {'Flex3_Low': 'c'}, {'Flex4': 1000, 'AnalogThreshEnable': 1}
        )
        fsm.add_state('c', 0, {'Flex1_Trig0': 'd'}, {'Flex4': 2500})
        fsm.add_state('d', 0, {'Flex1_Trig1': '>exit'}, {'Flex4': 4000})
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

    @pytest.mark.parametrize(
        'channel_type',
        [
            FlexIOChannelType.ANALOG_INPUT,
            FlexIOChannelType.DIGITAL_INPUT,
            FlexIOChannelType.DISABLED,
        ],
    )
    def test_global_timer_on_flexio_input_rejected(self, mock_bpod_2p, channel_type):
        """Global timers can't drive FlexIO channels that aren't outputs."""
        mock_bpod_2p.flex_io['Flex4'].channel_type = channel_type
        fsm = StateMachine()
        fsm.set_global_timer(index=0, duration=1, channel='Flex4')
        fsm.add_state('a', 1, {'Tup': '>exit'})
        with pytest.raises(ValueError, match='not configured as an output'):
            mock_bpod_2p.validate_state_machine(fsm)

    def test_global_timer_on_flexio_digital_output(self, mock_bpod_2p):
        """A global timer can drive a FlexIO digital output."""
        mock_bpod_2p.flex_io['Flex4'].channel_type = FlexIOChannelType.DIGITAL_OUTPUT
        fsm = StateMachine()
        fsm.set_global_timer(index=0, duration=1, channel='Flex4')
        fsm.add_state('a', 1, {'Tup': '>exit'})
        mock_bpod_2p.validate_state_machine(fsm)

    def test_global_timer_on_flexio_analog_output(self, mock_bpod_2p):
        """Timer values on a FlexIO analog output are millivolts, sent as DAC counts."""
        mock_bpod_2p.flex_io['Flex4'].channel_type = FlexIOChannelType.ANALOG_OUTPUT
        fsm = StateMachine()
        fsm.set_global_timer(
            index=0, duration=1, channel='Flex4', value_on=2500, value_off=1000
        )
        fsm.add_state('a', 1, {'Tup': '>exit'})
        mock_bpod_2p.run(fsm)
        # value_on then value_off, as 16-bit DAC counts: 2500 mV -> 2048, 1000 -> 819
        assert struct.pack('<2H', 2048, 819) in mock_bpod_2p.serial0.last_write

    @pytest.mark.parametrize(
        ('channel', 'value', 'match'),
        [
            ('Flex4', 5001, 'must not exceed 5000 mV'),
            ('PWM1', 256, 'must not exceed 255'),
        ],
    )
    def test_global_timer_value_limits(self, mock_bpod_2p, channel, value, match):
        """Analog outputs take up to 5000 mV, every other channel up to 255."""
        mock_bpod_2p.flex_io['Flex4'].channel_type = FlexIOChannelType.ANALOG_OUTPUT
        fsm = StateMachine()
        fsm.set_global_timer(index=0, duration=1, channel=channel, value_on=value)
        fsm.add_state('a', 1, {'Tup': '>exit'})
        with pytest.raises(ValueError, match=match):
            mock_bpod_2p.validate_state_machine(fsm)

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

    def test_run_flexio_2p(self, fsm_flexio, mock_bpod_2p):
        """Test running a state machine with flexIO channels on Bpod 2+."""
        mock_bpod_2p.flex_io['Flex1'].channel_type = FlexIOChannelType.ANALOG_INPUT
        mock_bpod_2p.flex_io['Flex2'].channel_type = FlexIOChannelType.DIGITAL_OUTPUT
        mock_bpod_2p.flex_io['Flex3'].channel_type = FlexIOChannelType.DIGITAL_INPUT
        mock_bpod_2p.flex_io['Flex4'].channel_type = FlexIOChannelType.ANALOG_OUTPUT
        mock_bpod_2p.run(fsm_flexio)
        assert mock_bpod_2p.serial0.last_write == (
            b'C\x01\x00d\x00\x04\x00\x00\x00\x00\x01\x02\x03\x01O\x01\x01P\x02\x01K\x03'
            b'\x01L\x04\x01\x00\x06\x00\x01\x00\x01\x00\x08\x003\x03\x01\x00\x08\x00'
            b'\x00\x08\x01\x00\x08\x00\xcc\x0c\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x01\x01\x01\x01\x00\x01\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
            b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00'
        )

    def test_run_flexio_channel_type_change_invalidates_cache(self, mock_bpod_2p):
        """Switching a Flex channel's type busts the compiled-FSM cache.

        The cache is shared by all Bpod objects, so a reconnected Bpod with another
        FlexIO layout must not reuse compiled bytes.
        """
        mock_bpod_2p.flex_io['Flex4'].channel_type = FlexIOChannelType.DIGITAL_OUTPUT
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Tup': '>exit'}, {'Flex4': 1000})
        mock_bpod_2p.run(fsm)
        idx = mock_bpod_2p._actions.index('Flex4')
        assert struct.pack('<HHH', 1, idx, 1000) in mock_bpod_2p.serial0.last_write

        mock_bpod_2p._state_machine_started = False  # as after reconnecting
        mock_bpod_2p.flex_io['Flex4'].channel_type = FlexIOChannelType.ANALOG_OUTPUT
        mock_bpod_2p.run(fsm)
        # 1000 mV -> round(1000 * 4095/5000) = 819 DAC counts
        assert struct.pack('<HHH', 1, idx, 819) in mock_bpod_2p.serial0.last_write

    @pytest.mark.parametrize(
        ('bpod', 'largest'), [('mock_bpod_25', 255), ('mock_bpod_2p', 65535)]
    )
    def test_action_value_fits_the_bpod(self, bpod, largest, request):
        """Action values fit the Bpod's 8-bit (before r2+) or 16-bit values."""
        bpod = request.getfixturevalue(bpod)
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Tup': '>exit'}, {'PWM1': largest})
        bpod.validate_state_machine(fsm)
        if largest < 65535:  # 65536 is already rejected by the state machine model
            fsm.states['a'].actions['PWM1'] = largest + 1
            with pytest.raises(ValueError, match=f'must not exceed {largest} on this'):
                bpod.validate_state_machine(fsm)

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


class TestFlexIOSettings:
    """Tests for Bpod.flex_io's configuration properties."""

    def test_run_locks_channel_types_and_rate(self, mock_bpod_2p, mocker):
        """Running a state machine fixes channel types and the sampling rate."""
        mocker.patch.object(Bpod, '_run_state_machine')
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Tup': '>exit'})
        mock_bpod_2p.run(fsm)
        with pytest.raises(BpodError, match='Close and reopen the Bpod'):
            mock_bpod_2p.flex_io['Flex1'].channel_type = FlexIOChannelType.ANALOG_INPUT
        with pytest.raises(BpodError, match='Close and reopen the Bpod'):
            mock_bpod_2p.flex_io.analog_sampling_rate = 500

    def test_lock_survives_session_clock_reset(self, mock_bpod_2p):
        """Resetting the session clock doesn't unlock channel types or the rate."""
        mock_bpod_2p._state_machine_started = True
        mock_bpod_2p.reset_session_clock()
        with pytest.raises(BpodError, match='Cannot change FlexIO channel types'):
            mock_bpod_2p.flex_io['Flex1'].channel_type = FlexIOChannelType.ANALOG_INPUT

    def test_other_settings_unaffected_by_lock(self, mock_bpod_2p):
        """Thresholds and reads per sample stay changeable between trials."""
        mock_bpod_2p.flex_io['Flex1'].channel_type = FlexIOChannelType.ANALOG_INPUT
        mock_bpod_2p._state_machine_started = True
        mock_bpod_2p.flex_io.n_reads_per_sample = 2
        mock_bpod_2p.flex_io['Flex1'].threshold_mode = FlexIOThresholdMode.LINKED
        mock_bpod_2p.flex_io['Flex1'].thresholds[0].voltage = 2500
        mock_bpod_2p.flex_io['Flex1'].thresholds[
            0
        ].polarity = FlexIOThresholdPolarity.FALLING
        mock_bpod_2p.flex_io['Flex1'].thresholds[0].enabled = True

    def test_run_flexio_action_on_non_output_channel(self, mock_bpod_2p):
        """Referencing a Flex action on a non-output-configured channel is rejected."""
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Tup': '>exit'}, {'Flex1': 1})
        with pytest.raises(ValueError, match="Invalid action 'Flex1'"):
            mock_bpod_2p.run(fsm)

    def test_analog_output_action_rejects_float_value(self, mock_bpod_2p):
        """A float value for a FlexIO ANALOG_OUTPUT action is no longer accepted."""
        with pytest.raises(ValueError, match='fractional part'):
            StateMachine().add_state('a', 0, {'Tup': '>exit'}, {'Flex4': 2.5})

    def test_analog_output_action_rejects_out_of_range_millivolts(self, mock_bpod_2p):
        """An out-of-range integer millivolt value for ANALOG_OUTPUT is rejected."""
        mock_bpod_2p.flex_io['Flex4'].channel_type = FlexIOChannelType.ANALOG_OUTPUT
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Tup': '>exit'}, {'Flex4': 5001})
        with pytest.raises(ValueError, match='between 0 and 5000'):
            mock_bpod_2p.validate_state_machine(fsm)

    def test_analog_output_action_reported_in_millivolts(self, mock_bpod_2p):
        """get_data()'s reported ANALOG_OUTPUT value is millivolts, not DAC counts."""
        mock_bpod_2p.flex_io['Flex4'].channel_type = FlexIOChannelType.ANALOG_OUTPUT
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Tup': '>exit'}, {'Flex4': 1000})
        fsm_bytes, annotations, _ = mock_bpod_2p._compile_state_machine(
            state_machine=fsm
        )
        # the wire still carries the raw 12-bit DAC count...
        idx = mock_bpod_2p._actions.index('Flex4')
        assert struct.pack('<HHH', 1, idx, 819) in fsm_bytes
        # ...while the annotation used to build get_data()'s value is millivolts
        assert annotations.state_actions[0]['Flex4'] == 1000

    def test_flexio_threshold_event_rejected_on_mis_ranked_channel(self, mock_bpod_2p):
        """A Trig event on a mis-ranked ANALOG_INPUT channel can't be bound.

        Firmware numbers analog-threshold events by rank among ANALOG_INPUT
        channels only; if an earlier physical Flex channel isn't also
        ANALOG_INPUT, the resulting event code collides with a different
        channel's event.
        """
        mock_bpod_2p.flex_io['Flex1'].channel_type = FlexIOChannelType.DIGITAL_INPUT
        mock_bpod_2p.flex_io['Flex3'].channel_type = FlexIOChannelType.ANALOG_INPUT
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Flex3_Trig0': '>exit'})
        with pytest.raises(ValueError, match='Invalid transition condition'):
            mock_bpod_2p.validate_state_machine(fsm)

    def test_flexio_threshold_arming_rejected_on_mis_ranked_channel(self, mock_bpod_2p):
        """Arming a mis-ranked ANALOG_INPUT channel's threshold is rejected."""
        mock_bpod_2p.flex_io['Flex1'].channel_type = FlexIOChannelType.DIGITAL_INPUT
        mock_bpod_2p.flex_io['Flex3'].channel_type = FlexIOChannelType.ANALOG_INPUT
        with pytest.raises(BpodError, match="Cannot arm 'Flex3'"):
            mock_bpod_2p.flex_io['Flex3'].thresholds[0].enabled = True

    def test_analog_thresh_enable_action_rejected_on_mis_ranked_channel(
        self, mock_bpod_2p
    ):
        """AnalogThreshEnable can't arm a mis-ranked channel's threshold either.

        This action bypasses the `.enabled` setter and sends a raw bitmask
        straight to firmware, so it needs its own validation against the same
        rank-ambiguity rule.
        """
        mock_bpod_2p.flex_io['Flex1'].channel_type = FlexIOChannelType.DIGITAL_INPUT
        mock_bpod_2p.flex_io['Flex3'].channel_type = FlexIOChannelType.ANALOG_INPUT
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Tup': '>exit'}, {'AnalogThreshEnable': 0b100})
        with pytest.raises(ValueError, match="arms channel 3's threshold"):
            mock_bpod_2p.validate_state_machine(fsm)

    @pytest.mark.parametrize(
        'action_name', ['AnalogThreshEnable', 'AnalogThreshDisable']
    )
    def test_analog_thresh_action_rejected_on_non_analog_input_channel(
        self, mock_bpod_2p, action_name
    ):
        """AnalogThreshEnable/Disable can't reference a non-ANALOG_INPUT channel."""
        mock_bpod_2p.flex_io['Flex1'].channel_type = FlexIOChannelType.DIGITAL_OUTPUT
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Tup': '>exit'}, {action_name: 0b1})
        with pytest.raises(ValueError, match='channel 1 is not configured'):
            mock_bpod_2p.validate_state_machine(fsm)

    def test_flexio_threshold_allowed_on_rank_0_channel(self, mock_bpod_2p):
        """A channel with no non-ANALOG_INPUT channels ahead of it stays usable."""
        mock_bpod_2p.flex_io['Flex1'].channel_type = FlexIOChannelType.ANALOG_INPUT
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Flex1_Trig0': '>exit'})
        mock_bpod_2p.validate_state_machine(fsm)
        mock_bpod_2p.flex_io['Flex1'].thresholds[0].enabled = True

    def test_flexio_analog_sampling_rate(self, mock_bpod_2p):
        """Setting the FlexIO analog sampling rate sends the '^' opcode."""
        cycle_frequency = mock_bpod_2p._hardware.cycle_frequency
        mock_bpod_2p.flex_io.analog_sampling_rate = 500
        n_cycles = round(cycle_frequency / 500)
        assert mock_bpod_2p.serial0.last_write == struct.pack('<cI', b'^', n_cycles)

    def test_flexio_analog_sampling_rate_refreshes_sample_period(
        self, mock_bpod_2p, mocker
    ):
        """Changing the sampling rate live refreshes the cached analog sample period."""
        cycle_frequency = mock_bpod_2p._hardware.cycle_frequency
        cycle_period_us = mock_bpod_2p._hardware.cycle_period_us
        mock_bpod_2p._flexio_serial = MagicMock(spec=ExtendedSerial, is_open=False)
        mocker.patch('bpod_core.bpod.ReaderThread')
        mocker.patch('bpod_core.bpod.ChunkedSerialReader')
        mock_bpod_2p.flex_io['Flex3'].channel_type = FlexIOChannelType.ANALOG_INPUT
        mock_bpod_2p.flex_io.analog_sampling_rate = 500
        mock_bpod_2p.flex_io.analog_sampling_rate = 100
        n_cycles = round(cycle_frequency / 100)
        assert mock_bpod_2p._analog_sample_period_us == n_cycles * cycle_period_us

    def test_flexio_threshold_enabled_always_writes(self, mock_bpod_2p):
        """Re-enabling a threshold sends 'e' even if the cached state looks unchanged.

        Firmware disarms a threshold autonomously when it fires, so the host's cached
        state can go stale; setting `.enabled = True` must not be a no-op just because
        the host's own cache already claims it's enabled.
        """
        threshold = mock_bpod_2p.flex_io['Flex1'].thresholds[0]
        threshold.enabled = True
        first_write = mock_bpod_2p.serial0.last_write
        assert first_write == struct.pack('<cBB?', b'e', 0, 0, True)
        mock_bpod_2p.serial0.last_write = b''
        threshold.enabled = True  # simulates re-arming after a firmware-side trigger
        assert mock_bpod_2p.serial0.last_write == first_write

    def test_flexio_n_reads_per_sample(self, mock_bpod_2p):
        """Setting the FlexIO reads-per-sample sends the 'o' opcode."""
        mock_bpod_2p.flex_io.n_reads_per_sample = 2
        assert mock_bpod_2p.serial0.last_write == struct.pack('<cB', b'o', 2)

    def test_flexio_setters_raise_while_running(self, mock_bpod_2p, mocker):
        """FlexIO setters refuse to write to serial0 while a trial is running.

        A running trial's read thread also owns serial0; interleaving a FlexIO
        settings write/ack with the live event stream would corrupt it.
        """
        mocker.patch.object(
            Bpod, 'is_running', new_callable=PropertyMock
        ).return_value = True
        with pytest.raises(BpodError, match='Cannot change FlexIO settings'):
            mock_bpod_2p.flex_io.n_reads_per_sample = 2
        with pytest.raises(BpodError, match='Cannot re-arm an analog threshold'):
            mock_bpod_2p.flex_io['Flex1'].thresholds[0].enabled = True

    def test_flexio_threshold_voltages_are_threshold_major(self, mock_bpod_2p):
        """The 't' opcode sends all channels' threshold-0 values, then threshold-1."""
        voltages = {0: (1000, 1100), 1: (2000, 2100), 2: (3000, 3100), 3: (4000, 4100)}
        for channel_index, (v0, v1) in voltages.items():
            channel = mock_bpod_2p.flex_io[f'Flex{channel_index + 1}']
            channel.thresholds[0].voltage = v0
            channel.thresholds[1].voltage = v1
        raw = [round(v0 * 4095 / 5000) for v0, _ in voltages.values()]
        raw += [round(v1 * 4095 / 5000) for _, v1 in voltages.values()]
        assert mock_bpod_2p.serial0.last_write == struct.pack('<c8H', b't', *raw)

    def test_flexio_threshold_voltage_matches_analog_data(self, mock_bpod_2p):
        """A threshold set to a recorded value maps to that sample's exact ADC count."""
        for count in (0, 1, 2047, 2048, 4094, 4095):
            millivolts = (count * 5000 + 4095 // 2) // 4095  # as in get_analog_data
            mock_bpod_2p.flex_io['Flex1'].thresholds[0].voltage = millivolts
            sent = struct.unpack('<c8H', mock_bpod_2p.serial0.last_write)[1]
            assert sent == count

    @pytest.mark.parametrize('value', [2.5, -1, 5001])
    def test_flexio_threshold_voltage_rejects_invalid(self, mock_bpod_2p, value):
        """Thresholds take whole millivolts from 0 to 5000."""
        with pytest.raises(ValidationError):
            mock_bpod_2p.flex_io['Flex1'].thresholds[0].voltage = value

    def test_flexio_threshold_polarities_are_threshold_major(self, mock_bpod_2p):
        """The 'p' opcode sends all channels' threshold-0 values, then threshold-1."""
        polarities = {
            0: (FlexIOThresholdPolarity.RISING, FlexIOThresholdPolarity.FALLING),
            1: (FlexIOThresholdPolarity.FALLING, FlexIOThresholdPolarity.RISING),
            2: (FlexIOThresholdPolarity.RISING, FlexIOThresholdPolarity.RISING),
            3: (FlexIOThresholdPolarity.FALLING, FlexIOThresholdPolarity.FALLING),
        }
        for channel_index, (p0, p1) in polarities.items():
            channel = mock_bpod_2p.flex_io[f'Flex{channel_index + 1}']
            channel.thresholds[0].polarity = p0
            channel.thresholds[1].polarity = p1
        expected = [p0 for p0, _ in polarities.values()] + [
            p1 for _, p1 in polarities.values()
        ]
        assert mock_bpod_2p.serial0.last_write == struct.pack('<c8B', b'p', *expected)


class TestClose:
    """Tests for Bpod.close()."""

    def test_closes_flexio_serial(self, mock_bpod_2p):
        """Closing the Bpod also closes the FlexIO analog serial port, if open."""
        analog_serial = MagicMock(spec=ExtendedSerial, is_open=True)
        mock_bpod_2p._flexio_serial = analog_serial
        mock_bpod_2p.close()
        analog_serial.close.assert_called_once()

    def test_flushes_flexio_serial_before_closing(self, mock_bpod_2p):
        """Bytes waiting on the FlexIO serial port are read and discarded first.

        Firmware keeps streaming analog samples until the 'X' stop command below
        takes effect; discarding whatever arrived up to that point keeps a stale
        byte from being misread as the next connection's discovery response.
        """
        analog_serial = MagicMock(spec=ExtendedSerial, is_open=True)
        mock_bpod_2p._flexio_serial = analog_serial
        mock_bpod_2p.close()
        analog_serial.read_all.assert_called_once()
        assert analog_serial.method_calls.index(
            ('read_all', (), {})
        ) < analog_serial.method_calls.index(('close', (), {}))

    def test_logs_discarded_bytes_on_close(self, mock_bpod_2p, caplog):
        """A non-empty read_all() on close is logged as a warning, with the count."""
        analog_serial = MagicMock(spec=ExtendedSerial, is_open=True)
        analog_serial.read_all.return_value = b'\x00\x00\x00\x00'
        mock_bpod_2p._flexio_serial = analog_serial
        with caplog.at_level(logging.WARNING):
            mock_bpod_2p.close()
        assert 'Discarded 4 stray byte(s)' in caplog.text

    def test_stops_analog_acquisition_on_close(self, mock_bpod_2p, mocker, caplog):
        """With an analog input, closing sends 'X' and reads the Bpod's reply.

        Firmware only stops FlexIO analog acquisition on an explicit 'X', not when
        a trial ends naturally via '>exit'. While idle, it answers 'X' with a
        trial-end report, which must not be taken for stray bytes.
        """
        # isolate the 'X' write from the later disconnect request, which would
        # otherwise also update last_write
        mocker.patch.object(Bpod, '_request_disconnect')
        serial0 = mock_bpod_2p.serial0
        serial0.is_open = True
        serial0.mock_responses[b'X'] = b'\x01\x01\xff' + bytes(16)
        mock_bpod_2p._analog_reader_thread = MagicMock()
        mock_bpod_2p.close()
        assert serial0.last_write == b'X'
        assert len(serial0.response_buffer) == 0
        assert 'stray' not in caplog.text
        assert 'Unexpected reply' not in caplog.text

    def test_no_stop_command_without_analog_input(self, mock_bpod_2p, mocker):
        """Without an analog input there is no stream to stop, so no 'X' is sent."""
        mocker.patch.object(Bpod, '_request_disconnect')
        mock_bpod_2p.serial0.is_open = True
        mock_bpod_2p.serial0.last_write = b''
        mock_bpod_2p.close()
        assert mock_bpod_2p.serial0.last_write == b''

    def test_unexpected_reply_to_stop_command(self, mock_bpod_2p, mocker, caplog):
        """A reply that isn't a trial-end report is logged."""
        mocker.patch.object(Bpod, '_request_disconnect')
        mock_bpod_2p.serial0.is_open = True
        mock_bpod_2p.serial0.mock_responses[b'X'] = b'\x02\x01'
        mock_bpod_2p._analog_reader_thread = MagicMock()
        mock_bpod_2p.close()
        assert 'Unexpected reply to stop command: 0201' in caplog.text

    def test_flushes_serial0_before_closing(self, mock_bpod_2p, mocker):
        """Bytes waiting on serial0 are read and discarded before the port closes.

        Cheap defensive hygiene: whatever's unread at this point is abandoned
        either way (the connection is closing), so discarding it keeps a
        straggler byte from confusing the next connection's own handshake.
        """
        mocker.patch.object(Bpod, '_request_disconnect')
        mock_bpod_2p.serial0.is_open = True
        mock_bpod_2p.serial0.response_buffer.extend(b'\x00\x00')  # simulated straggler
        mock_bpod_2p.close()
        assert len(mock_bpod_2p.serial0.response_buffer) == 0

    def test_closes_analog_reader_thread(self, mock_bpod_2p):
        """Closing the Bpod stops an active analog reader thread, if present."""
        reader_thread = MagicMock()
        mock_bpod_2p._analog_reader_thread = reader_thread
        mock_bpod_2p.close()
        reader_thread.stop.assert_called_once()
        assert mock_bpod_2p._analog_reader_thread is None

    def test_keeps_buffered_analog_samples_on_close(self, mock_bpod_2p):
        """Samples buffered since the last periodic flush are kept on close."""
        mock_bpod_2p._analog_reader_thread = MagicMock()
        mock_bpod_2p._analog_channel_names = ('Flex1',)
        mock_bpod_2p._analog_first_trial_time_us = 0
        mock_bpod_2p._analog_sample_period_us = 1000
        mock_bpod_2p._analog_buffer = [(0, 0, 2048)]
        mock_bpod_2p.close()
        assert mock_bpod_2p.get_analog_data()['Flex1'].to_list() == [2501]


class TestAnalogChunk:
    """Tests for Bpod._on_analog_chunk / _flush_analog_buffer / get_analog_data."""

    def test_trial_counter_zero_is_dropped(self, mock_bpod_2p):
        """A trial_counter of 0 (pre-trial idle state) is dropped, uncounted."""
        mock_bpod_2p._analog_channel_names = ('Flex3',)
        mock_bpod_2p._analog_struct = struct.Struct('<2H')
        mock_bpod_2p._on_analog_chunk(bytearray(struct.pack('<2H', 0, 2048)))
        assert mock_bpod_2p._analog_buffer == []
        assert mock_bpod_2p._analog_sample_index == 0

    def test_samples_are_counted_before_anchor_is_known(self, mock_bpod_2p):
        """Samples are buffered/counted even before the anchor is known.

        The analog reader thread and the EventThread that sets the anchor run on
        independent serial ports, so a real sample can arrive before the anchor
        is known; it must still be counted, or every later timestamp would shift
        by however many samples raced ahead of the anchor.
        """
        mock_bpod_2p._analog_channel_names = ('Flex3',)
        mock_bpod_2p._analog_struct = struct.Struct('<2H')
        mock_bpod_2p._analog_reader_thread = MagicMock()
        mock_bpod_2p._analog_first_trial_time_us = None
        mock_bpod_2p._analog_last_flush_ns = 0  # force an immediate flush attempt
        mock_bpod_2p._on_analog_chunk(bytearray(struct.pack('<2H', 1, 2048)))
        assert mock_bpod_2p._analog_sample_index == 1
        assert len(mock_bpod_2p._analog_buffer) == 1
        mock_bpod_2p._flush_analog_buffer()  # anchor unknown - must be a no-op
        assert len(mock_bpod_2p._analog_buffer) == 1

        mock_bpod_2p._analog_first_trial_time_us = 1_000_000
        mock_bpod_2p._analog_sample_period_us = 1000
        mock_bpod_2p._flush_analog_buffer()
        assert mock_bpod_2p._analog_buffer == []
        frame = mock_bpod_2p.get_analog_data()
        cycle_period_us = mock_bpod_2p._hardware.cycle_period_us
        assert frame['time'].dt.epoch('us').to_list() == [1_000_000 + cycle_period_us]

    def test_buffers_and_flushes_decoded_samples(self, mock_bpod_2p):
        """Decoded samples are buffered and flushed as a LazyFrame with timing."""
        mock_bpod_2p._analog_channel_names = ('Flex3',)
        mock_bpod_2p._analog_struct = struct.Struct('<2H')
        mock_bpod_2p._analog_reader_thread = MagicMock()
        mock_bpod_2p._analog_first_trial_time_us = 1_000_000
        mock_bpod_2p._analog_sample_period_us = 1000
        mock_bpod_2p._analog_trial_number_by_sequence = [7]
        mock_bpod_2p._analog_last_flush_ns = 0  # force an immediate flush
        # firmware's trial_counter is 1-indexed (increments before first use), so the
        # first entry in _analog_trial_number_by_sequence corresponds to counter 1
        mock_bpod_2p._on_analog_chunk(bytearray(struct.pack('<2H', 1, 2048)))
        mock_bpod_2p._on_analog_chunk(bytearray(struct.pack('<2H', 1, 4095)))
        mock_bpod_2p._flush_analog_buffer()  # flush the second sample too
        frame = mock_bpod_2p.get_analog_data()
        assert frame.columns == ['time', 'trial', 'Flex3']
        assert frame.schema['Flex3'] == pl.UInt16
        assert frame.schema['trial'] == pl.UInt32
        assert frame['trial'].to_list() == [7, 7]
        # 2048/4095 counts -> round(count * 5000/4095) mV, exact integer math
        assert frame['Flex3'].to_list() == [2501, 5000]
        # firmware's sample clock is preloaded, so the first sample fires one
        # state-machine cycle (not a full sample period) after trial start
        cycle_period_us = mock_bpod_2p._hardware.cycle_period_us
        assert frame['time'].dt.epoch('us').to_list() == [
            1_000_000 + cycle_period_us,
            1_000_000 + cycle_period_us + 1000,
        ]

    def test_unknown_trial_counter_falls_back_to_raw_value(self, mock_bpod_2p):
        """A trial_counter with no recorded mapping is used as-is."""
        mock_bpod_2p._analog_channel_names = ('Flex3',)
        mock_bpod_2p._analog_struct = struct.Struct('<2H')
        mock_bpod_2p._analog_reader_thread = MagicMock()
        mock_bpod_2p._analog_first_trial_time_us = 0
        mock_bpod_2p._analog_trial_number_by_sequence = []
        mock_bpod_2p._analog_last_flush_ns = 0
        mock_bpod_2p._on_analog_chunk(bytearray(struct.pack('<2H', 5, 0)))
        frame = mock_bpod_2p.get_analog_data()
        assert frame['trial'][0] == 5

    def test_get_analog_data_raises_when_unavailable(self, mock_bpod_2p):
        """get_analog_data raises if no data exists and no reader is active."""
        with pytest.raises(BpodError, match='No FlexIO channels configured'):
            mock_bpod_2p.get_analog_data()

    def test_get_analog_data_returns_data_after_reader_stopped(self, mock_bpod_2p):
        """Data recorded before the analog reader stopped can still be retrieved."""
        assert mock_bpod_2p._analog_reader_thread is None
        mock_bpod_2p._analog_data.put(pl.LazyFrame({'Flex1': [1, 2]}))
        assert mock_bpod_2p.get_analog_data()['Flex1'].to_list() == [1, 2]
        with pytest.raises(BpodError, match='No FlexIO channels configured'):
            mock_bpod_2p.get_analog_data()


class TestSyncAnalogReader:
    """Tests for Bpod._sync_analog_reader."""

    def test_noop_without_flexio_serial(self, mock_bpod_2p):
        """No reader is started when the analog serial port wasn't detected."""
        mock_bpod_2p._flexio_serial = None
        mock_bpod_2p._sync_analog_reader()
        assert mock_bpod_2p._analog_reader_thread is None

    def test_noop_without_analog_input_channels(self, mock_bpod_2p):
        """No reader is started while no channel is configured as ANALOG_INPUT."""
        mock_bpod_2p._flexio_serial = MagicMock(spec=ExtendedSerial)
        mock_bpod_2p._sync_analog_reader()
        assert mock_bpod_2p._analog_reader_thread is None

    def test_starts_reader_when_channel_becomes_analog_input(
        self, mock_bpod_2p, mocker
    ):
        """Configuring a channel as ANALOG_INPUT starts the analog reader thread."""
        mock_bpod_2p._flexio_serial = MagicMock(spec=ExtendedSerial, is_open=False)
        mock_reader_thread_cls = mocker.patch('bpod_core.bpod.ReaderThread')
        mocker.patch('bpod_core.bpod.ChunkedSerialReader')
        mock_bpod_2p.flex_io['Flex3'].channel_type = FlexIOChannelType.ANALOG_INPUT
        assert mock_bpod_2p._analog_channel_names == ('Flex3',)
        mock_reader_thread_cls.return_value.start.assert_called_once()

    def test_stops_reader_when_no_channel_remains_analog_input(
        self, mock_bpod_2p, mocker
    ):
        """Reconfiguring away from ANALOG_INPUT stops the analog reader thread."""
        mock_bpod_2p._flexio_serial = MagicMock(spec=ExtendedSerial, is_open=False)
        mocker.patch('bpod_core.bpod.ReaderThread')
        mocker.patch('bpod_core.bpod.ChunkedSerialReader')
        mock_bpod_2p.flex_io['Flex3'].channel_type = FlexIOChannelType.ANALOG_INPUT
        reader_thread = mock_bpod_2p._analog_reader_thread
        mock_bpod_2p.flex_io['Flex3'].channel_type = FlexIOChannelType.DIGITAL_INPUT
        reader_thread.stop.assert_called_once()
        assert mock_bpod_2p._analog_reader_thread is None
        assert mock_bpod_2p._analog_channel_names == ()

    def test_keeps_reader_when_analog_channels_unchanged(self, mock_bpod_2p, mocker):
        """Unrelated changes don't restart the reader, which could start mid-frame."""
        mock_bpod_2p._flexio_serial = MagicMock(spec=ExtendedSerial, is_open=False)
        reader_thread_cls = mocker.patch('bpod_core.bpod.ReaderThread')
        mocker.patch('bpod_core.bpod.ChunkedSerialReader')
        mock_bpod_2p.flex_io['Flex3'].channel_type = FlexIOChannelType.ANALOG_INPUT
        mock_bpod_2p._recompile_hardware_tables()  # as update_modules() does
        mock_bpod_2p.flex_io['Flex1'].channel_type = FlexIOChannelType.DIGITAL_OUTPUT
        mock_bpod_2p.flex_io.analog_sampling_rate = 500
        reader_thread_cls.assert_called_once()
        reader_thread_cls.return_value.stop.assert_not_called()
        assert mock_bpod_2p._analog_sample_period_us == 2000  # 500 Hz


class TestStopStateMachine:
    @pytest.fixture
    def running(self, mock_bpod_2p, mocker):
        mocker.patch.object(
            Bpod, 'is_running', new_callable=PropertyMock, return_value=True
        )
        mock_bpod_2p._read_thread = None
        return mock_bpod_2p

    def test_warns_about_analog_sampling(self, running, caplog):
        """With an analog input, stopping warns that analog sampling stops too."""
        running._analog_channel_names = ('Flex1',)
        running.stop_state_machine()
        assert running.serial0.last_write == b'X'
        assert 'also stops FlexIO analog sampling' in caplog.text

    def test_no_warning_without_analog_input(self, running, caplog):
        """Without analog inputs, stopping doesn't warn."""
        running.stop_state_machine()
        assert 'analog sampling' not in caplog.text


def test_missing_flexio_port_suggests_power_cycle(mock_bpod_2p, mocker):
    """A FlexIO port that can't be found suggests power-cycling the Bpod."""
    mocker.patch(
        'bpod_core.bpod.find_ports', return_value=[SimpleNamespace(device='COM4')]
    )
    mocker.patch('bpod_core.bpod.ExtendedSerial')
    mocker.patch(  # the secondary port answers its discovery byte, FlexIO's doesn't
        'bpod_core.bpod.verify_serial_discovery',
        side_effect=lambda _port, message, **_: message == bytes([222]),
    )
    with pytest.raises(BpodError, match='power-cycle the Bpod'):
        _DETECT_ADDITIONAL_SERIAL_PORTS(mock_bpod_2p)


class TestAnalogSessionBookkeeping:
    """Tests for analog-data bookkeeping tied to trial/session lifecycle."""

    def test_reset_session_clock_clears_analog_bookkeeping(self, mock_bpod_2p):
        """Resetting the session clock clears the analog anchor/trial mapping."""
        mock_bpod_2p._analog_first_trial_time_us = 123
        mock_bpod_2p._analog_trial_number_by_sequence = [0, 1, 2]
        mock_bpod_2p._analog_sample_index = 5
        mock_bpod_2p.reset_session_clock()
        assert mock_bpod_2p._analog_first_trial_time_us is None
        assert mock_bpod_2p._analog_trial_number_by_sequence == []
        assert mock_bpod_2p._analog_sample_index == 0

    def test_run_appends_trial_number_by_sequence(self, mock_bpod_25, mocker):
        """Each run() call records its trial number at the next sequence position.

        It is recorded before the state machine is sent, since the Bpod may start it
        right away.
        """
        mocker.patch('bpod_core.bpod.ReadThread')
        mocker.patch('bpod_core.bpod.EventThread')
        recorded_when_sent = []
        write = mock_bpod_25.serial0.write

        def record_and_write(data):
            if data[:1] == b'C':  # state machine transmission
                trial_numbers = mock_bpod_25._analog_trial_number_by_sequence
                recorded_when_sent.append(list(trial_numbers))
            return write(data)

        mocker.patch.object(mock_bpod_25.serial0, 'write', side_effect=record_and_write)
        fsm = StateMachine()
        fsm.add_state('a', 0, {'Tup': '>exit'})
        mock_bpod_25.run(fsm, trial_number=42)
        mock_bpod_25.run(fsm, trial_number=99)
        assert recorded_when_sent == [[42], [42, 99]]

    def test_capture_analog_anchor_sets_it_once(self, mock_bpod_2p):
        """The first captured time sets the analog anchor; later calls don't."""
        assert mock_bpod_2p._analog_first_trial_time_us is None
        mock_bpod_2p._capture_analog_anchor(1000)
        assert mock_bpod_2p._analog_first_trial_time_us == 1000
        mock_bpod_2p._capture_analog_anchor(2000)
        assert mock_bpod_2p._analog_first_trial_time_us == 1000


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
