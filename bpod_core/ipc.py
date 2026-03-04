"""Inter-process Communication, service discovery and related."""

import contextlib
import logging
import os
import queue
import socket
import sys
import threading
import time
import traceback
import weakref
from abc import abstractmethod
from collections import deque
from collections.abc import Callable, Iterator
from enum import IntEnum
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, Generic, Literal, NamedTuple, TypeVar, cast, overload
from uuid import UUID, uuid4

import msgspec
import platformdirs
import zmq
from platformdirs import user_runtime_path
from psutil import pid_exists
from pydantic import UUID4, validate_call
from typing_extensions import Self
from zeroconf import (
    InterfaceChoice,
    IPVersion,
    ServiceBrowser,
    ServiceInfo,
    ServiceListener,
    Zeroconf,
)

from bpod_core.constants import IPV4_LOOPBACK, IPV4_WILDCARD
from bpod_core.misc import (
    RE_NON_ALPHANUMERIC,
    get_local_ipv4,
    prune_empty_parent_directories,
    to_snake_case,
)

logger = logging.getLogger(__name__)

T = TypeVar('T')
U = TypeVar('U')


class ServiceError(Exception):
    """Base exception for IPC service errors."""


class RemoteError(ServiceError):
    """Exception representing an error on the remote side."""

    def __init__(self, error_data: 'ErrorData') -> None:
        self.original_error = error_data
        super().__init__(f'Remote {error_data.name}: {error_data.message}')


class MessageKind(IntEnum):
    """The types of messages exchanged between host and clients."""

    HELLO = 0
    """A message sent by the client to initiate the handshake."""
    WELCOME = 1
    """A message sent by the host to acknowledge the client's handshake."""
    REQUEST = 2
    """A request sent by the client."""
    REPLY = 3
    """A reply sent by the host."""
    ERROR = 4
    """An error message."""

    _as_bytes: bytes

    def __new__(cls, value: int) -> Self:
        """Create a new MessageKind instance."""
        if not 0 <= value <= 0xFF:
            raise ValueError('Values must fit in one byte')
        obj: MessageKind = int.__new__(cls, value)  # type: ignore[assignment]
        obj._value_ = value
        obj._as_bytes = value.to_bytes(1, 'little')
        return obj

    @property
    def as_bytes(self) -> bytes:
        """The message kind as a byte string."""
        return self._as_bytes


class ErrorData(msgspec.Struct):
    """A struct representing error data."""

    name: str
    """The name of the exception class."""
    message: str
    """The error message."""
    args: tuple
    """The arguments passed to the exception."""
    traceback: str | None = None
    """The formatted traceback of the exception."""

    @staticmethod
    def from_exception(exception: BaseException | None = None) -> 'ErrorData':
        """
        Serialize an exception to :class:`ErrorData`.

        Parameters
        ----------
        exception : BaseException, optional
            The exception to serialize.

        Returns
        -------
        ErrorData
            An ErrorData struct containing the serialized exception data.

        Raises
        ------
        ValueError
            If no exception is provided and no active exception is available.
        """
        if exception is None:
            exception = sys.exc_info()[1]
            if exception is None:
                raise ValueError('No exception provided and no active exception')
        return ErrorData(
            name=type(exception).__name__,
            message=str(exception),
            args=exception.args,
            traceback=''.join(
                traceback.format_exception(
                    type(exception), exception, exception.__traceback__
                )
            ),
        )


class ServiceEvent(NamedTuple):
    """A service discovery event yielded by :func:`iter_services`."""

    kind: Literal['added', 'removed']
    """The type of event: 'added' for new services, 'removed' for removed services."""
    address: str
    """The address of the service."""
    properties: dict[str, str | None]
    """The properties of the service."""


class WelcomeData(msgspec.Struct, kw_only=True):
    """Socket addresses returned by the host during the handshake."""

    tcp_pub_sub: str
    tcp_req_rep: str
    ipc_pub_sub: str | None = None
    ipc_req_rep: str | None = None


class ClientInfo(msgspec.Struct):
    """Identifying information about a connected client."""

    name: str
    type: Literal['IPC', 'RPC']
    address: str
    pid: int
    uuid: UUID


class LocalServiceInfo(msgspec.Struct):
    """Information about a locally advertised service."""

    service_name: str
    service_type: str
    address: str
    pid: int
    uuid: UUID
    properties: dict[str, str | None]


class LocalServiceAdvertisement(contextlib.AbstractContextManager):
    """
    File-based local service advertisement for IPC discovery.

    Advertises a service by writing a JSON file to the user's runtime directory.
    This provides a lightweight alternative to Zeroconf for discovering services
    on the same machine. Stale advertisements (from dead processes) are automatically
    cleaned up during discovery.

    The advertisement is automatically removed when the instance is garbage collected
    or when `stop()` is called explicitly.
    """

    runtime_directory = user_runtime_path('LocalServiceAdvertisements')
    """Directory where service advertisement files are stored."""

    service_file: Path
    """Path to the advertisement file."""

    _closed = False
    """Flag to prevent double-finalization."""

    @validate_call
    def __init__(
        self,
        service_name: str,
        service_type: str,
        address: str,
        properties: dict[str, str | None] | None = None,
        *,
        pid: int | None = None,
        uuid: UUID4 | None = None,
    ) -> None:
        """
        Create a local service advertisement.

        Parameters
        ----------
        service_name
            The name of the service being advertised (e.g., 'Bpod 3').
        service_type : str
            The type of service being advertised (e.g., 'bpod').
        address : str
            The address where the service can be reached (e.g., 'ipc:///tmp/foo.ipc').
        properties : dict, optional
            Additional key-value properties to advertise with the service.
        pid : int, optional
            Process ID of the service. Used to detect stale advertisements.
        uuid : UUID4, optional
            Unique identifier for this service instance. Generated if not provided.
        """
        uuid = uuid or uuid4()
        info = LocalServiceInfo(
            service_name=service_name,
            service_type=service_type,
            address=address,
            uuid=uuid,
            pid=pid if pid is not None else os.getpid(),
            properties=properties or {},
        )

        self.service_file = self._get_service_file(service_type, uuid)
        self.service_file.parent.mkdir(parents=True, exist_ok=True)
        self._finalizer = weakref.finalize(self, self._close, self.service_file)

        # write advertisement to JSON file (atomic)
        json_data = msgspec.json.encode(info)
        temp_file = self.service_file.with_suffix('.tmp')
        temp_file.write_bytes(json_data)
        temp_file.replace(self.service_file)
        logger.debug("Advertising local service at '%s'", self.service_file)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Remove the service advertisement and clean up empty directories."""
        if self._closed:
            return
        self._closed = True
        self._finalizer.detach()
        self._close(self.service_file)

    @staticmethod
    def _close(service_file: Path) -> None:
        with contextlib.suppress(Exception):
            service_file.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            prune_empty_parent_directories(
                service_file.parent,
                LocalServiceAdvertisement.runtime_directory,
                remove_root=True,
            )

    @staticmethod
    def _get_service_directory(service_type: str) -> Path:
        """Get the directory for a service type."""
        runtime_directory = LocalServiceAdvertisement.runtime_directory
        sanitized = RE_NON_ALPHANUMERIC.sub('_', service_type)
        return runtime_directory / sanitized

    @staticmethod
    def _get_service_file(service_type: str, uuid: UUID) -> Path:
        """Get the path to a local service file."""
        service_dir = LocalServiceAdvertisement._get_service_directory(service_type)
        return service_dir / f'{uuid.hex}.json'

    @staticmethod
    def discover(
        service_type: str,
        properties: dict[str, str | None] | None = None,
    ) -> Iterator[LocalServiceInfo]:
        """Discover locally advertised services.

        Parameters
        ----------
        service_type : str
            The service type to discover.
        properties : dict, optional
            Properties to match against the service's properties.

        Yields
        ------
        LocalServiceInfo
            Information structure describing the discovered services.
        """
        service_dir = LocalServiceAdvertisement._get_service_directory(service_type)
        properties = properties or {}

        if service_dir.exists():
            for service_file in service_dir.glob('*.json'):
                try:
                    data = service_file.read_bytes()
                    info = msgspec.json.decode(data, type=LocalServiceInfo)
                except (msgspec.DecodeError, OSError):
                    continue

                # Remove stale service file if process no longer exists
                if not pid_exists(info.pid):
                    service_file.unlink(missing_ok=True)
                    continue

                # Check if properties match
                if all(info.properties.get(k) == v for k, v in properties.items()):
                    yield info


class ServiceBase(contextlib.AbstractContextManager):
    """Base class to :class:`ServiceHost` and :class:`ServiceClient`."""

    _serialization: Literal['json', 'msgpack'] = 'msgpack'
    _encoder: msgspec.msgpack.Encoder | msgspec.json.Encoder
    _decoder: msgspec.msgpack.Decoder | msgspec.json.Decoder
    _event_thread: threading.Thread | None = None
    _socket_req_rep: zmq.Socket
    _socket_pub_sub: zmq.Socket
    _closed = False

    def __init__(self) -> None:
        self._lock_close = threading.Lock()
        self._zmq_context = zmq.Context()
        self._stop_event_loop = threading.Event()
        self._uuid = uuid4()

    @staticmethod
    def _finalize_base(
        event_thread: threading.Thread | None,
        stop_event: threading.Event,
        socket_req_rep: zmq.Socket,
        socket_pub_sub: zmq.Socket,
        zmq_context: zmq.Context,
    ) -> None:
        # event thread
        if event_thread is not None and event_thread.is_alive():
            with contextlib.suppress(Exception):
                stop_event.set()
                event_thread.join(timeout=1)
            if event_thread.is_alive():
                with contextlib.suppress(Exception):
                    logger.warning('Event thread did not terminate cleanly')

        # ZMQ sockets
        with contextlib.suppress(Exception):
            socket_req_rep.close(linger=0)
        with contextlib.suppress(Exception):
            socket_pub_sub.close(linger=0)

        # ZMQ context
        with contextlib.suppress(Exception):
            zmq_context.term()
        with contextlib.suppress(Exception):
            zmq_context.destroy(linger=0)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit context manager."""
        self.close()

    @abstractmethod
    def close(self) -> None:
        """Close the service and release all resources."""
        ...


def _format_zeroconf_service_type(service_type: str) -> str:
    service_type = service_type.removesuffix('._tcp.local.')
    return f'_{to_snake_case(service_type)}._tcp.local.'


def _format_zeroconf_service_name(service_name: str, service_type: str) -> str:
    service_type = _format_zeroconf_service_type(service_type)
    return f'{service_name}.{service_type}'


class ServiceHost(ServiceBase):
    """
    A ZeroMQ host providing REQ/REP and PUB/SUB sockets with service discovery.

    Provides two communication channels: a REQ/REP channel for synchronous
    request-reply messaging and a PUB/SUB channel for broadcasting events to
    subscribers. Incoming requests are dispatched to a user-provided ``event_handler``
    callback.

    The service is automatically advertised for discovery by :class:`ServiceClient`. It
    is advertised locally via a file in the user's runtime directory for inter-process
    communication. When ``remote=True``, the service is additionally advertised via
    Zeroconf (mDNS) for network-wide discovery and remote-process communication.
    """

    _named_pipe_rep: Path | None = None
    _named_pipe_pub: Path | None = None
    _clients: dict[int, ClientInfo]

    def __init__(
        self,
        service_name: str,
        service_type: str,
        properties: dict[str, str | None] | None = None,
        event_handler: Callable[[Any], Any] | None = None,
        port_pub: int | None = None,
        port_rep: int | None = None,
        serialization: Literal['json', 'msgpack'] = 'msgpack',
        *,
        remote: bool = True,
    ) -> None:
        """
        Initialize the ServiceHost.

        Parameters
        ----------
        service_name : str
            Service name to advertise.
        service_type : str
            Service type.
        properties : dict, optional
            Additional properties for service advertisement.
        event_handler : callable, optional
            Function to handle incoming requests.
        port_pub : int, optional
            TCP port to bind the PUB socket. If None, a random available port is chosen.
        port_rep : int, optional
            TCP port to bind the REP socket. If None, a random available port is chosen.
        serialization : str, default='msgpack'
            Serialization format for message encoding.
        remote : bool, default=True
            If True, binds TCP sockets to '0.0.0.0'. Otherwise, binds to '127.0.0.1'.
        """
        # initialize base class
        super().__init__()

        self._bind_ip = IPV4_WILDCARD if remote else IPV4_LOOPBACK
        self._local_ip = get_local_ipv4() if remote else IPV4_LOOPBACK

        # ZeroMQ sockets
        self._socket_req_rep = self._zmq_context.socket(zmq.REP)
        self._socket_pub_sub = self._zmq_context.socket(zmq.PUB)

        # socket options / high-water marks
        self._socket_req_rep.setsockopt(zmq.SNDHWM, 1000)
        self._socket_req_rep.setsockopt(zmq.RCVHWM, 1000)
        self._socket_pub_sub.setsockopt(zmq.SNDHWM, 1000)
        self._socket_pub_sub.setsockopt(zmq.IMMEDIATE, 1)

        # Bind sockets to IPC addresses for improved performance (POSIX only)
        rep_ipc_addr: str | None = None
        pub_ipc_addr: str | None = None
        if os.name == 'posix':
            if sys.platform.startswith('linux'):
                # On linux we use abstract sockets for IPC, avoiding issues with
                # filesystem, cleanup, permissions and stale files
                rep_ipc_addr = f'ipc://@REQ_REP_{self._uuid.hex}'
                pub_ipc_addr = f'ipc://@PUB_SUB_{self._uuid.hex}'
            else:
                # On other POSIX platforms we use filesystem Unix domain sockets
                runtime_path = platformdirs.user_runtime_path(ensure_exists=True)
                self._named_pipe_rep = runtime_path / f'REQ_REP_{self._uuid.hex}.ipc'
                self._named_pipe_pub = runtime_path / f'PUB_SUB_{self._uuid.hex}.ipc'
                self._named_pipe_rep.unlink(missing_ok=True)  # pre-unlink before bind
                self._named_pipe_pub.unlink(missing_ok=True)  # to avoid collisions
                rep_ipc_addr = 'ipc://' + self._named_pipe_rep.as_posix()
                pub_ipc_addr = 'ipc://' + self._named_pipe_pub.as_posix()
            try:
                self._socket_req_rep.bind(rep_ipc_addr)
                logger.debug("Binding REP socket to '%s'", rep_ipc_addr)
                self._socket_pub_sub.bind(pub_ipc_addr)
                logger.debug("Binding PUB socket to '%s'", pub_ipc_addr)
            except zmq.ZMQError:
                logger.warning('Failed to bind IPC sockets; continuing without IPC')
                if rep_ipc_addr:
                    with contextlib.suppress(zmq.ZMQError):
                        self._socket_req_rep.unbind(rep_ipc_addr)
                rep_ipc_addr = None
                pub_ipc_addr = None

        def bind_tcp(zmq_socket: zmq.Socket, tcp_port: int | None) -> tuple[str, int]:
            """Bind socket to TCP address with preferred port."""
            if tcp_port is not None:
                try:
                    zmq_socket.bind(f'tcp://{self._bind_ip}:{tcp_port}')
                except zmq.ZMQError:
                    tcp_port = None
            if tcp_port is None:
                tcp_port = zmq_socket.bind_to_random_port(f'tcp://{self._bind_ip}')
            return f'tcp://{self._local_ip}:{tcp_port}', tcp_port

        # bind sockets to TCP addresses
        self.rep_tcp_addr, self.rep_tcp_port = bind_tcp(self._socket_req_rep, port_rep)
        self.pub_tcp_addr, self.pub_tcp_port = bind_tcp(self._socket_pub_sub, port_pub)
        logger.debug("Binding REP socket to '%s'", self.rep_tcp_addr)
        logger.debug("Binding PUB socket to '%s'", self.pub_tcp_addr)

        # select serialization protocol / initialize encoders + decoders
        self._serialization = serialization
        if serialization == 'msgpack':
            self._encoder = msgspec.msgpack.Encoder()
            self._decoder = msgspec.msgpack.Decoder()
        elif serialization == 'json':
            self._encoder = msgspec.json.Encoder()
            self._decoder = msgspec.json.Decoder()
        else:
            raise ValueError(f'Unsupported serialization protocol: {serialization}')

        # start event loop for request handling
        self._event_handler_lock = threading.Lock()
        handshake_data = WelcomeData(
            ipc_pub_sub=pub_ipc_addr,
            ipc_req_rep=rep_ipc_addr,
            tcp_pub_sub=self.pub_tcp_addr,
            tcp_req_rep=self.rep_tcp_addr,
        )
        self._event_thread = threading.Thread(
            target=ServiceHost._event_loop,
            args=(
                self._stop_event_loop,
                self._socket_req_rep,
                self._decoder,
                self._encoder,
                self._serialization,
                event_handler or self._empty_event_handler,
                self._event_handler_lock,
                handshake_data,
            ),
            daemon=True,
        )
        self._event_thread.start()

        # advertise service locally
        self._local_advertisement = LocalServiceAdvertisement(
            service_name=service_name,
            service_type=service_type,
            address=rep_ipc_addr or self.rep_tcp_addr,
            pid=os.getpid(),
            uuid=self._uuid,
            properties=properties,
        )

        # advertise service via Zeroconf for remote discovery
        self._zeroconf: Zeroconf | None = None
        self._zeroconf_service_info: ServiceInfo | None = None
        if remote:
            self._zeroconf_service_info = ServiceInfo(
                type_=_format_zeroconf_service_type(service_type),
                name=_format_zeroconf_service_name(service_name, service_type),
                port=self.rep_tcp_port,
                addresses=[socket.inet_aton(self._local_ip)],
                properties=properties or {},
                server=f'{socket.gethostname()}.local.',
                other_ttl=60,
            )
            self._zeroconf = Zeroconf(
                interfaces=InterfaceChoice.All,
                ip_version=IPVersion.V4Only,
            )

            def _register(zc: Zeroconf, service: ServiceInfo) -> None:
                with contextlib.suppress(Exception):
                    zc.register_service(service, allow_name_change=True)
                    logger.debug("Advertising remote service at '%s'", service.name)

            threading.Thread(
                target=_register,
                args=(self._zeroconf, self._zeroconf_service_info),
                daemon=True,
            ).start()

        # register finalizer to clean up resources on exit
        self._finalizer = weakref.finalize(
            self,
            ServiceHost._finalize,
            self._event_thread,
            self._stop_event_loop,
            self._socket_req_rep,
            self._socket_pub_sub,
            self._zmq_context,
            self._local_advertisement,
            self._zeroconf,
            self._zeroconf_service_info,
            self._named_pipe_rep,
            self._named_pipe_pub,
        )

    @staticmethod
    def _finalize(
        event_thread: threading.Thread | None,
        stop_event: threading.Event,
        socket_req_rep: zmq.Socket,
        socket_pub_sub: zmq.Socket,
        zmq_context: zmq.Context,
        local_advertisement: LocalServiceAdvertisement,
        zeroconf: Zeroconf | None,
        service: ServiceInfo | None,
        named_pipe_rep: Path | None,
        named_pipe_pub: Path | None,
    ) -> None:
        """Finalize the host by unregistering the service."""
        # local advertisement
        with contextlib.suppress(Exception):
            local_advertisement.close()

        # zeroconf
        if zeroconf is not None:
            if service is not None:
                with contextlib.suppress(Exception):
                    zeroconf.unregister_service(service)
            with contextlib.suppress(Exception):
                zeroconf.close()

        # call base class finalizer
        ServiceBase._finalize_base(  # noqa: SLF001
            event_thread,
            stop_event,
            socket_req_rep,
            socket_pub_sub,
            zmq_context,
        )

        # named pipes
        for pipe in (named_pipe_rep, named_pipe_pub):
            if pipe is not None:
                with contextlib.suppress(Exception):
                    pipe.unlink(missing_ok=True)

    @staticmethod
    def _empty_event_handler(_: Any) -> dict:
        """Default event handler that returns an empty dict."""
        return {}

    @staticmethod
    def _event_loop(
        stop_event: threading.Event,
        req_rep_socket: zmq.Socket,
        decoder: msgspec.msgpack.Decoder | msgspec.json.Decoder,
        encoder: msgspec.msgpack.Encoder | msgspec.json.Encoder,
        serialization_protocol: Literal['json', 'msgpack'],
        event_handler: Callable[[Any], dict],
        event_handler_lock: threading.Lock,
        handshake_data: WelcomeData,
    ) -> None:
        # avoid overhead of attribute lookups
        send_multipart = req_rep_socket.send_multipart
        recv_multipart = req_rep_socket.recv_multipart
        decode = decoder.decode
        encode = encoder.encode
        reply_kind: MessageKind
        reply_data: Any
        serialize_exception = ErrorData.from_exception

        def encode_and_send(kind: MessageKind, data: Any) -> None:
            try:
                reply_frames = [kind.as_bytes, encode(data)]
            except Exception as e:
                logger.exception('Error encoding reply to client')
                reply_frames = [
                    MessageKind.ERROR.as_bytes,
                    encode(serialize_exception(e)),
                ]
            finally:
                try:
                    send_multipart(reply_frames, copy=False)
                except zmq.ZMQError:
                    logger.exception('Error sending reply to client')

        while not stop_event.is_set():
            # wait for incoming requests (short poll so we can check stop_event)
            if not req_rep_socket.poll(100):
                continue

            # receive request
            try:
                request_frames = recv_multipart(copy=False)
                request_data_buffer = request_frames[1].buffer
            except zmq.ZMQError as e:
                logger.exception('Error receiving request from client', exc_info=e)
                continue

            # determine the kind of request
            try:
                request_kind = MessageKind(request_frames[0].buffer[0])
            except ValueError as e:
                logger.exception(
                    'Received unknown request type: %s', request_frames[0].bytes
                )
                encode_and_send(MessageKind.ERROR, serialize_exception(e))
                continue

            # decode request
            try:
                request_data = decode(request_data_buffer)
            except msgspec.DecodeError as e:
                # try the other serialization as a fallback
                try:
                    if serialization_protocol == 'msgpack':
                        request_data = msgspec.json.decode(request_data_buffer)
                    else:
                        request_data = msgspec.msgpack.decode(request_data_buffer)
                except msgspec.DecodeError:
                    logger.exception('Error decoding request from client', exc_info=e)
                    encode_and_send(MessageKind.ERROR, serialize_exception(e))
                    continue

            # handle request depending on the request type
            match request_kind:
                case MessageKind.REQUEST:  # general request
                    reply_kind = MessageKind.REPLY
                    try:
                        with event_handler_lock:
                            reply_data = event_handler(request_data)
                    except Exception as e:
                        logger.exception('Error during event handler call')
                        reply_kind = MessageKind.ERROR
                        reply_data = serialize_exception(e)

                case MessageKind.HELLO:
                    reply_kind = MessageKind.WELCOME
                    reply_data = handshake_data

                case _:  # unexpected request type
                    logger.error('Unexpected request type: %s', request_kind)
                    reply_kind = MessageKind.ERROR
                    reply_data = serialize_exception(
                        ValueError(f'Unexpected request type: {request_kind}')
                    )

            # encode and send reply
            encode_and_send(reply_kind, reply_data)

    def close(self) -> None:
        """Close the host and clean up resources."""
        with self._lock_close:
            if self._closed:
                return
            self._closed = True
            self._finalizer.detach()
            self._finalize(
                self._event_thread,
                self._stop_event_loop,
                self._socket_req_rep,
                self._socket_pub_sub,
                self._zmq_context,
                self._local_advertisement,
                self._zeroconf,
                self._zeroconf_service_info,
                self._named_pipe_rep,
                self._named_pipe_pub,
            )


class ServiceClient(ServiceBase, Generic[U]):
    """A client for communicating with :class:`ServiceHost`."""

    is_local: bool
    """Whether the client is connected to a service on localhost."""

    _serialization_module: ModuleType

    def __init__(
        self,
        service_type: str,
        address: str | None = None,
        event_handler: Callable[[dict], Any] | None = None,
        discovery_timeout: float = 10.0,
        txt_properties: dict | None = None,
        default_data_type: type[U] | None = None,
        *,
        remote: bool = True,
    ) -> None:
        """
        Initialize a ServiceClient instance.

        Parameters
        ----------
        service_type : str
            The service type to discover or connect to.
        address : str, optional
            The direct connection address for the REQ channel, by default None.
        event_handler : callable, optional
            A callback to handle PUB messages, by default None.
        discovery_timeout : float, optional
            Timeout in seconds for service discovery, by default 10.0.
        txt_properties : dict, optional
            Properties for service filtering during discovery, by default None.
        default_data_type : type, optional
            The default data type for incoming messages.
        remote : bool, optional
            Whether to use Zeroconf for discovering remote services, by default True.
        """
        # initialize base class
        super().__init__()

        # ZeroMQ sockets
        self._socket_req_rep = self._zmq_context.socket(zmq.REQ)
        self._socket_pub_sub = self._zmq_context.socket(zmq.SUB)

        # define msgspec encoder/decoder
        self._serialization_module = getattr(msgspec, self._serialization)
        self._encoder = self._serialization_module.Encoder()
        self._default_data_type = default_data_type or Any
        self._decoder = self._serialization_module.Decoder(type=self._default_data_type)

        # connect REQ channel
        if address is not None:
            self._address_req = address
        else:
            self._address_req, _ = discover(
                service_type=service_type,
                properties=txt_properties,
                timeout=discovery_timeout,
                remote=remote,
            )
        self._socket_req_rep.connect(self._address_req)
        self._lock_req = threading.Lock()
        logger.debug("Connecting REQ socket to '%s'", self._address_req)
        self.is_local = any(
            x in self._address_req for x in ('127.0.0.1', 'ipc://', 'localhost', '::1')
        )

        # perform handshake
        self._handshake()

        # connect SUB channel
        if event_handler is not None:
            self._socket_pub_sub.connect(self._address_sub)
            self._socket_pub_sub.setsockopt_string(zmq.SUBSCRIBE, '')
            logger.debug("Connecting SUB socket to '%s'", self._address_sub)
        else:
            logger.debug('Not connecting SUB socket for lack of event handler')

        # start event loop for subscription handling
        self._event_thread = threading.Thread(
            target=ServiceClient._event_loop,
            args=(
                self._stop_event_loop,
                self._socket_pub_sub,
                self._decoder,
                event_handler,
            ),
            daemon=True,
        )
        if event_handler is not None:
            self._event_thread.start()

        # register finalizer to clean up resources on exit
        self._finalizer = weakref.finalize(
            self,
            ServiceBase._finalize_base,  # noqa: SLF001
            self._event_thread,
            self._stop_event_loop,
            self._socket_req_rep,
            self._socket_pub_sub,
            self._zmq_context,
        )

    @staticmethod
    def _event_loop(
        stop_event: threading.Event,
        socket_sub: zmq.Socket,
        decoder: msgspec.msgpack.Decoder | msgspec.json.Decoder,
        event_handler: Callable,
    ) -> None:
        """Process incoming PUB messages."""
        while not stop_event.is_set():
            if not socket_sub.poll(100):
                continue
            frame = socket_sub.recv(copy=False)
            message = decoder.decode(frame.buffer)
            try:
                event_handler(message)
            except Exception as e:
                logger.exception('Subscription handler raised an exception', exc_info=e)

    def _handshake(self) -> None:
        """Perform handshake with the host."""
        _, reply_data = self._req(MessageKind.HELLO, reply_type=WelcomeData)
        if (
            self.is_local
            and os.name == 'posix'
            and reply_data.ipc_req_rep is not None
            and reply_data.ipc_pub_sub is not None
        ):
            if self._address_req.startswith(('tcp://', 'tcp4://', 'tcp6://')):
                self._socket_req_rep.disconnect(self._address_req)
                self._address_req = reply_data.ipc_req_rep
                logger.debug("Reconnecting REQ socket to '%s'", self._address_req)
                self._socket_req_rep.connect(self._address_req)
            self._address_sub = reply_data.ipc_pub_sub
        else:
            self._address_sub = reply_data.tcp_pub_sub

    @overload
    def _req(
        self,
        request_kind: MessageKind,
        request_data: Any | None = None,
        *,
        reply_type: type[T],
    ) -> tuple[MessageKind, T]: ...

    @overload
    def _req(
        self,
        request_kind: MessageKind,
        request_data: Any | None = None,
        *,
        reply_type: None = None,
    ) -> tuple[MessageKind, U]: ...

    def _req(
        self,
        request_kind: MessageKind,
        request_data: Any | None = None,
        *,
        reply_type: type[T] | None = None,
    ) -> tuple[MessageKind, Any]:
        with self._lock_req:  # acquire lock
            # encode request
            try:
                request_kind_bytes = request_kind.as_bytes
                request_data_bytes = self._encoder.encode(request_data)
            except Exception as e:
                raise ValueError('Error encoding request to host') from e

            # send request / receive reply
            try:
                self._socket_req_rep.send_multipart(
                    [request_kind_bytes, request_data_bytes], copy=False
                )
                reply_frames = self._socket_req_rep.recv_multipart(copy=False)
            except zmq.ZMQError as e:
                raise RuntimeError('Error communicating with host') from e

            # decode reply
            try:
                reply_kind = MessageKind(reply_frames[0].buffer[0])
                reply_data_buffer = reply_frames[1].buffer
                if reply_kind == MessageKind.ERROR:
                    reply_data = self._serialization_module.decode(
                        reply_data_buffer, type=ErrorData
                    )
                elif reply_type is None:
                    reply_data = self._decoder.decode(reply_data_buffer)
                else:
                    reply_data = self._serialization_module.decode(
                        reply_data_buffer, type=reply_type
                    )
            except Exception as e:
                # if decoding fails, try the other serialization protocol as a fallback
                new_format: Literal['msgpack', 'json'] = (
                    'msgpack' if self._serialization == 'json' else 'json'
                )
                new_serialization_module = getattr(msgspec, new_format)
                try:
                    if reply_kind == MessageKind.ERROR:
                        reply_data = new_serialization_module.decode(
                            reply_data_buffer, type=ErrorData
                        )
                    elif reply_type is None:
                        reply_data = new_serialization_module.decode(reply_data_buffer)
                    else:
                        reply_data = new_serialization_module.decode(
                            reply_data_buffer, type=reply_type
                        )
                except msgspec.DecodeError:
                    raise ValueError('Error decoding reply from host') from e
                logger.debug('Switching to %s serialization', new_format.upper())
                self._serialization_module = new_serialization_module
                self._encoder = new_serialization_module.Encoder()
                self._decoder = new_serialization_module.Decoder(
                    type=self._default_data_type
                )
                self._serialization = new_format

            # return reply kind and data
            return reply_kind, reply_data

    @overload
    def request(self, request_data: Any, reply_type: type[T]) -> T: ...

    @overload
    def request(self, request_data: Any) -> U: ...

    def request(self, request_data: Any, reply_type: type[T] | None = None) -> Any:
        """
        Send a generic request to the server.

        Parameters
        ----------
        request_data : Any
            The request payload.
        reply_type : type, optional
            Override the expected reply type.

        Returns
        -------
        U or T
            The reply data from the server.

        Raises
        ------
        RemoteError
            If an error occurred on the host side.
        ServiceError
            If the host sent an unexpected reply kind.
        """
        reply_kind, reply_data = self._req(
            request_kind=MessageKind.REQUEST,
            request_data=request_data,
            reply_type=reply_type,
        )
        if reply_kind == MessageKind.REPLY:
            return reply_data
        if reply_kind == MessageKind.ERROR:
            error_data = cast('ErrorData', reply_data)
            raise RemoteError(error_data)
        raise ServiceError(f"Received unexpected reply type: '{reply_kind}'")

    def close(self) -> None:
        """Close the client and clean up resources."""
        with self._lock_close:
            if self._closed:
                return
            self._closed = True
            self._finalizer.detach()
            self._finalize_base(
                self._event_thread,
                self._stop_event_loop,
                self._socket_req_rep,
                self._socket_pub_sub,
                self._zmq_context,
            )

    @property
    def address_req(self) -> str:
        """The address for the REQ channel."""
        return self._address_req

    @property
    def address_sub(self) -> str:
        """The address for the SUB channel."""
        return self._address_sub


def discover(
    service_type: str,
    properties: dict[str, str | None] | None = None,
    timeout: float = 10,
    poll_interval: float = 1,
    *,
    local: bool = True,
    remote: bool = True,
) -> tuple[str, dict[str, str | None]]:
    """
    Discover a device/service on the local network matching given properties.

    Parameters
    ----------
    service_type : str
        The service type to discover, e.g., 'bpod'
    properties : dict, optional
        Dictionary of expected service properties to match.
    timeout : float, optional
        How many seconds to wait for a matching service before timing out.
        Default is 10.
    poll_interval : float, optional
        How often to poll for local service changes, in seconds. Default is 1.
    local : bool, optional
        Whether to search for a matching service on the local machine, by default True.
    remote : bool, optional
        Whether to search for a matching service on the network, by default True.

    Returns
    -------
    str
        The service address, e.g., 'tcp://192.168.1.10:1234'.
    dict
        A dictionary of service properties.

    Raises
    ------
    TimeoutError
        If no matching device/service is found within the timeout period.
    """
    with ServiceIterator(
        service_type=service_type,
        properties=properties,
        timeout=timeout,
        poll_interval=poll_interval,
        local=local,
        remote=remote,
    ) as iterator:
        for event in iterator:
            return event.address, event.properties
    raise TimeoutError('No matching service found')


class _ServiceListenerIterator(ServiceListener):
    """A Zeroconf :class:`ServiceListener` used with :class:`ServiceIterator`."""

    def __init__(
        self, q: queue.Queue[ServiceEvent], properties: dict[str, str | None]
    ) -> None:
        self._queue = q
        self._properties = properties
        self._seen_remote: dict[str, ServiceEvent] = {}
        self._local_ipv4 = get_local_ipv4()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        # get service info
        info = zc.get_service_info(type_, name)
        if info is None or not info.parsed_addresses():
            return

        # check if the service runs on localhost
        ipv4 = next(iter(info.parsed_addresses(IPVersion.V4Only)), None)
        if ipv4 is None or ipv4 == self._local_ipv4:
            return

        # check if the service matches the requested properties
        txt = info.decoded_properties
        if not all(txt.get(k) == v for k, v in self._properties.items()):
            return

        # add event to the queue
        event = ServiceEvent('added', f'tcp://{ipv4}:{info.port}', txt)
        self._seen_remote[name] = event
        self._queue.put(event)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:  # noqa: ARG002
        if name in self._seen_remote:
            _, address, properties = self._seen_remote.pop(name)
            event = ServiceEvent('removed', address, properties)
            self._queue.put(event)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:  # noqa: ARG002
        logger.debug('Ignoring update for service: %s', name)


class ServiceIterator(Iterator[ServiceEvent], contextlib.AbstractContextManager):
    """
    Lazy iterator for service discovery events.

    Monitors both local (IPC) and remote (Zeroconf/TCP) services, yielding
    :class:`ServiceEvent` instances as services appear and disappear. Local services are
    always preferred — if a service is reachable both via IPC and TCP, only the IPC
    address is yielded.

    Prefer :func:`iter_services` over instantiating this class directly.
    """

    _YIELDED = object()

    def __init__(
        self,
        service_type: str,
        properties: dict[str, str | None] | None = None,
        timeout: float | None = 10,
        poll_interval: float = 1,
        *,
        local: bool = True,
        remote: bool = True,
    ) -> None:
        """Initialize the ServiceIterator.

        Parameters
        ----------
        service_type : str
            The service type to discover, e.g., ``'bpod'``.
        properties : dict, optional
            Dictionary of expected service properties to match.
        timeout : float or None, optional
            How many seconds to monitor, by default 10.
            Pass ``None`` to monitor indefinitely until the iterator is closed.
        poll_interval : float, optional
            How often to poll for local service changes, in seconds. Default is 1.
        local : bool, optional
            Whether to search for services on the local machine, by default True.
        remote : bool, optional
            Whether to also search for services on the network, by default True.
        """
        if not local and not remote:
            raise ValueError('at least one of local or remote must be True')

        self._q: queue.Queue[ServiceEvent] = queue.Queue()
        self._stop = threading.Event()
        self._deadline = None if timeout is None else time.monotonic() + timeout
        self._zc: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._state: dict[str, ServiceEvent | object | None] = {}  # per-address state
        self._pending: deque[str] = deque()  # waiting to be emitted
        properties = properties or {}

        # watch for local service changes
        self._watcher: threading.Thread | None = None
        if local:

            def rescan(previous: dict[str, ServiceEvent]) -> dict[str, ServiceEvent]:
                current: dict[str, ServiceEvent] = {}
                for i in LocalServiceAdvertisement.discover(service_type, properties):
                    current[i.uuid.hex] = ServiceEvent('added', i.address, i.properties)
                for event in (v for k, v in previous.items() if k not in current):
                    self._q.put(ServiceEvent('removed', *event[1:]))
                for event in (v for k, v in current.items() if k not in previous):
                    self._q.put(event)
                return current

            def watch_local(state: dict[str, ServiceEvent]) -> None:
                while not self._stop.is_set():
                    state = rescan(state)
                    self._stop.wait(poll_interval)

            seen = rescan({})
            self._watcher = threading.Thread(
                target=watch_local, args=(seen,), daemon=True
            )
            self._watcher.start()

        # watch for remote service changes
        if remote:
            handler = _ServiceListenerIterator(self._q, properties)
            zc_service_type = _format_zeroconf_service_type(service_type)
            self._zc = Zeroconf()
            self._browser = ServiceBrowser(self._zc, zc_service_type, handler)

        # register finalizer to clean up resources on exit
        self._finalizer = weakref.finalize(
            self,
            ServiceIterator._cleanup,
            self._stop,
            self._watcher,
            self._zc,
            self._browser,
        )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> ServiceEvent:
        while True:
            # Drain all immediately available events
            try:
                while True:
                    self._process(self._q.get_nowait())
            except queue.Empty:
                pass

            # Yield next pending event
            while self._pending:
                address = self._pending.popleft()
                event = self._state.pop(address, None)
                if event is None or event is self._YIELDED:
                    continue
                self._state[address] = self._YIELDED
                return cast('ServiceEvent', event)

            # Handle timeout / blocking
            remaining = (
                None if self._deadline is None else self._deadline - time.monotonic()
            )
            if remaining is not None and remaining <= 0:
                self.close()
                raise StopIteration

            try:
                self._process(self._q.get(timeout=remaining))
            except queue.Empty as e:
                self.close()
                raise StopIteration from e

    @staticmethod
    def _cleanup(
        stop: threading.Event,
        watcher: threading.Thread | None,
        zc: Zeroconf | None,
        browser: ServiceBrowser | None,
    ) -> None:
        stop.set()
        if watcher is not None:
            with contextlib.suppress(Exception):
                watcher.join(timeout=2)
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.cancel()
        if zc is not None:
            with contextlib.suppress(Exception):
                zc.close()

    def _process(self, event: ServiceEvent) -> None:
        address = event.address
        state = self._state.get(address)

        if event.kind == 'removed':
            if state is None:  # no pending event / not known alive -> ignore
                return
            if state is self._YIELDED:  # was yielded, alive -> enqueue, go pending
                self._pending.append(address)
                self._state[address] = event
                return
            # pending add not yet yielded -> cancel
            self._state.pop(address, None)
            return

        if state is None:  # first time seen -> enqueue
            self._pending.append(address)
        self._state[address] = event  # go/stay pending

    def close(self) -> None:
        """Stop monitoring and release all resources."""
        self._finalizer()


def iter_services(
    service_type: str,
    properties: dict[str, str | None] | None = None,
    timeout: float | None = 10,
    poll_interval: float = 1,
    *,
    local: bool = True,
    remote: bool = True,
) -> Iterator[ServiceEvent]:
    """
    Discover all services matching the given type and properties.

    Continuously monitors both local (IPC) and remote (Zeroconf/TCP) services, yielding
    ``'added'`` and ``'removed'`` events as services appear and disappear.

    Parameters
    ----------
    service_type : str
        The service type to discover, e.g., 'bpod'.
    properties : dict, optional
        Dictionary of expected service properties to match.
    timeout : float or None, optional
        How many seconds to monitor, by default 10.
        Pass ``None`` to monitor indefinitely until the iterator is closed.
    poll_interval : float, optional
        How often to poll for local service changes, in seconds. Default is 1.
    local : bool, optional
        Whether to search for services on the local machine, by default True.
    remote : bool, optional
        Whether to also search for services on the network, by default True.

    Yields
    ------
    ServiceEvent
        A named tuple with the following fields:

        - kind: str, either 'added' or 'removed'
        - address: str, the service address, e.g., 'tcp://192.168.1.10:1234'
        - properties: dict, the service properties, e.g., {'name': 'MyDevice'}
    """
    return ServiceIterator(
        service_type=service_type,
        properties=properties,
        timeout=timeout,
        poll_interval=poll_interval,
        local=local,
        remote=remote,
    )
