"""Module providing extended serial communication functionality."""

import logging
import struct
from typing import Any

from serial import Serial
from serial.threaded import Protocol
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
