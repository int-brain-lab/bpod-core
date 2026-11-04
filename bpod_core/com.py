"""Module providing extended serial communication functionality."""

import contextlib
import logging
import os
import re
import select
import sys
import threading
import weakref
from collections.abc import Callable, Collection, Iterable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from struct import Struct
from types import TracebackType
from typing import Any, Literal, cast, overload

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
from bpod_core.misc import Filter, _match_filter

logger = logging.getLogger(__name__)


_DIRECT_READ = os.name == 'posix'
"""Whether :meth:`ExtendedSerial.read_waiting` waits on and reads a file descriptor."""
_READ_SIZE = 65536
"""Most bytes :meth:`ExtendedSerial.read_waiting` asks for at once."""
_RE_USB_INTERFACE = re.compile(r':\w+\.(\d+)$')
"""Match the interface number ending a port's location, see _usb_interface_number."""
_RE_USB_REVISION = re.compile(r'REV_([0-9A-Fa-f]{4})')
"""Match the device release number (bcdDevice) in a Windows hardware ID."""


class ReadCancelled(Exception):  # noqa: N818 - not an error
    """Raised by :meth:`ExtendedSerial.read_waiting` when the wait was cancelled."""


class PortBusyError(SerialException):
    """Another thread owns the port's replies, see :meth:`.exclusive_reads`."""


class ExtendedSerial(Serial):
    """Enhances pySerial's :class:`~serial.Serial` with additional functionality.

    Safe to share between threads: writes don't interleave (:attr:`write_lock`), and
    the replies belong to one thread at a time (:meth:`exclusive_reads`).
    """

    _read_cancelled: bool = False
    """A :meth:`cancel_read` that no read has reported yet."""

    write_lock: threading.RLock
    """Held while writing; take it to make several writes one. pySerial writes large
    buffers in pieces on POSIX, and doesn't support concurrent writes on Windows."""

    read_lock: threading.Lock
    """Taken by whoever the port's replies belong to: a thread in a conversation (see
    :meth:`exclusive_reads`), or a thread streaming them (see :meth:`claim_replies`)."""

    _read_owner: int | None = None
    """The thread owning the replies through :meth:`exclusive_reads`, if any."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.write_lock = threading.RLock()
        self.read_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    @override
    def cancel_read(self) -> None:
        """Cancel a blocking read; :meth:`read_waiting` reports it as ReadCancelled."""
        self._read_cancelled = True
        super().cancel_read()

    @override
    def write(self, data: Buffer, /) -> int | None:
        """Write to the port, as one piece: no other thread's write gets in between."""
        with self.write_lock:
            return super().write(data)

    @override
    def read(self, size: int = 1) -> bytes:
        """Read from the port; the replies must not belong to another thread.

        Raises
        ------
        PortBusyError
            If another thread owns the port's replies, see :attr:`read_lock`.
        """
        if self._read_owner == threading.get_ident():  # inside a conversation
            return super().read(size)
        with self.exclusive_reads():
            return super().read(size)

    @contextlib.contextmanager
    def exclusive_reads(self) -> Iterator[None]:
        """Own the port's replies for a conversation of several writes and reads.

        Nests: a thread that owns them already keeps them until the outermost exit.

        Raises
        ------
        PortBusyError
            If another thread owns the port's replies, or a reader streams them.
        """
        me = threading.get_ident()
        if self._read_owner == me:  # already ours, e.g. a query inside an exchange
            yield
            return
        # the owner changes under the write lock: claim_replies() runs under it and
        # must not take a conversation whose owner isn't recorded yet for a reader
        with self.write_lock:
            if not self.read_lock.acquire(blocking=False):
                raise PortBusyError("Another thread owns the port's replies")
            self._read_owner = me
        try:
            yield
        finally:
            with self.write_lock:
                self._read_owner = None
                self.read_lock.release()

    def claim_replies(self) -> bool:
        """Take the replies for a thread that streams them with :meth:`read_waiting`.

        The streaming thread doesn't own them (:attr:`_read_owner` stays unset): it
        releases :attr:`read_lock` from its own thread when done. Returns False if a
        streaming thread holds them already; it is then that thread's job to pass them
        on. Call under :attr:`write_lock` when that thread ends under it too, so its
        end can't slip in between.

        Raises
        ------
        PortBusyError
            If a thread owns the replies for a conversation.
        """
        if self.read_lock.acquire(blocking=False):
            return True
        if self._read_owner is None:
            return False
        raise PortBusyError("Another thread owns the port's replies")

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

    def read_waiting(self) -> bytes:
        """
        Block until at least one byte is pending, then return all pending bytes.

        On POSIX, the port's file descriptor is waited on and read directly, in one
        call each, ignoring the port's timeout; elsewhere :meth:`read` is used, which
        returns nothing once the port's timeout elapses. A read returns at most what the
        OS hands out at once, on Linux about 4 KB.

        Returns
        -------
        bytes
            The pending bytes, at least one - or none when, off POSIX, the port's
            timeout elapsed.

        Raises
        ------
        ReadCancelled
            If :meth:`cancel_read` ended the wait.
        SerialException
            If the port is closed, or if it reports pending bytes but returns none.
        """
        if not _DIRECT_READ:  # a streaming thread holds the replies without owning them
            data = super().read(max(1, self.in_waiting))
            if not data and self._read_cancelled:
                self._read_cancelled = False
                raise ReadCancelled
            return data
        fd = self.fileno()  # raises if the port is closed
        abort = cast('int', self.pipe_abort_read_r)  # written to by cancel_read()
        try:
            ready, _, _ = select.select([fd, abort], [], [])
            if abort in ready:
                os.read(abort, 1000)  # consume the cancellation
                self._read_cancelled = False
                raise ReadCancelled
            data = os.read(fd, _READ_SIZE)
        except OSError as e:
            raise SerialException(f'read failed: {e}') from e
        if not data:
            raise SerialException(
                'device reports readiness to read but returned no data '
                '(device disconnected or multiple access on port?)'
            )
        return data

    def discard_cancel_read(self) -> None:
        """Withdraw a :meth:`cancel_read` that no read has reported yet."""
        self._read_cancelled = False
        if not _DIRECT_READ:
            return  # pyserial's cancellation leaves nothing pending off POSIX
        abort = cast('int', self.pipe_abort_read_r)
        if select.select([abort], [], [], 0)[0]:
            os.read(abort, 1000)

    def write_struct(self, fmt: str | Struct, *data: Any) -> int | None:
        """
        Write structured data to the serial port.

        This method packs the provided data into a binary format according to the
        specified format string and writes it to the serial port.

        Parameters
        ----------
        fmt : str or struct.Struct
            A pre-compiled struct or a format string compatible with the :mod:`struct`
            module's `format specifications
            <https://docs.python.org/3/library/struct.html#format-strings>`__.
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

        See Also
        --------
        `Format specifications
        <https://docs.python.org/3/library/struct.html#format-strings>`__ used by the
        :mod:`struct` module.
        """
        s = fmt if isinstance(fmt, Struct) else Struct(fmt)
        buffer = s.pack(*data)
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
            <https://docs.python.org/3/library/struct.html#format-strings>`__.

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

        See Also
        --------
        `Format specifications
        <https://docs.python.org/3/library/struct.html#format-strings>`__ used by the
        :mod:`struct` module.
        """
        s = fmt if isinstance(fmt, Struct) else Struct(fmt)
        return s.unpack(self.read(s.size))

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
            <https://docs.python.org/3/library/struct.html#format-strings>`__.
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

        See Also
        --------
        `Format specifications
        <https://docs.python.org/3/library/struct.html#format-strings>`__ used by the
        :mod:`struct` module.
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
            <https://docs.python.org/3/library/struct.html#format-strings>`__.
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

        See Also
        --------
        `Format specifications
        <https://docs.python.org/3/library/struct.html#format-strings>`__ used by the
        :mod:`struct` module.
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
        with self.exclusive_reads():  # before the write: no query to another's stream
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

    def verify(
        self,
        query: Buffer = b'',
        expected_response: bytes = b'\x01',
        timeout: float | None = None,
    ) -> bool:
        r"""
        Verify the response of the serial port.

        This method sends a query to the serial port and checks if the response
        matches the expected response.

        Parameters
        ----------
        query : Buffer, default: b''
            The query to be sent to the serial port.
        expected_response : bytes, default: b'\x01'
            The expected response from the serial port. Its length determines the
            number of bytes read from the serial port. An empty value reads no bytes
            and always compares equal.
        timeout : float or None, optional
            Read timeout in seconds, applied to this call only. If ``None``, the
            port's configured timeout is used.

        Returns
        -------
        bool
            ``True`` if the response matches the expected response, ``False`` otherwise.
            A timeout yields a short response and hence returns ``False``.

        Examples
        --------
        Send a handshake byte and check for acknowledgement::

            if not serial_port.verify(b'\x48'):
                raise RuntimeError('Device did not acknowledge handshake')
        """
        with self.temporary_timeout(timeout):
            return self.query(query, len(expected_response)) == expected_response

    @contextlib.contextmanager
    def temporary_timeout(self, timeout: float | None) -> Iterator[None]:
        """
        Temporarily override the port's read timeout.

        Parameters
        ----------
        timeout : float or None
            Read timeout in seconds to apply within the context. If ``None``, the
            port's current timeout is left unchanged.

        Yields
        ------
        None
        """
        if timeout is None:
            yield
            return
        original_timeout = self.timeout
        self.timeout = timeout
        try:
            yield
        finally:
            self.timeout = original_timeout


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


def find_ports(
    *,
    device: Filter[str] | None = None,
    name: Filter[str] | None = None,
    description: Filter[str] | None = None,
    hwid: Filter[str] | None = None,
    vid: int | Collection[int] | None = None,
    pid: int | Collection[int] | None = None,
    serial_number: Filter[str] | None = None,
    location: Filter[str] | None = None,
    manufacturer: Filter[str] | None = None,
    product: Filter[str] | None = None,
    interface: Filter[str] | None = None,
) -> list[ListPortInfo]:
    r"""
    Find serial ports matching specified criteria.

    Each parameter filters on the :class:`~serial.tools.list_ports_common.ListPortInfo`
    attribute of the same name. Multiple filters use AND logic. Collections within a
    single filter use OR logic. Filters set to ``None`` are ignored.

    Parameters
    ----------
    device : Filter, optional
        Full device path, e.g., ``'/dev/ttyACM0'`` or ``'COM3'``.
    name : Filter, optional
        Short device name.
    description : Filter, optional
        Human-readable description.
    hwid : Filter, optional
        Hardware ID.
    vid : int or Collection of int, optional
        USB vendor ID.
    pid : int or Collection of int, optional
        USB product ID.
    serial_number : Filter, optional
        USB serial number.
    location : Filter, optional
        USB location.
    manufacturer : Filter, optional
        USB manufacturer.
    product : Filter, optional
        USB product.
    interface : Filter, optional
        USB interface.

    Returns
    -------
    list of ListPortInfo
        Ports matching all criteria.

    See Also
    --------
    bpod_core.misc.Filter : Semantics of the individual filter values.

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
    """
    return _filter_ports(sorted(comports()), **locals())


def _usb_interface_number(port: ListPortInfo) -> int | None:
    """The number of the USB interface behind a port, if its location tells.

    pyserial ends the location of a port on a composite USB device with the
    configuration and interface on Linux (e.g., ``3-2:1.0``) and with ``x`` and the
    interface on Windows (``1-2:x.0``); macOS doesn't report the interface.
    """
    match = _RE_USB_INTERFACE.search(port.location or '')
    return int(match[1]) if match else None


def _filter_ports(
    ports: Iterable[ListPortInfo], **filters: Filter[Any] | None
) -> list[ListPortInfo]:
    """Return the ports whose attributes match the filters (``None`` is ignored)."""
    criteria = {k: v for k, v in filters.items() if v is not None}
    return [
        port
        for port in ports
        if all(_match_filter(getattr(port, k), v) for k, v in criteria.items())
    ]


def verify_serial_discovery(
    port: str,
    expected_message: bytes,
    timeout: float = 1,
    trigger: Callable[[], Any] | None = None,
    *,
    raise_dtr: bool = False,
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
    raise_dtr : bool, default: False
        Open the port with DTR low and raise it once the port is open: a device that
        sends its discovery message when a host asserts DTR then does so after the
        port's input buffer has been cleared on opening, rather than into it. Costs
        two USB control transfers.

    Returns
    -------
    bool
        True if the device sent the expected message within the timeout period,
        False otherwise (including if the port cannot be opened).
    """
    try:
        ser = Serial(timeout=timeout)
        if raise_dtr:
            ser.dtr = False
        ser.port = port
        with ser:
            if raise_dtr:
                ser.dtr = True
            if trigger is not None:
                trigger()
            return ser.read(len(expected_message)) == expected_message
    except SerialException:
        return False


_BCD_DEVICES: dict[tuple[int, int, str], int] = {}
"""The release numbers found by :func:`_read_bcd_device`, by vid, pid and serial."""


def _read_bcd_device(port: ListPortInfo) -> int | None:
    """Read the USB device release number (bcdDevice) of the device behind a port.

    pyserial reads the port's other USB descriptor fields, but not this one. Teensy
    boards report their model in it, e.g., 0x0280 for a Teensy 4.1. A number found
    is kept for the device's vid, pid and serial number.

    Parameters
    ----------
    port : ListPortInfo
        The port of the device.

    Returns
    -------
    int or None
        The release number, or None if the port isn't a USB port with a serial
        number, the platform isn't supported, or the lookup failed (e.g., for a
        device unplugged on Linux or macOS; Windows keeps the records of absent
        devices).
    """
    if port.vid is None or port.pid is None or not port.serial_number:
        return None  # no way to tell the device from one that took over its port
    key = (port.vid, port.pid, port.serial_number)
    if (bcd_device := _BCD_DEVICES.get(key)) is None:
        bcd_device = _look_up_bcd_device(port)
        if bcd_device is not None:  # a failed lookup may succeed later
            _BCD_DEVICES[key] = bcd_device
    return bcd_device


def _look_up_bcd_device(port: ListPortInfo) -> int | None:
    """Look up bcdDevice in the operating system's records (see _read_bcd_device).

    Linux and macOS find the USB device by the port's path or name, which another
    device can take over after a replug: its serial number must match the port's.
    """
    try:
        if sys.platform == 'win32':  # the hardware IDs in the USB device's registry key
            import winreg  # noqa: PLC0415 - Windows only

            # the USB device's node is named after its serial number, also for a
            # composite device, whose serial ports are nodes below it
            key = (
                r'SYSTEM\CurrentControlSet\Enum\USB'
                rf'\VID_{port.vid:04X}&PID_{port.pid:04X}\{port.serial_number}'
            )
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
                hardware_ids, _ = winreg.QueryValueEx(handle, 'HardwareID')
            # e.g., USB\VID_16C0&PID_048C&REV_0280
            match = _RE_USB_REVISION.search(' '.join(hardware_ids))
            return int(match[1], 16) if match else None
        if sys.platform == 'darwin':  # the I/O Registry, with pyserial's IOKit helpers
            from serial.tools import list_ports_osx as iokit  # noqa: PLC0415 - IOKit

            for service in iokit.GetIOServicesByType('IOSerialBSDClient'):
                if iokit.get_string_property(service, 'IOCalloutDevice') == port.device:
                    usb_device = iokit.GetParentDeviceByType(service, 'IOUSBHostDevice')
                    if not usb_device:
                        usb_device = iokit.GetParentDeviceByType(service, 'IOUSBDevice')
                    serial_number = usb_device and iokit.get_string_property(
                        usb_device, iokit.kUSBSerialNumberString
                    )
                    if usb_device and serial_number == port.serial_number:
                        return iokit.get_int_property(
                            usb_device, 'bcdDevice', iokit.kCFNumberSInt32Type
                        )
            return None
        if (path := getattr(port, 'usb_device_path', None)) is None:  # pyserial's SysFS
            return None  # not a USB port
        sysfs = Path(path)
        same_device = (sysfs / 'serial').read_text().strip() == port.serial_number
        return int((sysfs / 'bcdDevice').read_text(), 16) if same_device else None
    except (OSError, ValueError):
        return None


class SerialDevice(AbstractContextManager):
    r"""Base class for implementing drivers for USB serial devices.

    Handles connection lifecycle — opening, closing, and cleanup on garbage collection —
    and provides subclasses with an :class:`ExtendedSerial` connection and port
    metadata. Derive from this class instead of using :class:`serial.Serial` directly to
    get automatic resource management and consistent error handling.

    Parameters
    ----------
    port : str or ListPortInfo
        The serial port device path (e.g., '/dev/ttyUSB0' or 'COM3'), or the port's
        :class:`~serial.tools.list_ports_common.ListPortInfo` if already enumerated.
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
        port: str | ListPortInfo,
        *,
        open_connection: bool = True,
    ) -> None:
        # obtain ListPortInfo for device on specified port
        if not isinstance(port, ListPortInfo):
            port_info = next((p for p in comports() if p.device == port), None)
            if port_info is None:
                raise SerialException(
                    f'Failed to connect to {self._serial_device_name} - '
                    f'serial port not found: {port}'
                )
            port = port_info
        self._port_info = port

        # instantiate ExtendedSerial
        self._serial = ExtendedSerial()
        self._serial.port = self._port_info.device  # only set port after the fact

        # finalizer for cleanup on garbage collection
        self._serial_device_finalizer: weakref.finalize | None = None

        # open connection
        if open_connection:
            self.open()

    def _rename_serial_device(self, new_name: str) -> None:
        """
        Rename the serial device.

        Use this method to change the name of the serial device after instantiation.
        It will ensure that the finalizer is updated to reflect the new name.

        Parameters
        ----------
        new_name : str
            The new name of the serial device.
        """
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

    @override
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
    def _bcd_device(self) -> int | None:
        """The USB device release number (bcdDevice), or None if unknown."""
        return _read_bcd_device(self._port_info)

    @property
    def port(self) -> str:
        """The name of the serial port.

        Returns
        -------
        str
            The device path of the serial port (e.g., '/dev/ttyACM0').
        """
        return self._port_info.device
