"""Module providing extended serial communication functionality."""

import errno
import logging
import re
import socket
import struct
import sys
import threading
import uuid
import weakref
from collections.abc import Callable, Iterable
from typing import Any, Literal, TypeAlias

import msgspec
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


def discover_device(
    service_type: str,
    properties: dict[str, str | None] | None = None,
    timeout: float = 10,
) -> tuple[str, dict[bytes, bytes | None]]:
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
    dict
        The TXT record of the service

    Raises
    ------
    TimeoutError
        If no matching device/service is found within the timeout period.
    """
    properties = properties or {}
    address = ''
    protocol = (m := re.search(r'_(tcp|udp)\.', service_type)) and m.group(1)
    event = threading.Event()
    txt_record = {}

    def on_state_change(*, name: str, state_change: ServiceStateChange, **_):
        nonlocal address, protocol, txt_record, event
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
            txt_record = info.properties
            event.set()

    zeroconf = Zeroconf()
    try:
        ServiceBrowser(zeroconf, service_type, handlers=[on_state_change])
        found = event.wait(timeout)
    finally:
        zeroconf.close()
    if not found:
        raise TimeoutError('No matching device found via Zeroconf')
    return address, txt_record


class ReqRepStruct(msgspec.Struct, omit_defaults=True):
    t: str  # message type
    d: Any | None = None  # message data


class DualChannelHost:
    def __init__(
        self,
        name: str,
        service_type: str,
        event_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        description: dict[str | bytes, str | bytes | None] | None = None,
        port_pub: int | None = None,
        port_rep: int | None = None,
        remote: bool = False,
        serialization: Literal['json', 'msgpack'] = 'json',
    ) -> None:
        self._closed = False
        self._close_lock = threading.Lock()
        self._finalizer = weakref.finalize(self, self.close)

        self.name = convert_to_snake_case(name).strip('_')
        self.uuid = uuid.uuid4()

        # create sockets
        self.zmq_context = Context()
        self.rep_socket = self.zmq_context.socket(zmq.REP)
        self.pub_socket = self.zmq_context.socket(zmq.PUB)

        # bind IPC addresses to sockets
        if sys.platform == 'win32':
            self.rep_ipc_addr = None
            self.pub_ipc_addr = None
        else:
            self.rep_ipc_addr = f'ipc:///tmp/REQ_REP_{self.uuid.hex}.ipc'
            self.pub_ipc_addr = f'ipc:///tmp/PUB_SUB_{self.uuid.hex}.ipc'
            self.rep_socket.bind(self.rep_ipc_addr)
            self.pub_socket.bind(self.pub_ipc_addr)
            logger.debug("Binding REP socket to '%s'", self.rep_ipc_addr)
            logger.debug("Binding PUB socket to '%s'", self.pub_ipc_addr)

        # bind TCP addresses to sockets
        self.bind_ip = '0.0.0.0' if remote else '127.0.0.1'
        self.rep_tcp_addr, self.rep_tcp_port = self._bind_tcp(self.rep_socket, port_rep)
        self.pub_tcp_addr, self.pub_tcp_port = self._bind_tcp(self.pub_socket, port_pub)
        logger.debug("Binding REP socket to '%s'", self.rep_tcp_addr)
        logger.debug("Binding PUB socket to '%s'", self.pub_tcp_addr)

        # select serialization protocol
        self._serialization_protocol = serialization
        if serialization == 'msgpack':
            self._encoder = msgspec.msgpack.Encoder()
            self._decoder = msgspec.msgpack.Decoder()
        elif serialization == 'json':
            self._encoder = msgspec.json.Encoder()
            self._decoder = msgspec.json.Decoder()
        else:
            raise ValueError(f'Unsupported serialization protocol: {serialization}')

        # start event loop for request handling
        self._stop_event_loop = threading.Event()
        self._event_handler = event_handler or self._empty_event_handler
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._event_thread.start()

        # advertise service via Zeroconf
        self.service_type = f'_{service_type.strip("_")}._tcp.local.'
        self.service_name = f'{self.name}.{self.service_type}'
        self._zeroconf = Zeroconf()
        self._service_info = ServiceInfo(
            type_=self.service_type,
            name=self.service_name,
            port=self.rep_tcp_port,
            addresses=[socket.inet_aton(self.bind_ip)],
            properties=description or {},
            server=f'{socket.gethostname()}.local.',
        )
        self._zeroconf.register_service(self._service_info, allow_name_change=True)
        self.service_name = self._service_info.name
        logger.debug("Registering Zeroconf service '%s'", self.service_name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @staticmethod
    def _empty_event_handler(*_) -> dict:
        return {}

    def _bind_tcp(self, zmq_socket, tcp_port):
        tcp_address = f'tcp://{self.bind_ip}'
        if tcp_port is not None:
            try:
                zmq_socket.bind(f'{tcp_address}:{tcp_port}')
            except zmq.ZMQError:
                tcp_port = None
        if tcp_port is None:
            tcp_port = zmq_socket.bind_to_random_port(tcp_address)
        address = f'{tcp_address}:{tcp_port}', tcp_port
        return address

    def _send_reply(self, response_type: str, data: dict | None):
        data = data or {}
        response = {'type': response_type, 'data': data}
        self.rep_socket.send(self._encoder.encode(response))

    def _event_loop(self):
        while not self._stop_event_loop.is_set():
            if not self.rep_socket.poll(100):
                continue
            request = self.rep_socket.recv()
            response = None
            try:
                decoded_request = self._decoder.decode(request)
            except msgspec.ValidationError as e:
                logger.error("The client's message didn’t match the expected schema")
                response = self._format_error(type(e).__name__, e.args[0])
            except msgspec.DecodeError as e:
                try:
                    decoded_request = msgspec.msgpack.decode(request)
                except msgspec.DecodeError:
                    logger.error('Error decoding message from client')
                    response = self._format_error(type(e).__name__, e.args[0])
            if response is None:
                req_type = decoded_request.get('type', 'invalid')
                req_data = decoded_request.get('data', None)
                if req_type == 'invalid' or req_data is None:
                    message = f'Received invalid request: {decoded_request}'
                    logger.error(message)
                    response = self._format_error('RequestError', message)
                elif req_type == 'REQ':
                    response = {
                        'type': 'REP',
                        'data': self._event_handler(decoded_request.get('data', {})),
                    }
                elif req_type == 'handshake':
                    response = {
                        'type': 'handshake',
                        'data': {
                            'ipc_pub_sub': self.pub_ipc_addr,
                            'ipc_req_rep': self.rep_ipc_addr,
                            'tcp_pub_sub': self.pub_tcp_addr,
                            'tcp_req_rep': self.rep_tcp_addr,
                        },
                    }
                else:
                    message = f'Received unknown request type: {req_type}'
                    logger.error(message)
                    response = self._format_error('RequestError', message)
            self.rep_socket.send(self._encoder.encode(response))

    @staticmethod
    def _format_error(name: str, message: str) -> dict:
        return {'type': 'error', 'data': {'name': name, 'message': message}}

    def close(self) -> None:
        """Close the ZeroMQ service and unregister the Zeroconf advertisement."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

            logger.debug("Unregistering Zeroconf service '%s'", self.service_name)
            self._zeroconf.unregister_service(self._service_info)
            self._zeroconf.close()

            self._stop_event_loop.set()
            self._event_thread.join()

            self.pub_socket.close(linger=0)
            self.rep_socket.close(linger=0)
            self.zmq_context.term()


class DualChannelClient:
    def __init__(
        self,
        service_type: str,
        address: str | None = None,
        topic: str = '',
        event_handler: Callable[[dict], Any] | None = None,
        timeout: float = 10.0,
        **kwargs,
    ):
        self.zmq_context = Context()
        self.req_socket = self.zmq_context.socket(zmq.REQ)
        self.sub_socket = self.zmq_context.socket(zmq.SUB)

        # msgspec encoder/decoder
        self._serialization_protocol = 'msgpack'
        self._encoder = msgspec.msgpack.Encoder()
        self._decoder = msgspec.msgpack.Decoder()

        # connect REQ channel
        if address is not None:
            self.req_address = address
        else:
            address, txt_record = discover_device(service_type, kwargs, timeout)
            self.req_address = address
        self.req_socket.connect(self.req_address)
        logger.debug("Binding REQ socket to '%s'", self.req_address)
        self.protocol = 'TCP'
        self.is_local = '127.0.0.1' in address or '0.0.0.0' in address

        # perform handshake
        self._handshake()

        # connect SUB channel
        self.sub_topic = topic
        self.sub_socket.connect(self.sub_address)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, self.sub_topic)
        logger.debug("Binding SUB socket to '%s'", self.req_address)

        # start event loop for subscription handling
        self._event_handler = event_handler or self._empty_event_handler
        # self._running = False
        # self._event_thread = None
        # if self._event_handler:
        #     self._start_event_loop()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @staticmethod
    def _empty_event_handler(*_) -> dict:
        return {}

    def _start_event_loop(self):
        self._running = True
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._event_thread.start()

    # def _event_loop(self):
    #     while self._running:
    #         msg = self.sub_socket.recv()
    #         msg = self._decoder.decode(msg)
    #         self._event_handler(msg)

    def _handshake(self):
        rep_type, rep_data = self._req('handshake')
        if (
            self.is_local
            and sys.platform in ('darwin', 'linux')
            and rep_data.get('ipc_req_rep') is not None
            and rep_data.get('ipc_pub_sub') is not None
        ):
            self.req_socket.unbind(self.req_address)
            self.req_address = rep_data.get('ipc_req_rep')
            logger.debug("Rebinding REQ socket to '%s'", self.req_address)
            self.req_socket.connect(self.req_address)
            self.sub_address = rep_data.get('ipc_pub_sub')
            self._req('handshake')
            self.protocol = 'IPC'
        else:
            self.sub_address = rep_data.get('tcp_pub_sub')

    def _req(
        self, request_type: str, data: dict | None = None
    ) -> tuple[str, dict | None]:
        message = {'type': request_type, 'data': data or {}}
        try:
            encoded_message = self._encoder.encode(message)
        except msgspec.EncodeError as e:
            raise ValueError(f'Invalid request: {message}') from e
        self.req_socket.send(encoded_message)
        reply = self.req_socket.recv()
        try:
            decoded_reply = self._decoder.decode(reply)
        except msgspec.DecodeError as e:
            try:
                decoded_reply = msgspec.json.decode(reply)
            except msgspec.DecodeError:
                raise ValueError('Invalid reply') from e
            else:
                logger.debug('Switching to JSON encoding')
                self._decoder = msgspec.json.Decoder()
                self._encoder = msgspec.json.Encoder()
        rep_type = decoded_reply.get('type', 'invalid')
        rep_data = decoded_reply.get('data', None)
        return rep_type, rep_data

    def request(self, **kwargs) -> Any:
        rep_type, rep_data = self._req('REQ', kwargs)
        if rep_type == 'REP' and rep_data is not None:
            return rep_data
        elif rep_type == 'invalid' or rep_data is None:
            logger.error('Received invalid response')
        elif rep_type == 'error':
            self.log_remote_error(
                rep_data.get('name', 'Error'), rep_data.get('message', '')
            )
        else:
            logger.error("Received unknown response type: '%s'", rep_type)
        return {}

    @staticmethod
    def log_remote_error(name: str, message: str) -> None:
        logger.error('Remote %s: %s', name, message)
