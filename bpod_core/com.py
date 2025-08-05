"""Module providing extended serial communication functionality."""

import errno
import json
import logging
import re
import socket
import struct
import threading
import weakref
from collections.abc import Iterable
from typing import Any, TypeAlias

import numpy as np
import zmq
from serial import Serial
from serial.serialutil import to_bytes as serial_to_bytes  # type: ignore[attr-defined]
from serial.threaded import Protocol
from typing_extensions import Buffer, Self
from zeroconf import (
    ServiceBrowser,
    ServiceInfo,
    ServiceStateChange,
    Zeroconf,
)
from zmq import Context

from bpod_core.misc import convert_to_snake_case

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
        unsigned 8-bit integers, strings (interpreted as utf-8) and iterables.

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
        """Initialize the protocol."""
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

        Parameters
        ----------
        data_chunk : bytearray
        """


def to_bytes(data: ByteLike) -> bytes:  # noqa: PLR0911
    """
    Convert data to bytestring.

    This method extends :meth:`serial.to_bytes` with support for NumPy types,
    unsigned 8-bit integers, strings (interpreted as utf-8) and iterables.

    Parameters
    ----------
    data : ByteLike
        Data to be converted to bytestring.

    Returns
    -------
    bytes
        Data converted to bytestring.
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


def get_local_ipv4() -> str:
    """
    Determine the primary local IPv4 address of the machine.

    This function attempts to determine the IPv4 address of the local machine
    that would be used for an outbound connection to the internet. It does this
    by creating a UDP socket and connecting to a known public IP address
    (Google DNS at 8.8.8.8). No data is sent, but the OS uses the routing table
    to select the appropriate local interface.

    Returns
    -------
    bytes
        The local IPv4 address as a string. If the network is unreachable or
        unavailable, returns the loopback address `127.0.0.1`.

    Raises
    ------
    OSError
        If an unexpected socket error occurs during interface detection.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(('8.8.8.8', 80))  # Doesn't have to be reachable
            return str(s.getsockname()[0])
        except OSError as e:
            if e.errno in {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL}:
                return '127.0.0.1'
            else:
                raise


class ZMQService:
    def __init__(
        self,
        name: str,
        description: dict[str | bytes, str | bytes | None],
        port: int | None = None,
        socket_type: int = zmq.DEALER,
        local: bool = False,
        service_type: str = '_zmq._tcp.local.',
    ) -> None:
        """
        Initialize a ZeroMQ service with Zeroconf advertisement.

        Parameters
        ----------
        name : str
            The base Zeroconf service instance name. If the name is already taken, a
            suffix is appended automatically.
        description : dict
            A dict published as a TXT record.
        port : int, optional
            The port number to bind the ZeroMQ socket to. If None, a random port is
            selected. Default is None.
        socket_type : int, optional
            The ZeroMQ socket type to create (e.g., zmq.DEALER, zmq.ROUTER).
            Default is zmq.DEALER.
        local : bool, optional
            If True, advertise the service on the loopback address (127.0.0.1).
            Otherwise, advertise on the primary local IPv4 address. Default is False.
        service_type : str, optional
            The Zeroconf service type. Default is '_zmq._tcp.local.'.

        Raises
        ------
        RuntimeError
            If the service cannot be registered after multiple attempts due to name
            conflicts on the Zeroconf network.
        """
        self._closed = False
        self._close_lock = threading.Lock()
        self._finalizer = weakref.finalize(self, self.close)

        self._zmq_context = Context()
        self.ip_address = '127.0.0.1' if local else get_local_ipv4()
        self._bind_address = f'tcp://{self.ip_address}'
        self._zmq_socket = self._zmq_context.socket(socket_type)
        if port is not None:
            try:
                self._zmq_socket.bind(f'{self._bind_address}:{port}')
                self._zmq_port = port
            except zmq.ZMQError:
                logger.debug(
                    'Could not bind ZMQ socket on %s:%d', self._bind_address, port
                )
        if not hasattr(self, '_zmq_port'):
            self._zmq_port = self._zmq_socket.bind_to_random_port(self._bind_address)
        logger.debug('Opening ZMQ socket on %s:%d', self._bind_address, self._zmq_port)

        self._zeroconf = Zeroconf()
        self._service_type = service_type
        self._register_service(name, description)

    def close(self) -> None:
        """Close the ZeroMQ service and unregister the Zeroconf advertisement."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            if self._zeroconf is not None:
                self._unregister_service()
                self._zeroconf.close()
            logger.debug(
                'Closing ZMQ socket on %s:%d', self._bind_address, self._zmq_port
            )
            self._zmq_socket.close(linger=0)
            self._zmq_context.term()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @property
    def port(self) -> int:
        """Get the port number of the ZeroMQ socket."""
        return self._zmq_port

    @property
    def bind_address(self) -> str:
        """Get the bind address of the ZeroMQ socket."""
        return self._bind_address

    @property
    def service_name(self) -> str:
        """Get the name of the Zeroconf service."""
        return self._service_info.name

    def _escape_service_name(self, name: str) -> str:
        name = name.removesuffix('.' + self._service_type)
        return f'{convert_to_snake_case(name)}.{self._service_type}'

    def _register_service(
        self, name: str, txt_record: dict[str | bytes, str | bytes | None]
    ) -> None:
        """Register a Zeroconf service."""
        service_info = ServiceInfo(
            type_=self._service_type,
            name=self._escape_service_name(name),
            port=self.port,
            addresses=[socket.inet_aton(self.ip_address)],
            properties=txt_record,
            server=f'{socket.gethostname()}.local.',
        )
        self._zeroconf.register_service(service_info, allow_name_change=True)
        self._service_info = service_info
        logger.debug("Registering Zeroconf service '%s'", self.service_name)

    def _unregister_service(self):
        """Unregister the Zeroconf service."""
        logger.debug("Unregistering Zeroconf service '%s'", self.service_name)
        self._zeroconf.unregister_service(self._service_info)
        self._service_info = None


def discover_device(
    service_type: str,
    properties: dict[str, str | None] | None = None,
    timeout: float = 10,
) -> str:
    """
    Discover a Zeroconf device/service on the local network matching given properties.

    Parameters
    ----------
    service_type : str
        The Zeroconf service type to discover, e.g., '_zmq._tcp.local.'
    properties : dict, optional
        Dictionary of expected service properties to match.
    timeout : float, optional
        How many seconds to wait for a matching service before timing out.
        Default is 10.

    Returns
    -------
    str
        The Zeroconf service address, e.g., 'tcp://192.168.1.10:1234'.

    Raises
    ------
    TimeoutError
        If no matching device/service is found within the timeout period.
    """
    properties = properties or {}
    address = ''
    protocol = (m := re.search(r'_(tcp|udp)\.', service_type)) and m.group(1)
    event = threading.Event()

    def on_state_change(*, name: str, state_change: ServiceStateChange, **_):
        nonlocal address, protocol
        if state_change is ServiceStateChange.Added:
            info = zeroconf.get_service_info(service_type, name)
            if not info or not info.addresses:
                return
            for k, v in properties.items():
                key = k.encode('utf-8') if isinstance(k, str) else k
                value = v.encode('utf-8') if isinstance(v, str) else v
                if info.properties.get(key) != value:
                    return
            port = info.port
            ip = socket.inet_ntoa(info.addresses[0])
            ip = '127.0.0.1' if ip == get_local_ipv4() else ip
            address = f'{protocol}://{ip}:{port}'
            event.set()

    zeroconf = Zeroconf()
    try:
        ServiceBrowser(zeroconf, service_type, handlers=[on_state_change])
        found = event.wait(timeout)
    finally:
        zeroconf.close()
    if not found:
        raise TimeoutError('No device found via Zeroconf')
    return address


class RemoteBpod:
    def __init__(
        self,
        address: str | None = None,
        name: str | None = None,
        serial_number: str | None = None,
        location: str | None = None,
        timeout: float = 10.0,
    ):
        self._zmq_context = Context()
        self._req_socket = self._zmq_context.socket(zmq.REQ)
        self._sub_socket = self._zmq_context.socket(zmq.SUB)

        self._connect(
            address,
            timeout,
            name=name,
            serial=serial_number,
            location=location,
        )

    def _connect(self, address: str | None, timeout: float, **kwargs) -> None:
        if address is not None:
            self._zmq_address = address
        else:
            props = {k: v for k, v in kwargs.items() if v is not None}
            try:
                self._zmq_address = discover_device('_bpod._tcp.local.', props, timeout)
            except TimeoutError as e:
                p = ', '.join([f'{k} = "{v}"' for k, v in props.items()])
                raise TimeoutError(f'Failed to discover remote Bpod with {p}.') from e
            logger.debug('Discovered Bpod on %s', self._zmq_address)
        self._req_socket.connect(self._zmq_address)

    def _request(self, **kwargs) -> Any:
        """
        Send a JSON-encoded request over the ZeroMQ socket and receive the reply.

        Parameters
        ----------
        **kwargs
            Arbitrary keyword arguments representing the request payload to be
            serialized and sent.

        Returns
        -------
        Any
            The decoded JSON response received from the remote endpoint.
        """
        encoded_message = json.dumps(kwargs).encode('utf-8')
        self._req_socket.send(encoded_message)
        reply = self._req_socket.recv()
        return json.loads(reply.decode('utf-8'))

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
        reply = self._request(type='call', method=method, args=args, kwargs=kwargs)
        if reply.get('success'):
            return reply['result']
        logger.error(f'Remote {reply["error"]["type"]}: ' + reply['error']['message'])
        return None
