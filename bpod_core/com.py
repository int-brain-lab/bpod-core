"""Module providing extended serial communication functionality."""

import contextlib
import logging
import re
import struct
import weakref
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any

from serial import Serial, SerialException
from serial.threaded import Protocol, ReaderThread
from serial.tools.list_ports import comports
from serial.tools.list_ports_common import ListPortInfo
from typing_extensions import Buffer, Self

from bpod_core.constants import (
    STRUCT_INT8,
    STRUCT_INT16_LE,
    STRUCT_INT32_LE,
    STRUCT_INT64_LE,
    STRUCT_UINT8,
    STRUCT_UINT16_LE,
    STRUCT_UINT32_LE,
    STRUCT_UINT64_LE,
)

logger = logging.getLogger(__name__)


class ExtendedSerial(Serial):
    """Enhances :class:`serial.Serial` with additional functionality."""

    def write_int8(self, value: int) -> int | None:
        """Write an 8-bit signed integer to the serial port."""
        return self.write(STRUCT_INT8.pack(value))

    def write_int16(self, value: int) -> int | None:
        """Write a 16-bit signed integer to the serial port (little-endian)."""
        return self.write(STRUCT_INT16_LE.pack(value))

    def write_int32(self, value: int) -> int | None:
        """Write a 32-bit signed integer to the serial port (little-endian)."""
        return self.write(STRUCT_INT32_LE.pack(value))

    def write_int64(self, value: int) -> int | None:
        """Write a 64-bit signed integer to the serial port (little-endian)."""
        return self.write(STRUCT_INT64_LE.pack(value))

    def write_uint8(self, value: int) -> int | None:
        """Write an 8-bit unsigned integer to the serial port."""
        return self.write(STRUCT_UINT8.pack(value))

    def write_uint16(self, value: int) -> int | None:
        """Write a 16-bit unsigned integer to the serial port (little-endian)."""
        return self.write(STRUCT_UINT16_LE.pack(value))

    def write_uint32(self, value: int) -> int | None:
        """Write a 32-bit unsigned integer to the serial port (little-endian)."""
        return self.write(STRUCT_UINT32_LE.pack(value))

    def write_uint64(self, value: int) -> int | None:
        """Write a 64-bit unsigned integer to the serial port (little-endian)."""
        return self.write(STRUCT_UINT64_LE.pack(value))

    def write_bool(self, value: bool) -> int | None:  # noqa: FBT001
        """Write a boolean value to the serial port."""
        return self.write(b'\x01' if value else b'\x00')

    def read_int8(self) -> int:
        """Read an 8-bit signed integer from the serial port."""
        return STRUCT_INT8.unpack(self.read(1))[0]  # type: ignore[no-any-return]

    def read_int16(self) -> int:
        """Read a 16-bit signed integer from the serial port (little-endian)."""
        return STRUCT_INT16_LE.unpack(self.read(2))[0]  # type: ignore[no-any-return]

    def read_int32(self) -> int:
        """Read a 32-bit signed integer from the serial port (little-endian)."""
        return STRUCT_INT32_LE.unpack(self.read(4))[0]  # type: ignore[no-any-return]

    def read_int64(self) -> int:
        """Read a 64-bit signed integer from the serial port (little-endian)."""
        return STRUCT_INT64_LE.unpack(self.read(8))[0]  # type: ignore[no-any-return]

    def read_uint8(self) -> int:
        """Read an 8-bit unsigned integer from the serial port."""
        return self.read(1)[0]  # type: ignore[no-any-return]

    def read_uint16(self) -> int:
        """Read a 16-bit unsigned integer from the serial port (little-endian)."""
        return STRUCT_UINT16_LE.unpack(self.read(2))[0]  # type: ignore[no-any-return]

    def read_uint32(self) -> int:
        """Read a 32-bit unsigned integer from the serial port (little-endian)."""
        return STRUCT_UINT32_LE.unpack(self.read(4))[0]  # type: ignore[no-any-return]

    def read_uint64(self) -> int:
        """Read a 64-bit unsigned integer from the serial port (little-endian)."""
        return STRUCT_UINT64_LE.unpack(self.read(8))[0]  # type: ignore[no-any-return]

    def read_bool(self) -> bool:
        """Read a boolean value from the serial port."""
        return self.read(1) != b'\x00'

    def write_struct(self, format_string: str, *data: Any) -> int | None:
        """
        Write structured data to the serial port.

        This method packs the provided data into a binary format according to the
        specified format string and writes it to the serial port.

        Parameters
        ----------
        format_string : str
            A format string that specifies the layout of the data. It should be
            compatible with the `struct` module's format specifications.
            See https://docs.python.org/3/library/struct.html#format-characters
        *data : Any
            Variable-length arguments representing the data to be packed and written,
            corresponding to the format specifiers in `format_string`.

        Returns
        -------
        int | None
            The number of bytes written to the serial port, or None if the write
            operation fails.

        Raises
        ------
        struct.error
            Error occurred during packing of the data into binary format.
        serial.SerialTimeoutException
            In case a write timeout is configured for the port and the time is exceeded.
        """
        buffer = struct.pack(format_string, *data)
        return self.write(buffer)

    def read_struct(self, format_string: str) -> tuple[Any, ...]:
        """
        Read structured data from the serial port.

        This method reads a specified number of bytes from the serial port and
        unpacks it into a tuple according to the provided format string.

        Parameters
        ----------
        format_string : str
            A format string that specifies the layout of the data to be read. It should
            be compatible with the `struct` module's format specifications.
            See https://docs.python.org/3/library/struct.html#format-characters

        Returns
        -------
        tuple[Any, ...]
            A tuple containing the unpacked data read from the serial port. The
            structure of the tuple corresponds to the format specified in
            `format_string`.
        """
        n_bytes = struct.calcsize(format_string)
        return struct.unpack(format_string, super().read(n_bytes))

    def query(self, query: Buffer, size: int = 1) -> bytes:
        r"""
        Query data from the serial port.

        This method is a combination of :meth:`~serial.Serial.write` and
        :meth:`~serial.Serial.read`.

        Parameters
        ----------
        query : Buffer
            Query to be sent to the serial port.
        size : int, default: 1
            The number of bytes to receive from the serial port.

        Returns
        -------
        bytes
            Data returned by the serial device in response to the query.
        """
        self.write(query)
        return self.read(size)

    def query_struct(
        self,
        query: Buffer,
        format_string: str,
    ) -> tuple[Any, ...]:
        """
        Query structured data from the serial port.

        This method queries a specified number of bytes from the serial port and
        unpacks it into a tuple according to the provided format string.

        Parameters
        ----------
        query : Buffer
            Query to be sent to the serial port.
        format_string : str
            A format string that specifies the layout of the data to be read. It should
            be compatible with the `struct` module's format specifications.
            See https://docs.python.org/3/library/struct.html#format-characters

        Returns
        -------
        tuple[Any, ...]
            A tuple containing the unpacked data read from the serial port. The
            structure of the tuple corresponds to the format specified in
            `format_string`.
        """
        self.write(query)
        return self.read_struct(format_string)

    def verify(self, query: Buffer = b'', expected_response: bytes = b'\x01') -> bool:
        r"""
        Verify the response of the serial port.

        This method sends a query to the serial port and checks if the response
        matches the expected response.

        Parameters
        ----------
        query : Buffer, default: b''
            The query to be sent to the serial port.
        expected_response : bytes, default: b'\x01'
            The expected response from the serial port.

        Returns
        -------
        bool
            True if the response matches the expected response, False otherwise.
        """
        return self.query(query) == expected_response


class ChunkedSerialReader(Protocol):
    """
    A protocol for reading chunked data from a serial port.

    This class provides methods to buffer incoming data and retrieve it in chunks.
    """

    _port: str | None = None

    def __init__(
        self,
        chunk_size: int,
        callback: Callable[[bytes], None],
        buffer: bytearray | None = None,
    ) -> None:
        """
        Initialize the protocol.

        Parameters
        ----------
        chunk_size : int
            The fixed size of chunks to emit to the callback function when enough data
            has accumulated in the buffer.
        callback : Callable
            A function to call with each chunk of data.
        buffer : bytearray, optional
            Pre-allocated buffer to use for accumulation. If `None`, a new bytearray
            is created.
        """
        self._chunk_size = chunk_size
        self._callback = callback
        if buffer is None:
            self._buffer = bytearray()
        else:
            self._buffer = buffer

    def __call__(self) -> Self:
        """Allow the instance to be used as a protocol factory for ReaderThread."""
        return self

    def connection_made(self, transport: 'ReaderThread[Self]') -> None:
        """
        Called when a connection is made.

        Parameters
        ----------
        transport : ReaderThread
            The reader thread that created this protocol instance.
        """
        self._port = transport.serial.portstr
        logger.debug('Starting serial reader thread for %s', self._port)

    def connection_lost(self, exc: BaseException | None) -> None:
        """
        Called when the serial port is closed or the reader loop terminated otherwise.

        Parameters
        ----------
        exc : BaseException, optional
            The exception that caused the connection to be closed, if any.
        """
        super().connection_lost(exc)
        logger.debug('Stopping serial reader thread for %s', self._port)

    def data_received(self, data: bytes) -> None:
        """
        Called with snippets received from the serial port.

        Parameters
        ----------
        data : bytes
            The binary data received from the serial port.
        """
        self._buffer.extend(data)
        while len(self._buffer) >= self._chunk_size:
            self._callback(self._buffer[: self._chunk_size])
            del self._buffer[: self._chunk_size]


FilterValue = str | int | re.Pattern[str] | None | Sequence['FilterValue']
"""Type for filter values used in :func:`find_ports`."""


def find_ports(**filters: FilterValue) -> list[ListPortInfo]:
    r"""
    Find serial ports matching specified criteria.

    Multiple filters use AND logic. Iterables within a single filter use OR logic.

    Parameters
    ----------
    **filters : FilterValue
        Port attributes to filter by. Values can be:

        - Scalar: exact match
        - Sequence: match any item (OR logic)
        - re.Pattern: regex match (use re.compile())

    Returns
    -------
    list[ListPortInfo]
        Ports matching all criteria.

    Examples
    --------
    Find by vendor ID::

        find_ports(vid=0x16C0)

    Find using regex pattern::

        find_ports(device=re.compile(r'/dev/ttyACM\d+'))

    Find multiple values::

        find_ports(pid=[0x0483, 0x048B])

    Combine filters::

        find_ports(vid=0x16C0, device=re.compile(r'/dev/ttyACM\d+'))

    Notes
    -----
    Strings use exact matching. Use re.compile() for regex patterns.
    """

    def matches(key: object, value: FilterValue) -> bool:
        if isinstance(value, Sequence) and not isinstance(value, str):
            return any(matches(key, v) for v in value)
        if isinstance(value, re.Pattern):
            return isinstance(key, str) and value.search(key) is not None
        return key == value

    return [
        port
        for port in comports()
        if all(matches(getattr(port, k, None), v) for k, v in filters.items())
    ]


def verify_serial_discovery(
    port: str,
    expected_message: bytes,
    timeout: float = 1,
    trigger: Callable[[], Any] | None = None,
) -> bool:
    """Check if a device sends an expected discovery message on a serial port.

    Opens the specified serial port and waits to receive bytes matching the expected
    discovery message. Optionally executes a function first, which can be used to
    trigger the device's discovery routine, e.g., by sending a command.

    Parameters
    ----------
    port : str
        The serial port to read from (e.g., '/dev/ttyUSB0' or 'COM3').
    expected_message : bytes
        The exact byte sequence expected from the device.
    timeout : float, default: 1
        Maximum time (in seconds) to wait for the discovery message.
    trigger : Callable, optional
        A function to call before reading. Use this to trigger the device's discovery
        routine.

    Returns
    -------
    bool
        True if the device sent the expected message within the timeout period,
        False otherwise (including if the port cannot be opened).
    """
    try:
        with Serial(port, timeout=timeout) as ser:
            if trigger is not None:
                trigger()
            return ser.read(len(expected_message)) == expected_message
    except SerialException:
        return False


class SerialDevice(AbstractContextManager):
    """Class that interfaces with a USB serial device."""

    _serial: ExtendedSerial
    """The serial connection to the device."""

    _port_info: ListPortInfo
    """Information about the serial port associated with the device."""

    _serial_device_name: str = 'serial device'
    """Name of the serial device."""

    def __init__(
        self,
        port: str,
        *,
        open_connection: bool = True,
    ) -> None:
        """Initialize the serial device.

        Parameters
        ----------
        port : str
            The serial port device path (e.g., '/dev/ttyUSB0' or 'COM3').
        open_connection : bool, default: True
            Whether to open the connection immediately.

        Raises
        ------
        serial.SerialException
            If the specified port does not exist.
        """
        # obtain ListPortInfo for device on specified port
        try:
            self._port_info = next(p for p in comports() if p.device == port)
        except StopIteration as e:
            raise SerialException(f'Serial port not found: {port}') from e

        # instantiate ExtendedSerial
        self._serial = ExtendedSerial()
        self._serial.port = port  # only set port after the fact

        # finalizer for cleanup on garbage collection
        self._serial_device_finalizer: weakref.finalize | None = None

        # open connection
        if open_connection:
            self.open()

    def _rename_serial_device(self, new_name: str) -> None:
        self._serial_device_name = new_name
        if self._serial_device_finalizer is not None:
            self._serial_device_finalizer.detach()
        self._serial_device_finalizer = weakref.finalize(
            self,
            SerialDevice._close_serial_connection,
            serial=self._serial,
            device_name=self._serial_device_name,
            raise_errors=False,
        )

    @staticmethod
    def _close_serial_connection(
        serial: Serial,
        *,
        device_name: str = 'serial device',
        raise_errors: bool = False,
    ) -> None:
        """Close a serial connection if open."""
        if not getattr(serial, 'is_open', False):
            return
        try:
            logger.debug('Closing connection to %s on %s', device_name, serial.port)
            serial.close()
        except Exception as e:
            if not raise_errors:
                return
            raise SerialException(
                f'Failed to close connection to {device_name} on {serial.port}'
            ) from e

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit the context manager.

        Closes the serial connection.

        Parameters
        ----------
        exc_type : type[BaseException] | None
            The type of exception raised, if any.
        exc_val : BaseException | None
            The exception instance raised, if any.
        exc_tb : TracebackType | None
            The traceback object, if any.
        """
        with contextlib.suppress(Exception):
            self.close()

    def open(self) -> None:
        """Open the serial connection.

        If the connection is already open, this method does nothing.

        Raises
        ------
        serial.SerialException
            If the connection cannot be opened.
        """
        if self._serial.is_open:
            return
        logger.debug(
            'Opening connection to %s on %s', self._serial_device_name, self.port
        )
        try:
            self._serial.open()
        except Exception as e:
            raise SerialException(
                f'Failed to open connection to {self._serial_device_name} on '
                f'{self.port}'
            ) from e

        # register destructors
        self._serial_device_finalizer = weakref.finalize(
            self,
            SerialDevice._close_serial_connection,
            serial=self._serial,
            device_name=self._serial_device_name,
            raise_errors=False,
        )

    def close(self) -> None:
        """Close the serial connection.

        If the connection is already closed, this method does nothing.

        Raises
        ------
        serial.SerialException
            If the connection cannot be closed.
        """
        if hasattr(self, '_serial'):
            self._close_serial_connection(
                serial=self._serial,
                device_name=self._serial_device_name,
                raise_errors=True,
            )
            if self._serial_device_finalizer is not None:
                self._serial_device_finalizer.detach()

    @property
    def port(self) -> str:
        """The name of the serial port.

        Returns
        -------
        str
            The device path of the serial port (e.g., '/dev/ttyACM0').
        """
        return self._port_info.device
