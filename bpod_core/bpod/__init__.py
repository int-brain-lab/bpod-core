"""Module for interfacing with the Bpod Finite State Machine."""

import contextlib
import functools
import logging
import os
import re
import struct
import threading
import weakref
from collections import deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from itertools import takewhile
from queue import Empty, SimpleQueue
from time import monotonic, perf_counter_ns, time_ns
from types import TracebackType
from typing import (
    Any,
    Concatenate,
    NoReturn,
    ParamSpec,
    TypeVar,
    cast,
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
    _IDLE_STOP_REPLY_HEADER,
    _IDLE_STOP_REPLY_SIZE,
    _TRIAL_DATA_SCHEMA,
    EventThread,
    ReadThread,
    SoftcodeThread,
    _EventCallbacks,
    _LateStarts,
    _Recorder,
    _Trial,
    wait_until,
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
    NON_BPOD_TEENSY_MODELS,
    USB_IDS_BPOD,
    FlexIOChannelType,
)
from bpod_core.bpod.errors import BpodError, BpodKeyError, owning_replies
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
    PortBusyError,
    SerialDevice,
    _filter_ports,
    _read_bcd_device,
    _usb_interface_number,
    find_ports,
    verify_serial_discovery,
)
from bpod_core.constants import (
    FMT_UINT8,
    FMT_UINT16_LE,
    UINT32_MAX,
    VID_TEENSY,
    TeensyModel,
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
# after the stop, a given-up trial is drained until the Bpod is quiet for 0.2 s, for at
# most a second, in case it keeps sending
_STOP_DRAIN_QUIET_S = 0.2
_STOP_DRAIN_MAX_S = 1.0
# a state machine sent before starts within its load time (milliseconds) once run() has
# returned for it, as the trial before it has ended by then
_START_TIMEOUT_S = 1.0


_S = TypeVar('_S')
_P = ParamSpec('_P')
_R = TypeVar('_R')


def _requires_idle(
    method: Callable[Concatenate[_S, _P], _R],
) -> Callable[Concatenate[_S, _P], _R]:
    """Refuse calls to `method` while a state machine runs or the stream is suspect.

    While a trial runs, its read thread owns the Bpod's serial port's replies (see
    :attr:`~bpod_core.com.ExtendedSerial.read_lock`): a command that expects a reply
    would steal the trial's event bytes or corrupt them with its reply, and
    reconfiguring the hardware mid-trial is unsafe. The method owns the replies for
    its duration, so a `run()` from another thread is refused meanwhile. After a trial
    was given up (see :meth:`Bpod.stop_state_machine`) or couldn't be read, the Bpod's
    stream can't be trusted, so commands are refused until the connection is closed
    and reopened. Applies to methods of :class:`Bpod` and of objects that refer to one
    as ``_bpod`` (channels, modules). Decorated methods are marked with
    ``requires_idle = True``.
    """

    @functools.wraps(method)
    def wrapper(self: _S, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        bpod = cast('Bpod', getattr(self, '_bpod', self))
        bpod._check_in_sync()  # noqa: SLF001 - same module
        with owning_replies(bpod.serial0, f'use {method.__qualname__}'):  # reads nest
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
    _out_of_sync: bool = False
    """Set when a trial was given up or couldn't be read: the stream can't be trusted
    until the connection is closed and reopened."""
    _read_thread: ReadThread | None = None
    """The connection's reader, which reads the trials one after the other."""
    _event_thread: EventThread | None = None
    """The connection's event thread, which records the trials the reader reads."""
    _event_callbacks: _HandlerSlot[_EventCallbacks | None]
    _connection_threads: list[ReadThread | EventThread | SoftcodeThread]
    """The connection's threads, for the finalizer: it must not reference the Bpod."""
    _last_sent: _Trial | None = None
    """The trial whose state machine was sent last: the one running, queued behind the
    running one, or the last one run."""
    _undelivered: deque[_Trial]
    """Trials that :meth:`get_data` hasn't returned yet, in the order they were sent."""
    _undelivered_lock: threading.Lock
    _state_machine_started: bool = False  # fixes FlexIO channel types and rate
    _late_starts: _LateStarts
    _zmq: ServiceHost[_BpodRequestUnion, list[BpodEventUnion]]
    _next_fsm_index: int = -1

    _compiler: StateMachineCompiler  # rebuilt by _recompile_hardware_tables
    # the last compiled state machine, and the hardware hash it was compiled for
    _last_compiled: tuple[bytes, _StateMachineLookup, bytes] | None = None

    _softcode_thread: SoftcodeThread | None = None
    """The connection's softcode thread, which runs the handler for each softcode."""
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
        self._late_starts = _LateStarts()
        self._connection_threads = []
        self._undelivered = deque()
        self._undelivered_lock = threading.Lock()
        self._analog_data: SimpleQueue[pl.LazyFrame] = SimpleQueue()
        self._analog_buffer = []
        self._analog_trial_number_by_sequence = []
        self._analog_sample_index = 0
        self._analog_last_flush_ns = perf_counter_ns()

        self._softcode_slot = _HandlerSlot(None)  # the handler outlives connections
        self._event_callbacks = _HandlerSlot(None)  # filled once the Bpod is set up

        # record reference system time before opening the port: the handshake's clock
        # reset is dated against it
        self._time_reference = _TimeReferences(
            init_system_time_ns=time_ns(),
            init_perf_counter_ns=perf_counter_ns(),
            reset_system_time_ns=0,
        )

        try:
            # enumerating serial ports is slow, so it is done once: for identifying the
            # Bpod by port or serial number and for detecting its additional ports
            bpod_ports = _find_bpod_ports()
            port_info = self._identify_bpod(bpod_ports, port, serial_number)
            super().__init__(port=port_info, open_connection=True)
            self._serial_number = port_info.serial_number or 'unknown'

            # get firmware version and machine type; enforce version requirements
            self._get_version_info()
            self._rename_serial_device(
                f'Bpod Finite State Machine {self._version.machine_str}'
            )

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

            # log hardware information
            logger.info('Connected to %s on %s', self._serial_device_name, self.port)
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
            if self._version.machine == 2:
                logger.warning(
                    'Support for %s is experimental', self._serial_device_name
                )

            # start ZeroMQ service
            self._start_zmq(use_zeroconf=remote)
            logger.info('ZeroMQ service started on %s', self.address)
            self._event_callbacks.handler = _EventCallbacks(
                publish=self._zmq.publish,
                publish_gate=lambda: self._zmq.has_subscribers,
                on_trial_start=self._capture_analog_anchor,
            )

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
            self, Bpod._bpod_cleanup, self._serial, self._connection_threads
        )

    @staticmethod
    def _bpod_cleanup(
        serial: ExtendedSerial,
        connection_threads: list[ReadThread | EventThread | SoftcodeThread],
    ) -> None:
        Bpod._stop_connection_threads(connection_threads)
        with contextlib.suppress(Exception):
            if serial.is_open:
                Bpod._request_disconnect(serial)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit context and close connection.

        Leaving on an exception stops a running state machine instead of waiting for it.
        """
        self._bpod_finalizer.detach()
        try:
            if exc_type is not None:
                self._abort_trial(exc_type)
        finally:
            self._shutdown()

    def _abort_trial(self, reason: type[BaseException]) -> None:
        """Stop the running state machine when leaving on an exception.

        Failures are logged, so that the exception being handled propagates unchanged.
        """
        if not self.is_running:
            return
        logger.warning('Stopping the state machine: leaving on %s', reason.__name__)
        try:
            self.stop_state_machine()
        except Exception as e:  # noqa: BLE001 - the caller's own exception matters more
            logger.warning('Could not stop the state machine: %s', e)

    def _check_in_sync(self) -> None:
        """Refuse commands on a stream that can't be trusted, until it is reopened.

        A reader's error is left for :meth:`wait` to report.
        """
        trial = self._trial_being_read
        error = None if trial is None else trial.read_error
        if self._out_of_sync or error is not None:
            raise BpodError(
                'The connection has been out of sync since a trial was given up or '
                'could not be read: close and reopen it.'
            ) from error

    @property
    def _trial_being_read(self) -> _Trial | None:
        """The trial the connection's reader is reading, or the last one it read."""
        reader = self._read_thread
        return None if reader is None else reader.trial

    @staticmethod
    def _stop_connection_threads(
        threads: list[ReadThread | EventThread | SoftcodeThread],
    ) -> None:
        """End a connection's threads, the reader first: it feeds the other two."""
        for thread in threads:
            thread.stop()
        threads.clear()

    def _raise_reader_error(self) -> None:
        """Raise what ended the reader, once; the stream stays untrusted."""
        trial = self._trial_being_read
        if trial is None or (error := trial.read_error) is None:
            return
        trial.read_error = None  # report it once
        self._out_of_sync = True  # the Bpod may still be sending
        if isinstance(error, OSError):
            message = f'Lost connection to the Bpod during trial #{trial.number}'
        else:
            message = (
                f'Could not read trial #{trial.number} from the Bpod ({error}): close '
                'and reopen the connection'
            )
        raise BpodError(message) from error

    def _give_up(self, what: str) -> NoReturn:
        """Give the trial being read up: the Bpod did not do `what` in time.

        The reader ends with it, settling the trials queued behind it unread.
        """
        if self._read_thread is not None:
            self._read_thread.cancel()
        self._out_of_sync = True  # the Bpod may still send, or start the trial later
        raise TimeoutError(f'The Bpod did not {what}; the trial was given up')

    def _shutdown(self) -> None:
        """Close the connection, then end the IPC service."""
        try:
            self.close()
        finally:
            self._stop_zmq()

    @_requires_idle
    def open(self) -> None:
        """
        Open the connection to the Bpod, handshake and align the session clock.

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
        # the connection's threads: a reader feeding an event thread and a softcode
        # thread, all asleep until a trial is sent
        self._softcode_thread = SoftcodeThread(handler_slot=self._softcode_slot)
        self._softcode_thread.set_time_reference(self._time_reference)
        self._softcode_thread.start()
        self._event_thread = EventThread(callbacks=self._event_callbacks)
        self._event_thread.start()
        self._read_thread = ReadThread(
            serial=self.serial0,
            queue_events=self._event_thread.queue,
            queue_softcodes=self._softcode_thread.queue,
            late_starts=self._late_starts,
        )
        self._read_thread.start()
        self._connection_threads[:] = [
            self._read_thread,
            self._event_thread,
            self._softcode_thread,
        ]

    def close(self) -> None:
        """
        Close the connection to the Bpod.

        Waits for the state machines sent to finish before closing the serial port; if
        that wait is interrupted, they are stopped first. The IPC service stays
        up, so that remote clients can still reach this object; a newer :class:`Bpod`
        for the same device takes the service over.

        .. note::
            Prefer using :class:`Bpod` as a context manager, which opens and closes the
            connection automatically.

        Raises
        ------
        BpodError
            If the connection was lost during the trial, or the trial couldn't be read,
            once the port is closed.
        SerialException
            If the port could not be closed.
        """
        try:
            self.wait()
        except BaseException as e:  # don't leave the trial running on the Bpod
            self._abort_trial(type(e))
            raise
        finally:
            self._close_ports()

    def _close_ports(self) -> None:
        """Stop analog sampling, request the disconnect and close the serial ports."""
        serial0_open = hasattr(self, '_serial') and getattr(
            self._serial, 'is_open', False
        )
        # the threads end with the connection; the reader releases the port's replies
        # if it held them, so the teardown's own reads go through
        self._stop_connection_threads(self._connection_threads)
        self._read_thread = self._event_thread = self._softcode_thread = None
        if serial0_open and self._out_of_sync:
            # a given-up trial may still be running on the Bpod: stop it (and analog
            # sampling with it) and swallow everything it sends, so that the next
            # connection's handshake is answered
            with contextlib.suppress(OSError):  # the Bpod may be unplugged
                self._stop_and_drain(self._serial)
        elif serial0_open and self._analog_reader_thread is not None:
            # firmware keeps streaming FlexIO analog samples on its own serial port
            # until explicitly told to stop - even after a trial ends naturally via
            # '>exit' - so the next connection's discovery handshake would race stray
            # bytes. Read the Bpod's reply, so it isn't taken for stray bytes below.
            with (
                contextlib.suppress(SerialException),  # the Bpod may be unplugged
                self._serial.temporary_timeout(0.2),
            ):
                reply = self._serial.query(b'X', _IDLE_STOP_REPLY_SIZE)
                if not reply.startswith(_IDLE_STOP_REPLY_HEADER):
                    logger.warning('Unexpected reply to stop command: %s', reply.hex())
        if self._analog_reader_thread is not None:
            self._analog_reader_thread.stop()  # thread stopped, serial still open
            self._analog_reader_thread = None
            self._analog_recording = False
            self._flush_analog_buffer()  # samples since the last periodic flush
            self._analog_channel_names = ()  # the next connection may record none
            self._analog_sample_period_us = 0
        if self._flexio_serial is not None and self._flexio_serial.is_open:
            # discard any analog samples firmware sent (or was still sending) up to
            # the 'X' above, so a stale byte can't be misread as the next
            # connection's discovery-handshake response
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
            self._discard_stray_bytes(self._serial, 'serial0')
        try:
            super().close()
        finally:
            self._out_of_sync = False  # the next open() handshakes
            self._state_machine_started = False  # and may set up FlexIO again
            # the disconnect disabled the relays (no modules if the setup failed early)
            for module in getattr(self, 'modules', {}).values():
                module._relay_is_enabled = False  # noqa: SLF001 - same module
            self._late_starts.summarize()

    @staticmethod
    def _stop_and_drain(serial: ExtendedSerial) -> None:
        """Send the stop command, then read until the Bpod is quiet (see above)."""
        serial.write(b'X')
        deadline = monotonic() + _STOP_DRAIN_MAX_S
        with serial.temporary_timeout(_STOP_DRAIN_QUIET_S):
            while serial.read(max(1, serial.in_waiting)) and monotonic() < deadline:
                pass

    @staticmethod
    def _discard_stray_bytes(serial: ExtendedSerial, name: str) -> None:
        """Read and discard any bytes waiting on `serial`, logging how many."""
        try:
            discarded = serial.read_all()
        except OSError:  # unplugged: pyserial raises OSError, not SerialException
            return
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
            The serial ports of supported Bpods, as found by ``_find_bpod_ports()``.
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
        machine_type_str = _machine_name(machine_type, self._bcd_device, pcv_rev)
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
            ports = [
                p
                for p in _filter_ports(
                    candidates,
                    pid=[TeensyPID.DUAL_SERIAL, TeensyPID.TRIPLE_SERIAL],
                    serial_number=self._serial_number,
                )
                if p.device != self.port
            ]
            # by USB interface where known, so the secondary port comes first: COM
            # port numbers on Windows need not follow the interfaces' order
            return sorted(ports, key=lambda p: _usb_interface_number(p) or 0)

        # First, assemble a list of candidate ports: the Bpod's other USB-serial ports.
        # Right after the Bpod was plugged in, the OS may not have registered all of
        # them yet when the Bpod was identified; if so, the ports are enumerated again.
        n_other_ports: dict[int, int] = {
            TeensyPID.DUAL_SERIAL: 1,
            TeensyPID.TRIPLE_SERIAL: 2,
        }
        candidate_ports = other_ports(ports)
        if len(candidate_ports) < n_other_ports.get(self._port_info.pid, 0):
            candidate_ports = other_ports(_find_bpod_ports())

        # Then, try to find the secondary USB-serial port, which the 'Z' softcode
        # channel (SoftCodeApp) belongs to
        serial1 = None
        if b'Z' in self._hardware.input_description:
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

        # and the third USB-serial port, for the FlexIO channels (State Machine 2+)
        flexio_serial = None
        if self._hardware.n_flexio:
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
        failed = f'Handshake with {self._serial_device_name} on {self.port} failed'
        try:
            # discovery bytes the Bpod sent while unconnected, e.g. in response to the
            # port being opened, may precede its reply: skip them
            self.serial0.write(b'6')
            with self.serial0.temporary_timeout(0.2):
                reply = self.serial0.read_until(b'5')
            if reply.lstrip(DISCOVERY_BYTE) != b'5':
                raise BpodError(failed)
        except SerialException as e:
            raise BpodError(failed) from e
        finally:
            self.serial0.reset_input_buffer()
        # the handshake restarted the Bpod's session clock: align the host's reference
        if not self.reset_session_clock():
            raise BpodError(f'{failed}: no reply to the session clock reset')
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
        return self.serial0.verify()

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
        if self._softcode_thread is not None:  # not yet during the handshake
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
            Which softcode to send, 0 to one less than the number the hardware takes:
            the state machine sees it as event ``SoftCode<softcode>``.

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

        self._check_in_sync()  # allowed while running, but not on a suspect stream

        # the Bpod holds one state machine besides the running one: the one sent before
        # must have started, as this one would otherwise reset its confirmation. Before
        # compiling, so that leaving early leaves nothing behind.
        previous = self._last_sent
        if previous is not None and not previous.started.is_set():
            logger.debug('Waiting for state machine #%d to start ...', previous.number)
            # a second at most, so Ctrl+C gets through; nothing has been sent yet
            if not previous.started.wait(_START_TIMEOUT_S):
                self._give_up(
                    f'start trial #{previous.number} within {_START_TIMEOUT_S:g} s'
                )
            self._check_in_sync()  # the reader may have failed instead
        debugging = logger.isEnabledFor(logging.DEBUG)
        self._disable_all_module_relays()

        # If the user did not provide a state machine, run the last one again
        if state_machine is None:
            if self._last_compiled is None:
                raise RuntimeError('No state machine has been run yet')
            fsm_bytes, lookup, hardware_hash = self._last_compiled
            if hardware_hash != self._compiler.hardware_hash:
                raise RuntimeError(
                    'The hardware configuration has changed since the last state '
                    'machine ran - pass it to run() again'
                )
            fsm_hash = lookup.fsm_hash
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
                fsm_bytes, lookup = self._compiler.compile(
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
                lookup,
                self._compiler.hardware_hash,
            )

        # count the trial only now, so that a rejected call doesn't use up its number
        self._next_fsm_index = trial_number

        # Send state machine to Bpod; always queue for immediate back-to-back execution.
        if not self._state_machine_started:
            # FlexIO channel types and the sampling rate are fixed from here on,
            # and the Bpod streams analog samples from the first trial on
            self._start_analog_reader()
        if self._analog_first_trial_time_us is None:
            # the Bpod (re)starts analog sampling with the first trial after a
            # session clock reset
            self._analog_recording = self._analog_reader_thread is not None
        if (reader := self._read_thread) is None:
            raise BpodError('Cannot run a state machine: the connection is closed')
        serial0 = self.serial0
        trial = _Trial(
            number=trial_number,
            lookup=lookup,
            recorder=_Recorder(
                trial=trial_number,
                fsm=lookup,
                event_tables=self._compiler.event_tables,
            ),
            time_reference=self._time_reference,
            cycle_period_us=self._hardware.cycle_period_us,
            queued=self.is_running,  # so due a cycle after the trial before
        )
        with self._undelivered_lock:
            self._undelivered.append(trial)
        self._state_machine_started = True
        message = struct.pack('<c?', b'C', b'\x01') + fsm_bytes
        # the reader will stream the port's replies: take them for it, unless it holds
        # them for the running trial still. Queue the trial under the same lock the
        # reader checks its queue under at a trial's end, so it keeps them for this one.
        # The Bpod may start the trial any time after the write: register it first, so
        # that is_running has no gap.
        handed = False  # to the reader
        try:
            with serial0.write_lock:
                try:
                    claimed = serial0.claim_replies()
                except PortBusyError as e:
                    raise BpodError(
                        'Cannot run a state machine while another thread is using '
                        'the Bpod'
                    ) from e
                try:  # the reader may have died meanwhile, releasing the replies
                    self._check_in_sync()
                except BpodError:
                    if claimed:
                        serial0.read_lock.release()
                    raise
                self._last_sent = trial
                reader.trials.put(trial)
                handed = True
                # record the trial number before the Bpod can start the state machine:
                # analog samples only carry the firmware's positional trial counter,
                # so decoding looks the number up by position from the trial's first
                # sample on
                self._analog_trial_number_by_sequence.append(trial_number)
                if debugging:
                    t0 = perf_counter_ns()
                    serial0.write(message)
                    logger.debug(
                        'Sent state machine %s to Bpod (%d μs)',
                        fsm_hash.hex(),
                        (perf_counter_ns() - t0) // 1000,
                    )
                else:
                    serial0.write(message)
        except BaseException:
            if handed:
                # the Bpod may hold part of the machine, and the reader waits for a
                # trial that won't come: the connection is out of sync until reopened
                self._out_of_sync = True
                reader.cancel()  # not under the write lock: the reader's end takes it
            self._drop(trial)
            raise

    def _drop(self, trial: _Trial) -> None:
        """Give up a trial that wasn't sent, or not in full: get_data() skips it."""
        with self._undelivered_lock, contextlib.suppress(ValueError):
            self._undelivered.remove(trial)  # unless a get_data() took it already
        trial.finish_unread()

    def _capture_analog_anchor(self, time_us: int) -> None:
        """Capture the first trial's start time as the FlexIO analog-sample anchor."""
        if self._analog_first_trial_time_us is None:
            self._analog_first_trial_time_us = time_us

    @property
    def is_running(self) -> bool:
        """Check if a state machine was sent and hasn't ended: it runs, or will."""
        trial = self._last_sent  # snapshot for thread-safety
        return trial is not None and not trial.ended.is_set()

    @property
    def is_queued(self) -> bool:
        """Check if a state machine was sent and hasn't started yet.

        The Bpod holds one state machine besides the running one: a machine sent
        while another runs is queued behind it until that one ends, and :meth:`run`
        would wait for it to start.
        """
        trial = self._last_sent  # snapshot for thread-safety
        return trial is not None and not trial.started.is_set()

    @property
    def is_ready(self) -> bool:
        """Check if the Bpod takes a state machine right away: :meth:`run` won't wait.

        See :attr:`is_queued`.
        """
        return not self.is_queued

    def wait(self, timeout: float | None = None) -> None:
        """
        Wait for every state machine sent to finish: the running one and a queued one.

        Blocks until their events have been recorded and published and the softcodes
        received so far have been handled. If no state machine is running, this method
        returns immediately.

        Parameters
        ----------
        timeout : float or None, default: None
            Seconds to wait before giving up. ``None`` waits indefinitely.

        Raises
        ------
        TimeoutError
            If the state machine hasn't finished within `timeout` seconds.
        BpodError
            If the connection to the Bpod was lost during the trial, or the trial
            couldn't be read; the connection then has to be reopened. The events
            received up to then are kept.
        """
        deadline = None if timeout is None else monotonic() + timeout
        trial = self._last_sent  # snapshot for thread-safety
        if trial is not None:
            if not trial.ended.is_set():
                logger.debug(
                    'Waiting for state machine #%d to finish ...', trial.number
                )
            # the trials are recorded in order: once this one is, all before it are
            for finished in (trial.ended, trial.decoded):
                if not wait_until(finished, deadline):
                    raise TimeoutError(
                        f'Trial #{trial.number} did not finish within {timeout:g} s'
                    )
        softcode_thread = self._softcode_thread  # None once the connection is closed
        if softcode_thread is not None and not softcode_thread.drain(deadline):
            raise TimeoutError(f'Softcode handlers still busy after {timeout:g} s')

        self._raise_reader_error()

    def peek_data(self, trigger_states: Collection[str] | None = None) -> pl.DataFrame:
        """Return a snapshot of the current or, between trials, the last trial's data.

        Parameters
        ----------
        trigger_states : Collection of str, optional
            Block until at least one of the given states has been entered, then return
            the snapshot. If ``None`` (default), returns immediately.

        Returns
        -------
        DataFrame
            Events recorded so far in the current trial, or those of the last trial if
            none is running. Returns an empty DataFrame if no trial has run yet.

        Raises
        ------
        ValueError
            If one or several of the trigger states are not part of the state machine.
        """
        # before the connection's first trial: the last one run
        trial = self._trial_being_read or self._last_sent
        if trial is None:
            return pl.DataFrame(schema=_TRIAL_DATA_SCHEMA)
        return trial.recorder.peek(trigger_states)

    @override
    def get_data(self, *, concat: bool = True) -> pl.DataFrame:
        while True:
            with self._undelivered_lock:
                if not self._undelivered:
                    raise BpodError('No trial data available')
                first = self._undelivered[0]
                if first.decoded.is_set():
                    trials = self._pop_decoded(concat=concat)
                    break
            wait_until(first.decoded, None)  # then check again: another may take it
        frames = [trial.recorder.to_frame() for trial in trials]
        return frames[0] if len(frames) == 1 else pl.concat(frames)

    def _pop_decoded(self, *, concat: bool) -> list[_Trial]:
        """Pop the first undelivered trial and, with `concat`, decoded ones after it.

        Raises the first failed trial's error instead: all the others stay queued. The
        first trial must be decoded, and the caller must hold the lock.
        """
        decoded = takewhile(lambda trial: trial.decoded.is_set(), self._undelivered)
        trials = list(decoded) if concat else [next(decoded)]
        for trial in trials:
            if (error := trial.decode_error) is not None:
                self._undelivered.remove(trial)
                raise error
        for _ in trials:
            self._undelivered.popleft()
        return trials

    def get_analog_data(self, *, concat: bool = True) -> pl.DataFrame:
        """
        Return FlexIO analog-input data from the data queue.

        Parameters
        ----------
        concat : bool, default: True
            If ``True``, pop and concatenate all DataFrames currently in the queue into
            a single DataFrame, blocking until at least one is available.
            If ``False``, pop and return one DataFrame, blocking until one is
            available.

        Returns
        -------
        DataFrame
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
        frames = [self._analog_data.get()]
        if concat:
            with contextlib.suppress(Empty):
                while True:
                    frames.append(self._analog_data.get(block=False))
        data = (
            frames[0] if len(frames) == 1 else pl.concat(frames, how='diagonal_relaxed')
        )
        # the in-memory engine avoids the streaming engine's start-up cost (polars 2
        # picks streaming by default)
        return data.collect(engine='in-memory')

    @override
    def stop_state_machine(self, timeout: float | None = 1.0) -> None:
        if not self.is_running:
            return
        if self._analog_channel_names:
            logger.warning(
                'Stopping the state machine also stops FlexIO analog sampling and '
                'threshold detection until reset_session_clock() is called and the '
                'next state machine starts'
            )
        logger.debug('Stopping state machine')
        # the running trial, and the one queued behind it, which the Bpod starts as
        # soon as the running one is stopped: each gets its own stop once it runs. A
        # stop sent earlier would be answered as by an idle Bpod, and the trial run
        # anyway: the firmware starts a loaded machine regardless.
        with self._undelivered_lock:  # in the order they were sent
            trials = [t for t in self._undelivered if not t.ended.is_set()]
        for trial in trials:
            deadline = None if timeout is None else monotonic() + timeout
            if not wait_until(trial.started, deadline):
                self._give_up(f'start trial #{trial.number} within {timeout:g} s')
            if trial.ended.is_set():  # meanwhile, e.g. by the stop meant for the one
                continue  # before: a stop now would be answered as by an idle Bpod
            self.serial0.write(b'X')
            self._analog_recording = False  # the Bpod stops analog sampling on 'X'
            if not wait_until(trial.ended, deadline):
                # its data stays incomplete
                self._give_up(
                    f'end trial #{trial.number} within {timeout:g} s of the stop '
                    'command'
                )

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

        A state's ``SoftCode`` action sends its value, 1 to 255. A global timer on the
        softcode channel sends its ``value_on`` and ``value_off``, 0 included.

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
            return f'{self.name} on module port {self.index + 1}'
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


_REMOTE_REPLY_MARGIN_S = 1.0
"""Extra wait for a reply beyond the remote method's own timeout."""

_REMOTE_ERRORS: dict[str, type[Exception]] = {
    cls.__name__: cls for cls in (BpodError, RuntimeError, TimeoutError, ValueError)
}
"""Errors of the host's methods that a :class:`RemoteBpod` raises as the same type."""


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
        with contextlib.suppress(TimeoutError):  # a dead host must not hang close()
            self._zmq.request(request_data=_RequestBye(), reply_type=str, timeout=1.0)
        self._zmq.close()

    def _remote_call(
        self,
        method: str,
        *args: Any,
        reply_timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """
        Perform a remote procedure call.

        Parameters
        ----------
        method : str
            The name of the remote method to invoke.
        *args : Any
            Positional arguments to pass to the remote method.
        reply_timeout : float, optional
            Seconds to wait for the host's reply; ``None`` waits indefinitely.
        **kwargs : Any
            Keyword arguments to pass to the remote method.

        Returns
        -------
        Any
            The result returned from the remote method.

        Raises
        ------
        BpodError, RuntimeError, TimeoutError, ValueError
            As raised by the remote method.
        TimeoutError
            If the host doesn't reply within `reply_timeout` seconds.
        """
        try:
            return self._zmq.request(
                request_data=_RequestCall(method_name=method, args=args, kwargs=kwargs),
                reply_type=_ReplyGeneric,
                timeout=reply_timeout,
            ).value
        except RemoteError as e:
            self._raise_local(e)

    @staticmethod
    def _raise_local(error: RemoteError) -> NoReturn:
        """Raise the host's error as the matching local type, else the RemoteError."""
        if (cls := _REMOTE_ERRORS.get(error.original_error.name)) is not None:
            raise cls(error.original_error.message) from error
        raise error

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

    @override
    def get_data(self, *, concat: bool = True) -> pl.DataFrame:
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
            self._raise_local(e)

        return pl.read_ipc(reply)

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
        return self._remote_call('reset_session_clock') is True

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
    def stop_state_machine(self, timeout: float | None = 1.0) -> None:
        self._remote_call(
            'stop_state_machine',
            timeout=timeout,
            # the host's own TimeoutError arrives before the reply times out
            reply_timeout=None if timeout is None else timeout + _REMOTE_REPLY_MARGIN_S,
        )

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
    ports = _find_bpod_ports(device=port, serial_number=serial_number)
    for p in _discover_bpod_ports(ports):
        yield BpodInfo(port=p.device, serial_number=p.serial_number or 'unknown')


def _machine_name(machine_type: int, teensy: int | None, pcb: int | None) -> str:
    """Name a Bpod as precisely as its machine type, Teensy model and PCB tell."""
    if machine_type == 3 and teensy == TeensyModel.TEENSY_3_6:
        if pcb == 0:
            return 'r2.0-2.3'
        return 'r2.4' if pcb == 4 else 'r2.0-2.4'
    if machine_type == 3 and teensy == TeensyModel.TEENSY_4_1:
        return 'r2.5'
    if machine_type == 4 and pcb in range(4):
        return f'2+ r1.{pcb}'
    return MACHINE_TYPES.get(machine_type, 'unknown')


def _find_bpod_ports(
    device: str | None = None, serial_number: str | None = None
) -> list[ListPortInfo]:
    """The serial ports of supported Bpods, by USB vendor and product ID together.

    Matching them separately would also open, e.g., an Arduino with a Teensy product ID.
    """
    ports = find_ports(device=device, serial_number=serial_number)
    return [p for p in ports if (p.vid, p.pid) in USB_IDS_BPOD]


def _discover_bpod_ports(ports: Iterable[ListPortInfo]) -> Iterator[ListPortInfo]:
    """Yield the serial ports in `ports` on which an idle Bpod answers a discovery.

    Only a Bpod's first serial port, on USB interface 0, sends discovery bytes. Its
    other ports are skipped without being opened: by their interface number where the
    platform reports it, else once the Bpod has answered on one of its ports. Ports of
    Teensy models no Bpod is built on are skipped too.
    """
    found: set[str] = set()  # serial numbers of the Bpods that answered
    for p in ports:
        if (
            _usb_interface_number(p)  # 0 or None: probe
            or p.serial_number in found
            or (p.vid == VID_TEENSY and _read_bcd_device(p) in NON_BPOD_TEENSY_MODELS)
        ):
            continue
        # an unconnected Bpod sends its discovery byte at regular intervals, and recent
        # firmware also when DTR is asserted
        if verify_serial_discovery(
            port=p.device,
            expected_message=DISCOVERY_BYTE,
            timeout=DISCOVERY_TIMEOUT,
            raise_dtr=True,
        ):
            if p.serial_number:
                found.add(p.serial_number)
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
