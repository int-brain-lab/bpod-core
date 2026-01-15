"""Module providing extended serial communication functionality."""

import logging
import re
import struct
from collections.abc import Callable
from typing import Any

from serial import Serial, SerialException
from serial.threaded import Protocol, ReaderThread
from serial.tools.list_ports import comports
from serial.tools.list_ports_common import ListPortInfo
from typing_extensions import Buffer, Self

logger = logging.getLogger(__name__)


class ExtendedSerial(Serial):
    """Enhances :class:`serial.Serial` with additional functionality."""

    def write_struct(self, format_string: str, *data: Any) -> int | None:  # noqa:ANN401
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
        query : Buffer, optional
            The query to be sent to the serial port. Defaults to an empty byte string.
        expected_response : bytes, optional
            The expected response from the serial port. Default: b'\x01'.

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
        callback: Callable[[bytes], Any],
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


def find_ports(**filters: Any) -> list[ListPortInfo]:
    r"""
    Find serial ports matching specified criteria.

    Multiple filters use AND logic. Iterables within a single filter use OR logic.

    Parameters
    ----------
    **filters : Any
        Port attributes to filter by. Values can be:

        - Scalar: exact match
        - List: match any item (OR logic)
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

    def matches(key, value):
        if isinstance(value, list):
            return any(matches(key, v) for v in value)
        if isinstance(value, re.Pattern):
            return isinstance(key, str) and bool(value.search(key))
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
    r"""Check if a device sends an expected discovery message on a serial port.

    Opens the specified serial port and waits to receive bytes matching the expected
    discovery message. Optionally executes a function first, which can be used trigger
    the device's discovery routine, e.g., by sending a command.

    Parameters
    ----------
    port : str
        The serial port to read from (e.g., '/dev/ttyUSB0' or 'COM3').
    expected_message : bytes
        The exact byte sequence expected from the device.
    timeout : float, default: 1
        Maximum time (in seconds) to wait for the discovery message. Defaults to 1 s.
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
