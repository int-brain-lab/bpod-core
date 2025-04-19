from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import validate_call
from serial import SerialException
from serial.tools.list_ports import comports

from bpod_core import __version__ as bpod_core_version
from bpod_core.serial_extensions import ExtendedSerial

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer  # noqa: F401

PROJECT_NAME = 'bpod-core'
VIDS_BPOD = [0x16C0]  # vendor IDs of supported Bpod devices
MIN_BPOD_FW_VERSION = (23, 0)  # minimum supported firmware version (major, minor)
MIN_BPOD_HW_VERSION = 3  # minimum supported hardware version
MAX_BPOD_HW_VERSION = 4  # maximum supported hardware version

logger = logging.getLogger(__name__)


class BpodError(Exception):
    """
    Exception class for Bpod-related errors.

    This exception is raised when an error specific to the Bpod device or its
    operations occurs.
    """


class Bpod:
    """Bpod class for interfacing with the Bpod Finite State Machine."""

    _info: dict[str, Any] = dict()

    @validate_call
    def __init__(self, port: str | None = None, serial_number: str | None = None):
        logger.info(f'bpod_core version {bpod_core_version}')

        # identify Bpod by port or serial number
        port, self._info['serial_number'] = self._identify_bpod(port, serial_number)

        # open primary serial port
        self.serial0 = ExtendedSerial()
        self.serial0.port = port
        self.open()

        # get firmware version and machine type; assert version requirements
        self._info.update(self._get_version_info())

    def __enter__(self):
        """Enter context."""
        return self

    def __exit__(self, type, value, traceback):
        """Exit context and close connection."""
        self.close()

    def __del__(self):
        self.close()

    @staticmethod
    def _identify_bpod(
        port: str | None = None, serial_number: str | None = None
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

        def sends_discovery_byte(port: str) -> bool:
            """Check if the device on the given port sends a discovery byte."""
            try:
                with ExtendedSerial(port, timeout=0.15) as ser:
                    return ser.read(1) == bytes([222])
            except SerialException:
                return False

        # If no port or serial number provided, try to automagically find an idle Bpod
        if port is None and serial_number is None:
            try:
                port_info = next(
                    p
                    for p in comports()
                    if getattr(p, 'vid', None) in VIDS_BPOD
                    and sends_discovery_byte(p.device)
                )
            except StopIteration as e:
                raise BpodError('No available Bpod found') from e
            return port_info.device, port_info.serial_number

        # Else, if a serial number was provided, try to match it with a serial device
        elif serial_number is not None:
            try:
                port_info = next(
                    p for p in comports() if p.serial_number == serial_number
                )
            except (StopIteration, AttributeError) as e:
                raise BpodError(f'No device with serial number {serial_number}') from e

        # Else, assure that the provided port exists and the device could be a Bpod
        else:
            try:
                port_info = next(p for p in comports() if p.device == port)
            except (StopIteration, AttributeError) as e:
                raise BpodError(f'Port not found: {port}') from e

        if port_info.vid not in VIDS_BPOD:
            raise BpodError('Device is not a supported Bpod')
        return port_info.device, port_info.serial_number

    def _get_version_info(self) -> dict[str, Any]:
        """
        Retrieve firmware version and machine type information from the Bpod.

        This method queries the Bpod to obtain its firmware version, machine type, and
        PCB revision. It also validates that the hardware and firmware versions meet
        the minimum requirements. If the versions are not supported, an Exception is
        raised.

        Returns
        -------
        dict[str, Any]
            A dictionary containing the following keys:

            - 'bpod_type': The type of the Bpod,
            - 'v_firmware': A tuple representing the firmware version (major, minor),
            - 'v_pcb': The PCB revision, if applicable.

        Raises
        ------
        BpodError
            If the hardware version or firmware version is not supported.
        """
        info_dict = dict()
        v_major, info_dict['bpod_type'] = self.serial0.query(b'F', '<2H')
        v_minor = self.serial0.query(b'f', '<H')[0] if v_major > 22 else 0
        info_dict['v_firmware'] = (v_major, v_minor)
        if not (MIN_BPOD_HW_VERSION <= info_dict['bpod_type'] <= MAX_BPOD_HW_VERSION):
            raise BpodError(
                f'The hardware version of the Bpod on {self.port0} is not supported.'
            )
        if info_dict['v_firmware'] < MIN_BPOD_FW_VERSION:
            raise BpodError(
                f'The Bpod on {self.port0} uses firmware v{v_major}.{v_minor} '
                f'which is not supported. Please update the device to '
                f'firmware v{MIN_BPOD_FW_VERSION[0]}.{MIN_BPOD_FW_VERSION[1]} or later.'
            )
        info_dict['v_pcb'] = self.serial0.query(b'v', '<B')[0] if v_major > 22 else None
        return info_dict

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
            if not self.serial0.validate_response(b'6', b'5'):
                raise BpodError(f'Handshake with device on {self.port0} failed')
            self.serial0.timeout = None
        except SerialException as e:
            raise BpodError(f'Handshake with device on {self.port0} failed') from e
        finally:
            self.serial0.reset_input_buffer()
        logger.debug(f'Handshake with Bpod on {self.port0} successful')

    @property
    def port0(self) -> str:
        return self.serial0.port or ''

    @property
    def serial_number(self) -> str:
        return self._info.get('serial_number') or ''

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


# class BpodOriginal(SerialSingleton):
#     """
#     Class for interfacing a Bpod Finite State Machine.
#
#     The Bpod class extends :class:`serial.Serial`.
#
#     Parameters
#     ----------
#     port : str, optional
#         The serial port for the Bpod device, or None to automatically detect a Bpod.
#     connect : bool, default: True
#         Whether to connect to the Bpod device. If True and 'port' is None, an
#         attempt will be made to automatically find and connect to a Bpod device.
#     **kwargs
#         Additional keyword arguments passed to :class:`serial.Serial`.
#
#     Examples
#     --------
#     * Try to automatically find a Bpod device and connect to it.
#
#         .. code-block:: python
#
#             my_bpod = Bpod()
#
#     * Connect to a Bpod device on COM3
#
#         .. code-block:: python
#
#             my_bpod = Bpod('COM3')
#
#     * Instantiate a Bpod object for a device on COM3 but only connect to it later.
#
#         .. code-block:: python
#
#             my_bpod = Bpod(port = "COM3", connect = False)
#             # (do other things)
#             my_bpod.open()
#     """
#
#     class _Info(NamedTuple):
#         serial_number: str
#         firmware_version: tuple[int, int]
#         machine_type: int
#         machine_type_string: str
#         pcb_revision: int
#         max_states: int
#         timer_period: int
#         max_serial_events: int
#         max_bytes_per_serial_message: int
#         n_global_timers: int
#         n_global_counters: int
#         n_conditions: int
#         n_inputs: int
#         input_description_array: bytes
#         n_outputs: int
#         output_description_array: bytes
#
#     def __new__(
#         cls,
#         port: str | None = None,
#         connect: bool = True,
#         **kwargs,
#     ):
#         """
#         Create or retrieve a singleton instance of the Bpod class.
#
#         This method implements a singleton pattern for the Bpod class, ensuring that
#         only one instance is created for a given port. If an instance already exists
#         for the specified port, that instance is returned.
#
#         Parameters
#         ----------
#         port : str, optional
#             The serial port for the Bpod device, or None to automatically detect a Bpod.
#         connect : bool, optional
#             Whether to connect to the Bpod device. If True and 'port' is None, an
#             attempt will be made to automatically find and connect to a Bpod device.
#         **kwargs
#             Additional keyword arguments passed to serial.Serial.
#
#         Returns
#         -------
#         Bpod
#             A singleton instance of the Bpod class.
#
#         Raises
#         ------
#         ValueError
#             If 'port' is not a string and is not None.
#
#         Notes
#         -----
#         The singleton instances are managed by a class-level lock and dictionary.
#         Automatic Bpod detection relies on the find method.
#
#         Example
#         -------
#         To create or retrieve a Bpod instance on a specific port:
#
#         .. code-block:: python
#             bpod_instance = Bpod(port='COM3')
#
#         To automatically detect and create or retrieve a Bpod instance:
#
#         .. code-block:: python
#             bpod_instance = Bpod()
#         """
#         # log version
#         logger.debug(f'{PROJECT_NAME} {VERSION}')
#
#         # try to automagically find a Bpod device
#         # if port is None and connect is True:
#         #     port = next(iter(Bpod._instances.keys()), next(_find_idle_bpod(), None))
#
#         # implement singleton
#         return super().__new__(cls, port, **kwargs)
#
#     def __init__(self, port: str | None = None, connect: bool = True, **kwargs) -> None:
#         """
#         Initialize a Bpod instance.
#
#         This method initializes a Bpod instance, allowing communication with a Bpod
#         device over a specified serial port.
#
#         Parameters
#         ----------
#         port : str, optional
#             The serial port for the Bpod device. If None and 'connect' is True, an
#             attempt will be made to automatically detect and use a Bpod port.
#         connect : bool, optional
#             Whether to establish a connection to the Bpod device. If True and 'port' is
#             None, automatic port detection will be attempted.
#         **kwargs
#             Additional keyword arguments to be passed to the constructor of
#             serial.Serial.
#
#         Notes
#         -----
#         -   If the Bpod instance is already instantiated, the method returns without
#             further action.
#         -   If 'port' is 'None' and 'connect' is True the former value may be
#             overridden based on existing instances
#         """
#         if self._initialized:
#             return
#
#         self.port1 = None
#         self.port2 = None
#         self.info: Bpod._Info | None = None
#         self.inputs = None
#         self.outputs = None
#
#         # automatic port discovery (also see __new__)
#         if port is None and connect is True:
#             port = next((k for (k, v) in self._instances.items() if v is self), None)
#
#         # initialize super class
#         if 'baudrate' not in kwargs:
#             kwargs['baudrate'] = 1312500
#         super().__init__(port=port, connect=connect, **kwargs)
#         assert self._initialized is True
#
#     def __repr__(self):
#         return f'Bpod(port={self.port})'
#
#     def open(self) -> None:
#         """
#         Open serial connection and connect to Bpod Finite State Machine.
#
#         Raises
#         ------
#         BpodException
#             Handshake failed: The Bpod did not acknowledge our request.
#         """
#         super().open()
#
#         # try to perform handshake
#         if self.handshake():
#             logger.debug('Handshake successful')
#
#         # get firmware version and machine type; assert version requirements
#         v_major, machine_type = self.query(b'F', '<2H')
#         version = (v_major, self.query(b'f', '<H')[0] if v_major > 22 else 0)
#         if not (2 < machine_type < 5):
#             raise BpodError(
#                 f'The hardware version of the Bpod on {self.port} is not supported.'
#             )
#         if version < (min_version := (23, 0)):
#             raise BpodError(
#                 f'The Bpod on {self.port} uses firmware v{version[0]}.{version[1]} '
#                 f'which is not supported. Please update the device to '
#                 f'firmware v{min_version[0]}.{min_version[1]} or later.'
#             )
#
#         # get some more hardware information
#         machine_str = {3: 'r2.0-2.5', 4: '2+ r1.0'}.get(machine_type, 'unknown')
#         serial_number = get_serial_number_from_port(self.port)
#         pcb_rev = self.query(b'v', '<B')[0] if v_major > 22 else None
#
#         # log hardware information
#         logger.info(f'Bpod Finite State Machine {machine_str} on {self.port}')
#         logger.info(f'Serial number {serial_number}') if serial_number else None
#         logger.info(f'PCB revision {pcb_rev}') if pcb_rev else None
#         logger.info('Firmware version {}.{}'.format(*version))
#
#         # get hardware self-description
#         info: list[Any] = [
#             serial_number,
#             version,
#             machine_type,
#             machine_str,
#             pcb_rev,
#         ]
#         info.extend(self.query(b'H', '<2H6B'))
#         info.extend(self.read(f'<{info[-1]}s1B'))
#         info.extend(self.read(f'<{info[-1]}s'))
#         self.info = Bpod._Info(*info)
#
#         # detect additional ports
#         self._detect_additional_serial_ports()
#
#         def collect_channels(description: bytes, dictionary: dict, channel_cls: type):
#             """
#             Generate a collection of Bpod channels.
#
#             This method takes a channel description array (as returned by the Bpod), a
#             dictionary mapping keys to names, and a channel class. It generates named
#             tuple instances and sets them as attributes on the current Bpod instance.
#             """
#             channels = []
#             types = []
#
#             for idx in range(len(description)):
#                 io_key = description[idx : idx + 1]
#                 if bytes(io_key) in dictionary:
#                     n = description[:idx].count(io_key) + 1
#                     name = f'{dictionary[io_key]}{n}'
#                     channels.append(channel_cls(self, name, io_key, idx))
#                     types.append((name, channel_cls))
#
#             cls_name = f'{channel_cls.__name__.lower()}s'
#             setattr(self, cls_name, NamedTuple(cls_name, types)._make(channels))
#
#         logger.debug('Configuring I/O ports')
#         input_dict = {b'B': 'BNC', b'V': 'Valve', b'P': 'Port', b'W': 'Wire'}
#         output_dict = {b'B': 'BNC', b'V': 'Valve', b'P': 'PWM', b'W': 'Wire'}
#         collect_channels(self.info.input_description_array, input_dict, Input)
#         collect_channels(self.info.output_description_array, output_dict, Output)
#
#         # logger.debug("Configuring modules")
#         # self.modules = Modules(self)
#
#     def _detect_additional_serial_ports(self) -> None:
#         """Detect additional USB-serial ports."""
#         # First, assemble a list of candidate ports
#         candidate_ports = [
#             p.device
#             for p in list_ports.comports()
#             if p.vid in VIDS_BPOD and p.device != self.port
#         ]
#
#         # Exclude all uninitialized Bpods from the list
#         for port in candidate_ports:
#             try:
#                 with serial.Serial(port, timeout=0.15) as ser:
#                     if ser.read(1) == bytes([222]):
#                         candidate_ports.remove(port)
#             except serial.SerialException:
#                 pass
#
#         # Find second USB-serial port
#         for port in candidate_ports:
#             try:
#                 with serial.Serial(port, timeout=0.05) as ser:
#                     self.write(b'{')
#                     if ser.read(1) == bytes([222]):
#                         ser.reset_input_buffer()
#                         ser.timeout = None
#                         self.port1 = ser
#                         candidate_ports.remove(port)
#                         break
#             except serial.SerialException:
#                 pass
#
#         # State Machine 2+ uses a third USB-serial port
#         if self.info.machine_type == 4:
#             for port in candidate_ports:
#                 try:
#                     with serial.Serial(port, timeout=0.05) as ser:
#                         self.write(b'}')
#                         if ser.read(1) == bytes([223]):
#                             ser.reset_input_buffer()
#                             ser.timeout = None
#                             self.port2 = ser
#                             break
#                 except serial.SerialException:
#                     pass
#
#     def close(self):
#         """Disconnect the state machine and close the serial connection."""
#         if not self.is_open:
#             return
#         logger.debug('Disconnecting state machine')
#         self.write(b'Z')
#         super().close()
#
#     def handshake(self, raise_exception_on_fail: bool = True) -> bool:
#         """
#         Try to perform handshake with Bpod device.
#
#         Returns
#         -------
#         bool
#             True if successful, False otherwise.
#
#         Notes
#         -----
#         This will reset the state machine's session clock and flush the serial port.
#         """
#         try:
#             return self.query(b'6') == b'5'
#         except SerialException as e:
#             if raise_exception_on_fail:
#                 raise BpodError('Handshake failed') from e
#         finally:
#             self.reset_input_buffer()
#
#         if raise_exception_on_fail:
#             raise BpodError('Handshake failed')
#         return False
#
#     def update_modules(self):
#         pass
#         # self.write(b"M")
#         # modules = []
#         # for i in range(len(modules)):
#         #     if self.read() == bytes([1]):
#         #         continue
#         #     firmware_version = self.read(4, np.uint32)[0]
#         #     name = self.read(int(self.read())).decode("utf-8")
#         #     port = i + 1
#         #     m = Module()
#         #     while self.read() == b"\x01":
#         #         match self.read():
#         #             case b"#":
#         #                 number_of_events = self.read(1, np.uint8)[0]
#         #             case b"E":
#         #                 for event_index in range(self.read(1, np.uint8)[0]):
#         #                     l_event_name = self.read(1, np.uint8)[0]
#         #                     module["events"]["index"] = event_index
#         #                     module["events"]["name"] = self.read(l_event_name, str)[0]
#         #         modules[i] = module
#         #     self._children = modules
#
#
# class Channel(ABC):
#     @abstractmethod
#     def __init__(self, bpod: Bpod, name: str, io_type: bytes, index: int):
#         """
#         Abstract base class representing a channel on the Bpod device.
#
#         Parameters
#         ----------
#         bpod : Bpod
#             The Bpod instance associated with the channel.
#         name : str
#             The name of the channel.
#         io_type : bytes
#             The I/O type of the channel (e.g., 'B', 'V', 'P').
#         index : int
#             The index of the channel.
#         """
#         self.name = name
#         self.io_type = io_type
#         self.index = index
#         self._query = bpod.query
#         self._write = bpod.write
#
#     def __repr__(self):
#         return self.__class__.__name__ + '()'
#
#
# class Input(Channel):
#     def __init__(self, *args, **kwargs):
#         """
#         Input channel class representing a digital input channel.
#
#         Parameters
#         ----------
#         *args, **kwargs
#             Arguments to be passed to the base class constructor.
#         """
#         super().__init__(*args, **kwargs)
#
#     def read(self) -> bool:
#         """
#         Read the state of the input channel.
#
#         Returns
#         -------
#         bool
#             True if the input channel is active, False otherwise.
#         """
#         return self._query(['I', self.index], 1) == b'\x01'
#
#     def override(self, state: bool) -> None:
#         """
#         Override the state of the input channel.
#
#         Parameters
#         ----------
#         state : bool
#             The state to set for the input channel.
#         """
#         self._write(['V', state])
#
#     def enable(self, state: bool) -> None:
#         """
#         Enable or disable the input channel.
#
#         Parameters
#         ----------
#         state : bool
#             True to enable the input channel, False to disable.
#         """
#         pass
#
#
# class Output(Channel):
#     def __init__(self, *args, **kwargs):
#         """
#         Output channel class representing a digital output channel.
#
#         Parameters
#         ----------
#         *args, **kwargs
#             Arguments to be passed to the base class constructor.
#         """
#         super().__init__(*args, **kwargs)
#
#     def override(self, state: bool | int) -> None:
#         """
#         Override the state of the output channel.
#
#         Parameters
#         ----------
#         state : bool or int
#             The state to set for the output channel. For binary I/O types, provide a
#             bool. For pulse width modulation (PWM) I/O types, provide an int (0-255).
#         """
#         if isinstance(state, int) and self.io_type in (b'D', b'B', b'W'):
#             state = state > 0
#         self._write(['O', self.index, state.to_bytes(1, 'little')])
#
#
# class Module:
#     pass
