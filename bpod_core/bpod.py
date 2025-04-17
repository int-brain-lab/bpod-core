from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, NamedTuple

import serial
from serial import SerialException
from serial.threaded import Protocol, ReaderThread
from serial.tools import list_ports

from bpod_core import __version__ as VERSION  # noqa: N812
from bpod_core.serial_extensions import (
    SerialSingleton,
    SerialSingletonException,
    get_serial_number_from_port,
)

if TYPE_CHECKING:
    from _typeshed import ReadableBuffer  # noqa: F401

PROJECT_NAME = 'bpod-core'
VID_TEENSY = 0x16C0
logger = logging.getLogger(__name__)


class SerialReaderProtocolRaw(Protocol):
    def connection_made(self, transport):
        """
        Called when the reader thread is started.

        Parameters
        ----------
        - transport: The transport object associated with the connection.
        """
        logger.info('Threaded serial reader started - ready to receive data ...')

    def data_received(self, data):
        """
        Called with snippets received from the serial port.

        Parameters
        ----------
        - data: The binary data received from the serial port.
        """
        print(data.decode())

    def connection_lost(self, exc):
        """
        Called when the connection is lost or encounters an exception.

        Parameters
        ----------
        - exc: The exception that caused the connection loss, if any.
        """
        logger.info(exc)


class BpodException(SerialSingletonException):
    pass


class Bpod(SerialSingleton):
    """
    Class for interfacing a Bpod Finite State Machine.

    The Bpod class extends :class:`serial.Serial`.

    Parameters
    ----------
    port : str, optional
        The serial port for the Bpod device, or None to automatically detect a Bpod.
    connect : bool, default: True
        Whether to connect to the Bpod device. If True and 'port' is None, an
        attempt will be made to automatically find and connect to a Bpod device.
    **kwargs
        Additional keyword arguments passed to :class:`serial.Serial`.

    Examples
    --------
    * Try to automatically find a Bpod device and connect to it.

        .. code-block:: python

            my_bpod = Bpod()

    * Connect to a Bpod device on COM3

        .. code-block:: python

            my_bpod = Bpod('COM3')

    * Instantiate a Bpod object for a device on COM3 but only connect to it later.

        .. code-block:: python

            my_bpod = Bpod(port = "COM3", connect = False)
            # (do other things)
            my_bpod.open()
    """

    class _Info(NamedTuple):
        serial_number: str
        firmware_version: tuple[int, int]
        machine_type: int
        machine_type_string: str
        pcb_revision: int
        max_states: int
        timer_period: int
        max_serial_events: int
        max_bytes_per_serial_message: int
        n_global_timers: int
        n_global_counters: int
        n_conditions: int
        n_inputs: int
        input_description_array: bytes
        n_outputs: int
        output_description_array: bytes

    def __new__(
        cls,
        port: str | None = None,
        connect: bool = True,
        **kwargs,
    ):
        """
        Create or retrieve a singleton instance of the Bpod class.

        This method implements a singleton pattern for the Bpod class, ensuring that
        only one instance is created for a given port. If an instance already exists
        for the specified port, that instance is returned.

        Parameters
        ----------
        port : str, optional
            The serial port for the Bpod device, or None to automatically detect a Bpod.
        connect : bool, optional
            Whether to connect to the Bpod device. If True and 'port' is None, an
            attempt will be made to automatically find and connect to a Bpod device.
        **kwargs
            Additional keyword arguments passed to serial.Serial.

        Returns
        -------
        Bpod
            A singleton instance of the Bpod class.

        Raises
        ------
        ValueError
            If 'port' is not a string and is not None.

        Notes
        -----
        The singleton instances are managed by a class-level lock and dictionary.
        Automatic Bpod detection relies on the find method.

        Example
        -------
        To create or retrieve a Bpod instance on a specific port:

        .. code-block:: python
            bpod_instance = Bpod(port='COM3')

        To automatically detect and create or retrieve a Bpod instance:

        .. code-block:: python
            bpod_instance = Bpod()
        """
        # log version
        logger.debug(f'{PROJECT_NAME} {VERSION}')

        # try to automagically find a Bpod device
        if port is None and connect is True:
            port = next(iter(Bpod._instances.keys()), next(find_bpod_ports(), None))

        # implement singleton
        return super().__new__(cls, port, **kwargs)

    def __init__(self, port: str | None = None, connect: bool = True, **kwargs) -> None:
        """
        Initialize a Bpod instance.

        This method initializes a Bpod instance, allowing communication with a Bpod
        device over a specified serial port.

        Parameters
        ----------
        port : str, optional
            The serial port for the Bpod device. If None and 'connect' is True, an
            attempt will be made to automatically detect and use a Bpod port.
        connect : bool, optional
            Whether to establish a connection to the Bpod device. If True and 'port' is
            None, automatic port detection will be attempted.
        **kwargs
            Additional keyword arguments to be passed to the constructor of
            serial.Serial.

        Notes
        -----
        -   If the Bpod instance is already instantiated, the method returns without
            further action.
        -   If 'port' is 'None' and 'connect' is True the former value may be
            overridden based on existing instances
        """
        if self._initialized:
            return

        self.info: Bpod._Info | None = None
        self.inputs = None
        self.outputs = None
        self._reader: ReaderThread = ReaderThread(self, SerialReaderProtocolRaw)

        # automatic port discovery (also see __new__)
        if port is None and connect is True:
            port = next((k for (k, v) in self._instances.items() if v is self), None)

        # initialize super class
        if 'baudrate' not in kwargs:
            kwargs['baudrate'] = 1312500
        super().__init__(port=port, connect=connect, **kwargs)
        assert self._initialized is True

    def __repr__(self):
        return f'Bpod(port={self.port})'

    def open(self) -> None:
        """
        Open serial connection and connect to Bpod Finite State Machine.

        Raises
        ------
        BpodException
            Handshake failed: The Bpod did not acknowledge our request.
        """
        super().open()

        # try to perform handshake
        if self.handshake():
            logger.debug('Handshake successful')

        # get firmware version and machine type; assert version requirements
        v_major, machine_type = self.query(b'F', '<2H')
        version = (v_major, self.query(b'f', '<H')[0] if v_major > 22 else 0)
        if not (2 < machine_type < 5):
            raise BpodException(
                f'The hardware version of the Bpod on {self.port} is not supported.'
            )
        if version < (min_version := (23, 0)):
            raise BpodException(
                f'The Bpod on {self.port} uses firmware v{version[0]}.{version[1]} '
                f'which is not supported. Please update the device to '
                f'firmware v{min_version[0]}.{min_version[1]} or later.'
            )

        # detect additional USB-serial ports
        candidate_ports = [
            p.device
            for p in list_ports.comports()
            if p.vid == VID_TEENSY and p.device != self.port
        ]
        for port in candidate_ports:
            try:
                with serial.Serial(port, timeout=0.2) as ser:
                    if ser.read(1) == bytes([222]):
                        candidate_ports.remove(port)
            except serial.SerialException:
                pass
        for port in candidate_ports:
            try:
                with serial.Serial(port, timeout=0.2) as ser:
                    self.write(b'{')
                    if ser.read(1) == bytes([222]):
                        print(port)
                        candidate_ports.remove(port)
                        continue
            except serial.SerialException:
                pass
        if machine_type == 4:
            for port in candidate_ports:
                try:
                    with serial.Serial(port, timeout=0.2) as ser:
                        self.write(b'}')
                        if ser.read(1) == bytes([223]):
                            print(port)
                            continue
                except serial.SerialException:
                    pass

        # get some more hardware information
        machine_str = {3: 'r2.0-2.5', 4: '2+ r1.0'}.get(machine_type, 'unknown')
        serial_number = get_serial_number_from_port(self.port)
        pcb_rev = self.query(b'v', '<B')[0] if v_major > 22 else None

        # log hardware information
        logger.info(f'Bpod Finite State Machine {machine_str}')
        logger.info(f'Serial number {serial_number}') if serial_number else None
        logger.info(f'PCB revision {pcb_rev}') if pcb_rev else None
        logger.info('Firmware version {}.{}'.format(*version))

        # get hardware self-description
        info: list[Any] = [
            serial_number,
            version,
            machine_type,
            machine_str,
            pcb_rev,
        ]
        info.extend(self.query(b'H', '<2H6B'))
        info.extend(self.read(f'<{info[-1]}s1B'))
        info.extend(self.read(f'<{info[-1]}s'))
        self.info = Bpod._Info(*info)

        def collect_channels(description: bytes, dictionary: dict, channel_cls: type):
            """
            Generate a collection of Bpod channels.

            This method takes a channel description array (as returned by the Bpod), a
            dictionary mapping keys to names, and a channel class. It generates named
            tuple instances and sets them as attributes on the current Bpod instance.
            """
            channels = []
            types = []

            for idx in range(len(description)):
                io_key = description[idx : idx + 1]
                if bytes(io_key) in dictionary:
                    n = description[:idx].count(io_key) + 1
                    name = f'{dictionary[io_key]}{n}'
                    channels.append(channel_cls(self, name, io_key, idx))
                    types.append((name, channel_cls))

            cls_name = f'{channel_cls.__name__.lower()}s'
            setattr(self, cls_name, NamedTuple(cls_name, types)._make(channels))

        logger.debug('Configuring I/O ports')
        input_dict = {b'B': 'BNC', b'V': 'Valve', b'P': 'Port', b'W': 'Wire'}
        output_dict = {b'B': 'BNC', b'V': 'Valve', b'P': 'PWM', b'W': 'Wire'}
        collect_channels(self.info.input_description_array, input_dict, Input)
        collect_channels(self.info.output_description_array, output_dict, Output)

        # logger.debug("Configuring modules")
        # self.modules = Modules(self)

    def close(self):
        """Disconnect the state machine and close the serial connection."""
        if not self.is_open:
            return
        logger.debug('Disconnecting state machine')
        self.write(b'Z')
        super().close()

    def handshake(self, raise_exception_on_fail: bool = True) -> bool:
        """
        Try to perform handshake with Bpod device.

        Returns
        -------
        bool
            True if successful, False otherwise.

        Notes
        -----
        This will reset the state machine's session clock and flush the serial port.
        """
        try:
            return self.query(b'6') == b'5'
        except SerialException as e:
            if raise_exception_on_fail:
                raise BpodException('Handshake failed') from e
        finally:
            self.reset_input_buffer()

        if raise_exception_on_fail:
            raise BpodException('Handshake failed')
        return False

    def update_modules(self):
        pass
        # self.write(b"M")
        # modules = []
        # for i in range(len(modules)):
        #     if self.read() == bytes([1]):
        #         continue
        #     firmware_version = self.read(4, np.uint32)[0]
        #     name = self.read(int(self.read())).decode("utf-8")
        #     port = i + 1
        #     m = Module()
        #     while self.read() == b"\x01":
        #         match self.read():
        #             case b"#":
        #                 number_of_events = self.read(1, np.uint8)[0]
        #             case b"E":
        #                 for event_index in range(self.read(1, np.uint8)[0]):
        #                     l_event_name = self.read(1, np.uint8)[0]
        #                     module["events"]["index"] = event_index
        #                     module["events"]["name"] = self.read(l_event_name, str)[0]
        #         modules[i] = module
        #     self._children = modules


class Channel(ABC):
    @abstractmethod
    def __init__(self, bpod: Bpod, name: str, io_type: bytes, index: int):
        """
        Abstract base class representing a channel on the Bpod device.

        Parameters
        ----------
        bpod : Bpod
            The Bpod instance associated with the channel.
        name : str
            The name of the channel.
        io_type : bytes
            The I/O type of the channel (e.g., 'B', 'V', 'P').
        index : int
            The index of the channel.
        """
        self.name = name
        self.io_type = io_type
        self.index = index
        self._query = bpod.query
        self._write = bpod.write

    def __repr__(self):
        return self.__class__.__name__ + '()'


class Input(Channel):
    def __init__(self, *args, **kwargs):
        """
        Input channel class representing a digital input channel.

        Parameters
        ----------
        *args, **kwargs
            Arguments to be passed to the base class constructor.
        """
        super().__init__(*args, **kwargs)

    def read(self) -> bool:
        """
        Read the state of the input channel.

        Returns
        -------
        bool
            True if the input channel is active, False otherwise.
        """
        return self._query(['I', self.index], 1) == b'\x01'

    def override(self, state: bool) -> None:
        """
        Override the state of the input channel.

        Parameters
        ----------
        state : bool
            The state to set for the input channel.
        """
        self._write(['V', state])

    def enable(self, state: bool) -> None:
        """
        Enable or disable the input channel.

        Parameters
        ----------
        state : bool
            True to enable the input channel, False to disable.
        """
        pass


class Output(Channel):
    def __init__(self, *args, **kwargs):
        """
        Output channel class representing a digital output channel.

        Parameters
        ----------
        *args, **kwargs
            Arguments to be passed to the base class constructor.
        """
        super().__init__(*args, **kwargs)

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
        self._write(['O', self.index, state.to_bytes(1, 'little')])


class Module:
    pass


def find_bpod_ports() -> Iterator[str]:
    """
    Discover serial ports used by Bpod devices.

    This method scans through the list of available serial ports and identifies ports
    that are in use by a Bpod device. It does so by briefly opening each port and
    checking for a specific byte pattern (byte 222). Ports matching this pattern are
    yielded.

    Yields
    ------
    str
        The names of available serial ports compatible with the Bpod device.

    Notes
    -----
    The method employs a brief timeout when opening each port to minimize the impact on
    system resources.

    SerialException is caught and ignored, allowing the method to continue scanning even
    if certain ports encounter errors during opening.

    Examples
    --------
    .. code-block:: python

        for port in Bpod.find():
            print(f'Bpod on {port}')
        # Bpod on COM3
        # Bpod on COM6
    """
    for port in (p for p in list_ports.comports() if p.vid == VID_TEENSY):
        try:
            with serial.Serial(port.device, timeout=0.2) as ser:
                if ser.read(1) == bytes([222]):
                    yield port.device
        except serial.SerialException:
            pass
