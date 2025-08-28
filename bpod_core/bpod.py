"""Module for interfacing with the Bpod Finite State Machine."""

import logging
import re
import struct
import threading
import traceback
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, NamedTuple, cast

import msgspec
import numpy as np
from numpy.typing import NDArray
from pydantic import validate_call
from serial import SerialException
from serial.tools.list_ports import comports
from typing_extensions import Self

from bpod_core import __version__ as bpod_core_version
from bpod_core.com import ExtendedSerial
from bpod_core.fsm import StateMachine
from bpod_core.ipc import DualChannelClient, DualChannelHost
from bpod_core.misc import SettingsDict, suggest_similar

PROJECT_NAME = 'bpod-core'
AUTHOR_NAME = 'International Brain Laboratory'
VENDOR_IDS_BPOD = [0x16C0]  # vendor IDs of supported Bpod devices
MIN_BPOD_FW_VERSION = (23, 0)  # minimum supported firmware version (major, minor)
MIN_BPOD_HW_VERSION = 3  # minimum supported hardware version
MAX_BPOD_HW_VERSION = 4  # maximum supported hardware version
CHANNEL_TYPES_INPUT = {
    b'U': 'Serial',
    b'X': 'SoftCode',
    b'Z': 'SoftCodeApp',
    b'F': 'Flex',
    b'D': 'Digital',
    b'B': 'BNC',
    b'W': 'Wire',
    b'P': 'Port',
}
CHANNEL_TYPES_OUTPUT = CHANNEL_TYPES_INPUT.copy()
CHANNEL_TYPES_OUTPUT.update({b'V': 'Valve', b'P': 'PWM'})
N_SERIAL_EVENTS_DEFAULT = 15
VALID_OPERATORS = ['exit', '>exit', '>back']
MACHINE_TYPES = {3: 'r2.0-2.5', 4: '2+ r1.0'}

logger = logging.getLogger(__name__)


class DeviceSettings(msgspec.Struct):
    """Settings for a specific Bpod device."""

    serial_number: str
    """Serial number of the device."""
    name: str = ''
    """User-defined name of the device."""
    location: str = ''
    """User-defined location of the device."""
    zmq_port_pub: int | None = None
    """Port number for the ZeroMQ PUB service"""
    zmq_port_rep: int | None = None
    """Port number for the ZeroMQ REP service"""


class VersionInfo(msgspec.Struct, frozen=True):
    """Represents the Bpod's on-board hardware configuration."""

    firmware: tuple[int, int]
    """Firmware version (major, minor)"""
    machine: int
    """Machine type (numerical)"""
    machine_str: str
    """Machine type (string)"""
    pcb: int | None
    """PCB revision, if applicable"""

    def _to_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.__struct_fields__}


class HardwareConfiguration(msgspec.Struct, frozen=True):
    """Represents the Bpod's on-board hardware configuration."""

    max_states: int
    """Maximum number of supported states in a single state machine description."""
    cycle_period: int
    """Period of the state machine's refresh cycle during a trial in microseconds."""
    max_serial_events: int
    """Maximum number of behavior events allocatable among connected modules."""
    max_bytes_per_serial_message: int
    """Maximum number of bytes allowed per serial message."""
    n_global_timers: int
    """Number of global timers supported."""
    n_global_counters: int
    """Number of global counters supported."""
    n_conditions: int
    """Number of condition-events supported."""
    n_inputs: int
    """Number of input channels."""
    input_description: bytes
    """Array indicating the state machine's onboard input channel types."""
    n_outputs: int
    """Number of channels in the state machine's output channel description array."""
    output_description: bytes
    """Array indicating the state machine's onboard output channel types."""
    cycle_frequency: int
    """Frequency of the state machine's refresh cycle during a trial in Hertz."""
    n_modules: int
    """Number of modules supported by the state machine."""


class BpodError(Exception):
    """
    Exception class for Bpod-related errors.

    This exception is raised when an error specific to the Bpod device or its
    operations occurs.
    """


class FSMThread(threading.Thread):
    """A thread for managing the execution of a finite state machine on the Bpod."""

    _struct_start = struct.Struct('<Q')
    _struct_cycles = struct.Struct('<I')
    _struct_exit = struct.Struct('<IQ')

    def __init__(  # noqa: PLR0913
        self,
        serial: ExtendedSerial,
        fsm_index: int,
        confirm_fsm: bool,
        cycle_period: int,
        softcode_handler: Callable,
        state_transitions: NDArray[np.uint8],
        use_back_op: bool,
    ) -> None:
        """
        Initialize the FSMThread.

        Parameters
        ----------
        serial : ExtendedSerial
            The serial connection to the Bpod device.
        fsm_index : int
            The index of the FSM being managed.
        confirm_fsm : bool
            Whether to confirm the FSM with the Bpod device.
        cycle_period : int
            The cycle period of the Bpod device in microseconds.
        softcode_handler : Callable
            A handler function for processing softcodes.
        state_transitions : np.ndarray
            The state transition matrix
        use_back_op : bool
            Whether the state machine makes use of the `>back` operator
        """
        super().__init__()
        self.daemon = True
        self.serial = serial
        self._stop_event = threading.Event()
        self._index = fsm_index
        self._confirm_fsm = confirm_fsm
        self._cycle_period = cycle_period
        self._softcode_handler = softcode_handler
        self._state_transitions = state_transitions
        self._use_back_op = use_back_op

    def stop(self):
        self._stop_event.set()

    def run(self) -> None:
        """Execute the FSMThread."""
        # assign members to local variables to avoid repeated attribute lookups
        serial = self.serial
        index = self._index
        cycle_period = self._cycle_period
        struct_cycles = self._struct_cycles
        softcode_handler = self._softcode_handler
        state_transitions = self._state_transitions
        previous_state = np.uint8(0)
        current_state = np.uint8(0)
        target_exit = np.uint8(state_transitions.shape[0])
        target_back = np.uint8(255)
        use_back_op = self._use_back_op

        # create buffers for repeated serial reads
        opcode_buf = bytearray(2)  # buffer for opcodes
        event_data_buf = bytearray(259)  # max 255 events + 4 bytes for n_cycles

        # should we use debug logging?
        debug = logger.isEnabledFor(logging.DEBUG)

        # confirm the state machine
        if self._confirm_fsm:
            if serial.read(1) != b'\x01':
                raise RuntimeError(f'State machine #{index} was not confirmed by Bpod')
            if debug:
                logger.debug('State machine #%d confirmed by Bpod', index)

        # read the start time of the state machine (uInt64)
        t0 = self._struct_start.unpack(serial.read(8))[0]
        if debug:
            logger.debug('%d µs: Starting state machine #%d', t0, index)
            logger.debug('%d µs: State %d', t0, current_state)
        # TODO: handle start of state machine
        # TODO: handle start of state

        # enter the reading loop
        while not self._stop_event.is_set():
            # read the next two opcodes
            serial.readinto(opcode_buf)
            opcode, param = opcode_buf

            if opcode == 1:  # handle events
                # read `param` event bytes + 4 bytes for n_cycles (uInt32)
                event_data_view = memoryview(event_data_buf)[: param + 4]
                serial.readinto(event_data_view)

                # unpack the number of cycles, calculate the event's timestamp
                n_cycles = struct_cycles.unpack_from(event_data_view, param)[0]
                micros = t0 + n_cycles * cycle_period

                # handle each event
                events = event_data_view[:param]
                for event in events:
                    if debug:
                        logger.debug('%d µs: Event %d', micros, event)
                    # TODO: handle event

                # handle state transitions / exit event
                for event in events:
                    if event == 255:  # exit event
                        self.stop()
                        break
                    target_state = state_transitions[current_state][event]
                    if target_state == current_state:  # no transition
                        continue
                    if target_state == target_exit:  # virtual exit state
                        # TODO: handle end of state
                        break
                    if target_state == target_back and use_back_op:  # back
                        target_state = previous_state
                    # TODO: handle end of state
                    previous_state = current_state
                    current_state = target_state
                    # TODO: handle start of state
                    if debug:
                        logger.debug('%d µs: State %d', micros, current_state)
                    break  # only handle the first state transition

            elif opcode == 2:  # handle softcodes
                if debug:
                    logger.debug('Softcode %d', param)
                softcode_handler(param)

            else:
                raise RuntimeError(f'Unknown opcode: {opcode}')

        # exit state machine
        # read 12 bytes: cycles (uInt32) and micros (uInt64)
        cycles, micros = self._struct_exit.unpack(serial.read(12))
        if debug:
            logger.debug(
                '%d µs: Ending state machine #%d (%d cycles)', micros, index, cycles
            )
        # TODO: handle end of state machine


class AbstractBpod:
    _version: VersionInfo
    _hardware: HardwareConfiguration

    @property
    def version(self) -> VersionInfo:
        """Version information of the Bpod's firmware and hardware."""
        return self._version


class Bpod(AbstractBpod):
    """Bpod class for interfacing with the Bpod Finite State Machine."""

    _settings: SettingsDict
    _name: str | None
    _fsm_thread: FSMThread | None = None
    _zmq_service: DualChannelHost
    _next_fsm_index: int = -1
    _serial_buffer = bytearray()  # buffer for TrialReader thread
    serial0: ExtendedSerial
    """Primary serial device for communication with the Bpod."""
    serial1: ExtendedSerial | None = None
    """Secondary serial device for communication with the Bpod."""
    serial2: ExtendedSerial | None = None
    """Tertiary serial device for communication with the Bpod - used by Bpod 2+ only."""
    inputs: NamedTuple
    """Available input channels."""
    outputs: NamedTuple
    """Available output channels."""
    modules: NamedTuple
    """Available modules."""
    event_names: list[str]
    """List of event names."""
    actions: list[str]
    """List of output actions."""

    @validate_call
    def __init__(
        self, port: str | None = None, serial_number: str | None = None
    ) -> None:
        self._finalizer = weakref.finalize(self, self._finalize)
        logger.info('bpod_core %s', bpod_core_version)
        self._settings = SettingsDict(PROJECT_NAME, AUTHOR_NAME)

        # initialize members
        self.event_names = []
        self.actions = []
        self._waiting_for_confirmation = False
        self._state_transitions: NDArray[np.uint8] = np.empty((0, 255), dtype=np.uint8)
        self._use_back_op = False

        # identify Bpod by port or serial number
        port, self._serial_number = self._identify_bpod(port, serial_number)

        # open primary serial port
        self.serial0 = ExtendedSerial()
        self.serial0.port = port
        self.open()

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
        self._start_zmq()

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
        # logger.info(
        #     'ZeroMQ service started on %s:%d',
        #     self._zmq_service.bind_address,
        #     self._zmq_service.port,
        # )

    def __enter__(self) -> Self:
        """Enter context."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit context and close connection."""
        self.close()
        self._stop_zmq()

    def open(self) -> None:
        """
        Open the connection to the Bpod.

        Raises
        ------
        SerialException
            If the port could not be opened.
        BpodException
            If the handshake fails.
        """
        if self.serial0.is_open:
            return
        self.serial0.open()
        self._handshake()

    def close(self) -> None:
        """Close the connection to the Bpod."""
        self.stop_state_machine()
        if hasattr(self, 'serial0') and self.serial0.is_open:
            logger.debug('Closing connection to Bpod on %s', self.port)
            self.serial0.write(b'Z')
            self.serial0.close()

    def _finalize(self) -> None:
        self.close()
        self._stop_zmq()

    def _zmq_handler(self, message: dict) -> dict[str, Any]:
        msg_type = message.get('type')
        if msg_type == 'call':
            method_name = message.get('method', '')
            args = message.get('args', ())
            kwargs = message.get('kwargs', {})
            try:
                method = getattr(self, method_name)
                result = method(*args, **kwargs)
                response = {'success': True, 'result': result}
            except Exception as e:
                print(message)
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
                'bpod-core': bpod_core_version,
                'version': self._version._to_dict(),
            }
        else:
            response = {
                'success': False,
                'error': f'Unknown message type: {msg_type}',
            }
        return response

    def _start_zmq(self):
        port_pub = self._get_setting(['devices', self._serial_number, 'port_pub'])
        port_rep = self._get_setting(['devices', self._serial_number, 'port_rep'])
        self._zmq_service = DualChannelHost(
            service_name=self.name if self.name else f'bpod_{self._serial_number}',
            service_type='_bpod',
            txt_record={
                'description': f'Bpod Finite State Machine {self.version.machine_str}',
                'serial': self._serial_number or '',
                'name': self.name or '',
                'location': self.location or '',
                'firmware': '.'.join([str(x) for x in self.version.firmware]),
                'core': bpod_core_version,
            },
            event_handler=self._zmq_handler,
            port_pub=cast('int | None', port_pub),
            port_rep=cast('int | None', port_rep),
        )
        self._set_setting(
            ['devices', self._serial_number, 'port_pub'],
            self._zmq_service.pub_tcp_port,
        )
        self._set_setting(
            ['devices', self._serial_number, 'port_rep'],
            self._zmq_service.rep_tcp_port,
        )

    def _stop_zmq(self):
        if hasattr(self, '_zmq_service'):
            self._zmq_service.close()

    def _get_setting(self, keys: list[str], default: Any = None) -> Any:
        return self._settings.get_nested(keys, default)

    def _set_setting(self, keys: list[str], value: Any = None) -> None:
        self._settings.set_nested(keys, value)

    def _sends_discovery_byte(
        self,
        port: str,
        byte: bytes = b'\xde',
        timeout: float = 0.11,
        trigger: bytes | None = None,
    ) -> bool:
        r"""Check if the device on the given port sends a discovery byte.

        Parameters
        ----------
        port : str
            The name of the serial port to check (e.g., '/dev/ttyUSB0' or 'COM3').
        byte : bytes, optional
            The discovery byte to expect from the device. Defaults to b'\\xde'.
        timeout : float, optional
            Timeout period (in seconds) for the serial read operation. Defaults to 0.11.
        trigger : bytes, optional
            An optional command to send on serial0 before reading from the given device.

        Returns
        -------
        bool
            Whether the given device responded with the expected discovery byte or not.
        """
        try:
            with ExtendedSerial(port, timeout=timeout) as ser:
                if trigger is not None and getattr(self, 'serial0', None) is not None:
                    self.serial0.write(trigger)
                return ser.read(1) == byte
        except SerialException:
            return False

    def _identify_bpod(
        self,
        port: str | None = None,
        serial_number: str | None = None,
    ) -> tuple[str, str | None]:
        """
        Try to identify a supported Bpod based on port or serial number.

        If neither port nor serial number are provided, this function will attempt to
        detect a supported Bpod automatically.

        Parameters
        ----------
        port : str | None, optional
            The port of the device.
        serial_number : str | None, optional
            The serial number of the device.

        Returns
        -------
        str
            the port of the device
        str | None
            the serial number of the device

        Raises
        ------
        BpodError
            If no Bpod is found or the indicated device is not supported.
        """
        # If no port or serial number provided, try to automagically find an idle Bpod
        if port is None and serial_number is None:
            try:
                port_info = next(
                    p
                    for p in comports()
                    if getattr(p, 'vid', None) in VENDOR_IDS_BPOD
                    and self._sends_discovery_byte(p.device)
                )
            except StopIteration as e:
                raise BpodError('No available Bpod found') from e
            return port_info.device, port_info.serial_number

        # If a serial number was provided, try to match it with a serial device
        if serial_number is not None:
            try:
                port_info = next(
                    p
                    for p in comports()
                    if p.serial_number == serial_number
                    and self._sends_discovery_byte(p.device)
                )
            except (StopIteration, AttributeError) as e:
                raise BpodError(f'No device with serial number {serial_number}') from e

        # Else, assure that the provided port exists and the device could be a Bpod
        else:
            try:
                port_info = next(p for p in comports() if p.device == port)
            except (StopIteration, AttributeError) as e:
                raise BpodError(f'Port not found: {port}') from e

        if port_info.vid not in VENDOR_IDS_BPOD:
            raise BpodError('Device is not a supported Bpod')
        return port_info.device, port_info.serial_number

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
        v_minor = self.serial0.query_struct(b'f', '<H')[0] if v_major > 22 else 0
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
        pcv_rev = self.serial0.query_struct(b'v', '<B')[0] if v_major > 22 else None
        self._version = VersionInfo(v_firmware, machine_type, machine_type_str, pcv_rev)

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
        cycle_frequency = 1000000 // hardware_conf[1]  # cycle_period is at index 1
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
        candidate_ports = [
            p.device
            for p in comports()
            if p.serial_number == self._serial_number and p.device != self.port
        ]

        # Exclude those devices from the list that are already sending a discovery byte
        # NB: this should not be necessary, as we already filter for devices with
        #     identical USB serial number.
        # for port in candidate_ports:
        #     if self._sends_discovery_byte(port):
        #         candidate_ports.remove(port)

        # Find secondary USB-serial port
        if self._version.firmware >= (23, 0):
            for port in candidate_ports:
                if self._sends_discovery_byte(port, bytes([222]), trigger=b'{'):
                    self.serial1 = ExtendedSerial()
                    self.serial1.port = port
                    candidate_ports.remove(port)
                    logger.debug('Detected secondary USB-serial port: %s', port)
                    break
            if self.serial1 is None:
                raise BpodError('Could not detect secondary serial port')

        # State Machine 2+ uses a third USB-serial port for FlexIO
        if self.version.machine == 4:
            for port in candidate_ports:
                if self._sends_discovery_byte(port, bytes([223]), trigger=b'}'):
                    self.serial2 = ExtendedSerial()
                    self.serial2.port = port
                    logger.debug('Detected tertiary USB-serial port: %s', port)
                    break
            if self.serial2 is None:
                raise BpodError('Could not detect tertiary serial port')

    def _handshake(self) -> None:
        """
        Perform a handshake with the Bpod.

        Raises
        ------
        BpodException
            If the handshake fails.
        """
        try:
            self.serial0.timeout = 0.2
            if not self.serial0.verify(b'6', b'5'):
                raise BpodError(f'Handshake with device on {self.port} failed')
            self.serial0.timeout = None
        except SerialException as e:
            raise BpodError(f'Handshake with device on {self.port} failed') from e
        finally:
            self.serial0.reset_input_buffer()
        logger.debug('Handshake with Bpod on %s successful', self.port)

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
        logger.debug('Resetting session clock')
        return self.serial0.verify(b'*')

    def _disable_all_module_relays(self) -> None:
        for module in self.modules:
            module.set_relay(False)

    def _compile_event_names(self) -> None:
        """Compile the list of event names supported by the Bpod hardware."""
        n_serial_events = sum(len(m.event_names) for m in self.modules)
        n_softcodes = self._hardware.max_serial_events - n_serial_events
        n_usb = self._hardware.input_description.count(b'X')
        n_usb_ext = self._hardware.input_description.count(b'Z')
        n_softcodes_per_usb = n_softcodes // (n_usb + n_usb_ext)
        n_app_softcodes = n_usb_ext * n_softcodes_per_usb
        self.event_names = []

        # Compile actions for output channels
        counters = dict.fromkeys(CHANNEL_TYPES_INPUT, 0)
        for io_key in [bytes([x]) for x in self._hardware.input_description]:
            name = CHANNEL_TYPES_INPUT[io_key]
            if io_key == b'U':  # Serial
                names = self.modules[counters[io_key]].event_names
            elif io_key == b'X':  # SoftCode
                names = (f'{name}{i + 1}' for i in range(n_softcodes_per_usb))
            elif io_key == b'Z':  # SoftCodeApp
                names = (f'{name}{i + 1}' for i in range(n_app_softcodes))
            elif io_key == b'F':  # Flex
                names = (f'{name}{counters[io_key] + 1}_{i + 1}' for i in range(2))
            elif io_key in b'PBW':  # Port, BNC, Wire
                names = (f'{name}{counters[io_key] + 1}_{s}' for s in ('High', 'Low'))
            else:
                continue
            self.event_names.extend(names)
            counters[io_key] += 1

        # Add global timers, global counters, conditions and 'Tup'
        for event_name, n in [
            ('GlobalTimer{}_Start', self._hardware.n_global_timers),
            ('GlobalTimer{}_End', self._hardware.n_global_timers),
            ('GlobalCounter{}_End', self._hardware.n_global_counters),
            ('Condition{}', self._hardware.n_conditions),
        ]:
            self.event_names.extend(event_name.format(i + 1) for i in range(n))
        self.event_names.append('Tup')

    def _compile_output_actions(self) -> None:
        """Compile the list of output actions supported by the Bpod hardware."""
        self.actions = []

        # Compile actions for output channels
        counters = dict.fromkeys(CHANNEL_TYPES_OUTPUT, 0)
        for io_key in [bytes([x]) for x in self._hardware.output_description]:
            if io_key == b'U':  # Serial
                name = self.modules[counters[io_key]].name
            elif io_key in b'XZ':  # SoftCode, SoftCodeApp
                name = CHANNEL_TYPES_OUTPUT[io_key]
            elif io_key in b'FVPBW':  # Flex, Valve, PWM, BNC, Wire
                name = f'{CHANNEL_TYPES_OUTPUT[io_key]}{counters[io_key] + 1}'
            else:
                continue
            self.actions.append(name)
            counters[io_key] += 1

        # Add output actions for global timers, global counters and analog thresholds
        self.actions.extend(
            ['GlobalTimerTrig', 'GlobalTimerCancel', 'GlobalCounterReset'],
        )
        if self.version.machine == 4:
            self.actions.extend(['AnalogThreshEnable', 'AnalogThreshDisable'])

    @property
    def port(self) -> str | None:
        """The port of the Bpod's primary serial device."""
        return self.serial0.port

    @validate_call
    def set_status_led(self, enabled: bool) -> bool:
        """
        Enable or disable the Bpod's status LED.

        Parameters
        ----------
        enabled : bool
            True to enable the status LED, False to disable.

        Returns
        -------
        bool
            True if the operation was successful, False otherwise.
        """
        self.serial0.write_struct('<c?', b':', enabled)
        return self.serial0.verify(b'')

    def update_modules(self) -> None:
        """Update the list of connected modules and their configurations."""
        # self._disable_all_module_relays()
        self.serial0.write(b'M')
        modules = []
        for idx in range(self._hardware.n_modules):
            # check connection state
            if not (is_connected := self.serial0.read_struct('<?')[0]):
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
                        n_events = self.serial0.read_struct('<B')[0]
                    case b'E':
                        n_event_names = self.serial0.read_struct('<B')[0]
                        for _ in range(n_event_names):
                            n_chars = self.serial0.read_struct('<B')[0]
                            event_name = self.serial0.read_struct(f'<{n_chars}s')[0]
                            custom_event_names.append(event_name.decode('UTF8'))
                more_info = self.serial0.read_struct('<?')[0]

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

        # create NamedTuple and store as class attribute
        self.modules = NamedTuple('modules', [(m.name, Module) for m in modules])._make(
            modules,
        )

        # update event names and output actions
        self._compile_event_names()
        self._compile_output_actions()

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
        self.send_state_machine(state_machine, validate_only=True)

    @validate_call(config={'arbitrary_types_allowed': True})
    def send_state_machine(
        self,
        state_machine: StateMachine,
        *,
        run_asap: bool = False,
        validate_only: bool = False,
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
        validate_only : bool, optional
            If True, the state machine is only validated and not sent to the device.
            Default is False.

        Raises
        ------
        ValueError
            If the state machine is invalid or exceeds hardware limitations.
        :exc:`~validate_call.roar.validate_callCallHintViolation`
            If function arguments don’t match type hints.
        """
        # Disable all active module relays
        if not validate_only:
            self._disable_all_module_relays()

        # Ensure that the state machine has at least one state
        if (n_states := len(state_machine.states)) == 0:
            raise ValueError('State machine needs to have at least one state')

        # Check if '>back' operator is being used
        targets_used = {
            target
            for state in state_machine.states.values()
            for target in state.transitions.values()
        }
        self._use_back_op = '>back' in targets_used

        # Validate the number of states, global timers, global counters and conditions.
        n_global_timers = max(state_machine.global_timers.keys(), default=-1) + 1
        n_global_counters = max(state_machine.global_counters.keys(), default=-1) + 1
        n_conditions = max(state_machine.conditions.keys(), default=-1) + 1
        for name, value, maximum_value in (
            ('states', n_states, self._hardware.max_states - 1 - self._use_back_op),
            ('global timers', n_global_timers, self._hardware.n_global_timers),
            ('global counters', n_global_counters, self._hardware.n_global_counters),
            ('conditions', n_conditions, self._hardware.n_conditions),
        ):
            if value > maximum_value:
                raise ValueError(
                    f'Too many {name} in state machine - hardware supports up to '
                    f'{maximum_value} {name}'
                )

        # Validate states
        valid_targets = list(state_machine.states.keys()) + VALID_OPERATORS
        max_state_duration = np.iinfo(np.uint32).max / self._hardware.cycle_frequency
        for state_name, state in state_machine.states.items():
            if state.timer < 0 or state.timer > max_state_duration:
                raise ValueError(
                    f"Invalid timer value {state.timer} for state '{state_name}' - "
                    f'must be between 0 and {max_state_duration} seconds',
                )
            for condition_name, target in state.transitions.items():
                if target not in valid_targets:
                    target_type = 'operator' if target[0] == '>' else 'target state'
                    raise ValueError(
                        f"Invalid {target_type} '{target}' for transition "
                        f"'{condition_name}' in state '{state_name}'"
                        + suggest_similar(target, valid_targets),
                    )
                if condition_name not in self.event_names:
                    raise ValueError(
                        f"Invalid state change condition '{condition_name}' in state "
                        f"'{state_name}'"
                        + suggest_similar(condition_name, self.event_names),
                    )
            actions = set(state.actions.keys())
            if invalid_actions := actions.difference(self.actions):
                invalid_action = invalid_actions.pop()
                raise ValueError(
                    f"Invalid action '{invalid_action}' in state '{state_name}'"
                    + suggest_similar(invalid_action, self.actions),
                )

        # Compile list of physical channels
        # TODO: this is ugly
        physical_output_channels = [m.name for m in self.modules] + [
            o.name for o in self.outputs if o.io_type != b'U'
        ]
        physical_input_channels = [m.name for m in self.modules] + [
            o.name for o in self.inputs if o.io_type != b'U'
        ]

        # Validate global timers
        for timer_id, timer in state_machine.global_timers.items():
            if timer.channel not in (*physical_output_channels, None):
                raise ValueError(
                    f"Invalid channel '{timer.channel}' for global timer {timer_id}"
                    + suggest_similar(timer.channel or '', physical_output_channels),
                )

        # TODO: validate global timer onset triggers
        # TODO: validate global counters
        # TODO: validate conditions
        # TODO: Check that sync channel is not used as state output

        # return here if we're only validating the state machine
        if validate_only:
            return

        # compile dicts of indices to resolve strings to integers
        target_indices = {
            k: v for v, k in enumerate([*state_machine.states.keys(), 'exit'])
        }
        target_indices.update({'exit': n_states, '>exit': n_states})
        target_indices.update({'>back': 255} if self._use_back_op else {})
        event_indices = {k: v for v, k in enumerate(self.event_names)}
        action_indices = {k: v for v, k in enumerate(self.actions)}

        # Initialize bytearray. This will be appended to in the following sections.
        byte_array = bytearray(
            (n_states, n_global_timers, n_global_counters, n_conditions),
        )

        # Compile target indices for state timers and append to bytearray
        # Target indices default to the respective state's index unless 'Tup' is used
        for state_idx, state in enumerate(state_machine.states.values()):
            for event, target in state.transitions.items():
                if event == 'Tup':
                    byte_array.append(target_indices[target])
                    break
            else:
                byte_array.append(state_idx)

        # Helper function for appending events and their target indices to bytearray
        def append_events(event0: str, event1: str) -> None:
            idx0 = event_indices[event0]
            idx1 = event_indices[event1]
            for state in state_machine.states.values():
                counter_idx = len(byte_array)
                byte_array.append(0)
                for event, target in state.transitions.items():
                    if idx0 <= (key_idx := event_indices[event]) < idx1:
                        byte_array[counter_idx] += 1
                        byte_array.extend((key_idx - idx0, target_indices[target]))

        # Append input events to bytearray (i.e., events on physical input channels)
        append_events(self.event_names[0], 'GlobalTimer1_Start')

        # Append output actions and their values to bytearray
        # TODO: this could be more efficient?
        i1 = action_indices['GlobalTimerTrig']
        tmp_list: list[int] = []
        for state in state_machine.states.values():
            counter_pos = len(tmp_list)
            tmp_list.append(0)
            for invalid_action, value in state.actions.items():
                if (key_idx := action_indices[invalid_action]) < i1:
                    tmp_list[counter_pos] += 1
                    tmp_list.extend((key_idx, value))
        format_string = 'H' if self.version.machine == 4 else 'B'
        byte_array.extend(
            struct.pack(
                f'<{len(tmp_list)}{format_string}',
                *tmp_list,
            ),
        )

        # state transition matrix
        n_states = len(state_machine.states)
        self._state_transitions = np.arange(n_states, dtype=np.uint8)[
            :,
            np.newaxis,
        ] * np.ones((1, 255), dtype=np.uint8)
        for state_idx, state in enumerate(state_machine.states.values()):
            for event, target in state.transitions.items():
                target_idx = target_indices[target]
                self._state_transitions[state_idx][event_indices[event]] = target_idx

        # Append remaining events
        append_events('GlobalTimer1_Start', 'GlobalTimer1_End')  # global timer start
        append_events('GlobalTimer1_End', 'GlobalCounter1_End')  # global timer end
        append_events('GlobalCounter1_End', 'Condition1')  # global counter end
        append_events('Condition1', 'Tup')  # conditions

        # Compile indices for global timers channels
        timer_channel_indices = {k: v for v, k in enumerate(physical_output_channels)}
        timer_channel_indices[None] = 254

        # Helper function for packing a collection of integers into byte_array
        def pack_values(values: list[int], format_str: str) -> None:
            byte_array.extend(struct.pack(f'<{len(values)}{format_str}', *values))

        # Append values for global timer channels to byte_array
        idx0 = len(byte_array)
        byte_array.extend(b'\xfe' * n_global_timers)  # default: 254
        for timer_id, global_timer in state_machine.global_timers.items():
            byte_array[idx0 + timer_id] = timer_channel_indices[global_timer.channel]

        # Append values for global timers value_on and value_off to bytearray
        # Bpod 2+ uses 16-bit values for value_on and value_off
        format_string = 'H' if self.version.machine == 4 else 'B'
        for field_name in ('value_on', 'value_off'):
            pack_values(
                [
                    getattr(state_machine.global_timers.get(idx), field_name, 0)
                    for idx in range(n_global_timers)
                ],
                format_string,
            )

        # Append values for global timers loop and send_events to bytearray
        for field_name, default in (('loop', b'\x00'), ('send_events', b'\x01')):
            idx0 = len(byte_array)
            byte_array.extend(default * n_global_timers)  # default: 254
            for timer_id, global_timer in state_machine.global_timers.items():
                byte_array[idx0 + timer_id] = getattr(global_timer, field_name)

        # Append global counter events to bytearray
        idx0 = len(byte_array)
        byte_array.extend((254,) * n_global_counters)
        for counter_id, global_counter in state_machine.global_counters.items():
            byte_array[idx0 + counter_id] = event_indices[global_counter.event]

        # Compile indices for condition channels
        global_timers = [
            f'GlobalTimer{i + 1}' for i in range(self._hardware.n_global_timers)
        ]
        condition_channel_indices = {
            k: v for v, k in enumerate(physical_input_channels + global_timers)
        }

        # Append values for conditions to bytearray
        idx0 = len(byte_array)
        byte_array.extend((0,) * n_conditions * 2)
        for condition_id, condition in state_machine.conditions.items():
            offset = idx0 + condition_id
            byte_array[offset : offset + 2 * n_conditions : n_conditions] = (
                condition_channel_indices[condition.channel],
                condition.value,
            )

        # Append global counter resets
        if self.version.firmware < (23, 0):
            byte_array.extend(
                s.actions.get('GlobalCounterReset', 0)
                for s in state_machine.states.values()
            )
        else:
            counter_idx = len(byte_array)
            byte_array.append(0)
            for state_idx, state in enumerate(state_machine.states.values()):
                if (value := state.actions.get('GlobalCounterReset', 0)) > 0:
                    byte_array[counter_idx] += 1
                    byte_array.extend([state_idx, value])

        # Enable / disable analog thresholds
        # TODO: this is just a placeholder for now
        if self.version.machine == 4:
            byte_array.extend([0, 0])

        # The format of the next values depends on the number of global timers
        if self._hardware.n_global_timers > 16:
            format_string = 'I'  # uint32
        elif self._hardware.n_global_timers > 8:
            format_string = 'H'  # uint16
        else:
            format_string = 'B'  # uint8

        # Pack global timer triggers and cancels into bytearray
        for key in ('GlobalTimerTrig', 'GlobalTimerCancel'):
            pack_values(
                [s.actions.get(key, 0) for s in state_machine.states.values()],
                format_string,
            )

        # Pack global timer onset triggers into bytearray
        pack_values(
            [
                getattr(state_machine.global_timers.get(idx, {}), 'onset_trigger', 0)
                for idx in range(n_global_timers)
            ],
            format_string,
        )

        # Pack state timers
        pack_values(
            [
                round(s.timer * self._hardware.cycle_frequency)
                for s in state_machine.states.values()
            ],
            'I',  # uint32
        )

        # Pack global timer durations, onset delays and loop intervals
        for key in ('duration', 'onset_delay', 'loop_interval'):
            pack_values(
                [
                    round(
                        getattr(state_machine.global_timers.get(idx, {}), key, 0)
                        * self._hardware.cycle_frequency,
                    )
                    for idx in range(n_global_timers)
                ],
                'I',  # uint32
            )

        # Pack global counter thresholds
        pack_values(
            [
                getattr(state_machine.global_counters.get(idx, {}), 'threshold', 0)
                for idx in range(n_global_counters)
            ],
            'I',  # uint32
        )

        # Append additional opcodes
        # TODO: why?
        if self.version.firmware > (22, 0):
            byte_array.append(0)

        # Send to state machine
        self._next_fsm_index += 1
        logger.debug('Sending state machine #%d to Bpod', self._next_fsm_index)
        n_bytes = len(byte_array)
        self.serial0.write_struct(
            f'<c2?H{n_bytes}s', b'C', run_asap, self._use_back_op, n_bytes, byte_array
        )
        self._waiting_for_confirmation = True

        if run_asap:
            self._run_state_machine(blocking=False)

    @property
    def is_running(self) -> bool:
        """Check if the Bpod is currently running a state machine."""
        return self._fsm_thread is not None and self._fsm_thread.is_alive()

    def wait(self) -> None:
        """
        Wait for the currently running state machine to finish.

        This method blocks until the state machine has finished executing.
        If no state machine is currently running, it raises a RuntimeError.
        """
        if self.is_running:
            self._fsm_thread.join()  # type: ignore[union-attr]

    @validate_call
    def run_state_machine(self, *, blocking: bool = True) -> None:
        """Temporary run method for debugging purposes."""
        if self.is_running:
            raise RuntimeError('A state machine is already running')
        self.serial0.write(b'R')
        self._run_state_machine(blocking=blocking)

    def _run_state_machine(self, *, blocking: bool) -> None:
        # Wait for an already running state machine to finish
        self.wait()

        # Start a new FSM thread
        self._fsm_thread = FSMThread(
            self.serial0,
            self._next_fsm_index,
            self._waiting_for_confirmation,
            self._hardware.cycle_period,
            self._softcode_handler,
            self._state_transitions,
            self._use_back_op,
        )
        self._fsm_thread.start()
        self._waiting_for_confirmation = False

        # Wait for the FSM thread to finish
        if blocking:
            self._fsm_thread.join()

    def stop_state_machine(self) -> None:
        """Stop the currently running state machine."""
        if not self.is_running:
            return
        logger.debug('Stopping state machine')
        self.serial0.write(b'X')
        if self._fsm_thread is not None:
            self._fsm_thread.join()

    @staticmethod
    def _softcode_handler(softcode: int) -> None:
        pass

    @property
    def name(self) -> str | None:
        """Get the name of the Bpod device."""
        return cast(
            'str | None',
            self._get_setting(['devices', str(self._serial_number), 'name'], None),
        )

    @name.setter
    def name(self, name: str | None) -> None:
        """Set the name of the Bpod device."""
        self._set_setting(['devices', str(self._serial_number), 'name'], name)

    @property
    def location(self) -> str | None:
        """Get the location of the Bpod device."""
        return cast(
            'str | None',
            self._get_setting(['devices', str(self._serial_number), 'location'], None),
        )

    @location.setter
    def location(self, location: str | None) -> None:
        """Set the location of the Bpod device."""
        self._set_setting(['devices', str(self._serial_number), 'location'], location)


class Channel:
    """Base class representing a channel on the Bpod device."""

    def __init__(self, bpod: Bpod, name: str, io_key: bytes, index: int) -> None:
        """
        Abstract base class representing a channel on the Bpod device.

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
        """
        Input channel class representing a digital input channel.

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
        super().__init__(bpod, name, io_key, index)
        self._set_enable_inputs = bpod._set_enable_inputs
        self._enabled = io_key in (b'PBWF')  # Enable Port, BNC, Wire and FlexIO inputs

    def read(self) -> bool:
        """
        Read the state of the input channel.

        Returns
        -------
        bool
            True if the input channel is active, False otherwise.
        """
        return self._serial0.verify([b'I', self.index])

    def override(self, state: bool) -> None:
        """
        Override the state of the input channel.

        Parameters
        ----------
        state : bool
            The state to set for the input channel.
        """
        self._serial0.write_struct('<cB', b'V', state)

    def enable(self, enabled: bool) -> bool:
        """
        Enable or disable the input channel.

        Parameters
        ----------
        enabled : bool
            True to enable the input channel, False to disable.

        Returns
        -------
        bool
            True if the operation was success, False otherwise.
        """
        if self.io_type not in b'FDBWVP':
            logger.warning(
                '%sabling input `%s` has no effect',
                'En' if enabled else 'Dis',
                self.name,
            )
        self._enabled = enabled
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

    def override(self, state: bool | int) -> None:
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

    def __post_init__(self) -> None:
        self._relay_enabled = False
        self._define_event_names()

    def _define_event_names(self) -> None:
        """Define the module's event names."""
        self.event_names = []
        for idx in range(self.n_events):
            if len(self._custom_event_names) > idx:
                self.event_names.append(f'{self.name}_{self._custom_event_names[idx]}')
            else:
                self.event_names.append(f'{self.name}_{idx + 1}')

    @validate_call
    def set_relay(self, enable: bool) -> None:
        """
        Enable or disable the serial relay for the module.

        Parameters
        ----------
        enable : bool
            True to enable the relay, False to disable it.
        """
        if enable == self._relay_enabled:
            return
        if enable is True:
            self._bpod._disable_all_module_relays()
        logger.info(
            '%sabling relay for module %s', {'En' if enable else 'Dis'}, self.name
        )
        self._bpod.serial0.write_struct('<cB?', b'J', self.index, enable)
        self._relay_enabled = enable

    @property
    def relay(self) -> bool:
        """The current state of the serial relay."""
        return self._relay_enabled

    @relay.setter
    def relay(self, state: bool) -> None:
        """The current state of the serial relay."""
        self.set_relay(state)


class RemoteBpod:
    def __init__(
        self,
        address: str | None = None,
        name: str | None = None,
        serial_number: str | None = None,
        location: str | None = None,
        timeout: float = 10.0,
    ):
        properties = {
            'address': address,
            'name': name,
            'serial': serial_number,
            'location': location,
        }
        properties = {k: v for k, v in properties.items() if v is not None}

        try:
            self._zmq = DualChannelClient(
                '_bpod._tcp.local.',
                address=address,
                discovery_timeout=timeout,
                txt_properties=properties,
            )
        except TimeoutError as e:
            raise TimeoutError('Failed to discover remote Bpod.') from e
        self._handshake()

        # log hardware information
        logger.info(
            'Connected to Bpod Finite State Machine %s on %s',
            self._version['machine_str'],
            self._zmq._address_req,
        )

    def _request(self, request_type: str, **kwargs) -> dict:
        return cast('dict', self._zmq.request(type=request_type, **kwargs))

    def _remote_call(self, method: str, *args, **kwargs) -> Any | None:
        """
        Perform a remote procedure call by sending a 'call' type request.

        Parameters
        ----------
        method : str
            The name of the remote method to invoke.
        *args
            Positional arguments to pass to the remote method.
        **kwargs
            Keyword arguments to pass to the remote method.

        Returns
        -------
        Any or None
            The result returned from the remote method.
        """
        reply = self._request('call', method=method, args=args, kwargs=kwargs)
        if reply.get('success'):
            return reply['result']
        logger.error(f'Remote {reply["error"]["type"]}: ' + reply['error']['message'])
        return None

    def _handshake(self):
        reply = self._request('handshake')
        self._version = reply['version']
        self._version['bpod_core'] = reply['bpod-core']

    def set_status_led(self, enabled: bool) -> None:
        self._remote_call('set_status_led', enabled)

    def _event_handler(self, message: dict):
        pass
