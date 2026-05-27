"""Module providing extended serial communication functionality."""

import contextlib
import logging
import re
import struct
import weakref
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from struct import Struct
from types import TracebackType
from typing import Any, Literal, overload

from serial import Serial, SerialException
from serial.threaded import Protocol, ReaderThread
from serial.tools.list_ports import comports
from serial.tools.list_ports_common import ListPortInfo
from typing_extensions import Buffer, Self, override

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
        """
        Write an 8-bit signed integer to the serial port.

        Parameters
        ----------
        value : int
            An integer in the range [-128, 127].

        Returns
        -------
        int or None
            Number of bytes written, or None if the write fails.

        Raises
        ------
        struct.error
            If `value` is out of range.
        """
        return self.write(STRUCT_INT8.pack(value))

    def write_int16(self, value: int) -> int | None:
        """
        Write a 16-bit signed integer to the serial port (little-endian).

        Parameters
        ----------
        value : int
            An integer in the range [-32768, 32767].

        Returns
        -------
        int or None
            Number of bytes written, or None if the write fails.

        Raises
        ------
        struct.error
            If `value` is out of range.
        """
        return self.write(STRUCT_INT16_LE.pack(value))

    def write_int32(self, value: int) -> int | None:
        """
        Write a 32-bit signed integer to the serial port (little-endian).

        Parameters
        ----------
        value : int
            An integer in the range [-2147483648, 2147483647].

        Returns
        -------
        int or None
            Number of bytes written, or None if the write fails.

        Raises
        ------
        struct.error
            If `value` is out of range.
        """
        return self.write(STRUCT_INT32_LE.pack(value))

    def write_int64(self, value: int) -> int | None:
        """
        Write a 64-bit signed integer to the serial port (little-endian).

        Parameters
        ----------
        value : int
            An integer in the range [-9223372036854775808, 9223372036854775807].

        Returns
        -------
        int or None
            Number of bytes written, or None if the write fails.

        Raises
        ------
        struct.error
            If `value` is out of range.
        """
        return self.write(STRUCT_INT64_LE.pack(value))

    def write_uint8(self, value: int) -> int | None:
        """
        Write an 8-bit unsigned integer to the serial port.

        Parameters
        ----------
        value : int
            An integer in the range [0, 255].

        Returns
        -------
        int or None
            Number of bytes written, or None if the write fails.

        Raises
        ------
        struct.error
            If `value` is out of range.
        """
        return self.write(STRUCT_UINT8.pack(value))

    def write_uint16(self, value: int) -> int | None:
        """
        Write a 16-bit unsigned integer to the serial port (little-endian).

        Parameters
        ----------
        value : int
            An integer in the range [0, 65535].

        Returns
        -------
        int or None
            Number of bytes written, or None if the write fails.

        Raises
        ------
        struct.error
            If `value` is out of range.
        """
        return self.write(STRUCT_UINT16_LE.pack(value))

    def write_uint32(self, value: int) -> int | None:
        """
        Write a 32-bit unsigned integer to the serial port (little-endian).

        Parameters
        ----------
        value : int
            An integer in the range [0, 4294967295].

        Returns
        -------
        int or None
            Number of bytes written, or None if the write fails.

        Raises
        ------
        struct.error
            If `value` is out of range.
        """
        return self.write(STRUCT_UINT32_LE.pack(value))

    def write_uint64(self, value: int) -> int | None:
        """
        Write a 64-bit unsigned integer to the serial port (little-endian).

        Parameters
        ----------
        value : int
            An integer in the range [0, 18446744073709551615].

        Returns
        -------
        int or None
            Number of bytes written, or None if the write fails.

        Raises
        ------
        struct.error
            If `value` is out of range.
        """
        return self.write(STRUCT_UINT64_LE.pack(value))

    def write_bool(self, value: bool) -> int | None:  # noqa: FBT001
        """
        Write a boolean value to the serial port.

        Parameters
        ----------
        value : bool
            The boolean value to write (``True`` → ``0x01``, ``False`` → ``0x00``).

        Returns
        -------
        int or None
            Number of bytes written, or None if the write fails.
        """
        return self.write(b'\x01' if value else b'\x00')

    def read_int8(self) -> int:
        """
        Read an 8-bit signed integer from the serial port.

        Returns
        -------
        int
            The 8-bit signed integer read from the port.
        """
        return STRUCT_INT8.unpack(self.read(1))[0]  # type: ignore[no-any-return]

    def read_int16(self) -> int:
        """
        Read a 16-bit signed integer from the serial port (little-endian).

        Returns
        -------
        int
            The 16-bit signed integer read from the port.
        """
        return STRUCT_INT16_LE.unpack(self.read(2))[0]  # type: ignore[no-any-return]

    def read_int32(self) -> int:
        """
        Read a 32-bit signed integer from the serial port (little-endian).

        Returns
        -------
        int
            The 32-bit signed integer read from the port.
        """
        return STRUCT_INT32_LE.unpack(self.read(4))[0]  # type: ignore[no-any-return]

    def read_int64(self) -> int:
        """
        Read a 64-bit signed integer from the serial port (little-endian).

        Returns
        -------
        int
            The 64-bit signed integer read from the port.
        """
        return STRUCT_INT64_LE.unpack(self.read(8))[0]  # type: ignore[no-any-return]

    def read_uint8(self) -> int:
        """
        Read an 8-bit unsigned integer from the serial port.

        Returns
        -------
        int
            The 8-bit unsigned integer read from the port.
        """
        return self.read(1)[0]  # type: ignore[no-any-return]

    def read_uint16(self) -> int:
        """
        Read a 16-bit unsigned integer from the serial port (little-endian).

        Returns
        -------
        int
            The 16-bit unsigned integer read from the port.
        """
        return STRUCT_UINT16_LE.unpack(self.read(2))[0]  # type: ignore[no-any-return]

    def read_uint32(self) -> int:
        """
        Read a 32-bit unsigned integer from the serial port (little-endian).

        Returns
        -------
        int
            The 32-bit unsigned integer read from the port.
        """
        return STRUCT_UINT32_LE.unpack(self.read(4))[0]  # type: ignore[no-any-return]

    def read_uint64(self) -> int:
        """
        Read a 64-bit unsigned integer from the serial port (little-endian).

        Returns
        -------
        int
            The 64-bit unsigned integer read from the port.
        """
        return STRUCT_UINT64_LE.unpack(self.read(8))[0]  # type: ignore[no-any-return]

    def read_bool(self) -> bool:
        """
        Read a boolean value from the serial port.

        Returns
        -------
        bool
            ``True`` if the byte read is non-zero, ``False`` otherwise.
        """
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
            compatible with the :mod:`struct` module's `format specifications
            <https://docs.python.org/3/library/struct.html#format-characters>`__.
        *data : Any
            Variable-length arguments representing the data to be packed and written,
            corresponding to the format specifiers in `format_string`.

        Returns
        -------
        int or None
            The number of bytes written to the serial port, or None if the write
            operation fails.

        Raises
        ------
        struct.error
            If `data` cannot be packed with the given `format_string`.
        SerialTimeoutException
            In case a write timeout is configured for the port and the time is exceeded.

        Examples
        --------
        Write a command byte followed by a 16-bit unsigned integer::

            serial_port.write_struct('<BH', 0x4A, 1000)
        """
        buffer = struct.pack(format_string, *data)
        return self.write(buffer)

    def read_struct(self, fmt: str | Struct) -> tuple[Any, ...]:
        """
        Read structured data from the serial port.

        This method reads a specified number of bytes from the serial port and
        unpacks it into a tuple according to the provided format.

        Parameters
        ----------
        fmt : str or struct.Struct
            A pre-compiled struct or a format string compatible with the :mod:`struct`
            module's `format specifications
            <https://docs.python.org/3/library/struct.html#format-characters>`__.

        Returns
        -------
        tuple
            A tuple containing the unpacked data read from the serial port. The
            structure of the tuple corresponds to the format specified in `fmt`.

        Raises
        ------
        struct.error
            If `fmt` is invalid or the data cannot be unpacked.

        Examples
        --------
        Read one unsigned 16-bit integer followed by two unsigned 8-bit integers::

            major, minor, patch = serial_port.read_struct('<HBB')

        Pass a pre-compiled :class:`struct.Struct` to avoid re-parsing the format string
        on every call::

            fmt = struct.Struct('<HBB')
            while acquiring:
                major, minor, patch = serial_port.read_struct(fmt)
        """
        s = fmt if isinstance(fmt, Struct) else Struct(fmt)
        return s.unpack(super().read(s.size))

    @overload
    def read_struct_iter(
        self, fmt: str | Struct, n: int = 1, *, flatten: Literal[False] = False
    ) -> Iterator[tuple[Any, ...]]: ...

    @overload
    def read_struct_iter(
        self, fmt: str | Struct, n: int = 1, *, flatten: Literal[True]
    ) -> Iterator[Any]: ...

    def read_struct_iter(
        self,
        fmt: str | Struct,
        n: int = 1,
        *,
        flatten: bool = False,
    ) -> Iterator[tuple[Any, ...]] | Iterator[Any]:
        """
        Read structured data from the serial port as an iterator.

        Parameters
        ----------
        fmt : str or struct.Struct
            A pre-compiled struct or a format string compatible with the :mod:`struct`
            module's `format specifications
            <https://docs.python.org/3/library/struct.html#format-characters>`__.
        n : int, default: 1
            Number of records to read.
        flatten : bool, default: False
            If ``True``, yield individual values instead of tuples.

        Yields
        ------
        tuple or Any
            Each unpacked record as a tuple, or individual values if ``flatten=True``.

        Notes
        -----
        All bytes are read in a single call before any records are yielded. Use
        :meth:`stream_struct` instead if records should be yielded as they arrive.

        Examples
        --------
        Read three records as tuples::

            for value, flag in serial_port.read_struct_iter('<HB', 3):
                print(value, flag)

        Read two records as individual integers::

            v1, f1, v2, f2 = serial_port.read_struct_iter('<HB', 2, flatten=True)
        """
        s = fmt if isinstance(fmt, Struct) else Struct(fmt)
        data = self.read(n * s.size)
        if flatten:
            yield from (v for t in s.iter_unpack(data) for v in t)
        else:
            yield from s.iter_unpack(data)

    @overload
    def stream_struct(
        self, fmt: str | Struct, n: int, *, flatten: Literal[True] = True
    ) -> Iterator[Any]: ...

    @overload
    def stream_struct(
        self, fmt: str | Struct, n: int, *, flatten: Literal[False]
    ) -> Iterator[tuple[Any, ...]]: ...

    def stream_struct(
        self,
        fmt: str | Struct,
        n: int,
        *,
        flatten: bool = True,
    ) -> Iterator[Any] | Iterator[tuple[Any, ...]]:
        """
        Stream structured data from the serial port, yielding one record at a time.

        Parameters
        ----------
        fmt : str or struct.Struct
            A pre-compiled struct or a format string compatible with the :mod:`struct`
            module's `format specifications
            <https://docs.python.org/3/library/struct.html#format-characters>`__.
        n : int
            Number of records to read.
        flatten : bool, default: True
            If ``True``, yield individual values instead of tuples.

        Yields
        ------
        Any or tuple
            Individual values if ``flatten=True``, otherwise one tuple per record.

        Notes
        -----
        Each record is read and yielded as soon as its bytes arrive, using one
        :meth:`~serial.Serial.readinto` call per record. Use :meth:`read_struct_iter`
        instead if all data is available upfront and a single read call is preferred.

        Examples
        --------
        Stream 100 uint32 samples, yielding each as it arrives::

            for sample in serial_port.stream_struct('<I', 100):
                process(sample)

        Stream 10 records of 3 floats as tuples::

            for x, y, z in serial_port.stream_struct('<3f', 10, flatten=False):
                print(x, y, z)
        """
        s = fmt if isinstance(fmt, Struct) else Struct(fmt)
        buf = bytearray(s.size)
        for _ in range(n):
            self.readinto(buf)
            if flatten:
                yield from s.unpack(buf)
            else:
                yield s.unpack(buf)

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

        Examples
        --------
        Send a command and read back multiple bytes::

            response = serial_port.query(b'\x4A', size=4)
        """
        self.write(query)
        return self.read(size)

    def query_struct(
        self,
        query: Buffer,
        fmt: str | Struct,
    ) -> tuple[Any, ...]:
        r"""
        Query structured data from the serial port.

        This method queries a specified number of bytes from the serial port and
        unpacks it into a tuple according to the provided format.

        Parameters
        ----------
        query : Buffer
            Query to be sent to the serial port.
        fmt : str or struct.Struct
            A pre-compiled struct or a format string compatible with the :mod:`struct`
            module's `format specifications
            <https://docs.python.org/3/library/struct.html#format-characters>`__.

        Returns
        -------
        tuple
            A tuple containing the unpacked data read from the serial port. The
            structure of the tuple corresponds to the format specified in `fmt`.

        Examples
        --------
        Send a command and unpack the response as two unsigned 8-bit integers::

            major, minor = serial_port.query_struct(b'\x4a', 'BB')

        Pass a pre-compiled :class:`struct.Struct` to avoid re-parsing the format string
        on every call::

            fmt = struct.Struct('<2H')
            while acquiring:
                value, flag = serial_port.query_struct(b'\x4a', fmt)
        """
        s = fmt if isinstance(fmt, Struct) else Struct(fmt)
        return s.unpack(self.query(query, s.size))

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
            ``True`` if the response matches the expected response, ``False`` otherwise.

        Examples
        --------
        Send a handshake byte and check for acknowledgement::

            if not serial_port.verify(b'\x48'):
                raise RuntimeError('Device did not acknowledge handshake')
        """
        return self.query(query) == expected_response


class ChunkedSerialReader(Protocol):
    """
    A protocol for reading chunked data from a serial port.

    This class provides methods to buffer incoming data and retrieve it in chunks.

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

    _port: str | None = None

    def __init__(
        self,
        chunk_size: int,
        callback: Callable[[bytearray], None],
        buffer: bytearray | None = None,
    ) -> None:
        self._chunk_size = chunk_size
        self._callback = callback
        if buffer is None:
            self._buffer = bytearray()
        else:
            self._buffer = buffer

    def __call__(self) -> Self:
        """Allow the instance to be used as a protocol factory for ReaderThread."""
        return self

    @override
    def connection_made(self, transport: 'ReaderThread[Self]') -> None:
        """
        Called when a connection is made.

        Parameters
        ----------
        transport : ~serial.threaded.ReaderThread
            The reader thread that created this protocol instance.
        """
        self._port = transport.serial.portstr
        logger.debug('Starting serial reader thread for %s', self._port)

    @override
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

    @override
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
        - re.Pattern: regex match (use :func:`re.compile`)

    Returns
    -------
    list of ListPortInfo
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
    Strings use exact matching. Use :func:`re.compile` for regex patterns.
    """

    def matches(key: object, value: FilterValue) -> bool:
        if isinstance(value, Sequence) and not isinstance(value, str):
            return any(matches(key, v) for v in value)
        if isinstance(value, re.Pattern):
            return isinstance(key, str) and value.search(key) is not None
        return key == value

    return [
        port
        for port in sorted(comports())
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
    r"""Base class for implementing drivers for USB serial devices.

    Handles connection lifecycle — opening, closing, and cleanup on garbage collection —
    and provides subclasses with an :class:`ExtendedSerial` connection and port
    metadata. Derive from this class instead of using :class:`serial.Serial` directly to
    get automatic resource management and consistent error handling.

    Subclasses have access to the following private attributes:

    - ``_serial``: the underlying :class:`ExtendedSerial` connection.
    - ``_port_info``: a :class:`~serial.tools.list_ports.ListPortInfo` instance with
      metadata about the serial port (vendor ID, serial number, etc.).
    - ``_serial_device_name``: the name of the serial device (defaults to
      'serial device'). Used in log messages and exception text.

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

    Examples
    --------
    Subclass :class:`SerialDevice` and use ``_serial`` to communicate::

        class MyDevice(SerialDevice):
            _serial_device_name = 'My Device'

            def ping(self) -> bool:
                return self._serial.verify(b'\x48')

        with MyDevice('/dev/ttyACM0') as dev:
            assert dev.ping()
    """

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
        # obtain ListPortInfo for device on specified port
        try:
            self._port_info = next(p for p in comports() if p.device == port)
        except StopIteration as e:
            raise SerialException(
                f'Failed to connect to {self._serial_device_name} - '
                f'serial port not found: {port}'
            ) from e

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

    def __enter__(self) -> Self:
        """Enter the context manager."""
        return self

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
