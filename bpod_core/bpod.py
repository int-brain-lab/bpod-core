"""Module for interfacing with the Bpod Finite State Machine."""

import logging
import struct
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, NamedTuple

from pydantic import validate_call
from serial import SerialException
from serial.tools.list_ports import comports

from bpod_core import __version__ as bpod_core_version
from bpod_core.com import ExtendedSerial

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer  # noqa: F401

PROJECT_NAME = 'bpod-core'
VENDOR_IDS_BPOD = [0x16C0]  # vendor IDs of supported Bpod devices
MIN_BPOD_FW_VERSION = (23, 0)  # minimum supported firmware version (major, minor)
MIN_BPOD_HW_VERSION = 3  # minimum supported hardware version
MAX_BPOD_HW_VERSION = 4  # maximum supported hardware version
CHANNEL_TYPES_INPUT = {
    b'U': 'Serial',
    b'X': 'SoftCode',
    b'Z': 'SoftCodeApp',
    b'F': 'FlexIO',
    b'S': 'SPI',
    b'D': 'Digital',
    b'B': 'BNC',
    b'W': 'Wire',
    b'P': 'Port',
}
CHANNEL_TYPES_OUTPUT = CHANNEL_TYPES_INPUT.copy()
CHANNEL_TYPES_OUTPUT.update({b'V': 'Valve', b'P': 'PWM'})

logger = logging.getLogger(__name__)


class VersionInfo(NamedTuple):
    """Represents the Bpod's on-board hardware configuration."""

    firmware: tuple[int, int]
    """Firmware version (major, minor)"""
    machine: int
    """Machine type (numerical)"""
    pcb: int | None
    """PCB revision, if applicable"""


class HardwareConfiguration(NamedTuple):
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

    @property
    def cycle_frequency(self) -> int:
        """Frequency of the state machine's refresh cycle during a trial in Hertz."""
        return 1000000 // self.cycle_period

    @property
    def n_modules(self) -> int:
        """Number of modules supported by the state machine."""
        return self.input_description.count(b'U')


class BpodError(Exception):
    """
    Exception class for Bpod-related errors.

    This exception is raised when an error specific to the Bpod device or its
    operations occurs.
    """


class Bpod:
    """Bpod class for interfacing with the Bpod Finite State Machine."""

    _version: VersionInfo
    _hardware: HardwareConfiguration
    serial0: ExtendedSerial
    """Primary serial device for communication with the Bpod."""
    serial1: ExtendedSerial | None = None
    """Secondary serial device for communication with the Bpod."""
    serial2: ExtendedSerial | None = None
    """Tertiary serial device for communication with the Bpod - used by Bpod 2+ only."""
    inputs: NamedTuple
    outputs: NamedTuple

    @validate_call
    def __init__(self, port: str | None = None, serial_number: str | None = None):
        logger.info(f'bpod_core {bpod_core_version}')

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

        # log hardware information
        machine = {3: 'r2.0-2.5', 4: '2+ r1.0'}.get(self.version.machine, 'unknown')
        logger.info(f'Connected to Bpod Finite State Machine {machine} on {self.port}')
        logger.info(
            f'Firmware Version {"{}.{}".format(*self.version.firmware)}, '
            f'Serial Number {self._serial_number}, PCB Revision {self.version.pcb}'
        )

    def __enter__(self):
        """Enter context."""
        return self

    def __exit__(self, type, value, traceback):
        """Exit context and close connection."""
        self.close()

    def __del__(self):
        self.close()

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
        self, port: str | None = None, serial_number: str | None = None
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

        # Else, if a serial number was provided, try to match it with a serial device
        elif serial_number is not None:
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
        v_minor = self.serial0.query_struct(b'f', '<H')[0] if v_major > 22 else 0
        v_firmware = (v_major, v_minor)
        if not (MIN_BPOD_HW_VERSION <= machine_type <= MAX_BPOD_HW_VERSION):
            raise BpodError(
                f'The hardware version of the Bpod on {self.port} is not supported.'
            )
        if v_firmware < MIN_BPOD_FW_VERSION:
            raise BpodError(
                f'The Bpod on {self.port} uses firmware v{v_major}.{v_minor} '
                f'which is not supported. Please update the device to '
                f'firmware v{MIN_BPOD_FW_VERSION[0]}.{MIN_BPOD_FW_VERSION[1]} or later.'
            )
        pcv_rev = self.serial0.query_struct(b'v', '<B')[0] if v_major > 22 else None
        self._version = VersionInfo(v_firmware, machine_type, pcv_rev)

    def _get_hardware_configuration(self) -> None:
        """Retrieve the Bpod's onboard hardware configuration."""
        logger.debug('Retrieving onboard hardware configuration')
        if self.version.firmware > (22, 0):
            hardware_conf = list(self.serial0.query_struct(b'H', '<2H6B'))
        else:
            hardware_conf = list(self.serial0.query_struct(b'H', '<2H5B'))
            hardware_conf.insert(-4, 3)  # max bytes per serial msg always = 3
        hardware_conf.extend(self.serial0.read_struct(f'<{hardware_conf[-1]}s1B'))
        hardware_conf.extend(self.serial0.read_struct(f'<{hardware_conf[-1]}s'))
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
                    logger.debug(f'Detected secondary USB-serial port: {port}')
                    break
            if self.serial1 is None:
                raise BpodError('Could not detect secondary serial port')

        # State Machine 2+ uses a third USB-serial port for FlexIO
        if self.version.machine == 4:
            for port in candidate_ports:
                if self._sends_discovery_byte(port, bytes([223]), trigger=b'}'):
                    self.serial2 = ExtendedSerial()
                    self.serial2.port = port
                    logger.debug(f'Detected tertiary USB-serial port: {port}')
                    break
            if self.serial2 is None:
                raise BpodError('Could not detect tertiary serial port')

    def _handshake(self):
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
        logger.debug(f'Handshake with Bpod on {self.port} successful')

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

    def _reset_session_clock(self) -> bool:
        logger.debug('Resetting session clock')
        return self.serial0.verify(b'*')

    @property
    def port(self) -> str | None:
        """The port of the Bpod's primary serial device."""
        return self.serial0.port

    @property
    def version(self) -> VersionInfo:
        """Version information of the Bpod's firmware and hardware."""
        return self._version

    def open(self):
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

    def close(self):
        """Close the connection to the Bpod."""
        if hasattr(self, 'serial0') and self.serial0.is_open:
            self.serial0.write(b'Z')
            self.serial0.close()

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

    def update_modules(self):
        pass
        # self.serial0.write(b'M')
        # modules = []
        # for idx in range(self._hardware.n_modules):
        #     is_connected = self.serial0.read_struct('<?')[0]
        #     if not is_connected:
        #         module_name = f'{CHANNEL_TYPES[b"U"]}{idx + 1}'
        #     else:
        #         firmware_version, n = self.serial0.read_struct('<IB')
        #         module_name, more_info = self.serial0.read_struct(f'<{n}s?')
        #         while more_info:
        #             match self.serial0.read(1):
        #                 case b'#':
        #                     n_serial_events = self.serial0.read_struct('<B')[0]
        #                 case b'E':
        #                     n_evt_names = self.serial0.read(0)
        #                     evt_names = []
        #                     for i_event_name in range(n_evt_names):
        #                         n = self.serial0.read_struct('<B')[0]
        #                         evt_names.append(self.serial0.read_struct(f'<{n}s'))
        #             more_info = self.serial0.read_struct('<?')[0]


class Channel(ABC):
    """Abstract base class representing a channel on the Bpod device."""

    @abstractmethod
    def __init__(self, bpod: Bpod, name: str, io_key: bytes, index: int):
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

    def __repr__(self):
        return self.__class__.__name__ + '()'


class Input(Channel):
    """Input channel class representing a digital input channel."""

    def __init__(self, bpod: Bpod, name: str, io_key: bytes, index: int):
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
                f'{"En" if enabled else "Dis"}abling input `{self.name}` has no effect'
            )
        self._enabled = enabled
        success = self._set_enable_inputs()
        return success

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

    def __init__(self, bpod: Bpod, name: str, io_key: bytes, index: int):
        """
        Output channel class representing a digital output channel.

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


# class Module:
#     """Base class for Bpod modules."""
#
#     def __init__(
#         self,
#         bpod: Bpod,
#         index: int,
#         name: str,
#         n_events: int,
#         event_names: list[str] | None = None,
#         firmware_version: int | None = None,
#         connected: bool = False,
#     ):
#         self._bpod = bpod
#         self._index = index
#         self._name = name
#         self._n_events = n_events
