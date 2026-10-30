"""Module for interfacing with the Bpod Finite State Machine."""

import contextlib
import functools
import logging
import os
import re
import struct
import weakref
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from queue import Empty, SimpleQueue
from time import perf_counter_ns, time_ns
from types import TracebackType
from typing import (
    Any,
    Concatenate,
    Literal,
    ParamSpec,
    TypeVar,
    cast,
    overload,
)
from uuid import uuid5

import polars as pl
from pydantic import ConfigDict, validate_call
from serial import SerialException
from serial.threaded import ReaderThread
from serial.tools.list_ports_common import ListPortInfo
from typing_extensions import override

from bpod_core import __version__ as bpod_core_version
from bpod_core.bpod._compiler import StateMachineCompiler, _name_channels
from bpod_core.bpod._threads import (
    _TRIAL_DATA_SCHEMA,
    EventThread,
    ReadThread,
    SoftcodeThread,
)
from bpod_core.bpod.abc import AbstractBpod
from bpod_core.bpod.constants import (
    _REMOTE_CALL_METHODS,
    _REMOTE_DATA_METHODS,
    BPOD_UUID_NAMESPACE,
    CHANNEL_TYPES_INPUT,
    CHANNEL_TYPES_OUTPUT,
    CONFIG_PATH,
    DISCOVERY_BYTE,
    DISCOVERY_TIMEOUT,
    MACHINE_TYPES,
    MAX_BPOD_HW_VERSION,
    MIN_BPOD_FW_VERSION,
    MIN_BPOD_HW_VERSION,
    N_SERIAL_EVENTS_DEFAULT,
    PIDS_BPOD,
    VIDS_BPOD,
    FlexIOChannelType,
)
from bpod_core.bpod.errors import BpodError, BpodKeyError
from bpod_core.bpod.flexio import (
    FlexIO,
    _counts_to_mv,
)
from bpod_core.bpod.structs import (
    BpodEventUnion,
    BpodInfo,
    EventTrialStart,
    VersionInfo,
    _BpodReplyUnion,
    _BpodRequestUnion,
    _HardwareConfiguration,
    _ReplyGeneric,
    _ReplyWelcome,
    _RequestBye,
    _RequestCall,
    _RequestData,
    _RequestHello,
    _StateMachineLookup,
    _TimeReferences,
)
from bpod_core.com import (
    ChunkedSerialReader,
    ExtendedSerial,
    SerialDevice,
    _filter_ports,
    find_ports,
    verify_serial_discovery,
)
from bpod_core.constants import (
    FMT_UINT8,
    FMT_UINT16_LE,
    UINT32_MAX,
    TeensyPID,
)
from bpod_core.fsm import StateMachine
from bpod_core.ipc import (
    RemoteError,
    ServiceClient,
    ServiceEvent,
    ServiceHost,
    iter_services,
)
from bpod_core.misc import (
    SettingsDict,
    SuggestionMapping,
    _HandlerSlot,
    suppress_logging,
)

logger = logging.getLogger(__name__)

_ANALOG_FLUSH_INTERVAL_NS = 100_000_000  # 100 ms
# a session clock reset is aligned to the host clock by the midpoint of its round trip,
# so its timestamps are off by up to half the round trip; a slow reset is repeated
_SESSION_CLOCK_RESET_ATTEMPTS = 3
_SESSION_CLOCK_MAX_ERROR_NS = 5_000_000  # 5 ms
# while idle, the Bpod answers 'X' with a trial-end report: opcode 1, one event, event
# code 255 (end of trial), then 2 x uInt32 cycle counts and a uInt64 timestamp
_IDLE_STOP_REPLY_HEADER = b'\x01\x01\xff'
_IDLE_STOP_REPLY_SIZE = len(_IDLE_STOP_REPLY_HEADER) + 16


_S = TypeVar('_S')
_P = ParamSpec('_P')
_R = TypeVar('_R')


def _requires_idle(
    method: Callable[Concatenate[_S, _P], _R],
) -> Callable[Concatenate[_S, _P], _R]:
    """Refuse calls to `method` while a state machine is running.

    While a trial runs, its read thread owns the Bpod's serial port: a command that
    expects a reply would steal the trial's event bytes or corrupt them with its reply,
    and reconfiguring the hardware mid-trial is unsafe. Applies to methods of
    :class:`Bpod` and of objects that refer to one as ``_bpod`` (channels, modules).
    Decorated methods are marked with ``requires_idle = True``.
    """

    @functools.wraps(method)
    def wrapper(self: _S, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        if cast('Bpod', getattr(self, '_bpod', self)).is_running:
            raise BpodError(
                f'Cannot use {method.__qualname__} while a state machine is running.'
            )
        return method(self, *args, **kwargs)

    wrapper.requires_idle = True  # type: ignore[attr-defined]
    return wrapper


class Bpod(SerialDevice, AbstractBpod):
    """Interface to a Bpod Finite State Machine.

    Connects to the Bpod hardware over USB. If neither `port` nor `serial_number` is
    given, the first idle Bpod found on any USB port is used.

    Parameters
    ----------
    port : str, optional
        USB serial port of the device (e.g., '/dev/ttyACM0' or 'COM3'). Mutually
        exclusive with `serial_number`.
    serial_number : str, optional
        Serial number of the device to connect to. Mutually exclusive with `port`.
    remote : bool, default: False
        Advertise the ZeroMQ service via Zeroconf so that other processes can
        connect to this Bpod instance remotely.

    Raises
    ------
    BpodError
        If no idle Bpod is found, the indicated port does not exist, or the device
        is not a supported Bpod model.

    Examples
    --------
    Connect to a Bpod on ``COM3``::

        with Bpod(port='COM3') as bpod:
            # do things
    """

    _settings: SettingsDict
    _read_thread: ReadThread | None = None
    _event_thread: EventThread | None = None
    _starting_state_machine: bool = False
    _state_machine_started: bool = False  # fixes FlexIO channel types and rate
    _zmq: ServiceHost[_BpodRequestUnion, list[BpodEventUnion]]
    _next_fsm_index: int = -1
    _serial_buffer = bytearray()  # buffer for TrialReader thread

    _compiler: StateMachineCompiler  # rebuilt by _recompile_hardware_tables
    # the last compiled state machine, and the hardware hash it was compiled for
    _last_compiled: tuple[bytes, _StateMachineLookup, bytes] | None = None

    _softcode_thread: SoftcodeThread
    _softcode_slot: _HandlerSlot[Callable[[int], None] | None]

    serial1: ExtendedSerial | None = None
    """Secondary serial device for communication with the Bpod."""

    # FlexIO analog-input reader state; see _start_analog_reader/_on_analog_chunk
    _flexio_serial: ExtendedSerial | None = None
    _analog_reader_thread: ReaderThread | None = None
    _analog_recording: bool = False  # the Bpod is sampling the analog inputs
    _analog_channel_names: tuple[str, ...] = ()
    _analog_struct: struct.Struct | None = None
    _analog_sample_period_us: int = 0
    _analog_data: SimpleQueue[pl.LazyFrame]
    _analog_buffer: list[tuple[int, ...]]
    _analog_first_trial_time_us: int | None = None
    _analog_trial_number_by_sequence: list[int]
    _analog_sample_index: int = 0
    _analog_last_flush_ns: int = 0

    inputs: Mapping[str, 'Input']
    """Read-only mapping of available input channels, keyed by name.

    FlexIO channels are accessed through :attr:`flex_io` instead."""

    outputs: Mapping[str, 'Output']
    """Read-only mapping of available output channels, keyed by name.

    FlexIO channels are accessed through :attr:`flex_io` instead."""

    modules: Mapping[str, 'Module']
    """Dictionary of available modules, keyed by name."""

    @validate_call()
    def __init__(
        self,
        port: str | None = None,
        serial_number: str | None = None,
        *,
        remote: bool = False,
    ) -> None:
        logger.info('bpod-core %s', bpod_core_version)
        self._settings = SettingsDict(CONFIG_PATH / 'settings.json')

        # environmental overrides
        if 'BPOD_OVERRIDE_PORT' in os.environ:
            port = os.getenv('BPOD_OVERRIDE_PORT')
        if 'BPOD_OVERRIDE_SERIAL_NUMBER' in os.environ:
            serial_number = os.getenv('BPOD_OVERRIDE_SERIAL_NUMBER')
        if 'BPOD_OVERRIDE_REMOTE' in os.environ:
            remote = os.getenv('BPOD_OVERRIDE_REMOTE') in ('True', 'true', '1')

        # initialize members
        self._fsm_annotations: _StateMachineLookup | None = None
        self._trial_data: SimpleQueue[pl.LazyFrame] = SimpleQueue()
        self._analog_data: SimpleQueue[pl.LazyFrame] = SimpleQueue()
        self._analog_buffer = []
        self._analog_trial_number_by_sequence = []
        self._analog_sample_index = 0
        self._analog_last_flush_ns = perf_counter_ns()

        self._softcode_slot = _HandlerSlot(None)
        self._softcode_thread = SoftcodeThread(handler_slot=self._softcode_slot)
        self._softcode_thread.start()

        try:
            # enumerating serial ports is slow, so it is done once: for identifying the
            # Bpod by port or serial number and for detecting its additional ports
            bpod_ports = find_ports(vid=VIDS_BPOD, pid=PIDS_BPOD)
            port_info = self._identify_bpod(bpod_ports, port, serial_number)
            super().__init__(port=port_info, open_connection=True)
            self._serial_number = port_info.serial_number or 'unknown'

            # record reference system time
            self._time_reference = _TimeReferences(
                init_system_time_ns=time_ns(),
                init_perf_counter_ns=perf_counter_ns(),
                reset_system_time_ns=0,
            )

            # get firmware version and machine type; enforce version requirements
            self._get_version_info()

            # get the Bpod's onboard hardware configuration
            self._get_hardware_configuration()

            # detect additional serial ports
            self.serial1, self._flexio_serial = self._detect_additional_serial_ports(
                bpod_ports
            )

            # configure input and output channels
            self._configure_io()
            self._configure_flex_io()

            # update modules
            self.update_modules()

            # sync the FlexIO subsystem to its default settings; must run after
            # _configure_io()/update_modules() since it recompiles the hardware
            # tables, which depend on self.inputs/outputs/modules being set
            if self._flex_io is not None:
                self._flex_io.reset()

            # reset the session clock after all other setup traffic, so that nothing
            # delays its reply: the clocks are aligned by the reply's round trip
            self.reset_session_clock()

            # log hardware information
            logger.info(
                'Connected to Bpod Finite State Machine %s on %s',
                self.version.machine_str,
                self.port,
            )
            if self.serial1 is not None:
                logger.info(
                    'Secondary serial port available for connections on %s',
                    self.serial1.portstr,
                )
            logger.info(
                'Firmware Version %d.%d, Serial Number %s, PCB Revision %d',
                *self.version.firmware,
                self._serial_number,
                self.version.pcb,
            )

            # start ZeroMQ service
            self._start_zmq(use_zeroconf=remote)
            logger.info('ZeroMQ service started on %s', self.address)

            # when launched by a supervising process (e.g. QBpod), briefly wait for
            # its subscription to register so the first trial's messages aren't lost
            # to the PUB/SUB slow-joiner race
            if 'BPOD_OVERRIDE_REMOTE' in os.environ:
                if self._zmq.wait_for_subscribers(timeout=1.0):
                    logger.debug('Subscriber registered on the PUB/SUB channel')
                else:
                    logger.debug('No subscriber registered within timeout')
        except BaseException:
            # release what was acquired so far; report the original error
            with contextlib.suppress(Exception):
                self._shutdown()
            raise

        # register destructors
        self._bpod_finalizer = weakref.finalize(
            self,
            Bpod._bpod_cleanup,
            self._serial,
            self._softcode_thread,
        )

    @staticmethod
    def _bpod_cleanup(serial: ExtendedSerial, softcode_thread: SoftcodeThread) -> None:
        softcode_thread.stop()
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
        self._shutdown()

    def _shutdown(self) -> None:
        """Close the connection, then end the IPC service and softcode thread."""
        try:
            self.close()
        finally:
            self._stop_zmq()
            self._softcode_thread.stop()

    @_requires_idle
    def open(self) -> None:
        """
        Open the connection to the Bpod and perform a handshake.

        .. note::
            Prefer using :class:`Bpod` as a context manager, which opens and closes the
            connection automatically.

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

        .. note::
            Prefer using :class:`Bpod` as a context manager, which opens and closes the
            connection automatically.

        Raises
        ------
        SerialException
            If the port could not be closed.
        """
        self.wait()
        serial0_open = hasattr(self, '_serial') and getattr(
            self._serial, 'is_open', False
        )
        if serial0_open and self._analog_reader_thread is not None:
            # firmware keeps streaming FlexIO analog samples on its own serial port
            # until explicitly told to stop - even after a trial ends naturally via
            # '>exit' - so the next connection's discovery handshake would race stray
            # bytes. Read the Bpod's reply, so it isn't taken for stray bytes below.
            with contextlib.suppress(SerialException):  # the Bpod may be unplugged
                self._serial.write(b'X')
                with self._serial.temporary_timeout(0.2):
                    reply = self._serial.read(_IDLE_STOP_REPLY_SIZE)
                if not reply.startswith(_IDLE_STOP_REPLY_HEADER):
                    logger.warning('Unexpected reply to stop command: %s', reply.hex())
        self._softcode_thread.drain()
        if self._analog_reader_thread is not None:
            self._analog_reader_thread.stop()  # thread stopped, serial still open
            self._analog_reader_thread = None
            self._analog_recording = False
            self._flush_analog_buffer()  # samples since the last periodic flush
        if self._flexio_serial is not None and self._flexio_serial.is_open:
            # discard any analog samples firmware sent (or was still sending) up to
            # the 'X' above, so a stale byte can't be misread as the next
            # connection's discovery-handshake response
            with contextlib.suppress(SerialException):  # the Bpod may be unplugged
                self._discard_stray_bytes(self._flexio_serial, 'the FlexIO serial port')
            self._flexio_serial.close()
        if hasattr(self, 'serial0'):
            # the Bpod may have been unplugged
            with contextlib.suppress(SerialException):
                self._request_disconnect(self.serial0)
        if serial0_open:
            # discard anything unread at this point - we're about to abandon the
            # connection either way, so this just keeps a straggler byte from
            # confusing the next connection's own handshake on this port
            with contextlib.suppress(SerialException):
                self._discard_stray_bytes(self._serial, 'serial0')
        super().close()

    @staticmethod
    def _discard_stray_bytes(serial: ExtendedSerial, name: str) -> None:
        """Read and discard any bytes waiting on `serial`, logging how many."""
        discarded = serial.read_all()
        if discarded:
            logger.warning(
                'Discarded %d stray byte(s) from %s before closing it',
                len(discarded),
                name,
            )

    @staticmethod
    def _request_disconnect(serial: ExtendedSerial) -> None:
        """
        Send a close request to the Bpod.

        This will:

        - disable all module relays
        - resume sending of the discovery byte
        - change the color of the status LED
        - disable the valve driver (if applicable)

        Logs a warning if the Bpod doesn't confirm the request.
        """
        if getattr(serial, 'is_open', False):
            logger.debug('Sending close request to Bpod Finite State Machine')
            # note: unlike other commands, the firmware acknowledges 'Z' with ASCII '1'
            # instead of 0x01 (an upstream inconsistency)
            if not serial.verify(b'Z', b'1', timeout=0.2):
                logger.warning('Bpod did not confirm the disconnect request')

    @property
    def input_event_names(self) -> tuple[str, ...]:
        """Names of all input events a state machine can transition on."""
        return self._compiler.input_event_names

    @property
    def action_names(self) -> tuple[str, ...]:
        """Names of all actions a state machine can set."""
        return self._compiler.action_names

    @property
    def serial0(self) -> ExtendedSerial:
        """Primary serial device for communication with the Bpod."""
        return self._serial

    def _request_handler(self, request: _BpodRequestUnion) -> Any:
        reply: Any
        match request:
            case _RequestHello():
                # Handle connection requests
                if request.bpod_core_version != bpod_core_version:
                    raise RuntimeError(
                        f'Version mismatch. Host uses bpod-core {bpod_core_version}, '
                        f'client uses bpod-core {request.bpod_core_version}. Please '
                        f'ensure that both use the same version.'
                    )
                if logger.isEnabledFor(logging.INFO):
                    address = self._zmq.get_metadata('Peer-Address', 'unknown address')
                    hostname = self._zmq.get_metadata('X-Hostname', 'unknown hostname')
                    logger.info('Client connected: %s (%s)', address, hostname)
                reply = _ReplyWelcome(
                    version=self._version,
                    serial_number=self._serial_number,
                    name=self.name,
                    location=self.location,
                )

            case _RequestData():
                # Handle data requests
                if request.method_name not in _REMOTE_DATA_METHODS:
                    raise BpodError(
                        f"Method '{request.method_name}' cannot be called remotely"
                    )
                method = getattr(self, request.method_name)
                data: pl.DataFrame = method(*request.args, **request.kwargs)
                reply = data.write_ipc(None, compression=request.compression).getvalue()

            case _RequestCall():
                # Handle method call requests
                if request.method_name not in _REMOTE_CALL_METHODS:
                    raise BpodError(
                        f"Method '{request.method_name}' cannot be called remotely"
                    )
                method = getattr(self, request.method_name)
                return_value = method(*request.args, **request.kwargs)
                reply = _ReplyGeneric(value=return_value)

            case _RequestBye():
                # Handle disconnect notice
                if logger.isEnabledFor(logging.INFO):
                    address = self._zmq.get_metadata('Peer-Address', 'unknown address')
                    hostname = self._zmq.get_metadata('X-Hostname', 'unknown hostname')
                    logger.info('Client disconnected: %s (%s)', address, hostname)
                reply = 'ciao!'

            case _:
                # Handle unknown request types
                raise BpodError(
                    f'Unknown request type: {type(request).__name__}. Ensure that host '
                    f'and client use the same version of bpod-core.'
                )
        return reply

    def _start_zmq(self, *, use_zeroconf: bool) -> None:
        # service properties used for advertising the Bpod via ZeroConf / locally
        properties = {
            'description': f'Bpod Finite State Machine {self.version.machine_str}',
            'serial_number': self._serial_number,
            'serial_port': self._port_info.device,
            'firmware': '.'.join([str(x) for x in self.version.firmware]),
            'core': bpod_core_version,
        }
        if self.name is not None:
            properties['name'] = self.name
        if self.location is not None:
            properties['location'] = self.location
        self._zmq = ServiceHost(
            service_name=self.name or f'bpod_{self._serial_number}',
            service_type='bpod',
            properties=properties,
            uuid=uuid5(BPOD_UUID_NAMESPACE, self._serial_number),
            request_handler=self._request_handler,
            request_type=_BpodRequestUnion,
            remote=use_zeroconf,
        )

    def _stop_zmq(self) -> None:
        if hasattr(self, '_zmq'):
            self._zmq.close()

    @property
    def address(self) -> str:
        """The ZeroMQ address of the Bpod."""
        return self._zmq.rep_tcp_addr

    def _get_setting(self, keys: list[str], default: Any = None) -> Any:
        return self._settings.get_nested(keys, default)

    def _set_setting(self, keys: list[str], value: Any = None) -> None:
        self._settings.set_nested(keys, value)

    @staticmethod
    def _identify_bpod(
        ports: list[ListPortInfo],
        port: str | None = None,
        serial_number: str | None = None,
    ) -> ListPortInfo:
        """
        Try to identify a supported Bpod based on port or serial number.

        If neither port nor serial number are provided, this function will attempt to
        detect a supported Bpod automatically.

        Parameters
        ----------
        ports : list of ListPortInfo
            The serial ports with a Bpod vendor and product ID, see :func:`find_ports`.
        port : str, optional
            The port of the device.
        serial_number : str, optional
            The serial number of the device.

        Returns
        -------
        ListPortInfo
            the serial port of the identified Bpod

        Raises
        ------
        BpodError
            If no Bpod is found or the indicated device is not supported.
        """
        candidates = _filter_ports(ports, device=port, serial_number=serial_number)
        try:
            return next(_discover_bpod_ports(candidates))
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
            hw_conf = list(self.serial0.query_struct(b'H', '<2H6B'))
        else:
            hw_conf = list(self.serial0.query_struct(b'H', '<2H5B'))
            hw_conf.insert(-4, 3)  # max bytes per serial msg always = 3
        hw_conf.extend(self.serial0.read_struct(f'<{hw_conf[-1]}sB'))
        hw_conf.append(self.serial0.read(hw_conf[-1]))

        # create Struct for hardware configuration
        input_description = hw_conf[-3]  # input_description is third to last
        hw_conf.append(1_000_000 // hw_conf[1])  # cycle_frequency; cycle_period_us @ 1
        hw_conf.append(input_description.count(b'U'))  # n_modules
        hw_conf.append(input_description.count(b'F'))  # n_flexio
        self._hardware = _HardwareConfiguration(*hw_conf)

    def _configure_flex_io(self) -> None:
        """Construct the FlexIO subsystem, if the hardware has FlexIO channels."""
        # default FlexIO channel configuration (one entry per Flex channel)
        # TODO: replace with the device query once the opcode is available
        if self._hardware.n_flexio > 0:
            self._flex_io = FlexIO(
                bpod_serial=self.serial0,
                n=self._hardware.n_flexio,
                cycle_frequency=self._hardware.cycle_frequency,
                on_input_events_changed=self._recompile_hardware_tables,
                is_running=lambda: self.is_running,
                configuration_locked=lambda: self._state_machine_started,
            )

    def _configure_io(self) -> None:
        """Configure the input and output channels of the Bpod."""
        logger.debug('Configuring I/O')
        for description, channel_class, channel_names in (
            (self._hardware.input_description, Input, CHANNEL_TYPES_INPUT),
            (self._hardware.output_description, Output, CHANNEL_TYPES_OUTPUT),
        ):
            n_channels = len(description)
            io_class = f'{channel_class.__name__.lower()}s'
            channels: dict[str, Any] = {}

            # loop over the description array and create channels - except for FlexIO
            # channels, which are accessed through flex_io
            for io_key in struct.unpack(f'<{n_channels}c', description):
                if io_key not in channel_names:
                    raise RuntimeError(f'Unknown {io_class[:-1]} type: {io_key}')
            for idx, (io_key, name) in enumerate(
                _name_channels(description, channel_names)
            ):
                if io_key != b'F':
                    channels[name] = channel_class(self, name, io_key, idx)

            # store channels to typed dict and set as a class attribute
            name = 'input channel' if channel_class is Input else 'output channel'
            setattr(
                self,
                io_class,
                SuggestionMapping(channels, name=name, error_class=BpodKeyError),
            )

        # set the enabled state of the input channels
        self._set_enable_inputs()

    def _detect_additional_serial_ports(
        self, ports: list[ListPortInfo]
    ) -> tuple[ExtendedSerial | None, ExtendedSerial | None]:
        """
        Detect additional USB-serial ports.

        Parameters
        ----------
        ports : list of ListPortInfo
            The serial ports with a Bpod vendor and product ID, as enumerated when the
            Bpod was identified.

        Returns
        -------
        tuple of (ExtendedSerial or None, ExtendedSerial or None)
            The secondary "app" serial port and the tertiary FlexIO serial port.
            Either is None if not applicable for this device.
        """
        logger.debug('Detecting additional USB-serial ports')

        def other_ports(candidates: list[ListPortInfo]) -> list[ListPortInfo]:
            return [
                p
                for p in _filter_ports(
                    candidates,
                    pid=[TeensyPID.DUAL_SERIAL, TeensyPID.TRIPLE_SERIAL],
                    serial_number=self._serial_number,
                )
                if p.device != self.port
            ]

        # First, assemble a list of candidate ports: the Bpod's other USB-serial ports.
        # Right after the Bpod was plugged in, the OS may not have registered all of
        # them yet when the Bpod was identified; if so, the ports are enumerated again.
        n_other_ports: dict[int, int] = {
            TeensyPID.DUAL_SERIAL: 1,
            TeensyPID.TRIPLE_SERIAL: 2,
        }
        candidate_ports = other_ports(ports)
        if len(candidate_ports) < n_other_ports.get(self._port_info.pid, 0):
            candidate_ports = other_ports(find_ports(vid=VIDS_BPOD, pid=PIDS_BPOD))

        # Then, try to find the secondary USB-serial port
        serial1 = None
        if self._version.firmware >= (23, 0):
            for port in candidate_ports:
                if verify_serial_discovery(
                    port.device,
                    bytes([222]),
                    timeout=DISCOVERY_TIMEOUT,
                    trigger=lambda: self.serial0.write(b'{'),
                ):
                    serial1 = ExtendedSerial()
                    serial1.port = port.device
                    candidate_ports.remove(port)
                    logger.debug('Detected secondary serial port: %s', port.device)
                    break
            if serial1 is None:
                raise BpodError('Could not detect secondary serial port')

        # State Machine 2+ uses a third USB-serial port for FlexIO
        flexio_serial = None
        if self.version.machine == 4:
            for port in candidate_ports:
                if verify_serial_discovery(
                    port.device,
                    bytes([223]),
                    timeout=DISCOVERY_TIMEOUT,
                    trigger=lambda: self.serial0.write(b'}'),
                ):
                    flexio_serial = ExtendedSerial()
                    flexio_serial.port = port.device
                    logger.debug('Detected FlexIO serial port: %s', port.device)
                    break
            if flexio_serial is None:
                raise BpodError(
                    'Could not detect FlexIO serial port. If another program left '
                    'FlexIO analog sampling running (e.g., after a crash), power-cycle '
                    'the Bpod.'
                )

        return serial1, flexio_serial

    def _handshake(self) -> None:
        """
        Perform a handshake with the Bpod.

        Raises
        ------
        BpodError
            If the handshake fails.
        """
        try:
            # discovery bytes the Bpod sent while unconnected, e.g. in response to the
            # port being opened, may precede its reply: skip them
            self.serial0.write(b'6')
            with self.serial0.temporary_timeout(0.2):
                reply = self.serial0.read_until(b'5')
            if reply.lstrip(DISCOVERY_BYTE) != b'5':
                raise BpodError(
                    f'Handshake with {self._serial_device_name} on {self.port} failed'
                )
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
        # the Bpod expects a value for each of its inputs: FlexIO inputs, which aren't
        # in self.inputs, stay enabled, as their channel type determines their events
        enabled = {i.index: i.enabled for i in self.inputs.values()}
        enable = [enabled.get(idx, True) for idx in range(self._hardware.n_inputs)]
        self.serial0.write_struct(f'<c{self._hardware.n_inputs}?', b'E', *enable)
        return self.serial0.read(1) == b'\x01'

    @override
    @_requires_idle
    def reset_session_clock(self) -> bool:
        # bracket the serial round trip to estimate the reset time within ±(t1-t0)/2;
        # a late reply would skew the estimate, so the reset is repeated after one
        logger.debug('Resetting session clock')
        for _ in range(_SESSION_CLOCK_RESET_ATTEMPTS):
            t0 = perf_counter_ns()
            self.serial0.write(b'*')
            if not self.serial0.read_bool():
                return False
            t1 = perf_counter_ns()
            round_trip_ns = t1 - t0
            if round_trip_ns <= 2 * _SESSION_CLOCK_MAX_ERROR_NS:
                break
            logger.debug('Session clock reset took %d μs', round_trip_ns // 1000)
        else:
            logger.warning(
                'Session clock reset took %d μs - timestamps may be off by up to %d μs',
                round_trip_ns // 1000,
                round_trip_ns // 2000,
            )
        perf_count_ns = (t0 + t1) // 2

        # Store time reference
        start_system_time_ns = self._time_reference.init_system_time_ns
        start_perf_counter_ns = self._time_reference.init_perf_counter_ns
        reset_time_ns = start_system_time_ns + (perf_count_ns - start_perf_counter_ns)
        self._time_reference = _TimeReferences(
            init_system_time_ns=start_system_time_ns,
            init_perf_counter_ns=start_perf_counter_ns,
            reset_system_time_ns=reset_time_ns,
        )
        self._softcode_thread.set_time_reference(self._time_reference)

        # firmware resets its trial counter alongside the session clock (same opcode);
        # mirror that here so the analog data pipeline's bookkeeping stays in sync
        self._flush_analog_buffer()
        self._analog_first_trial_time_us = None
        self._analog_trial_number_by_sequence = []
        self._analog_sample_index = 0
        return True

    def _disable_all_module_relays(self) -> None:
        # relays can only be enabled while idle and are disabled before every state
        # machine, so this sends nothing while one is running (e.g. when queuing)
        for module in self.modules.values():
            if module.relay:
                module.set_relay(False)

    @_requires_idle
    @validate_call()
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
        return self.serial0.verify()

    @override
    @_requires_idle
    def update_modules(self) -> None:
        # self._disable_all_module_relays()
        self.serial0.write(b'M')
        modules = []
        n_requested = [0] * self._hardware.n_modules
        for idx in range(self._hardware.n_modules):
            # check connection state
            if not (is_connected := self.serial0.read_bool()):
                module_name = f'{CHANNEL_TYPES_INPUT[b"U"]}{idx + 1}'
                modules.append(Module(_bpod=self, index=idx, name=module_name))
                continue

            # read further information if module is connected
            firmware_version, n_chars = self.serial0.read_struct('<IB')
            base_name, more_info = self.serial0.read_struct(f'<{n_chars}s?')
            base_name = base_name.decode('UTF8')
            custom_event_names = []
            while more_info:
                match self.serial0.read(1):
                    case b'#':
                        n_requested[idx] = self.serial0.read_uint8()
                    case b'V' | b'v':  # hardware version: major / minor
                        self.serial0.read(1)
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
                    _custom_event_names=custom_event_names,
                ),
            )

        n_events = _allocate_module_events(modules, n_requested)
        for module, n in zip(modules, n_events, strict=True):
            module.n_events = n
        self.modules = SuggestionMapping(
            {m.name: m for m in modules}, name='module', error_class=BpodKeyError
        )

        # update event names and output actions, then tell the state machine how many
        # events each serial channel may report - until then, it drops all module
        # events and softcodes
        self._recompile_hardware_tables()
        serial_event_allocation = self._compiler.serial_event_allocation
        if not self.serial0.verify(b'%' + serial_event_allocation):
            raise BpodError('State machine did not acknowledge serial event allocation')
        logger.debug('Serial event allocation sent: %s', list(serial_event_allocation))

    def _recompile_hardware_tables(self) -> None:
        """Build a new state machine compiler for the current hardware configuration.

        Must be called whenever anything that affects event/action naming or event
        values changes after the initial connection (e.g. module discovery, FlexIO
        channel types or threshold voltages), so that compiled state machines and
        recorded events reflect the current hardware configuration and stale cache
        entries aren't served.
        """
        flexio = dict(self._flex_io) if self._flex_io is not None else {}
        self._compiler = StateMachineCompiler(
            self._hardware,
            self._version,
            {m.name: m.event_names for m in self.modules.values()},
            flexio_channel_types={n: c.channel_type for n, c in flexio.items()},
            threshold_voltages={
                n: [t.voltage for t in c.thresholds] for n, c in flexio.items()
            },
        )

    def _start_analog_reader(self) -> None:
        """Start the FlexIO analog reader if any channel is an ANALOG_INPUT."""
        if self._flexio_serial is None or self._flex_io is None:
            return
        names = tuple(
            name
            for name, channel in self._flex_io.items()
            if channel.channel_type == FlexIOChannelType.ANALOG_INPUT
        )
        if not names:
            return  # firmware transmits nothing until >=1 channel is ANALOG_INPUT
        n_cycles_per_sample = round(
            self._hardware.cycle_frequency / self._flex_io.analog_sampling_rate
        )
        self._analog_sample_period_us = (
            n_cycles_per_sample * self._hardware.cycle_period_us
        )
        self._analog_channel_names = names
        n = len(names)
        serial = self._flexio_serial
        if not serial.is_open:
            serial.open()
        self._analog_struct = struct.Struct(f'<{1 + n}H')  # trial_counter + n samples
        chunk_size = self._analog_struct.size
        self._analog_reader_thread = ReaderThread(
            serial,
            lambda: ChunkedSerialReader(chunk_size, callback=self._on_analog_chunk),
        )
        self._analog_reader_thread.start()

    def _on_analog_chunk(self, chunk: bytearray) -> None:
        """Decode one FlexIO analog sample block and buffer it for `get_analog_data`."""
        if self._analog_struct is None:
            return  # reader was stopped mid-flight
        trial_counter, *raw_counts = self._analog_struct.unpack(chunk)
        if trial_counter < 1:
            return  # firmware hasn't started sampling yet (pre-trial idle state)
        # firmware's trial_counter increments before first use, so the first trial of
        # a session reports 1, not 0 - convert to a 0-based sequence-position index
        sequence_index = trial_counter - 1
        if 0 <= sequence_index < len(self._analog_trial_number_by_sequence):
            trial_number = self._analog_trial_number_by_sequence[sequence_index]
        else:
            trial_number = trial_counter

        # buffer the sample's ordinal index rather than a computed timestamp: the
        # anchor may not be known yet (the analog reader thread and the EventThread
        # that sets it run on independent serial ports), and counting must not be
        # coupled to knowing it - see _flush_analog_buffer
        self._analog_buffer.append(
            (self._analog_sample_index, trial_number, *raw_counts)
        )
        self._analog_sample_index += 1

        now = perf_counter_ns()
        if now - self._analog_last_flush_ns >= _ANALOG_FLUSH_INTERVAL_NS:
            self._flush_analog_buffer()

    def _flush_analog_buffer(self) -> None:
        """Flush any buffered analog samples onto the analog data queue."""
        self._analog_last_flush_ns = perf_counter_ns()
        if self._analog_first_trial_time_us is None:
            return  # anchor not known yet; keep buffering until it is
        buffer, self._analog_buffer = self._analog_buffer, []
        if not buffer or not self._analog_channel_names:
            return
        columns = ['k', 'Trial', *self._analog_channel_names]
        # firmware preloads its sample clock so the first sample fires one
        # state-machine cycle after trial start, not a full sample period
        anchor_us = self._analog_first_trial_time_us + self._hardware.cycle_period_us
        period_us = self._analog_sample_period_us
        frame = (
            pl.LazyFrame(buffer, schema=columns, orient='row')
            .with_columns(
                pl.from_epoch(anchor_us + pl.col('k') * period_us, time_unit='us')
                .dt.replace_time_zone('UTC')
                .alias('Time'),
                pl.col('Trial').cast(pl.UInt32),
                _counts_to_mv(pl.col(self._analog_channel_names).cast(pl.UInt32)).cast(
                    pl.UInt16
                ),
            )
            .select('Time', 'Trial', *self._analog_channel_names)
        )
        self._analog_data.put(frame)

    def validate_state_machine(self, state_machine: StateMachine) -> None:
        """
        Validate the provided state machine for compatibility with the hardware.

        Parameters
        ----------
        state_machine : StateMachine
            The state machine to validate.

        Raises
        ------
        ValueError
            If the state machine is invalid or not compatible with the hardware.
        """
        self._compiler.validate(state_machine)

    def send_softcode(self, softcode: int) -> None:
        """Send a softcode to the state machine.

        Can be used to trigger transitions. While no state machine is running, the Bpod
        would ignore the softcode, so it isn't sent and a warning is logged instead.

        Parameters
        ----------
        softcode : int
            The softcode value to send.

        Raises
        ------
        ValueError
            If ``softcode`` is out of range.
        """
        if not (0 <= softcode < self._compiler.n_softcodes):
            raise ValueError(
                f'Softcode {softcode} is out of range - hardware supports softcodes '
                f'0..{self._compiler.n_softcodes - 1}'
            )
        if not self.is_running:
            logger.warning(
                'No state machine is running - softcode %d not sent', softcode
            )
            return
        logger.debug('Sending softcode %d to Bpod', softcode)
        self._serial.write_struct('<cB', b'~', softcode)

    @override
    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    def run(
        self,
        state_machine: StateMachine | None = None,
        *,
        trial_number: int | None = None,
        validate: bool = True,
    ) -> None:
        if trial_number is None:
            trial_number = self._next_fsm_index + 1
        if not 0 <= trial_number <= UINT32_MAX:
            raise ValueError(f'trial_number must be between 0 and {UINT32_MAX}')
        debugging = logger.isEnabledFor(logging.DEBUG)
        self._disable_all_module_relays()

        # If the user did not provide a state machine, run the last one again
        if state_machine is None:
            if self._last_compiled is None:
                raise RuntimeError('No state machine has been run yet')
            fsm_bytes, annotations, hardware_hash = self._last_compiled
            if hardware_hash != self._compiler.hardware_hash:
                raise RuntimeError(
                    'The hardware configuration has changed since the last state '
                    'machine ran - pass it to run() again'
                )
            self._fsm_annotations = annotations
            fsm_hash = annotations.fsm_hash
            if debugging:
                logger.debug('Running state machine %s again', fsm_hash.hex())

        # Otherwise go through the process of validating and compiling the state machine
        else:
            # 1) COMPUTE HASH
            #    This allows us to bypass validation and compilation given a cache hit
            if debugging:
                t0 = perf_counter_ns()
                fsm_hash = state_machine.hash
                d = (perf_counter_ns() - t0) // 1000
                logger.debug(
                    'Computed hash of state machine: %s (%d μs)', fsm_hash.hex(), d
                )
            else:
                fsm_hash = state_machine.hash

            # 2) VALIDATE AND COMPILE
            try:
                fsm_bytes, self._fsm_annotations = self._compiler.compile(
                    state_machine=state_machine,
                    known_hash=fsm_hash,
                    validate=validate,
                )
            except Exception as e1:
                # if compilation failed after bypassing validation, validate post-mortem
                # to get a more informative error message
                if not validate:
                    try:
                        with suppress_logging():
                            self._compiler.validate(
                                state_machine=state_machine,
                                known_hash=fsm_hash,
                            )
                    except Exception as e2:
                        raise e2 from e1
                raise

            # 3) KEEP THE COMPILED STATE MACHINE FOR REPEAT CALLS
            self._last_compiled = (
                fsm_bytes,
                self._fsm_annotations,
                self._compiler.hardware_hash,
            )

        # count the trial only now, so that a rejected call doesn't use up its number
        self._next_fsm_index = trial_number

        # Send state machine to Bpod; always queue for immediate back-to-back execution.
        # The Bpod may start it at any time from here on - count it as running until
        # the threads reading it are registered, so that is_running doesn't drop out
        # while the previous state machine's threads finish.
        if not self._state_machine_started:
            # FlexIO channel types and the sampling rate are fixed from here on,
            # and the Bpod streams analog samples from the first trial on
            self._start_analog_reader()
        if self._analog_first_trial_time_us is None:
            # the Bpod (re)starts analog sampling with the first trial after a
            # session clock reset
            self._analog_recording = self._analog_reader_thread is not None
        self._starting_state_machine = True
        self._state_machine_started = True
        # record the trial number before the Bpod can start the state machine: analog
        # samples only carry the firmware's positional trial counter, so decoding looks
        # the number up by position from the trial's first sample on
        self._analog_trial_number_by_sequence.append(self._next_fsm_index)
        try:
            message = struct.pack('<c?', b'C', b'\x01') + fsm_bytes
            if debugging:
                t0 = perf_counter_ns()
                self.serial0.write(message)
                logger.debug(
                    'Sent state machine %s to Bpod (%d μs)',
                    fsm_hash.hex(),
                    (perf_counter_ns() - t0) // 1000,
                )
            else:
                self.serial0.write(message)

            # Start threads
            self._run_state_machine()
        finally:
            self._starting_state_machine = False

    def _capture_analog_anchor(self, time_us: int) -> None:
        """Capture the first trial's start time as the FlexIO analog-sample anchor."""
        if self._analog_first_trial_time_us is None:
            self._analog_first_trial_time_us = time_us

    def _run_state_machine(self) -> None:
        state_machine_lookup = cast('_StateMachineLookup', self._fsm_annotations)

        # initialize new threads
        event_thread = EventThread(
            trial=self._next_fsm_index,
            fsm=state_machine_lookup,
            data_queue=self._trial_data,
            event_tables=self._compiler.event_tables,
            time_reference=self._time_reference,
            publish=self._zmq.publish,
            publish_gate=lambda: self._zmq.has_subscribers,
            on_trial_start=self._capture_analog_anchor,
        )
        # wait for the previous trial's reader to finish, then resume reading the serial
        # stream without further delay (softcodes and raw events must not wait for the
        # previous trial's event processing). The previous reader may already have read
        # the start of this trial - hand those bytes over to the new reader.
        previous_read_thread = self._read_thread
        if previous_read_thread is not None and previous_read_thread.is_alive():
            logger.debug(
                'Waiting for state machine #%d to finish ...',
                previous_read_thread.trial_number,
            )
            previous_read_thread.join()
        leftover, leftover_received_ns = (
            (previous_read_thread.leftover, previous_read_thread.leftover_received_ns)
            if previous_read_thread is not None
            else (b'', 0)
        )
        read_thread = ReadThread(
            serial=self.serial0,
            state_machine_hash=state_machine_lookup.fsm_hash,
            trial=self._next_fsm_index,
            cycle_period_us=self._hardware.cycle_period_us,
            queue_events=event_thread.queue,
            queue_softcodes=self._softcode_thread.queue,
            initial_bytes=leftover,
            initial_received_ns=leftover_received_ns,
        )
        read_thread.start()

        # before the new event thread publishes anything, let the previous one drain its
        # backlog - this keeps trial streams strictly sequential on the PUB/SUB channel.
        previous_event_thread = self._event_thread
        if previous_event_thread is not None and previous_event_thread.is_alive():
            previous_event_thread.join()
        event_thread.start()

        # set private class attributes
        self._event_thread = event_thread
        self._read_thread = read_thread
        self._fsm_annotations = None

    @property
    def is_running(self) -> bool:
        """Check if the Bpod is currently running a state machine."""
        if self._starting_state_machine:  # cleared only once _read_thread is updated
            return True
        read_thread = self._read_thread  # snapshot for thread-safety
        return read_thread is not None and read_thread.is_alive()

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

        Blocks until the state machine's reader and event threads complete - i.e., until
        all of the trial's events have been recorded and published. If no state machine
        is currently running, this method returns immediately.

        Raises
        ------
        BpodError
            If the connection to the Bpod was lost during the trial. The events received
            up to then are kept.
        """
        read_thread = self._read_thread  # snapshot for thread-safety
        if read_thread is not None and read_thread.is_alive():
            logger.debug(
                'Waiting for state machine #%d to finish ...',
                read_thread.trial_number,
            )
            read_thread.join()

        event_thread = self._event_thread  # snapshot for thread-safety
        if event_thread is not None and event_thread.is_alive():
            event_thread.join()

        if read_thread is not None and (error := read_thread.error) is not None:
            read_thread.error = None  # report it once
            trial = read_thread.trial_number
            raise BpodError(
                f'Lost connection to the Bpod during trial #{trial}'
            ) from error

    @overload
    def peek_data(
        self,
        trigger_states: Collection[str] | None = ...,
        *,
        lazy: Literal[False] = False,
    ) -> pl.DataFrame: ...

    @overload
    def peek_data(
        self, trigger_states: Collection[str] | None = ..., *, lazy: Literal[True]
    ) -> pl.LazyFrame: ...

    def peek_data(self, trigger_states: Collection[str] | None = None, *, lazy=False):
        """Return a snapshot of the current trial's data.

        Parameters
        ----------
        trigger_states : Collection of str, optional
            Block until at least one of the given states has been entered, then return
            the snapshot. If ``None`` (default), returns immediately.
        lazy : bool, default: False
            If ``True``, return a :class:`polars.LazyFrame`.
            If ``False`` (default), return a :class:`polars.DataFrame`.

        Returns
        -------
        DataFrame or LazyFrame
            Events recorded so far in the current trial. Returns an empty DataFrame if
            no trial is running.

        Raises
        ------
        ValueError
            If one or several of the trigger states are not part of the state machine.
        """
        if self._event_thread is None:
            empty = pl.DataFrame(schema=_TRIAL_DATA_SCHEMA)
            return empty.lazy() if lazy else empty
        data = self._event_thread.peek_data(trigger_states)
        return data if lazy else data.collect(engine='in-memory')

    @overload
    def get_data(
        self, *, concat: bool = ..., lazy: Literal[False] = False
    ) -> pl.DataFrame: ...

    @overload
    def get_data(self, *, concat: bool = ..., lazy: Literal[True]) -> pl.LazyFrame: ...

    @override
    def get_data(self, *, concat=True, lazy=False):
        # the EventThread queues a trial's data as it ends: check it before the queue
        event_thread = self._event_thread
        busy = self.is_running or (event_thread is not None and event_thread.is_alive())
        if not busy and self._trial_data.empty():
            raise BpodError('No trial data available')
        return self._drain_queue(self._trial_data, concat=concat, lazy=lazy)

    @staticmethod
    def _drain_queue(
        queue: SimpleQueue[pl.LazyFrame],
        *,
        concat: bool,
        lazy: bool,
        how: Literal['vertical', 'diagonal_relaxed'] = 'vertical',
    ) -> pl.DataFrame | pl.LazyFrame:
        """Pop one or more LazyFrames from a queue, optionally concatenating them."""
        frames = [queue.get()]
        if concat:
            try:
                while True:
                    frames.append(queue.get(block=False))
            except Empty:
                pass
            data = pl.concat(frames, how=how)
        else:
            data = frames[0]

        # trial data is small: the in-memory engine avoids the streaming engine's
        # start-up cost (polars 2 picks streaming by default)
        return data if lazy else data.collect(engine='in-memory')

    @overload
    def get_analog_data(
        self, *, concat: bool = ..., lazy: Literal[False] = False
    ) -> pl.DataFrame: ...

    @overload
    def get_analog_data(
        self, *, concat: bool = ..., lazy: Literal[True]
    ) -> pl.LazyFrame: ...

    def get_analog_data(self, *, concat=True, lazy=False):
        """
        Return FlexIO analog-input data from the data queue.

        Parameters
        ----------
        concat : bool, default: True
            If ``True``, pop and concatenate all DataFrames currently in the queue into
            a single DataFrame, blocking until at least one is available.
            If ``False``, pop and return one DataFrame, blocking until one is
            available.
        lazy : bool, default: False
            If ``True``, return a :class:`polars.LazyFrame`.
            If ``False``, return a :class:`polars.DataFrame`.

        Returns
        -------
        DataFrame or LazyFrame
            One row per sample, with the columns:

            - ``Time`` (``Datetime``, µs, UTC): absolute time of the sample;
            - ``Trial`` (``UInt32``): zero-based index of the trial the sample was
              taken in; samples taken between trials belong to the preceding trial;
            - ``Flex{N}`` (``UInt16``): voltage in millivolts (0-5000), one column per
              channel configured as ``ANALOG_INPUT``.

            Data recorded before closing and reopening the Bpod with other analog
            inputs has other ``Flex{N}`` columns; concatenating fills the missing ones
            with nulls.

        Raises
        ------
        BpodError
            If the Bpod has no FlexIO channels, or if no data is available and the
            analog inputs aren't recording: none is configured as
            ``ANALOG_INPUT``, no state machine has run yet, or
            :meth:`stop_state_machine` stopped them.

        Notes
        -----
        While the analog inputs are recording and no samples are available, this
        waits for new ones. Recording starts with the first state machine;
        :meth:`stop_state_machine` stops it until the first state machine after
        :meth:`reset_session_clock`.

        Voltages are stored as integer millivolts, which hold the 12-bit ADC readings
        losslessly. The original ADC codes are ``round(millivolts * 4095 / 5000)``,
        which correspond to ``adc_code * 5 / 4095`` volts.
        """
        if not self._hardware.n_flexio:
            raise BpodError(
                f'Bpod Finite State Machine {self._version.machine_str} does not have '
                f'FlexIO channels.'
            )
        if not self._analog_recording:
            # no new samples arrive to trigger the periodic flush
            self._flush_analog_buffer()
            if self._analog_data.empty():
                raise BpodError('No FlexIO analog input is recording')
        return self._drain_queue(
            self._analog_data,
            concat=concat,
            lazy=lazy,
            how='diagonal_relaxed',
        )

    @override
    def stop_state_machine(self) -> None:
        if not self.is_running:
            return
        if self._analog_channel_names:
            logger.warning(
                'Stopping the state machine also stops FlexIO analog sampling and '
                'threshold detection until reset_session_clock() is called and the '
                'next state machine starts'
            )
        logger.debug('Stopping state machine')
        self.serial0.write(b'X')
        self._analog_recording = False  # the Bpod stops analog sampling on 'X'
        if self._read_thread is not None:
            self._read_thread.join()

    @override
    @property
    def name(self) -> str | None:
        return cast(
            'str | None',
            self._get_setting(['devices', self._serial_number, 'name'], None),
        )

    @name.setter
    def name(self, name: str | None) -> None:
        self._set_setting(['devices', self._serial_number, 'name'], name)

    @override
    @property
    def location(self) -> str | None:
        return cast(
            'str | None',
            self._get_setting(['devices', self._serial_number, 'location'], None),
        )

    @location.setter
    def location(self, location: str | None) -> None:
        self._set_setting(['devices', self._serial_number, 'location'], location)

    @override
    @property
    def address_control(self) -> str:
        return self._zmq.rep_tcp_addr

    @override
    @property
    def address_events(self) -> str:
        return self._zmq.pub_tcp_addr

    @validate_call()
    def set_softcode_handler(
        self, softcode_handler: Callable[[int], None] | None = None
    ) -> None:
        """Set the handler function for softcodes sent from the Bpod.

        Parameters
        ----------
        softcode_handler : Callable, optional
            The function to call when a softcode is received.
        """
        self._softcode_slot.handler = softcode_handler


class Channel:
    """Base class representing a channel on the Bpod device.

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

    def __init__(self, bpod: Bpod, name: str, io_key: bytes, index: int) -> None:
        self.name = name
        self.io_type = io_key
        self.index = index
        self._bpod = bpod
        self._serial0 = bpod.serial0

    def __repr__(self) -> str:
        return self.__class__.__name__ + '()'


class Input(Channel):
    """Input channel class representing a digital input channel."""

    def __init__(self, bpod: Bpod, name: str, io_key: bytes, index: int) -> None:
        super().__init__(bpod, name, io_key, index)
        self._set_enable_inputs = bpod._set_enable_inputs  # noqa: SLF001
        self._enabled = io_key in (b'PBW')  # Enable Port, TTL and Wire inputs

    @_requires_idle
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

        Creates a virtual event for the running state machine. While no state machine
        is running, the Bpod would ignore the override, so it isn't sent and a warning
        is logged instead. Once overridden, the physical input is ignored until the next
        state machine starts.

        Parameters
        ----------
        state : bool
            The state to set for the input channel.
        """
        if not self._bpod.is_running:
            logger.warning(
                'No state machine is running - override of %s not sent', self.name
            )
            return
        self._serial0.write_struct('<c2B', b'V', self.index, state)

    @_requires_idle
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
        if self.io_type not in b'DBWVP':
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
    @_requires_idle
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

    @_requires_idle
    @validate_call()
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
            '%sabling relay for module %s', 'En' if enabled else 'Dis', self.name
        )
        self._bpod.serial0.write_struct('<cB?', b'J', self.index, enabled)
        self._relay_is_enabled = enabled

    @property
    def relay(self) -> bool:
        """Whether the serial relay for this module is enabled.

        When ``True``, the Bpod forwards bytes from the module to the host via
        the Bpod's USB serial port.
        """
        return self._relay_is_enabled

    @_requires_idle
    @validate_call()
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

    @property
    def event_names(self) -> list[str]:
        """A list of event names associated with the module."""
        names = self._custom_event_names[: self.n_events]
        names += [str(idx) for idx in range(len(names), self.n_events)]
        return [f'{self.name}_{name}' for name in names]


def _allocate_module_events(modules: list[Module], n_requested: list[int]) -> list[int]:
    """Allocate serial events to module ports, as Bpod's MATLAB software does.

    The module ports share a fixed budget of 15 events per port
    (``N_SERIAL_EVENTS_DEFAULT``). Requests are met by moving events between ports, so
    the softcodes' share of the state machine's serial events is never touched:

    1. Every port starts with 15 events, including ports whose module requests fewer.
    2. Modules requesting more than 15 events are served in port order. Each takes the
       missing events from the ports in reverse order, starting with the last port:

       - unused ports and valve modules give up all of their events,
       - modules that requested fewer events than they hold give up the surplus,
       - all other modules give up nothing.

    3. If the ports cannot cover a request, a :class:`BpodError` is raised.

    For example, a Bpod 2+ with a module requesting 24 events on port 1 and no modules
    on ports 2 and 3 gets ``[24, 15, 6]``: port 3 gives up the 9 missing events.

    Unlike in MATLAB, a module that can cover only part of a request gives up just its
    surplus, not all of its events.

    Parameters
    ----------
    modules : list of Module
        The modules, ordered by port.
    n_requested : list of int
        The number of events requested by each module, 0 if it did not request any.

    Returns
    -------
    list of int
        The number of events allocated to each module port.

    Raises
    ------
    BpodError
        If the requested events exceed the events available.
    """
    n_events = [N_SERIAL_EVENTS_DEFAULT] * len(modules)
    n_reserved = [  # events a port keeps: none for unused ports and valve modules
        (n or N_SERIAL_EVENTS_DEFAULT)
        if m.is_connected and 'ValveModule' not in m.name
        else 0
        for m, n in zip(modules, n_requested, strict=True)
    ]
    for idx, n in enumerate(n_requested):
        n_missing = n - n_events[idx]
        if n_missing <= 0:
            continue
        n_events[idx] = n
        for pos in reversed(range(len(modules))):
            n_taken = min(max(n_events[pos] - n_reserved[pos], 0), n_missing)
            n_events[pos] -= n_taken
            n_missing -= n_taken
        if n_missing > 0:
            raise BpodError(
                f'{modules[idx].name} requests {n} serial events, but only '
                f'{n - n_missing} are available - please reconfigure modules'
            )
    return n_events


class RemoteBpod(AbstractBpod):
    """Proxy for a :class:`Bpod` instance running in another process.

    Use this when the Bpod hardware is managed by a separate process that was
    started with ``remote=True``. :class:`RemoteBpod` discovers that process via
    Zeroconf and forwards all method calls over ZeroMQ.

    .. note::
        This class is not yet fully functional. Some methods may be missing or
        incomplete.

    Parameters
    ----------
    address : str, optional
        ZeroMQ address of the remote Bpod service. Discovered automatically if
        not given.
    name : str, optional
        Zeroconf service name to filter by during discovery.
    serial_number : str, optional
        Serial number of the target Bpod to filter by during discovery.
    location : str, optional
        Zeroconf location string to filter by during discovery.
    timeout : float, default: 10.0
        Discovery timeout in seconds.
    event_callback : Callable, optional
        Called with each live trial event received over the events channel;
        return values are ignored. Runs on a background thread and should not block;
        exceptions it raises are logged but not propagated.

    Raises
    ------
    TimeoutError
        If no matching remote Bpod is found within `timeout` seconds.
    """

    _zmq: ServiceClient[_BpodRequestUnion, _BpodReplyUnion, list[BpodEventUnion]]
    _name: str | None = None
    _location: str | None = None
    _event_callback: Callable[[BpodEventUnion], object] | None = None

    def __init__(
        self,
        address: str | None = None,
        *,
        name: str | None = None,
        serial_number: str | None = None,
        location: str | None = None,
        timeout: float = 10.0,
        event_callback: Callable[[BpodEventUnion], object] | None = None,
    ) -> None:
        # the event loop may deliver messages before this constructor returns, so the
        # callback must be in place before the client exists
        self._event_callback = event_callback
        try:
            # without a callback there is no consumer for live events - skip the
            # subscription entirely so the host bypasses publishing altogether
            subscribe = event_callback is not None
            self._zmq = ServiceClient(
                service_type='bpod',
                address=address,
                event_handler=self._event_handler if subscribe else None,
                discovery_timeout=timeout,
                txt_properties={
                    'name': name,
                    'serial_number': serial_number,
                    'location': location,
                },
                default_reply_type=_BpodReplyUnion,
                event_type=list[BpodEventUnion] if subscribe else Any,
            )
        except TimeoutError as e:
            raise TimeoutError('Failed to discover remote Bpod.') from e
        try:
            self._handshake()
        except BaseException:
            self._zmq.close()
            raise

        # log hardware information
        logger.info(
            'Connected to Bpod Finite State Machine %s on %s',
            self._version.machine_str,
            self._zmq.address_req,
        )

    @override
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the context manager.

        Closes the connection to the remote Bpod.

        Parameters
        ----------
        exc_type : type[BaseException] | None
            The type of exception raised, if any.
        exc_val : BaseException | None
            The exception instance raised, if any.
        exc_tb : TracebackType | None
            The traceback object, if any.
        """
        self.close()

    def close(self) -> None:
        """Close the connection to the remote Bpod."""
        self._zmq.request(request_data=_RequestBye(), reply_type=str)
        self._zmq.close()

    def _remote_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
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
        Any
            The result returned from the remote method.
        """
        try:
            return self._zmq.request(
                request_data=_RequestCall(method_name=method, args=args, kwargs=kwargs),
                reply_type=_ReplyGeneric,
            ).value
        except RemoteError as e:
            match e.original_error.name:
                case 'RuntimeError':
                    raise RuntimeError(e.original_error.message) from e
                case 'ValueError':
                    raise ValueError(e.original_error.message) from e
            raise

    def _handshake(self) -> None:
        reply = self._zmq.request(
            request_data=_RequestHello(bpod_core_version=bpod_core_version),
            reply_type=_ReplyWelcome,
        )
        self._version = reply.version
        self._serial_number = reply.serial_number
        self._name = reply.name
        self._location = reply.location

    def _event_handler(self, messages: list[BpodEventUnion]) -> None:
        """Handle a batch of messages received on the PUB/SUB channel.

        Logs selected messages, then forwards each message to the ``event_callback``
        given at construction.

        Parameters
        ----------
        messages : list of BpodEventUnion
            The decoded messages, in the order they were recorded.
        """
        for message in messages:
            match message:
                case EventTrialStart(trial=trial, fsm_hash=fsm_hash):
                    logger.info('Starting state machine %s / trial %s', fsm_hash, trial)
            if self._event_callback is not None:
                # a failing callback mustn't cost the rest of the batch
                try:
                    self._event_callback(message)
                except Exception:
                    logger.exception('event_callback raised an exception')

    @overload
    def get_data(
        self, *, concat: bool = ..., lazy: Literal[False] = False
    ) -> pl.DataFrame: ...

    @overload
    def get_data(self, *, concat: bool = ..., lazy: Literal[True]) -> pl.LazyFrame: ...

    @override
    def get_data(self, *, concat=True, lazy=False):
        try:
            reply = self._zmq.request(
                request_data=_RequestData(
                    method_name='get_data',
                    kwargs={'concat': concat},
                    compression='uncompressed' if self._zmq.is_local else 'lz4',
                ),
                reply_type=bytes,
            )
        except RemoteError as e:
            if e.original_error.name == 'BpodError':
                raise BpodError(e.original_error.message) from e
            raise

        return pl.scan_ipc(reply) if lazy else pl.read_ipc(reply)

    @override
    @property
    def name(self) -> str | None:
        return self._name

    @override
    @property
    def location(self) -> str | None:
        return self._location

    @override
    @property
    def address_control(self) -> str:
        return self._zmq.address_req

    @override
    @property
    def address_events(self) -> str:
        return self._zmq.address_sub

    @override
    def reset_session_clock(self) -> bool:
        try:
            return self._remote_call('reset_session_clock') is True
        except RemoteError as e:
            if e.original_error.name == 'BpodError':
                raise BpodError(e.original_error.message) from e
            raise

    @override
    @validate_call()
    def run(
        self,
        state_machine: StateMachine | None = None,
        *,
        trial_number: int | None = None,
        validate: bool = True,
    ) -> None:
        fsm = (
            state_machine.to_dict(exclude_defaults=True)
            if state_machine is not None
            else None
        )
        self._remote_call(
            'run',
            state_machine=fsm,
            trial_number=trial_number,
            validate=validate,
        )

    @override
    def set_status_led(self, enable: bool) -> bool:
        return self._remote_call('set_status_led', enable) or False

    @override
    def stop_state_machine(self) -> None:
        self._remote_call('stop_state_machine')

    @override
    def update_modules(self) -> None:
        self._remote_call('update_modules')


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
    ports = find_ports(
        vid=VIDS_BPOD, pid=PIDS_BPOD, device=port, serial_number=serial_number
    )
    for p in _discover_bpod_ports(ports):
        yield BpodInfo(port=p.device, serial_number=str(p.serial_number))


def _discover_bpod_ports(ports: Iterable[ListPortInfo]) -> Iterator[ListPortInfo]:
    """Yield the serial ports in `ports` on which an idle Bpod answers a discovery."""
    for p in ports:
        # an unconnected Bpod sends its discovery byte at regular intervals, and recent
        # firmware also when DTR is asserted
        if verify_serial_discovery(
            port=p.device,
            expected_message=DISCOVERY_BYTE,
            timeout=DISCOVERY_TIMEOUT,
            raise_dtr=True,
        ):
            yield p


def discover_remote_bpod(
    name: str | None = None,
    serial_number: str | None = None,
    location: str | None = None,
    timeout: float | None = 10.0,
    poll_interval: float = 1.0,
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
    timeout : float or None, default: 10.0
        How many seconds to monitor.
        Pass ``None`` to monitor indefinitely until the iterator is closed.
    poll_interval : float, default: 1.0
        How often to poll for local service changes, in seconds. Default is 1.
    local : bool, default: True
        Whether to search for services on the local machine.
    remote : bool, default: True
        Whether to also search for services on the network.

    Yields
    ------
    ~bpod_core.ipc.ServiceEvent
        A named tuple with the following fields:

        - kind: str, either ``'added'`` or ``'removed'``
        - address: str, the service address, e.g., ``'tcp://192.168.1.10:1234'``
        - properties: dict, the service properties, e.g., ``{'name': 'MyDevice'}``
    """
    yield from iter_services(
        service_type='bpod',
        properties={
            'name': name,
            'serial_number': serial_number,
            'location': location,
        },
        timeout=timeout,
        poll_interval=poll_interval,
        local=local,
        remote=remote,
    )
