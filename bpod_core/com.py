"""Module providing extended serial communication functionality."""

import contextlib
import errno
import logging
import os
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


class DualChannelMessage(msgspec.Struct, omit_defaults=True):
    type: str = msgspec.field(name='T')  # message type
    data: Any | None = msgspec.field(default=None, name='D')  # message data


class DualChannelHost:
    _encoder: msgspec.msgpack.Encoder | msgspec.json.Encoder
    _decoder: msgspec.msgpack.Decoder | msgspec.json.Decoder
    rep_ipc_addr: str | None = None
    pub_ipc_addr: str | None = None

    def __init__(
        self,
        name: str,
        service_type: str,
        txt_record: dict[str | bytes, str | bytes | None] | None = None,
        event_handler: Callable[[Any | None], Any] | None = None,
        port_pub: int | None = None,
        port_rep: int | None = None,
        remote: bool = True,
        serialization: Literal['json', 'msgpack'] = 'msgpack',
    ) -> None:
        self._closed = False
        self._close_lock = threading.Lock()
        self._finalizer = weakref.finalize(self, self.close)

        self.name = convert_to_snake_case(name).strip('_')
        self.uuid = uuid.uuid4()

        # ZeroMQ context and sockets
        self.zmq_context = zmq.Context()
        self.rep_socket = self.zmq_context.socket(zmq.REP)
        self.pub_socket = self.zmq_context.socket(zmq.PUB)

        # socket options / high-water marks
        self.pub_socket.setsockopt(zmq.SNDHWM, 1000)
        self.rep_socket.setsockopt(zmq.SNDHWM, 1000)
        self.rep_socket.setsockopt(zmq.RCVHWM, 1000)

        # bind sockets to IPC addresses (POSIX only)
        # clients on localhost can upgrade to IPC (named pipes) for improved performance
        if 'win' not in sys.platform:
            self.rep_ipc_addr = f'ipc:///tmp/REQ_REP_{self.uuid.hex}.ipc'
            self.pub_ipc_addr = f'ipc:///tmp/PUB_SUB_{self.uuid.hex}.ipc'
            try:
                self.rep_socket.bind(self.rep_ipc_addr)
                self.pub_socket.bind(self.pub_ipc_addr)
                logger.debug("Binding REP socket to '%s'", self.rep_ipc_addr)
                logger.debug("Binding PUB socket to '%s'", self.pub_ipc_addr)
            except zmq.ZMQError:
                logger.warning('Failed to bind IPC sockets; continuing without IPC')
                self.rep_ipc_addr = None
                self.pub_ipc_addr = None

        # bind sockets to TCP addresses
        self.bind_ip = '0.0.0.0' if remote else '127.0.0.1'
        self.rep_tcp_addr, self.rep_tcp_port = self._bind_tcp(self.rep_socket, port_rep)
        self.pub_tcp_addr, self.pub_tcp_port = self._bind_tcp(self.pub_socket, port_pub)
        logger.debug("Binding REP socket to '%s'", self.rep_tcp_addr)
        logger.debug("Binding PUB socket to '%s'", self.pub_tcp_addr)

        # select serialization protocol / initialize encoders + decoders
        self._serialization_protocol = serialization
        if serialization == 'msgpack':
            self._encoder = msgspec.msgpack.Encoder()
            self._decoder = msgspec.msgpack.Decoder(type=DualChannelMessage)
        elif serialization == 'json':
            self._encoder = msgspec.json.Encoder()
            self._decoder = msgspec.json.Decoder(type=DualChannelMessage)
        else:
            raise ValueError(f'Unsupported serialization protocol: {serialization}')

        # start event loop for request handling
        self._stop_event_loop = threading.Event()
        self._user_event_handler = event_handler or self._empty_event_handler
        self._event_handler_lock = threading.RLock()
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
            properties=txt_record or {},
            server=f'{socket.gethostname()}.local.',
        )
        self._zeroconf.register_service(self._service_info, allow_name_change=True)
        self.service_name = self._service_info.name
        logger.debug("Registering Zeroconf service '%s'", self.service_name)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @staticmethod
    def _empty_event_handler(_: Any) -> dict:
        return {}

    def _bind_tcp(
        self, zmq_socket: zmq.Socket, tcp_port: int | None
    ) -> tuple[str, int]:
        tcp_address = f'tcp://{self.bind_ip}'
        if tcp_port is not None:
            try:
                zmq_socket.bind(f'{tcp_address}:{tcp_port}')
            except zmq.ZMQError:
                logger.error('Could not bind to port %d - using random port', tcp_port)
                tcp_port = None
        if tcp_port is None:
            tcp_port = zmq_socket.bind_to_random_port(tcp_address)
        return f'{tcp_address}:{tcp_port}', tcp_port

    def _event_loop(self) -> None:
        while not self._stop_event_loop.is_set():
            # wait for incoming requests (short poll so we can check stop_event)
            if not self.rep_socket.poll(100):
                continue

            # guard the actual recv with try/except so shutdown can't hang us
            try:
                request_frame = self.rep_socket.recv(copy=False)
            except zmq.ZMQError as e:
                logger.exception('Error receiving request from client', exc_info=e)

            # try to decode the request
            try:
                request: DualChannelMessage = self._decoder.decode(request_frame.bytes)
            except msgspec.DecodeError as e:
                # try the other serialization as a fallback
                try:
                    if self._serialization_protocol == 'msgpack':
                        request = msgspec.json.decode(
                            request_frame.bytes, type=DualChannelMessage
                        )
                    else:
                        request = msgspec.msgpack.decode(
                            request_frame.bytes, type=DualChannelMessage
                        )
                except msgspec.DecodeError:
                    logger.exception(
                        'Error decoding request from client: %s',
                        request_frame.bytes,
                        exc_info=e,
                    )
                    reply = self._format_error(type(e).__name__, e.args[0])
                    try:
                        reply_bytes = self._encoder.encode(reply)
                        self.rep_socket.send(reply_bytes, copy=False)
                    except (msgspec.EncodeError, zmq.ZMQError) as e:
                        logger.exception('Error sending reply to client', exc_info=e)
                    continue

            # handle request depending on request type
            match request.type:
                case 'R':  # general request
                    try:
                        with self._event_handler_lock:
                            reply_data = self._user_event_handler(request.data)
                    except Exception as e:
                        logger.exception(
                            'Event handler raised an exception', exc_info=e
                        )
                        reply = self._format_error(type(e).__name__, e.args[0])
                    else:
                        reply = DualChannelMessage('R', reply_data)

                case 'H':  # handshake for communicating TCP and IPC addresses
                    reply = DualChannelMessage(
                        type='H',
                        data={
                            'ipc_pub_sub': self.pub_ipc_addr,
                            'ipc_req_rep': self.rep_ipc_addr,
                            'tcp_pub_sub': self.pub_tcp_addr,
                            'tcp_req_rep': self.rep_tcp_addr,
                        },
                    )

                case _:  # unknown request type
                    message = f'Unknown request type: {request.type}'
                    logger.error(message)
                    reply = self._format_error('RequestError', message)

            # encode reply
            try:
                reply_bytes = self._encoder.encode(reply)
            except msgspec.EncodeError as e:
                logger.exception('Error encoding reply to client', exc_info=e)
                reply = self._format_error(type(e).__name__, e.args[0])
                reply_bytes = self._encoder.encode(reply)

            # send reply
            try:
                self.rep_socket.send(reply_bytes, copy=False)
            except zmq.ZMQError as e:
                logger.exception('Error sending reply to client', exc_info=e)

    @staticmethod
    def _format_error(name: str, message: str) -> DualChannelMessage:
        return DualChannelMessage(type='E', data={'name': name, 'message': message})

    def close(self) -> None:
        """Close the ZeroMQ service and unregister the Zeroconf advertisement."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

            # Stop event loop
            if self._event_thread.is_alive():
                self._stop_event_loop.set()
                self._event_thread.join()

            # Unregister Zeroconf service
            logger.debug("Unregistering Zeroconf service '%s'", self.service_name)
            self._zeroconf.unregister_service(self._service_info)
            self._zeroconf.close()

            # close ZMQ sockets and terminate context
            self.pub_socket.close(linger=0)
            self.rep_socket.close(linger=0)
            self.zmq_context.term()

            # remove IPC files
            with contextlib.suppress(FileNotFoundError):
                if self.rep_ipc_addr is not None:
                    os.remove(self.rep_ipc_addr[6:])
                if self.pub_ipc_addr is not None:
                    os.remove(self.pub_ipc_addr[6:])


class DualChannelClient:
    _encoder: msgspec.msgpack.Encoder | msgspec.json.Encoder
    _decoder: msgspec.msgpack.Decoder | msgspec.json.Decoder

    def __init__(
        self,
        service_type: str,
        address: str | None = None,
        topic: str = '',
        event_handler: Callable[[dict], Any] | None = None,
        discovery_timeout: float = 10.0,
        properties: dict | None = None,
    ):
        self._closed = False
        self._close_lock = threading.Lock()
        self._req_lock = threading.Lock()
        self._finalizer = weakref.finalize(self, self.close)

        self.zmq_context = zmq.Context()
        self.req_socket = self.zmq_context.socket(zmq.REQ)
        self.sub_socket = self.zmq_context.socket(zmq.SUB)

        # define msgspec encoder/decoder
        self._serialization = 'msgpack'
        serialization_module = getattr(msgspec, self._serialization)
        self._encoder = serialization_module.Encoder()
        self._decoder = serialization_module.Decoder(type=DualChannelMessage)

        # connect REQ channel
        if address is not None:
            self.req_address = address
        else:
            self.req_address, txt_record = discover_device(
                service_type, properties, discovery_timeout
            )
        self.req_socket.connect(self.req_address)
        logger.debug("Binding REQ socket to '%s'", self.req_address)
        self.protocol = 'TCP'
        self.is_local = '127.0.0.1' in self.req_address or '0.0.0.0' in self.req_address

        # perform handshake
        self._handshake()

        # connect SUB channel
        self.sub_topic = topic
        self.sub_socket.connect(self.sub_address)
        self.sub_socket.setsockopt_string(zmq.SUBSCRIBE, self.sub_topic)
        logger.debug("Binding SUB socket to '%s'", self.sub_address)

        # start event loop for subscription handling
        self._stop_event_loop = threading.Event()
        self._event_handler = event_handler
        self._event_thread = None
        if self._event_handler:
            self._start_event_loop()

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

            if self._event_thread and self._event_thread.is_alive():
                self._stop_event_loop.set()
                self._event_thread.join()

            self.req_socket.close(linger=0)
            self.sub_socket.close(linger=0)
            self.zmq_context.term()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _start_event_loop(self):
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._event_thread.start()

    def _event_loop(self):
        while not self._stop_event_loop.is_set():
            if not self.sub_socket.poll(100):
                continue
            msg = self.sub_socket.recv()
            msg = self._decoder.decode(msg)
            try:
                self._event_handler(msg)
            except Exception as e:
                logger.exception('Subscription handler raised an exception', exc_info=e)

    def _handshake(self):
        reply_type, reply_data = self._req('H')
        if (
            self.is_local
            and sys.platform in ('darwin', 'linux')
            and reply_data.get('ipc_req_rep') is not None
            and reply_data.get('ipc_pub_sub') is not None
        ):
            self.req_socket.disconnect(self.req_address)
            self.req_address = reply_data.get('ipc_req_rep')
            logger.debug("Rebinding REQ socket to '%s'", self.req_address)
            self.req_socket.connect(self.req_address)
            self.sub_address = reply_data.get('ipc_pub_sub')
            self.protocol = 'IPC'
        else:
            self.sub_address = reply_data.get('tcp_pub_sub')

    def _req(self, request_type: str, data: Any | None = None) -> tuple[str, Any]:
        # acquire lock
        with self._req_lock:
            # encode and send request
            try:
                request = DualChannelMessage(type=request_type, data=data)
                request_bytes = self._encoder.encode(request)
            except msgspec.EncodeError as e:
                raise ValueError(f'Invalid request: {request}') from e
            self.req_socket.send(request_bytes)

            # receive and decode reply
            reply_frame = self.req_socket.recv(copy=False)
            try:
                reply: DualChannelMessage = self._decoder.decode(reply_frame.bytes)

            # switch serialization format
            except msgspec.DecodeError as e:
                new_serialization = ({'json', 'msgpack'} - {self._serialization}).pop()
                new_serialization_module = getattr(msgspec, new_serialization)
                new_decoder = new_serialization_module.Decoder(type=DualChannelMessage)
                try:
                    reply = new_decoder.decode(reply_frame.bytes)
                except msgspec.DecodeError:
                    raise ValueError('Invalid reply') from e
                else:
                    logger.debug(f'Switching to {new_serialization} serialization')
                    self._encoder = new_serialization_module.Encoder()
                    self._decoder = new_decoder
                    self._serialization = new_serialization

            return reply.type, reply.data

    def request(self, **kwargs) -> Any:
        reply_type, reply_data = self._req('R', kwargs)
        match reply_type:
            case 'R':
                return reply_data
            case 'E':
                self.log_remote_error(
                    reply_data.get('name', 'Error'), reply_data.get('message', '')
                )
            case _:
                logger.error("Received unknown reply type: '%s'", reply_type)
        return {}

    @staticmethod
    def log_remote_error(name: str, message: str) -> None:
        logger.error('Remote %s: %s', name, message)
