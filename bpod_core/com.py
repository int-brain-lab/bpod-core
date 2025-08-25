"""Module providing extended serial communication functionality."""

import logging
import struct
from collections.abc import Iterable
from typing import Any, TypeAlias

import numpy as np
from serial import Serial
from serial.serialutil import to_bytes as serial_to_bytes  # type: ignore[attr-defined]
from serial.threaded import Protocol
from typing_extensions import Buffer, Self

logger = logging.getLogger(__name__)

ByteLike: TypeAlias = (
    Buffer | int | np.ndarray | np.generic | str | Iterable['ByteLike']
)
"""
A recursive type alias representing any data that can be converted to bytes for serial
communication.

Includes:

- Buffer: Any buffer-compatible object (e.g., bytes, bytearray, memoryview)
- int: Single integer values (interpreted as a single byte)
- np.ndarray, np.generic: NumPy arrays and scalars (converted via .tobytes())
- str: Strings (encoded as UTF-8)
- Iterable['ByteLike']: Nested iterables of ByteLike types (recursively flattened)
"""


class ExtendedSerial(Serial):
    """Enhances :class:`serial.Serial` with additional functionality."""

    def write(self, data: ByteLike) -> int | None:  # type: ignore[override]
        """
        Write data to the serial port.

        This method extends :meth:`serial.Serial.write` with support for NumPy types,
        unsigned 8-bit integers, strings (interpreted as UTF-8) and iterables.

        Parameters
        ----------
        data : ByteLike
            Data to be written to the serial port.

        Returns
        -------
        int or None
            Number of bytes written to the serial port.
        """
        return super().write(to_bytes(data))

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
        """
        buffer = struct.pack(format_string, *data)
        return super().write(buffer)

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

    def query(self, query: ByteLike, size: int = 1) -> bytes:
        r"""
        Query data from the serial port.

        This method is a combination of :meth:`write` and :meth:`~serial.Serial.read`.

        Parameters
        ----------
        query : ByteLike
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
        query: ByteLike,
        format_string: str,
    ) -> tuple[Any, ...]:
        """
        Query structured data from the serial port.

        This method queries a specified number of bytes from the serial port and
        unpacks it into a tuple according to the provided format string.

        Parameters
        ----------
        query : ByteLike
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

    def verify(self, query: ByteLike, expected_response: bytes = b'\x01') -> bool:
        r"""
        Verify the response of the serial port.

        This method sends a query to the serial port and checks if the response
        matches the expected response.

        Parameters
        ----------
        query : ByteLike
            The query to be sent to the serial port.
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

    def __init__(self, chunk_size: int, buffer: bytearray | None = None) -> None:
        """
        Initialize the protocol.

        Parameters
        ----------
        chunk_size : int
            The fixed size of chunks to emit to `process` when enough data has
            accumulated in the internal buffer.
        buffer : bytearray, optional
            Pre-allocated buffer to use for accumulation. If `None`, a new bytearray
            is created.
        """
        self._chunk_size = chunk_size
        if buffer is None:
            self._buf = bytearray()
        else:
            self._buf = buffer

    def __call__(self) -> Self:
        """Allow the instance to be used as a protocol factory for ReaderThread."""
        return self

    def put(self, data: bytes) -> None:
        """
        Add data to the buffer.

        Parameters
        ----------
        data : bytes
            The binary data to be added to the buffer.
        """
        self._buf.extend(data)

    def get(self, size: int) -> bytearray:
        """
        Retrieve a specified amount of data from the buffer.

        Parameters
        ----------
        size : int
            The number of bytes to retrieve from the buffer.

        Returns
        -------
        bytearray
            The retrieved data.
        """
        data: bytearray = self._buf[:size]
        del self._buf[:size]
        return data

    def __len__(self) -> int:
        """
        Get the current size of the buffer.

        Returns
        -------
        int
            The number of bytes currently in the buffer.
        """
        return len(self._buf)

    def data_received(self, data: bytes) -> None:
        """
        Called with snippets received from the serial port.

        Parameters
        ----------
        data : bytes
            The binary data received from the serial port.
        """
        self.put(data)
        while len(self) >= self._chunk_size:
            self.process(self.get(self._chunk_size))

    def process(self, data_chunk: bytearray) -> None:
        """
        Process a chunk of data.

        Subclasses should override this method to implement application-specific
        handling of fixed-size chunks. It is called repeatedly by `data_received`
        whenever enough bytes have accumulated to reach `chunk_size`.

        Parameters
        ----------
        data_chunk : bytearray
            A contiguous slice of bytes of length `chunk_size`.
        """


def to_bytes(data: ByteLike) -> bytes:  # noqa: PLR0911
    """
    Convert data to a bytes object.

    This function extends :func:`serial.to_bytes` with support for:
    - NumPy arrays and scalars
    - Unsigned 8-bit integers
    - Strings (encoded as UTF-8)
    - Arbitrary iterables of ByteLike

    Parameters
    ----------
    data : ByteLike
        Data to be converted to a bytes object.

    Returns
    -------
    bytes
        Data converted to bytes.

    Raises
    ------
    TypeError
        If the input type cannot be interpreted as bytes
    ValueError
        If an integer is out of the 0..255 range when coerced to a single byte.
    """
    match data:
        case bytes():
            return data
        case bytearray():
            return bytes(data)
        case memoryview() | np.ndarray() | np.generic():
            return data.tobytes()
        case int():
            return bytes([data])
        case str():
            return data.encode('utf-8')
        case _ if isinstance(data, Iterable):
            return b''.join(to_bytes(item) for item in data)
        case _:
            return serial_to_bytes(data)  # type: ignore[no-any-return]
