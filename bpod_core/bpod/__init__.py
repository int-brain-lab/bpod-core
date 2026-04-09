"""Module for interfacing with the Bpod Finite State Machine."""

import contextlib
import logging
import re
import struct
import time
import traceback
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from time import perf_counter_ns
from types import TracebackType
from typing import Any, Literal, NamedTuple, cast, overload

import numpy as np
import polars as pl
from pydantic import ConfigDict, validate_call
from serial import SerialException

from bpod_core import __version__ as bpod_core_version
from bpod_core.bpod.abc import AbstractBpod
from bpod_core.bpod.constants import (
    _CHANNEL_BASE_NAME_CONDITION,
    _CHANNEL_BASE_NAME_GLOBAL_COUNTER,
    _CHANNEL_BASE_NAME_GLOBAL_TIMER,
    CHANNEL_TYPES_INPUT,
    CHANNEL_TYPES_OUTPUT,
    CONFIG_PATH,
    DISCOVERY_TIMEOUT,
    MACHINE_TYPES,
    MAX_BPOD_HW_VERSION,
    MIN_BPOD_FW_VERSION,
    MIN_BPOD_HW_VERSION,
    N_SERIAL_EVENTS_DEFAULT,
    PIDS_BPOD,
    VALID_OPERATORS,
    VIDS_BPOD,
)
from bpod_core.bpod.structs import (
    BpodInfo,
    HardwareConfiguration,
    StateMachineLookup,
    TimeReferences,
    VersionInfo,
    _InputEventRanges,
    _InputEvents,
)
from bpod_core.bpod.threads import (
    _TRIAL_DATA_SCHEMA,
    EventThread,
    ReadThread,
    SoftcodeThread,
    _build_event_lookup,
)
from bpod_core.com import (
    ExtendedSerial,
    SerialDevice,
    find_ports,
    verify_serial_discovery,
)
from bpod_core.constants import FMT_UINT8, FMT_UINT16_LE, FMT_UINT32_LE, TeensyPID
from bpod_core.fsm import StateMachine
from bpod_core.ipc import ServiceClient, ServiceEvent, ServiceHost, iter_services
from bpod_core.misc import SettingsDict, extend_packed, suggest_similar

logger = logging.getLogger(__name__)


class BpodError(Exception):
    """
    Exception class for Bpod-related errors.

    This exception is raised when an error specific to the Bpod device or its
    operations occurs.
    """


class Bpod(SerialDevice, AbstractBpod):
    """Class for interfacing with a Bpod Finite State Machine."""

    _settings: SettingsDict
    _read_thread: ReadThread | None = None
    _zmq_service: ServiceHost
    _next_fsm_index: int = -1
    _serial_buffer = bytearray()  # buffer for TrialReader thread
    _valid_state_machines: set[bytes]
    _state_machine_cache: OrderedDict[bytes, tuple[bytes, StateMachineLookup]]

    _softcode_thread: SoftcodeThread
    _softcode_handler: Callable[[int], None] | None = None

    serial1: ExtendedSerial | None = None
    """Secondary serial device for communication with the Bpod."""

    serial2: ExtendedSerial | None = None
    """Tertiary serial device for communication with the Bpod - used by Bpod 2+ only."""

    inputs: NamedTuple
    """Available input channels."""

    outputs: NamedTuple
    """Available output channels."""

    modules: '_ModuleDict'
    """Available modules, keyed by name."""

    @validate_call
    def __init__(
        self,
        port: str | None = None,
        serial_number: str | None = None,
        *,
        remote: bool = False,
    ) -> None:
        logger.info('bpod_core %s', bpod_core_version)
        self._settings = SettingsDict(CONFIG_PATH / 'settings.json')

        # initialize members
        self._input_events: _InputEvents = _InputEvents(
            names=[], channels=[], values=[]
        )
        self._actions: list[str] = []
        self._event_lookup: pl.DataFrame = pl.DataFrame()
        self._fsm_annotations: StateMachineLookup | None = None
        self._trial_data: SimpleQueue[pl.LazyFrame] = SimpleQueue()
        self._valid_state_machines = set()
        self._state_machine_cache = OrderedDict()

        self._n_softcodes = 0
        self._softcode_thread = SoftcodeThread(softcode_handler=self._softcode_handler)
        self._softcode_thread.start()

        # identify Bpod by port or serial number, open connection
        bpod_port, _ = self._identify_bpod(port, serial_number)
        super().__init__(port=bpod_port, open_connection=True)
        self._serial_number = self._port_info.serial_number or 'unknown'

        # record reference system time
        self._time_reference = TimeReferences(
            init_system_time_ns=time.time_ns(),
            init_perf_counter_ns=time.perf_counter_ns(),
            reset_system_time_ns=0,
        )
        self.reset_session_clock()

        # get firmware version and machine type; enforce version requirements
        self._get_version_info()

        # get the Bpod's onboard hardware configuration
        self._get_hardware_configuration()

        # configure input and output channels
        self._configure_io()

        # detect additional serial ports
        self._detect_additional_serial_ports()

        # update modules
        self.update_modules()

        # start ZeroMQ service
        self._start_zmq(use_zeroconf=remote)

        # register destructors
        self._bpod_finalizer = weakref.finalize(
            self,
            Bpod._bpod_cleanup,
            self._serial,
        )

        # log hardware information
        logger.info(
            'Connected to Bpod Finite State Machine %s on %s',
            self.version.machine_str,
            self.port,
        )
        logger.info(
            'Firmware Version %d.%d, Serial Number %s, PCB Revision %d',
            *self.version.firmware,
            self._serial_number,
            self.version.pcb,
        )

    @staticmethod
    def _bpod_cleanup(serial: ExtendedSerial) -> None:
        with contextlib.suppress(Exception):
            if serial.is_open:
                Bpod._request_disconnect(serial)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit context and close connection."""
        self._bpod_finalizer.detach()
        self.close()
        self._stop_zmq()

    def open(self) -> None:
        """
        Open the connection to the Bpod.

        Raises
        ------
        SerialException
            If the port could not be opened.
        BpodError
            If the handshake fails.
        """
        super().open()
        self._handshake()

    def close(self) -> None:
        """
        Close the connection to the Bpod.

        Waits for any running trial to finish before closing the serial port.

        Raises
        ------
        SerialException
            If the port could not be closed.
        """
        self.wait()
        self._softcode_thread.drain()
        if hasattr(self, 'serial0'):
            self._request_disconnect(self.serial0)
        super().close()

    @staticmethod
    def _request_disconnect(serial: ExtendedSerial) -> None:
        """
        Send a close request to the Bpod.

        This will:

        - disable all module relays
        - resume sending of the discovery byte
        - change the color of the status LED
        - disable the valve driver (if applicable)
        """
        if getattr(serial, 'is_open', False):
            logger.debug('Sending close request to Bpod Finite State Machine')
            serial.verify(b'Z')

    @property
    def input_event_names(self) -> list[str]:
        """Names of all hardware input events."""
        return self._input_events.names

    @property
    def serial0(self) -> ExtendedSerial:
        """Primary serial device for communication with the Bpod."""
        return self._serial

    def _request_handler(self, message: dict[str, Any]) -> dict[str, Any]:
        msg_type = message.get('type', 'unknown')
        if msg_type == 'call':
            method_name = message.get('method', '')
            args = message.get('args', ())
            kwargs = message.get('kwargs', {})
            try:
                method = getattr(self, method_name)
                result = method(*args, **kwargs)
                response = {'success': True, 'result': result}
            except Exception as e:
                response = {
                    'success': False,
                    'error': {
                        'type': type(e).__name__,
                        'message': str(e),
                        'traceback': traceback.format_exc(),
                    },
                }
        elif msg_type == 'handshake':
            response = {
                'version': self._version,
                'serial_number': self._serial_number,
                'name': self.name,
                'location': self.location,
            }
        else:
            response = {
                'success': False,
                'error': f'Unknown message type: {msg_type}',
            }
        return response

    def _start_zmq(self, *, use_zeroconf: bool) -> None:
        port_pub = self._get_setting(['devices', self._serial_number, 'port_pub'])
        port_rep = self._get_setting(['devices', self._serial_number, 'port_rep'])
        self._zmq_service = ServiceHost(
            service_name=self.name or f'bpod_{self._serial_number}',
            service_type='bpod',
            properties={
                'description': f'Bpod Finite State Machine {self.version.machine_str}',
                'serial': self._serial_number,
                'name': self.name or '',
                'location': self.location or '',
                'firmware': '.'.join([str(x) for x in self.version.firmware]),
                'core': bpod_core_version,
            },
            event_handler=self._request_handler,
            port_pub=cast('int | None', port_pub),
            port_rep=cast('int | None', port_rep),
            remote=use_zeroconf,
        )
        self._set_setting(
            ['devices', self._serial_number, 'port_pub'], self._zmq_service.pub_tcp_port
        )
        self._set_setting(
            ['devices', self._serial_number, 'port_rep'], self._zmq_service.rep_tcp_port
        )

    def _stop_zmq(self) -> None:
        if hasattr(self, '_zmq_service'):
            self._zmq_service.close()

    def _get_setting(self, keys: list[str], default: Any = None) -> Any:
        return self._settings.get_nested(keys, default)

    def _set_setting(self, keys: list[str], value: Any = None) -> None:
        self._settings.set_nested(keys, value)

    @staticmethod
    def _identify_bpod(
        port: str | None = None, serial_number: str | None = None
    ) -> tuple[str, str]:
        """
        Try to identify a supported Bpod based on port or serial number.

        If neither port nor serial number are provided, this function will attempt to
        detect a supported Bpod automatically.

        Parameters
        ----------
        port : str | None, optional
            The port of the device.
        serial_number : str, optional
            The serial number of the device.

        Returns
        -------
        str
            the port of the device
        str
            the serial number of the device

        Raises
        ------
        BpodError
            If no Bpod is found or the indicated device is not supported.
        """
        try:
            port_info = next(discover_bpod(port, serial_number))
            return cast('str', port_info.port), str(port_info.serial_number)
        except StopIteration as e:
            if port is not None:
                if len(find_ports(device=port)) == 0:
                    raise BpodError(f'Port not found: {port}') from None
                raise BpodError(f'Device on {port} is not an idle Bpod') from None
            msg = 'No idle Bpod found'
            if serial_number is not None:
                msg += f' matching serial number {serial_number}'
            raise BpodError(msg) from e

    def _get_version_info(self) -> None:
        """
        Retrieve firmware version and machine type information from the Bpod.

        This method queries the Bpod to obtain its firmware version, machine type, and
        PCB revision. It also validates that the hardware and firmware versions meet
        the minimum requirements. If the versions are not supported, an Exception is
        raised.

        Raises
        ------
        BpodError
            If the hardware version or firmware version is not supported.
        """
        logger.debug('Retrieving version information')
        v_major, machine_type = self.serial0.query_struct(b'F', '<2H')
        machine_type_str = MACHINE_TYPES.get(machine_type, 'unknown')
        v_minor = (
            self.serial0.query_struct(b'f', FMT_UINT16_LE)[0] if v_major > 22 else 0
        )
        v_firmware = (v_major, v_minor)
        if not MIN_BPOD_HW_VERSION <= machine_type <= MAX_BPOD_HW_VERSION:
            raise BpodError(
                f'The hardware version of the Bpod on {self.port} is not supported.',
            )
        if v_firmware < MIN_BPOD_FW_VERSION:
            raise BpodError(
                f'The Bpod on {self.port} uses firmware v{v_major}.{v_minor} '
                f'which is not supported. Please update the device to firmware '
                f'v{MIN_BPOD_FW_VERSION[0]}.{MIN_BPOD_FW_VERSION[1]} or later.',
            )
        pcv_rev = (
            self.serial0.query_struct(b'v', FMT_UINT8)[0] if v_major > 22 else None
        )
        self._version = VersionInfo(
            v_firmware, machine_type, machine_type_str, pcv_rev, bpod_core_version
        )

    def _get_hardware_configuration(self) -> None:
        """Retrieve the Bpod's onboard hardware configuration."""
        logger.debug('Retrieving onboard hardware configuration')

        # retrieve hardware configuration from Bpod
        if self.version.firmware > (22, 0):
            hardware_conf = list(self.serial0.query_struct(b'H', '<2H6B'))
        else:
            hardware_conf = list(self.serial0.query_struct(b'H', '<2H5B'))
            hardware_conf.insert(-4, 3)  # max bytes per serial msg always = 3
        hardware_conf.extend(self.serial0.read_struct(f'<{hardware_conf[-1]}s1B'))
        hardware_conf.extend(self.serial0.read_struct(f'<{hardware_conf[-1]}s'))

        # compute additional fields
        cycle_frequency = 1_000_000 // hardware_conf[1]  # cycle_period_us is at index 1
        n_modules = hardware_conf[-3].count(b'U')  # input_description is third to last
        hardware_conf.extend([cycle_frequency, n_modules])

        # create NamedTuple for hardware configuration
        self._hardware = HardwareConfiguration(*hardware_conf)

    def _configure_io(self) -> None:
        """Configure the input and output channels of the Bpod."""
        logger.debug('Configuring I/O')
        for description, channel_class, channel_names in (
            (self._hardware.input_description, Input, CHANNEL_TYPES_INPUT),
            (self._hardware.output_description, Output, CHANNEL_TYPES_OUTPUT),
        ):
            n_channels = len(description)
            io_class = f'{channel_class.__name__.lower()}s'
            channels = []
            types = []

            # loop over the description array and create channels
            for idx, io_key in enumerate(struct.unpack(f'<{n_channels}c', description)):
                if io_key not in channel_names:
                    raise RuntimeError(f'Unknown {io_class[:-1]} type: {io_key}')
                n = description[:idx].count(io_key) + 1
                name = f'{channel_names[io_key]}{n}'
                channels.append(channel_class(self, name, io_key, idx))
                types.append((name, channel_class))

            # store channels to NamedTuple and set the latter as a class attribute
            named_tuple = NamedTuple(io_class, types)._make(channels)
            setattr(self, io_class, named_tuple)

        # set the enabled state of the input channels
        self._set_enable_inputs()

    def _detect_additional_serial_ports(self) -> None:
        """Detect additional USB-serial ports."""
        logger.debug('Detecting additional USB-serial ports')

        # First, assemble a list of candidate ports
        candidate_ports = find_ports(
            vid=VIDS_BPOD,
            pid=[TeensyPID.DUAL_SERIAL, TeensyPID.TRIPLE_SERIAL],
            serial_number=self._serial_number,
            device=re.compile(rf'^(?!{re.escape(str(self.port))}$).*$'),
        )

        # Then, try to find the secondary USB-serial port
        if self._version.firmware >= (23, 0):
            for port in candidate_ports:
                if verify_serial_discovery(
                    port.device,
                    bytes([222]),
                    timeout=DISCOVERY_TIMEOUT,
                    trigger=lambda: self.serial0.write(b'{'),
                ):
                    self.serial1 = ExtendedSerial()
                    self.serial1.port = port.device
                    candidate_ports.remove(port)
                    logger.debug('Detected secondary USB-serial port: %s', port.device)
                    break
            if self.serial1 is None:
                raise BpodError('Could not detect secondary serial port')

        # State Machine 2+ uses a third USB-serial port for FlexIO
        if self.version.machine == 4:
            for port in candidate_ports:
                if verify_serial_discovery(
                    port.device,
                    bytes([223]),
                    timeout=DISCOVERY_TIMEOUT,
                    trigger=lambda: self.serial0.write(b'}'),
                ):
                    self.serial2 = ExtendedSerial()
                    self.serial2.port = port.device
                    logger.debug('Detected tertiary USB-serial port: %s', port.device)
                    break
            if self.serial2 is None:
                raise BpodError('Could not detect tertiary serial port')

    def _handshake(self) -> None:
        """
        Perform a handshake with the Bpod.

        Raises
        ------
        BpodError
            If the handshake fails.
        """
        try:
            self.serial0.timeout = 0.2
            if not self.serial0.verify(b'6', b'5'):
                raise BpodError(
                    f'Handshake with {self._serial_device_name} on {self.port} failed'
                )
            self.serial0.timeout = None
        except SerialException as e:
            raise BpodError(
                f'Handshake with {self._serial_device_name} on {self.port} failed'
            ) from e
        finally:
            self.serial0.reset_input_buffer()
        self._rename_serial_device('Bpod Finite State Machine')
        logger.debug(
            'Handshake with %s on %s successful', self._serial_device_name, self.port
        )

    def _test_psram(self) -> bool:
        """
        Test the Bpod's PSRAM.

        Returns
        -------
        bool
            True if the PSRAM test passed, False otherwise.
        """
        return self.serial0.verify(b'_')

    def _set_enable_inputs(self) -> bool:
        logger.debug('Updating enabled state of input channels')
        enable = [i.enabled for i in self.inputs]
        self.serial0.write_struct(f'<c{self._hardware.n_inputs}?', b'E', *enable)
        return self.serial0.read(1) == b'\x01'

    def reset_session_clock(self) -> bool:
        """Reset the Bpod session clock.

        Returns
        -------
        bool
            True if the Bpod acknowledged the command.

        Raises
        ------
        BpodError
            When the method is called while a state machine is running.
        """
        if self.is_running:
            raise BpodError(
                'Cannot reset session clock while a state machine is running.'
            )

        # reset the session clock
        # bracket the serial round-trip to estimate reset time within ±(t1-t0)/2
        logger.debug('Resetting session clock')
        t0 = time.perf_counter_ns()
        self.serial0.write(b'*')
        if not self.serial0.read_bool():
            return False
        t1 = time.perf_counter_ns()
        perf_count_ns = (t0 + t1) // 2

        # Store time reference
        start_system_time_ns = self._time_reference.init_system_time_ns
        start_perf_counter_ns = self._time_reference.init_perf_counter_ns
        reset_time_ns = start_system_time_ns + (perf_count_ns - start_perf_counter_ns)
        self._time_reference = TimeReferences(
            init_system_time_ns=start_system_time_ns,
            init_perf_counter_ns=start_perf_counter_ns,
            reset_system_time_ns=reset_time_ns,
        )
        self._softcode_thread.set_time_reference(self._time_reference)
        return True

    def _disable_all_module_relays(self) -> None:
        for module in self.modules.values():
            module.set_relay(False)

    def _compile_input_events(self) -> None:
        """Compile input events supported by the Bpod hardware."""
        hw = self._hardware
        n_serial_events = sum(len(m.event_names) for m in self.modules.values())
        n_softcodes = hw.max_serial_events - n_serial_events
        n_usb = hw.input_description.count(b'X')
        n_usb_ext = hw.input_description.count(b'Z')
        n_softcodes_per_usb = n_softcodes // (n_usb + n_usb_ext)
        n_app_softcodes = n_usb_ext * n_softcodes_per_usb
        event_names: list[str] = []
        event_channels: list[str | None] = []
        event_values: list[int | None] = []

        # physical input channel events
        counters = dict.fromkeys(CHANNEL_TYPES_INPUT, 0)
        for io_key in [bytes([x]) for x in hw.input_description]:
            channel_name = CHANNEL_TYPES_INPUT[io_key]
            if io_key == b'U':  # Serial
                module = list(self.modules.values())[counters[io_key]]
                ev_names = module.event_names
                ev_channels: list[str | None] = [module.name] * len(ev_names)
                ev_values: list[int | None] = [None] * len(ev_names)
            elif io_key == b'X':  # SoftCode
                ev_names = [f'{channel_name}{i}' for i in range(n_softcodes_per_usb)]
                ev_channels = [channel_name] * n_softcodes_per_usb
                ev_values = list(range(n_softcodes_per_usb))
            elif io_key == b'Z':  # SoftCodeApp
                ev_names = [f'{channel_name}{i}' for i in range(n_app_softcodes)]
                ev_channels = [channel_name] * n_app_softcodes
                ev_values = list(range(n_app_softcodes))
            elif io_key == b'F':  # Flex
                channel = f'{channel_name}{counters[io_key] + 1}'
                ev_names = [f'{channel}_{i}' for i in range(2)]
                ev_channels = [channel] * 2
                ev_values = [0, 1]
            elif io_key in b'PBW':  # Port, TTL, Wire
                channel = f'{channel_name}{counters[io_key] + 1}'
                ev_names = [f'{channel}_{s}' for s in ('High', 'Low')]
                ev_channels = [channel] * 2
                ev_values = [1, 0]
            else:
                logger.warning('Skipping unsupported input channel: %s', io_key)
                continue
            event_names.extend(ev_names)
            event_channels.extend(ev_channels)
            event_values.extend(ev_values)
            counters[io_key] += 1
        _n_events = len(event_names)
        range_input = range(_n_events)

        # global timer start events
        range_global_timer_starts = range(_n_events, _n_events + hw.n_global_timers)
        for i in range(hw.n_global_timers):
            event_names.append(f'{_CHANNEL_BASE_NAME_GLOBAL_TIMER}{i}_Start')
            event_channels.append(f'{_CHANNEL_BASE_NAME_GLOBAL_TIMER}{i}')
            event_values.append(1)
        _n_events += hw.n_global_timers

        # global timer end events
        range_global_timer_ends = range(_n_events, _n_events + hw.n_global_timers)
        for i in range(hw.n_global_timers):
            event_names.append(f'{_CHANNEL_BASE_NAME_GLOBAL_TIMER}{i}_End')
            event_channels.append(f'{_CHANNEL_BASE_NAME_GLOBAL_TIMER}{i}')
            event_values.append(0)
        _n_events += hw.n_global_timers

        # global counter end events
        range_global_counter_ends = range(_n_events, _n_events + hw.n_global_counters)
        for i in range(hw.n_global_counters):
            event_names.append(f'{_CHANNEL_BASE_NAME_GLOBAL_COUNTER}{i}_End')
            event_channels.append(None)
            event_values.append(None)
        _n_events += hw.n_global_counters

        # condition events
        range_conditions = range(_n_events, _n_events + hw.n_conditions)
        for i in range(hw.n_conditions):
            event_names.append(f'{_CHANNEL_BASE_NAME_CONDITION}{i}')
            event_channels.append(None)
            event_values.append(None)

        # state timer end event
        event_names.append('Tup')
        event_channels.append(None)
        event_values.append(None)

        self._n_softcodes = n_softcodes_per_usb
        self._input_events = _InputEvents(
            names=event_names, channels=event_channels, values=event_values
        )
        self._event_indices = {k: v for v, k in enumerate(event_names)}
        self._input_event_ranges = _InputEventRanges(
            input_channels=range_input,
            global_timer_starts=range_global_timer_starts,
            global_timer_ends=range_global_timer_ends,
            global_counter_ends=range_global_counter_ends,
            conditions=range_conditions,
        )

        modules = list(self.modules)
        physical_input_channels = [i.name for i in self.inputs if i.io_type != b'U']
        global_timers = [
            f'{_CHANNEL_BASE_NAME_GLOBAL_TIMER}{i}' for i in range(hw.n_global_timers)
        ]
        self._condition_channel_indices = {
            k: v
            for v, k in enumerate(modules + physical_input_channels + global_timers)
        }

    def _compile_output_actions(self) -> None:
        """Compile the list of output actions supported by the Bpod hardware."""
        self._actions = []

        # compile actions for output channels
        counters = dict.fromkeys(CHANNEL_TYPES_OUTPUT, 0)
        for io_key in [bytes([x]) for x in self._hardware.output_description]:
            if io_key == b'U':  # Serial
                name = list(self.modules)[counters[io_key]]
            elif io_key in b'XZ':  # SoftCode, SoftCodeApp
                name = CHANNEL_TYPES_OUTPUT[io_key]
            elif io_key in b'FVPBW':  # Flex, Valve, PWM, TTL, Wire
                name = f'{CHANNEL_TYPES_OUTPUT[io_key]}{counters[io_key] + 1}'
            else:
                continue
            self._actions.append(name)
            counters[io_key] += 1

        # add output actions for global timers, global counters and analog thresholds
        self._actions.extend(
            ['GlobalTimerTrig', 'GlobalTimerCancel', 'GlobalCounterReset'],
        )
        if self.version.machine == 4:
            self._actions.extend(['AnalogThreshEnable', 'AnalogThreshDisable'])
        self._action_indices = {k: v for v, k in enumerate(self._actions)}
        self._physical_output_channels = list(self.modules) + [
            o.name for o in self.outputs if o.io_type != b'U'
        ]
        self._timer_channel_indices = {
            k: v for v, k in enumerate(self._physical_output_channels)
        }
        self._timer_channel_indices[None] = 254

    @validate_call
    def set_status_led(self, enable: bool) -> bool:  # noqa: FBT001
        """Enable or disable the Bpod status LED.

        Parameters
        ----------
        enable : bool
            True to turn the LED on, False to turn it off.

        Returns
        -------
        bool
            True if the Bpod acknowledged the command.
        """
        self.serial0.write_struct('<c?', b':', enable)
        return self.serial0.verify(b'')

    def update_modules(self) -> None:
        """Update the list of connected modules and their configurations."""
        # self._disable_all_module_relays()
        self.serial0.write(b'M')
        modules = []
        for idx in range(self._hardware.n_modules):
            # check connection state
            if not (is_connected := self.serial0.read_bool()):
                module_name = f'{CHANNEL_TYPES_INPUT[b"U"]}{idx + 1}'
                modules.append(Module(_bpod=self, index=idx, name=module_name))
                continue

            # read further information if module is connected
            n_events = N_SERIAL_EVENTS_DEFAULT
            firmware_version, n_chars = self.serial0.read_struct('<IB')
            base_name, more_info = self.serial0.read_struct(f'<{n_chars}s?')
            base_name = base_name.decode('UTF8')
            custom_event_names = []
            while more_info:
                match self.serial0.read(1):
                    case b'#':
                        n_events = self.serial0.read_uint8()
                    case b'E':
                        n_event_names = self.serial0.read_uint8()
                        for _ in range(n_event_names):
                            n_chars = self.serial0.read_uint8()
                            event_name = self.serial0.read_struct(f'<{n_chars}s')[0]
                            custom_event_names.append(event_name.decode('UTF8'))
                more_info = self.serial0.read_bool()

            # create module name with trailing index
            matches = [re.match(rf'^{base_name}(\d$)', m.name) for m in modules]
            index = max([int(m.group(1)) for m in matches if m is not None] + [0])
            module_name = f'{base_name}{index + 1}'
            logger.debug('Detected %s on module port %d', module_name, idx + 1)

            # create instance of Module
            modules.append(
                Module(
                    _bpod=self,
                    index=idx,
                    name=module_name,
                    is_connected=is_connected,
                    firmware_version=firmware_version,
                    n_events=n_events,
                    _custom_event_names=custom_event_names,
                ),
            )

        self.modules = _ModuleDict(
            {m.name: m for m in modules},
            available_modules=[m.name for m in modules if m.is_connected],
        )

        # update event names and output actions
        self._compile_input_events()
        self._compile_output_actions()
        self._event_lookup = _build_event_lookup(self._input_events, self._actions)

        # invalidate caches
        self._state_machine_cache.clear()
        self._valid_state_machines.clear()

    def validate_state_machine(self, state_machine: StateMachine) -> bytes:
        """
        Validate the provided state machine for compatibility with the hardware.

        Parameters
        ----------
        state_machine : StateMachine
            The state machine to validate.

        Returns
        -------
        bytes
            The hash of the validated state machine.

        Raises
        ------
        ValueError
            If the state machine is invalid or not compatible with the hardware.
        """
        # get nanosecond count for benchmarking
        t0 = time.perf_counter_ns()

        # check the general validity of the state machine (independent of hardware)
        fsm_hash = state_machine.check()

        # skip validation if the state machine has been successfully validated before
        if fsm_hash in self._valid_state_machines:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    'Skipped validation of known state machine %s in %d μs',
                    fsm_hash.hex(),
                    (time.perf_counter_ns() - t0) // 1e3,
                )
            return fsm_hash

        # check if the '>back' operator is being used
        use_back_op = '>back' in state_machine.states.transition_targets

        # validate the number of states, global timers, global counters and conditions.
        n_states = len(state_machine.states)
        n_global_timers = max(state_machine.global_timers.keys(), default=-1) + 1
        n_global_counters = max(state_machine.global_counters.keys(), default=-1) + 1
        n_conditions = max(state_machine.conditions.keys(), default=-1) + 1
        for name, value, maximum_value in (
            ('states', n_states, self._hardware.max_states - 1 - use_back_op),
            ('global timers', n_global_timers, self._hardware.n_global_timers),
            ('global counters', n_global_counters, self._hardware.n_global_counters),
            ('conditions', n_conditions, self._hardware.n_conditions),
        ):
            if value > maximum_value:
                raise ValueError(
                    f'Too many {name} in state machine - hardware supports up to '
                    f'{maximum_value} {name}'
                )

        # validate states
        max_uint32 = np.iinfo(np.uint32).max
        max_time_uint32 = max_uint32 / self._hardware.cycle_frequency
        for state_name, state in state_machine.states.items():
            if state.timer > max_time_uint32:
                raise ValueError(
                    f"Invalid timer value {state.timer} for state '{state_name}' - "
                    f'must be between 0 and {max_time_uint32} seconds',
                )
            for condition_name, target in state.transitions.items():
                if target.startswith('>') and target not in VALID_OPERATORS:
                    raise ValueError(
                        f"Invalid operator '{target}' for transition condition "
                        f"'{condition_name}' in state '{state_name}'"
                        + suggest_similar(target, VALID_OPERATORS),
                    )
                if condition_name not in self.input_event_names:
                    raise ValueError(
                        f"Invalid transition condition '{condition_name}' in state "
                        f"'{state_name}'"
                        + suggest_similar(condition_name, self.input_event_names),
                    )
            actions = set(state.actions.keys())
            if invalid_actions := actions.difference(self._actions):
                invalid_action = invalid_actions.pop()
                raise ValueError(
                    f"Invalid action '{invalid_action}' in state '{state_name}'"
                    + suggest_similar(invalid_action, self._actions),
                )

        # validate global timers
        for timer_id, timer in state_machine.global_timers.items():
            if timer.channel not in (*self._physical_output_channels, None):
                raise ValueError(
                    f"Invalid channel '{timer.channel}' for global timer {timer_id}"
                    + suggest_similar(
                        timer.channel or '', self._physical_output_channels
                    ),
                )
            for key in ('duration', 'onset_delay', 'loop_interval'):
                if getattr(timer, key) > max_time_uint32:
                    name = key.replace('_', ' ')
                    raise ValueError(
                        f'Invalid {name} {getattr(timer, key)} for global timer '
                        f'{timer_id} - must be between 0 and {max_time_uint32} seconds'
                    )

        # TODO: validate global timer onset triggers
        # TODO: validate global counters
        # TODO: validate conditions
        # TODO: Check that sync channel is not used as state output

        # add the state machine to the list of valid state machines
        self._valid_state_machines.add(fsm_hash)

        # report benchmarking results
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                'Validated state machine %s in %d μs',
                fsm_hash.hex(),
                (time.perf_counter_ns() - t0) // 1e3,
            )

        # return the state machine hash
        return fsm_hash

    def send_softcode(self, softcode: int) -> None:
        """Send a softcode to the state machine.

        Can be used to trigger transitions.

        Parameters
        ----------
        softcode : int
            The softcode value to send.

        Raises
        ------
        ValueError
            If ``softcode`` is out of range.
        """
        if not (0 <= softcode < self._n_softcodes):
            raise ValueError(
                f'Softcode {softcode} is out of range - hardware supports softcodes '
                f'0..{self._n_softcodes}'
            )
        if not self.is_running:
            logger.warning(
                'No state machine is running - softcode %d will have no effect',
                softcode,
            )
        logger.debug('Sending softcode %d to Bpod', softcode)
        self._serial.write_struct('<cB', b'~', softcode)

    def _compile_state_machine(
        self,
        state_machine: StateMachine,
        state_machine_hash: bytes | None = None,
    ) -> tuple[bytes, StateMachineLookup]:
        """Compile a state machine into its binary wire format and annotation data.

        Builds the state transition matrix, encodes states, transitions, actions,
        global timers, global counters, and conditions into a bytearray compatible
        with the Bpod firmware, and returns annotation data for post-trial decoding
        by :class:`~bpod_core.bpod.threads.EventThread`.

        Parameters
        ----------
        state_machine : StateMachine
            The state machine to compile.
        state_machine_hash : bytes | None
            Pre-computed hash of the state machine, as returned by
            :meth:`~bpod_core.fsm.StateMachine.check`. If provided, skips
            recomputation. If ``None``, the hash is computed internally.

        Returns
        -------
        bytes
            Binary payload ready to be sent to the Bpod device.
        StateMachineLookup
            Annotation data for post-trial event stream decoding.
        """
        # get nanosecond count for benchmarking
        t0 = time.perf_counter_ns()

        if state_machine_hash is None:
            state_machine_hash = state_machine.hash
        if state_machine_hash in self._state_machine_cache:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    'Reusing cached compiled state machine %s in %d μs',
                    state_machine_hash.hex(),
                    (time.perf_counter_ns() - t0) // 1e3,
                )
            self._state_machine_cache.move_to_end(state_machine_hash)
            return self._state_machine_cache[state_machine_hash]

        # state machine
        states = list(state_machine.states.values())
        state_names = list(state_machine.states.keys())
        use_back_op = '>back' in state_machine.states.transition_targets
        n_states = len(states)

        # hardware
        version = self.version
        cycle_frequency = self._hardware.cycle_frequency

        # dense lists for global timers, counters, conditions
        n_global_timers = max(state_machine.global_timers.keys(), default=-1) + 1
        global_timers_list = [
            state_machine.global_timers.get(i) for i in range(n_global_timers)
        ]
        n_global_counters = max(state_machine.global_counters.keys(), default=-1) + 1
        global_counters_list = [
            state_machine.global_counters.get(i) for i in range(n_global_counters)
        ]
        n_conditions = max(state_machine.conditions.keys(), default=-1) + 1
        conditions_list = [state_machine.conditions.get(i) for i in range(n_conditions)]

        # index lookups
        target_indices = {k: v for v, k in enumerate([*state_names, '>exit'])}
        if use_back_op:
            target_indices['>back'] = 255
        event_indices = self._event_indices
        action_indices = self._action_indices
        condition_channel_indices = self._condition_channel_indices
        timer_channel_indices = self._timer_channel_indices

        # build the state transition matrix (n_states x 255 events)
        state_transition_matrix = np.arange(n_states, dtype=np.uint8)[
            :, np.newaxis
        ].repeat(255, axis=1)
        for state_idx, state in enumerate(states):
            for event, target in state.transitions.items():
                state_transition_matrix[state_idx][event_indices[event]] = (
                    target_indices[target]
                )

        # build annotation data structure for event decoding in EventThread
        annotations = StateMachineLookup(
            fsm_hash=state_machine_hash,
            state_names=state_names,
            state_transition_matrix=state_transition_matrix,
            state_actions=[dict(s.actions) for s in states],
            use_back_op=use_back_op,
            state_lookup=dict(enumerate(state_names)),
        )

        # Initialize bytearray for the compiled state machine.
        # This will be appended to in the following sections.
        #
        # The first 3 bytes are reserved for the state machine header and will be set
        # at the very end of the compilation
        fsm_bytes = bytearray(3)

        # Pre-build indexed transitions per state - for use in append_events closure
        state_transition_indices = [
            [(event_indices[e], target_indices[t]) for e, t in s.transitions.items()]
            for s in states
        ]

        def append_events(index_range: range) -> None:
            """Encode transitions for events in index_range into byte_array.

            For each state, this closure appends: [count] [event_idx, target_idx] ...
            where count is the number of transitions, event_idx is relative to event0,
            and target_idx is the target state index.

            Parameters
            ----------
            index_range : range
                Range of events to encode.
            """
            for transitions in state_transition_indices:
                # add counter byte
                counter_pos = len(fsm_bytes)
                fsm_bytes.append(0)
                # add transitions, increment counter
                for event_idx, target_state_idx in transitions:
                    if event_idx in index_range:
                        fsm_bytes[counter_pos] += 1
                        fsm_bytes.extend((event_idx - index_range[0], target_state_idx))

        # COUNTERS (4 bytes):
        fsm_bytes.extend((n_states, n_global_timers, n_global_counters, n_conditions))

        # STATE TIMER TARGET INDICES (n_states bytes):
        # Target state index for each state's 'Tup' event (defaults to self)
        fsm_bytes.extend(state_transition_matrix[:, event_indices['Tup']].tobytes())

        # INPUT EVENTS (variable length, per state):
        #   [count] [event_idx, target_idx] ...  for events on physical input channels
        append_events(self._input_event_ranges.input_channels)

        # ACTIONS (variable length, per state):
        #   [count] [action_idx, value] ...  (8-bit on Bpod 0.5-1, 16-bit on Bpod 2+)
        i1 = action_indices['GlobalTimerTrig']
        tmp_list: list[int] = []
        for state in states:
            counter_pos = len(tmp_list)
            tmp_list.append(0)
            for action_name, action_value in state.actions.items():
                if (key_idx := action_indices[action_name]) < i1:
                    tmp_list[counter_pos] += 1
                    tmp_list.extend(
                        (key_idx, action_value + ('SoftCode' in action_name))
                    )
        extend_packed(
            fsm_bytes, tmp_list, FMT_UINT16_LE if version.machine == 4 else FMT_UINT8
        )

        # REMAINING EVENTS
        #   [count] [event_idx, target_idx] ...  for each event
        append_events(self._input_event_ranges.global_timer_starts)
        append_events(self._input_event_ranges.global_timer_ends)
        append_events(self._input_event_ranges.global_counter_ends)
        append_events(self._input_event_ranges.conditions)

        # GLOBAL TIMER CHANNELS
        fsm_bytes.extend(
            timer_channel_indices[gt.channel if gt else None]
            for gt in global_timers_list
        )

        # GLOBAL TIMER ON & OFF VALUES
        # Bpod 2+ uses 16-bit values for value_on and value_off
        format_string = FMT_UINT16_LE if version.machine == 4 else FMT_UINT8
        for field_name in ('value_on', 'value_off'):
            extend_packed(
                fsm_bytes,
                [getattr(gt, field_name, 0) for gt in global_timers_list],
                format_string,
            )

        # GLOBAL TIMER LOOP & SEND_EVENTS
        for field_name, default in (('loop', 0), ('send_events', 1)):
            fsm_bytes.extend(
                getattr(gt, field_name, default) for gt in global_timers_list
            )

        # GLOBAL COUNTER EVENTS
        fsm_bytes.extend(
            event_indices[gc.event] if gc else 254 for gc in global_counters_list
        )

        # CONDITION CHANNELS & VALUES
        fsm_bytes.extend(
            condition_channel_indices[c.channel] if c else 0 for c in conditions_list
        )
        fsm_bytes.extend(c.value if c else 0 for c in conditions_list)

        # GLOBAL COUNTER RESETS
        if version.firmware < (23, 0):
            fsm_bytes.extend(
                s.actions.get('GlobalCounterReset', -1) + 1 for s in states
            )
        else:
            counter_idx = len(fsm_bytes)
            fsm_bytes.append(0)
            for state_idx, state in enumerate(states):
                if (value := state.actions.get('GlobalCounterReset', -1)) >= 0:
                    fsm_bytes[counter_idx] += 1
                    fsm_bytes.extend([state_idx, value + 1])

        # ANALOG THRESHOLDS
        # TODO: this is just a placeholder for now
        if version.machine == 4:
            fsm_bytes.extend([0, 0])

        # Timer trigger/cancel bitmasks need enough bits to address all timers.
        # Use the smallest integer type that fits n_global_timers bits.
        if self._hardware.n_global_timers > 16:
            format_string = FMT_UINT32_LE
        elif self._hardware.n_global_timers > 8:
            format_string = FMT_UINT16_LE
        else:
            format_string = FMT_UINT8

        # GLOBAL TIMER TRIGGERS AND CANCELS
        for key in ('GlobalTimerTrig', 'GlobalTimerCancel'):
            idx = [s.actions.get(key, -1) + 1 for s in states]
            extend_packed(fsm_bytes, idx, format_string)

        # GLOBAL TIMER ONSET TRIGGERS
        extend_packed(
            fsm_bytes,
            [getattr(gt, 'onset_trigger', 0) for gt in global_timers_list],
            format_string,
        )

        # STATE TIMERS (uInt32)
        state_timers = [round(s.timer * cycle_frequency) for s in states]
        extend_packed(fsm_bytes, state_timers, FMT_UINT32_LE)

        # GLOBAL TIMER DURATION, ONSET DELAY, LOOP INTERVAL (uInt32)
        for key in ('duration', 'onset_delay', 'loop_interval'):
            extend_packed(
                fsm_bytes,
                [
                    round(getattr(gt, key, 0) * cycle_frequency)
                    for gt in global_timers_list
                ],
                FMT_UINT32_LE,
            )

        # GLOBAL COUNTER THRESHOLDS (uInt32)
        extend_packed(
            fsm_bytes,
            [getattr(gc, 'threshold', 0) for gc in global_counters_list],
            FMT_UINT32_LE,
        )

        # FOOTER (firmware 23+, 1 byte):
        #   Reserved for additional opcodes
        if version.firmware > (22, 0):
            fsm_bytes.append(0)

        # HEADER
        struct.pack_into('<?H', fsm_bytes, 0, use_back_op, len(fsm_bytes) - 3)

        # store the compiled state machine for future use
        self._state_machine_cache[state_machine_hash] = (fsm_bytes, annotations)
        if len(self._state_machine_cache) > 1024:
            self._state_machine_cache.popitem(last=False)

        # report benchmarking results
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                'Compiled state machine %s in %d μs',
                state_machine_hash.hex(),
                (time.perf_counter_ns() - t0) // 1e3,
            )

        # Return the compiled state machine and annotations
        return fsm_bytes, annotations

    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    def send_state_machine(
        self,
        state_machine: StateMachine,
        *,
        run_asap: bool = False,
    ) -> None:
        """
        Send a state machine to the Bpod.

        This method compiles the provided state machine into a byte array format
        compatible with the Bpod and sends it to the device. It also validates the
        state machine for compatibility with the hardware before sending.

        Parameters
        ----------
        state_machine : StateMachine
            The state machine to be sent to the Bpod device.
        run_asap : bool, optional
            If True, the state machine will run immediately after the current one has
            finished. Default is False.

        Raises
        ------
        ValueError
            If the state machine is invalid or exceeds hardware limitations.
        :exc:`~validate_call.roar.validate_callCallHintViolation`
            If function arguments don't match type hints.
        """
        self._next_fsm_index += 1
        self._disable_all_module_relays()

        # validate state machine
        fsm_hash = self.validate_state_machine(state_machine)

        # compile state machine
        fsm_bytes, self._fsm_annotations = self._compile_state_machine(
            state_machine, fsm_hash
        )

        # Send state machine to Bpod
        t0 = perf_counter_ns()
        message = struct.pack('<c?', b'C', run_asap) + fsm_bytes
        self.serial0.write(message)
        if logger.isEnabledFor(logging.DEBUG):
            micros = (perf_counter_ns() - t0) // 1000
            logger.debug(
                'Sent state machine %s to Bpod in %d μs',
                fsm_hash.hex(),
                micros,
            )
        if run_asap:
            self._run_state_machine(blocking=False)

    @property
    def is_running(self) -> bool:
        """Check if the Bpod is currently running a state machine."""
        return self._read_thread is not None and self._read_thread.is_alive()

    @property
    def is_ready(self) -> bool:
        """Check if a compiled state machine is loaded and ready to run."""
        return self._fsm_annotations is not None

    @property
    def is_queued(self) -> bool:
        """Check if a state machine is queued to run after the current one."""
        return self.is_running and self.is_ready

    def wait(self) -> None:
        """
        Wait for the currently running state machine to finish.

        Blocks until the state machine thread completes. If no state machine is
        currently running, this method returns immediately.
        """
        if self._read_thread is not None and self._read_thread.is_alive():
            logger.debug(
                'Waiting for state machine #%d to finish ...',
                self._read_thread.trial_number,
            )
            self._read_thread.join()

    @overload
    def peek_data(self, *, lazy: Literal[False] = False) -> pl.DataFrame: ...

    @overload
    def peek_data(self, *, lazy: Literal[True]) -> pl.LazyFrame: ...

    def peek_data(self, *, lazy=False):
        """Return a snapshot of the current trial's data without blocking.

        Parameters
        ----------
        lazy : bool, optional
            If ``True``, return a :class:`polars.LazyFrame`.
            If ``False`` (default), return a :class:`polars.DataFrame`.

        Returns
        -------
        pl.DataFrame or pl.LazyFrame
            Events recorded so far in the current trial. Returns an empty DataFrame if
            no trial is running.
        """
        if self._event_thread is None:
            empty = pl.DataFrame(schema=_TRIAL_DATA_SCHEMA)
            return empty.lazy() if lazy else empty
        data = self._event_thread.peek_data()
        return data if lazy else data.collect()

    @overload
    def get_data(
        self, *, concat: bool = ..., rechunk: bool = ..., lazy: Literal[False] = False
    ) -> pl.DataFrame: ...

    @overload
    def get_data(
        self, *, concat: bool = ..., rechunk: bool = ..., lazy: Literal[True]
    ) -> pl.LazyFrame: ...

    def get_data(self, *, concat=False, rechunk=False, lazy=False):
        """Return trial data from the data queue.

        Parameters
        ----------
        concat : bool, optional
            If ``False`` (default), pop and return one DataFrame, blocking until one is
            available.
            If ``True``, pop and concatenate all DataFrames currently in the queue into
            a single DataFrame, blocking until at least one is available.
        rechunk : bool, optional
            If ``True``, make sure that the result data is in contiguous memory. Only
            applies when ``concat=True``. Default is ``False``.
        lazy : bool, optional
            If ``True``, return a :class:`polars.LazyFrame`.
            If ``False`` (default), return a :class:`polars.DataFrame`.

        Returns
        -------
        pl.DataFrame
            One trial's data, or all available trials concatenated when ``concat=True``.

            Columns:

            - ``time`` – absolute Bpod timestamp (``Datetime(time_unit='us')``)
            - ``trial`` – zero-based trial index (``UInt16``)
            - ``state`` – state name (``Categorical``)
            - ``type`` – event type (``Enum``)
            - ``event`` – input event name (``Categorical``)
            - ``channel`` – channel name (``Categorical``)
            - ``value`` – channel value (``UInt8``)

        Raises
        ------
        BpodError
            If the queue is empty and no state machine is currently running.
        """
        if self._trial_data.empty() and not self.is_running:
            raise BpodError('No trial data available')

        frames = [self._trial_data.get()]
        if concat:
            while True:
                try:
                    frames.append(self._trial_data.get(block=False))
                except Empty:  # noqa: PERF203
                    break
            data = pl.concat(frames, rechunk=rechunk)
        else:
            data = frames[0]

        # return data
        return data if lazy else data.collect()

    @validate_call
    def run_state_machine(self, *, blocking: bool = True) -> None:
        """Run the previously sent state machine.

        Parameters
        ----------
        blocking : bool, optional
            If True (default), block until the state machine finishes.
            If False, return immediately after starting.

        Raises
        ------
        RuntimeError
            - If no state machine has been sent.
            - If a state machine is already running.
        """
        if self.is_running:
            raise RuntimeError('A state machine is already running')
        if not self.is_ready:
            raise RuntimeError('No state machine has been sent')
        self.serial0.write(b'R')
        self._run_state_machine(blocking=blocking)

    def _run_state_machine(self, *, blocking: bool) -> None:
        state_machine_lookup = cast('StateMachineLookup', self._fsm_annotations)

        # initialize new threads
        event_thread = EventThread(
            trial=self._next_fsm_index,
            fsm=state_machine_lookup,
            data_queue=self._trial_data,
            event_lookup=self._event_lookup,
            action_names=self._actions,
            time_reference=self._time_reference,
        )
        read_thread = ReadThread(
            serial=self.serial0,
            state_machine_hash=state_machine_lookup.fsm_hash,
            trial=self._next_fsm_index,
            cycle_period_us=self._hardware.cycle_period_us,
            queue_events=event_thread.queue,
            queue_softcodes=self._softcode_thread.queue,
        )

        # wait for an already running state machine to finish
        self.wait()

        # start threads
        read_thread.start()
        event_thread.start()

        # set private class attributes
        self._event_thread = event_thread
        self._read_thread = read_thread
        self._fsm_annotations = None

        # wait for state machine to finish
        if blocking and self.is_running:
            self.wait()

    def stop_state_machine(self) -> None:
        """Stop the currently running state machine."""
        if not self.is_running:
            return
        logger.debug('Stopping state machine')
        self.serial0.write(b'X')
        if self._read_thread is not None:
            self._read_thread.join()

    @property
    def name(self) -> str | None:
        """Get the name of the Bpod device."""
        return cast(
            'str | None',
            self._get_setting(['devices', self._serial_number, 'name'], None),
        )

    @name.setter
    def name(self, name: str | None) -> None:
        """Set the name of the Bpod device."""
        self._set_setting(['devices', self._serial_number, 'name'], name)

    @property
    def location(self) -> str | None:
        """Get the location of the Bpod device."""
        return cast(
            'str | None',
            self._get_setting(['devices', self._serial_number, 'location'], None),
        )

    @location.setter
    def location(self, location: str | None) -> None:
        """Set the location of the Bpod device."""
        self._set_setting(['devices', self._serial_number, 'location'], location)

    @validate_call
    def set_softcode_handler(
        self, softcode_handler: Callable[[int], None] | None = None
    ) -> None:
        """Set the handler function for softcodes sent from the Bpod.

        Parameters
        ----------
        softcode_handler : Callable, optional
            The function to call when a softcode is received.
        """
        self._softcode_handler = softcode_handler
        self._softcode_thread.set_handler(softcode_handler)


class Channel:
    """Base class representing a channel on the Bpod device."""

    def __init__(self, bpod: Bpod, name: str, io_key: bytes, index: int) -> None:
        """
        Initialize a channel on the Bpod device.

        Parameters
        ----------
        bpod : Bpod
            The Bpod instance associated with the channel.
        name : str
            The name of the channel.
        io_key : bytes
            The I/O type of the channel (e.g., b'B', b'V', b'P').
        index : int
            The index of the channel.
        """
        self.name = name
        self.io_type = io_key
        self.index = index
        self._serial0 = bpod.serial0

    def __repr__(self) -> str:
        return self.__class__.__name__ + '()'


class Input(Channel):
    """Input channel class representing a digital input channel."""

    def __init__(self, bpod: Bpod, name: str, io_key: bytes, index: int) -> None:
        super().__init__(bpod, name, io_key, index)
        self._set_enable_inputs = bpod._set_enable_inputs  # noqa: SLF001
        self._enabled = io_key in (b'PBWF')  # Enable Port, TTL, Wire and FlexIO inputs

    def read(self) -> bool:
        """
        Read the state of the input channel.

        Returns
        -------
        bool
            True if the input channel is active, False otherwise.
        """
        return self._serial0.verify(struct.pack('<cB', b'I', self.index))

    def override(self, state: bool) -> None:  # noqa: FBT001
        """
        Override the state of the input channel.

        Parameters
        ----------
        state : bool
            The state to set for the input channel.
        """
        self._serial0.write_struct('<cB', b'V', state)

    def enable(self, enable: bool) -> bool:  # noqa: FBT001
        """
        Enable or disable the input channel.

        Parameters
        ----------
        enable : bool
            True to enable the input channel, False to disable.

        Returns
        -------
        bool
            True if the operation was successful, False otherwise.
        """
        if self.io_type not in b'FDBWVP':
            logger.warning(
                '%sabling input `%s` has no effect',
                'En' if enable else 'Dis',
                self.name,
            )
        self._enabled = enable
        return self._set_enable_inputs()

    @property
    def enabled(self) -> bool:
        """
        Check if the input channel is enabled.

        Returns
        -------
        bool
            True if the input channel is enabled, False otherwise.
        """
        return self._enabled

    @enabled.setter
    def enabled(self, enabled: bool) -> None:
        """
        Enable or disable the input channel.

        Parameters
        ----------
        enabled : bool
            True to enable the input channel, False to disable.
        """
        self.enable(enabled)


class Output(Channel):
    """Output channel class representing a digital output channel."""

    def override(self, state: bool | int) -> None:  # noqa: FBT001
        """
        Override the state of the output channel.

        Parameters
        ----------
        state : bool or int
            The state to set for the output channel. For binary I/O types, provide a
            bool. For pulse width modulation (PWM) I/O types, provide an int (0-255).
        """
        if isinstance(state, int) and self.io_type in (b'D', b'B', b'W'):
            state = state > 0
        self._serial0.write_struct('<c2B', b'O', self.index, state)


class _ModuleDict(dict[str, 'Module']):
    """A dict of :class:`Module` objects keyed by name."""

    def __init__(
        self, dictionary: dict[str, 'Module'], *, available_modules: list[str]
    ) -> None:
        super().__init__(dictionary)
        self._available_modules = [f"'{x}'" for x in available_modules]

    def __getitem__(self, key: str) -> 'Module':
        try:
            return super().__getitem__(key)
        except KeyError as e:
            if self._available_modules:
                hint = f'connected modules: {", ".join(self._available_modules)}'
            else:
                hint = 'no modules connected to Bpod'
            raise BpodError(f"No such module: '{key}'; {hint}") from e


@dataclass
class Module:
    """Represents a Bpod module with its configuration and event names."""

    _bpod: Bpod
    """A reference to the Bpod."""

    index: int
    """The index of the module."""

    name: str
    """The name of the module."""

    is_connected: bool = False
    """Whether the module is connected."""

    firmware_version: int | None = None
    """The firmware version of the module."""

    n_events: int = N_SERIAL_EVENTS_DEFAULT
    """The number of events assigned to the module."""

    _custom_event_names: list[str] = field(default_factory=list)
    """A list of custom event names."""

    _relay_is_enabled = False
    """Whether relay for the module is enabled."""

    def __repr__(self) -> str:
        if not self.is_connected:
            return 'unused module port'
        if self.firmware_version:
            return f'{self.name} on module port {self.index + 1})'
        return self.name

    def __post_init__(self) -> None:
        self._define_event_names()

    def _define_event_names(self) -> None:
        """Define the module's event names."""
        self.event_names = []
        for idx in range(self.n_events):
            if len(self._custom_event_names) > idx:
                self.event_names.append(f'{self.name}_{self._custom_event_names[idx]}')
            else:
                self.event_names.append(f'{self.name}_{idx}')

    @validate_call
    def set_relay(self, enabled: bool) -> None:  # noqa: FBT001
        """
        Enable or disable the serial relay for the module.

        Parameters
        ----------
        enabled : bool
            True to enable the relay, False to disable it.
        """
        if enabled == self._relay_is_enabled:
            return
        if enabled:
            for module in self._bpod.modules.values():
                module.set_relay(False)
        logger.info(
            '%sabling relay for module %s', {'En' if enabled else 'Dis'}, self.name
        )
        self._bpod.serial0.write_struct('<cB?', b'J', self.index, enabled)
        self._relay_is_enabled = enabled

    @property
    def relay(self) -> bool:
        """The current state of the serial relay."""
        return self._relay_is_enabled

    @validate_call
    def load_serial_message(
        self,
        message_id: int,
        message_bytes: bytes,
    ) -> bool:
        """
        Load a serial message targeting the module.

        Serial messages are byte sequences targeting a specific module that can be
        triggered as output actions during a state machine run. Each message is
        identified by a ``message_id``.

        Parameters
        ----------
        message_id : int
            Identifier for the message, in the range ``[0, 254]``.
        message_bytes : bytes
            The message payload (1 to 3 bytes).

        Returns
        -------
        bool
            :obj:`True` if the Bpod acknowledged the message, :obj:`False` otherwise.

        Raises
        ------
        ValidationError
            If the provided parameters cannot be validated or coerced to the expected
            type.
        ValueError
            If ``message_id``, or ``message_bytes`` length is out of range.
        """
        if not (0 <= message_id <= 254):
            raise ValueError('Message ID must be between 0 and 254')
        if not (1 <= (message_length := len(message_bytes)) <= 3):
            raise ValueError('Message must be between 1 and 3 bytes long')

        self._bpod.serial0.write_struct(
            f'<c4B{message_length}s',
            b'L',
            self.index,
            1,  # number of messages loaded - always 1 for now
            message_id,
            message_length,
            message_bytes,
        )
        return self._bpod.serial0.verify()


class RemoteBpod(AbstractBpod):
    """Class representing a Bpod connected via zeroMQ."""

    _name: str | None = None
    _location: str | None = None

    def __init__(
        self,
        address: str | None = None,
        name: str | None = None,
        serial_number: str | None = None,
        location: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        properties = {
            'address': address,
            'name': name,
            'serial': serial_number,
            'location': location,
        }
        properties = {k: v for k, v in properties.items() if v is not None}

        try:
            self._zmq = ServiceClient(
                service_type='bpod',
                address=address,
                event_handler=self._event_handler,
                discovery_timeout=timeout,
                txt_properties=properties,
                default_data_type=dict,
            )
        except TimeoutError as e:
            raise TimeoutError('Failed to discover remote Bpod.') from e
        self._handshake()

        # log hardware information
        logger.info(
            'Connected to Bpod Finite State Machine %s on %s',
            self._version.machine_str,
            self._zmq.address_req,
        )

    def _request(self, request_type: str, **kwargs: Any) -> dict:
        return cast('dict', self._zmq.request({'type': request_type, **kwargs}))

    def _remote_call(self, method: str, *args: Any, **kwargs: Any) -> Any | None:
        """
        Perform a remote procedure call.

        Parameters
        ----------
        method : str
            The name of the remote method to invoke.
        *args : Any
            Positional arguments to pass to the remote method.
        **kwargs : Any
            Keyword arguments to pass to the remote method.

        Returns
        -------
        Any or None
            The result returned from the remote method.
        """
        reply = self._request('call', method=method, args=args, kwargs=kwargs)
        if reply.get('success'):
            return reply['result']
        logger.error(
            'Remote %s: %s, ', reply['error']['type'], reply['error']['message']
        )
        return None

    def _handshake(self) -> None:
        reply = self._request('handshake')
        self._version = VersionInfo(**reply['version'])
        self._serial_number = reply['serial_number']
        self._name = reply['name']
        self._location = reply['location']

    def _event_handler(self, message: dict) -> None:
        pass

    @property
    def name(self) -> str | None:  # noqa: D102
        return self._name

    @property
    def location(self) -> str | None:  # noqa: D102
        return self._location

    def set_status_led(self, enable: bool) -> bool:  # noqa: D102, FBT001
        return self._remote_call('set_status_led', enable) or False


def discover_bpod(
    port: str | None = None, serial_number: str | None = None
) -> Iterator[BpodInfo]:
    """Identify available Bpod devices connected via USB.

    Scans for USB serial ports matching Bpod vendor/product IDs and verifies each
    device responds to a discovery message. Yields information about identified devices.

    Parameters
    ----------
    port : str, optional
        Filter by specific device path (e.g., '/dev/ttyACM0' or 'COM3').
    serial_number : str, optional
        Filter by USB serial number.

    Yields
    ------
    BpodInfo
        Information structure describing a Bpod device.

    Examples
    --------
    Iterate over available Bpods::

        for device in discover_bpod():
            print(f"Found Bpod at {device}")

    Get as a list::

        devices = list(discover_bpod())
    """
    # create filter dict
    filters: dict[str, str] = {}
    if port is not None:
        filters['device'] = port
    if serial_number is not None:
        filters['serial_number'] = serial_number

    # find matching devices
    for p in find_ports(vid=VIDS_BPOD, pid=PIDS_BPOD, **filters):
        if verify_serial_discovery(
            port=p.device, expected_message=b'\xde', timeout=DISCOVERY_TIMEOUT
        ):
            yield BpodInfo(port=p.device, serial_number=str(p.serial_number))


def discover_remote_bpod(
    name: str | None = None,
    serial_number: str | None = None,
    location: str | None = None,
    timeout: float | None = 10.0,
    poll_interval: float = 1,
    *,
    local: bool = True,
    remote: bool = True,
) -> Iterator[ServiceEvent]:
    """
    Identify available Bpod devices connected via ZeroMQ.

    Parameters
    ----------
    name : str, optional
        Name of the Bpod device.
    serial_number : str, optional
        Serial number of the Bpod device.
    location : str, optional
        Location of the Bpod device.
    local : bool, optional
        Whether to search for services on the local machine, by default True.
    remote : bool, optional
        Whether to also search for services on the network, by default True.
    timeout : float or None, optional
        How many seconds to monitor, by default 10.
        Pass ``None`` to monitor indefinitely until the iterator is closed.
    poll_interval : float, optional
        How often to poll for local service changes, in seconds. Default is 1.

    Yields
    ------
    ServiceEvent
        A named tuple with the following fields:

        - kind: str, either 'added' or 'removed'
        - address: str, the service address, e.g., 'tcp://192.168.1.10:1234'
        - properties: dict, the service properties, e.g., {'name': 'MyDevice'}
    """
    properties = {
        'name': name,
        'serial': serial_number,
        'location': location,
    }
    properties = {k: v for k, v in properties.items() if v is not None}
    yield from iter_services(
        service_type='bpod',
        properties=properties,
        timeout=timeout,
        poll_interval=poll_interval,
        local=local,
        remote=remote,
    )
