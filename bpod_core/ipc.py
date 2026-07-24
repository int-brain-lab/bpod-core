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
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextvars import ContextVar
from ipaddress import ip_address
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, Generic, Literal, NamedTuple, TypeAlias, cast, overload
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import msgspec
import platformdirs
import zmq
from filelock import FileLock
from psutil import pid_exists
from pydantic import validate_call
from typing_extensions import Self, TypeForm, TypeVar, override
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
    _RE_NON_ALPHANUMERIC,
    ByteEnum,
    get_local_ipv4,
    prune_empty_parent_directories,
    to_snake_case,
)

logger = logging.getLogger(__name__)

Serialization: TypeAlias = Literal['json', 'msgpack']
"""Serialization supported by :class:`~ServiceHost` and :class:`~ServiceClient`."""

T = TypeVar('T')
"""Per-call reply type for :class:`~ServiceClient`."""
R = TypeVar('R', default=Any)
"""Default reply type for :class:`ServiceClient`, set at construction."""
Q = TypeVar('Q', default=Any)
"""Request type; parameterizes :class:`ServiceClient` (set via annotation) and
:class:`ServiceHost` (inferred from ``request_type``, correlated with the
``request_handler``)."""
E = TypeVar('E', default=Any)
"""Event type; parameterizes :class:`ServiceHost` (set via annotation) and
:class:`ServiceClient` (inferred from ``event_type``, correlated with
the ``event_handler``)."""
_DefaultT = TypeVar('_DefaultT', bound=int | bytes | str | None)
"""Type of the fallback value returned by :meth:`ServiceHost.get_metadata`."""

_EVENT_LOOP_POLL_MS = 100
"""Receive timeout / poll interval of the event loops, in milliseconds.

Determines how quickly the event loops react to stop requests, and how quickly
a blocking :meth:`ServiceClient._req` notices close() or an expired deadline.
"""

_HANDSHAKE_TIMEOUT_S = 10.0
"""Timeout in seconds for the client's handshake with the host."""

_HELLO_GRACE_S = 1.0
"""Seconds after a HELLO request during which the host presumes a subscriber.

A subscribing client sends HELLO before connecting its SUB socket, so the
handshake serves as an early signal that a subscription may be imminent;
see :attr:`ServiceHost.has_subscribers`.
"""

_current_frame: ContextVar[zmq.Frame | None] = ContextVar(
    '_current_frame', default=None
)
"""The request frame currently being handled, scoped to the event thread."""


class ServiceError(Exception):
    """Base exception for IPC service errors."""


class RemoteError(ServiceError):
    """Exception representing an error on the remote side."""

    def __init__(self, error_data: 'ErrorData') -> None:
        self.original_error = error_data
        super().__init__(f'Remote {error_data.name}: {error_data.message}')

    def __str__(self) -> str:
        if self.original_error.traceback is None:
            return f'Remote {self.original_error.name}: {self.original_error.message}'
        return (
            f'A {self.original_error.name} has occurred on the remote side\n\n'
            f'Remote {self.original_error.traceback}'
        )


class _MessageKind(ByteEnum):
    """The types of messages exchanged between ServiceHost and ServiceClient."""

    HELLO = ord('H')
    """A message sent by the client to initiate the handshake."""
    WELCOME = ord('W')
    """A message sent by the host to acknowledge the client's handshake."""
    REQUEST = ord('Q')
    """A request sent by the client."""
    REPLY = ord('R')
    """A reply sent by the host."""
    ERROR = ord('E')
    """An error message."""


def _safe_str(obj: Any) -> str:
    """Convert an object to a string, tolerating broken ``__str__`` methods."""
    try:
        return str(obj)
    except Exception:  # noqa: BLE001
        return f'<unprintable {type(obj).__name__} object>'


class ErrorData(msgspec.Struct):
    """A struct representing error data."""

    name: str
    """The name of the exception class."""
    message: str
    """The error message."""
    args: tuple
    """The exception's arguments."""
    traceback: str | None = None
    """The formatted traceback of the exception."""

    @classmethod
    def from_exception(cls, exception: BaseException | None = None) -> 'ErrorData':
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
        try:
            formatted_traceback = ''.join(
                traceback.format_exception(
                    type(exception), exception, exception.__traceback__
                )
            )
        except Exception:  # noqa: BLE001
            formatted_traceback = None
        return cls(
            name=type(exception).__name__,
            message=_safe_str(exception),
            args=tuple(
                arg
                if arg is None or isinstance(arg, (bool, int, float, str, bytes))
                else _safe_str(arg)
                for arg in exception.args
            ),
            traceback=formatted_traceback,
        )


class ServiceEvent(NamedTuple):
    """A service discovery event yielded by :func:`iter_services`."""

    kind: Literal['added', 'removed']
    """``added`` when a service appears, ``removed`` when it disappears."""
    address: str
    """The address of the service."""
    properties: dict[str, str | None]
    """The properties of the service."""


class _WelcomeData(msgspec.Struct, kw_only=True):
    """
    Handshake data returned by :class:`ServiceHost`.

    WELCOME messages are encoded with the host's serialization format. Since a struct
    only decodes with the matching format, clients can detect the format by trial and
    error; the ``serialization`` field states it authoritatively.

    The PUB/SUB channel is communicated as a bare TCP port rather than a full
    address: a multi-homed host cannot know which of its interfaces the client can
    reach, but the client already knows a working host IP - the one it used to reach
    the REQ/REP socket - and builds the PUB address from it.
    """

    serialization: Serialization
    """Serialization format used by the host on both channels."""
    tcp_port_pub: int
    """TCP port of the publish/subscribe socket."""
    ipc_pub_sub: str | None = None
    """IPC address for the publish/subscribe socket, if available."""
    ipc_req_rep: str | None = None
    """IPC address for the request/reply socket, if available."""


class LocalServiceInfo(msgspec.Struct):
    """
    Information about a locally advertised service.

    Yielded by :meth:`LocalServiceAdvertisement.discover`.
    """

    service_name: str
    """The name of the service being advertised."""
    service_type: str
    """The type of service being advertised."""
    address: str
    """The address where the service can be reached."""
    pid: int
    """Process ID of the service."""
    uuid: UUID
    """Unique identifier for the service instance."""
    properties: dict[str, str | None]
    """Additional key-value properties to advertise with the service."""


class LocalServiceAdvertisement:
    """
    File-based local service advertisement for IPC discovery.

    Advertises a service by writing a JSON file to the user's runtime directory.
    This provides a lightweight alternative to Zeroconf for discovering services
    on the same machine. Stale advertisements (from dead processes) are automatically
    cleaned up during discovery.

    The advertisement is automatically removed when the instance is garbage collected
    or when :meth:`close` is called explicitly.

    Parameters
    ----------
    service_name : str
        The name of the service being advertised (e.g., 'Bpod 3').
    service_type : str
        The type of service being advertised (e.g., 'bpod').
    address : str
        The address where the service can be reached (e.g., 'ipc:///tmp/foo.ipc').
    properties : Mapping, optional
        Additional key-value properties to advertise with the service.
    pid : int, optional
        Process ID of the service. Used to detect stale advertisements.
    uuid : UUID, optional
        Unique identifier for this service instance. Generated if not provided.

    Examples
    --------
    Advertise a service::

        >>> ad = LocalServiceAdvertisement('Bpod1', 'bpod', 'tcp://127.0.0.1:5555')

    Discover advertised services::

        >>> services = list(LocalServiceAdvertisement.discover('bpod'))

    Notes
    -----
    Importing this class suppresses debug-level log messages from the ``filelock``
    logger, as the per-file lock/unlock events it emits are too noisy for routine use.
    To re-enable them::

        logging.getLogger('filelock').setLevel(logging.DEBUG)
    """

    logging.getLogger('filelock').setLevel(logging.WARNING)

    service_file: Path
    """Path to the advertisement file."""

    _runtime_directory: Path = platformdirs.user_runtime_path(
        'LocalServiceAdvertisements'
    )
    """Directory where service advertisement files are stored."""

    @validate_call()
    def __init__(
        self,
        service_name: str,
        service_type: str,
        address: str,
        properties: Mapping[str, str | None] | None = None,
        *,
        pid: int | None = None,
        uuid: UUID | None = None,
    ) -> None:
        uuid = uuid or uuid4()
        info = LocalServiceInfo(
            service_name=service_name,
            service_type=service_type,
            address=address,
            uuid=uuid,
            pid=pid if pid is not None else os.getpid(),
            properties=dict(properties or {}),
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

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Remove the service advertisement and clean up empty directories."""
        self._finalizer()

    @staticmethod
    def _close(service_file: Path) -> None:
        lock_file = service_file.with_suffix('.lock')
        with contextlib.suppress(Exception), FileLock(lock_file):
            logger.debug("Removing local service advertisement at '%s'", service_file)
            service_file.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            lock_file.unlink(missing_ok=True)
        with contextlib.suppress(Exception):
            prune_empty_parent_directories(
                service_file.parent,
                LocalServiceAdvertisement._runtime_directory,
                remove_root=True,
            )

    @staticmethod
    def _get_service_directory(service_type: str) -> Path:
        """Get the directory for a service type."""
        runtime_directory = LocalServiceAdvertisement._runtime_directory
        sanitized = _RE_NON_ALPHANUMERIC.sub('_', service_type)
        return runtime_directory / sanitized

    @staticmethod
    def _get_service_file(service_type: str, uuid: UUID) -> Path:
        """Get the path to a local service file."""
        service_dir = LocalServiceAdvertisement._get_service_directory(service_type)
        return service_dir / f'{uuid.hex}.json'

    @staticmethod
    def discover(
        service_type: str,
        properties: Mapping[str, str | None] | None = None,
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
                lock_file = service_file.with_suffix('.lock')
                try:
                    with FileLock(lock_file):
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


class ServiceBase(ABC):
    """Abstract Base class to :class:`ServiceHost` and :class:`ServiceClient`."""

    _event_thread: threading.Thread | None = None
    _closed = False
    _finalizer: weakref.finalize

    def __init__(self) -> None:
        self._lock_close = threading.Lock()
        self._zmq_context = zmq.Context()
        self._stop_event_loop = threading.Event()

    @staticmethod
    def _finalize_base(
        event_thread: threading.Thread | None,
        stop_event: threading.Event,
        sockets: Iterable[zmq.Socket],
        zmq_context: zmq.Context,
    ) -> None:
        # event thread
        if event_thread is not None and event_thread.is_alive():
            stop_event.set()
            event_thread.join(timeout=1)
            if event_thread.is_alive():
                logger.warning('Event thread did not terminate cleanly')

        # ZMQ sockets
        logger.debug('Closing ZMQ sockets')
        for zmq_socket in sockets:
            with contextlib.suppress(Exception):
                zmq_socket.close(linger=0)

        # ZMQ context
        try:
            logger.debug('Terminating ZMQ context')
            zmq_context.term()
        except zmq.ZMQError:
            logger.debug('Destroying ZMQ context')
            with contextlib.suppress(Exception):
                zmq_context.destroy(linger=0)

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

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


class ServiceHost(ServiceBase, Generic[Q, E]):
    """
    A ZeroMQ host providing REQ/REP and PUB/SUB sockets with service discovery.

    Provides two communication channels: a REQ/REP channel for synchronous
    request-reply messaging and a PUB/SUB channel for broadcasting events to
    subscribers. Incoming requests are dispatched to a user-provided ``request_handler``
    callback. Events are broadcast with :meth:`publish`; subscribers decode them via
    the ``event_type`` of :class:`ServiceClient`.

    The type parameters are the request type decoded from incoming requests
    (``Q``, inferred from ``request_type``) and the published event type (``E``,
    set via annotation); calls to :meth:`publish` are checked against ``E``.
    Both default to :data:`~typing.Any`.

    The service is automatically advertised for discovery by :class:`ServiceClient`.
    Local advertisement uses the :class:`LocalServiceAdvertisement` class. When
    ``remote=True``, the service is additionally advertised via Zeroconf (mDNS) for
    network-wide discovery and remote-process communication.

    Parameters
    ----------
    service_name : str
        Service name to advertise.
    service_type : str
        Service type.
    request_handler : Callable
        Function to handle incoming requests. Called with the decoded request payload
        (``Q``, see ``request_type``); its return value is sent to the client as the
        reply. Runs on a background thread (calls are serialized); exceptions it raises
        are caught and sent to the client, where they raise :class:`RemoteError`.
    properties : Mapping, optional
        Additional properties for service advertisement.
    uuid : UUID, optional
        UUID identifying the service instance; determines the IPC addresses, the
        local advertisement file, and the TCP ports remembered across restarts.
        Will be generated if not provided.
    port_pub : int, optional
        Preferred TCP port for the PUB socket; takes precedence over the port
        remembered for ``uuid``. If unavailable, a random port is chosen instead.
    port_rep : int, optional
        Preferred TCP port for the REP socket; takes precedence over the port
        remembered for ``uuid``. If unavailable, a random port is chosen instead.
    serialization : str, default='msgpack'
        Serialization format for message encoding. Can be either 'msgpack' or 'json'.
    request_type
        The data type for decoding incoming requests (``Q``); the ``request_handler``'s
        parameter must accept this type. May be a tagged union; ``Any`` by default.
    remote : bool, default=True
        If True, binds TCP sockets to '0.0.0.0'. Otherwise, binds to '127.0.0.1'.

    Raises
    ------
    ValueError
        If ``serialization`` is not a supported format.
    """

    _req_decoder: msgspec.msgpack.Decoder[Q] | msgspec.json.Decoder[Q]
    _rep_encoder: msgspec.msgpack.Encoder | msgspec.json.Encoder
    _pub_encoder: msgspec.msgpack.Encoder | msgspec.json.Encoder
    _rep_socket: zmq.Socket
    _pub_socket: zmq.Socket

    rep_tcp_addr: str
    """TCP address of the REP socket, as handed out to clients."""
    rep_tcp_port: int
    """TCP port of the REP socket."""
    pub_tcp_addr: str
    """TCP address of the PUB socket, as handed out to clients."""
    pub_tcp_port: int
    """TCP port of the PUB socket."""

    _named_pipe_rep: Path | None = None
    _named_pipe_pub: Path | None = None
    _local_advertisement: LocalServiceAdvertisement | None = None
    _zeroconf: Zeroconf | None = None

    _ports_file: Path = (
        platformdirs.user_state_path('bpod-core', appauthor=False)
        / 'service_ports.json'
    )
    """State file storing the TCP ports last bound per service UUID."""

    def __init__(
        self,
        service_name: str,
        service_type: str,
        request_handler: Callable[[Q], Any],
        properties: Mapping[str, str | None] | None = None,
        *,
        uuid: UUID | None = None,
        port_pub: int | None = None,
        port_rep: int | None = None,
        serialization: Serialization = 'msgpack',
        request_type: TypeForm[Q] = Any,
        remote: bool = True,
    ) -> None:
        # serialization
        if serialization not in ('msgpack', 'json'):
            raise ValueError(f'Unsupported serialization protocol: {serialization}')
        self._request_type = request_type
        serialization_module = getattr(msgspec, serialization)
        self._rep_encoder = serialization_module.Encoder()
        self._req_decoder = serialization_module.Decoder(type=self._request_type)

        # initialize base class
        super().__init__()

        # dedicated encoder + lock for the PUB/SUB channel; a separate encoder is
        # required as msgspec encoders are not thread-safe. The lock guards the
        # non-thread-safe PUB socket against concurrent publish() calls and the
        # event loop's subscription drains. The event flag mirrors the socket's
        # subscription state (see has_subscribers).
        self._pub_encoder = serialization_module.Encoder()
        self._pub_lock = threading.Lock()
        self._has_subscribers = threading.Event()

        # set UUID (generated if not provided by user)
        self._uuid = uuid or uuid4()

        # create ZeroMQ sockets
        self._rep_socket, self._pub_socket = self._create_sockets(self._zmq_context)

        try:
            # bind sockets to TCP addresses. Explicit port arguments take precedence
            # over the ports remembered for a caller-supplied UUID. Sockets listen on
            # bind_ip; addresses handed out to clients are built from advertised_ip.
            bind_ip = IPV4_WILDCARD if remote else IPV4_LOOPBACK
            advertised_ip = get_local_ipv4() if remote else IPV4_LOOPBACK
            if uuid is not None:
                remembered_rep, remembered_pub = self._recall_ports(uuid)
                port_rep = port_rep if port_rep is not None else remembered_rep
                port_pub = port_pub if port_pub is not None else remembered_pub
            self.rep_tcp_addr, self.rep_tcp_port = self._bind_tcp(
                self._rep_socket, port_rep, bind_ip, advertised_ip
            )
            self.pub_tcp_addr, self.pub_tcp_port = self._bind_tcp(
                self._pub_socket, port_pub, bind_ip, advertised_ip
            )
            if uuid is not None:
                self._remember_ports(uuid, self.rep_tcp_port, self.pub_tcp_port)

            # bind sockets to IPC addresses for improved performance (POSIX only)
            rep_ipc_addr, self._named_pipe_rep = self._bind_ipc(
                zmq_socket=self._rep_socket,
                identifier=f'REQ_REP_{self._uuid.hex}',
            )
            pub_ipc_addr, self._named_pipe_pub = self._bind_ipc(
                zmq_socket=self._pub_socket,
                identifier=f'PUB_SUB_{self._uuid.hex}',
            )

            # start event loop for request handling; HELLO requests are answered with
            # handshake_data, letting local clients upgrade from TCP to IPC addresses
            handshake_data = _WelcomeData(
                serialization=serialization,
                tcp_port_pub=self.pub_tcp_port,
                ipc_pub_sub=pub_ipc_addr,
                ipc_req_rep=rep_ipc_addr,
            )
            self._start_event_loop(request_handler, handshake_data)

            # advertise service for discovery by clients
            self._advertise(
                service_name,
                service_type,
                properties,
                rep_ipc_addr,
                advertised_ip,
                use_zeroconf=remote,
            )

            # register finalizer to clean up resources on exit
            self._finalizer = weakref.finalize(
                self,
                ServiceHost._finalize,
                event_thread=self._event_thread,
                stop_event=self._stop_event_loop,
                sockets=[self._rep_socket, self._pub_socket],
                zmq_context=self._zmq_context,
                local_advertisement=self._local_advertisement,
                zeroconf=self._zeroconf,
                named_pipes=[self._named_pipe_rep, self._named_pipe_pub],
            )
        except BaseException:
            # unwind partially-acquired resources when construction fails
            ServiceHost._finalize(
                event_thread=self._event_thread,
                stop_event=self._stop_event_loop,
                sockets=[self._rep_socket, self._pub_socket],
                zmq_context=self._zmq_context,
                local_advertisement=self._local_advertisement,
                zeroconf=self._zeroconf,
                named_pipes=[self._named_pipe_rep, self._named_pipe_pub],
            )
            raise

    @staticmethod
    def _create_sockets(zmq_context: zmq.Context) -> tuple[zmq.Socket, zmq.Socket]:
        """Create the REP and PUB sockets and set their respective options."""
        socket_req_rep = zmq_context.socket(zmq.REP)
        # XPUB behaves like PUB towards subscribers, but additionally surfaces
        # subscription state changes as readable frames - the basis for
        # has_subscribers and for skipping encoding when nobody is listening
        socket_pub_sub = zmq_context.socket(zmq.XPUB)

        # high-water marks cap the number of queued messages
        socket_req_rep.setsockopt(zmq.SNDHWM, 1000)
        socket_req_rep.setsockopt(zmq.RCVHWM, 1000)
        socket_pub_sub.setsockopt(zmq.SNDHWM, 1000)

        # Keep the PUB socket from queueing to incomplete connections
        socket_pub_sub.setsockopt(zmq.IMMEDIATE, 1)

        return socket_req_rep, socket_pub_sub

    @staticmethod
    def _bind_ipc(
        zmq_socket: zmq.Socket, identifier: str
    ) -> tuple[str | None, Path | None]:
        """Bind a socket to an IPC address for improved performance (POSIX only).

        On linux we use abstract sockets for IPC, avoiding issues with filesystem,
        cleanup, permissions and stale files. On other POSIX platforms we use filesystem
        Unix domain sockets.

        Returns the IPC address and - on platforms using filesystem sockets - the path
        of the named pipe. Returns ``(None, None)`` if IPC is unavailable on the
        platform or binding failed. Each socket is bound independently, and a channel
        without an IPC address falls back to TCP: the handshake upgrades clients to IPC
        only if both channels offer it, though locally discovered clients may still
        reach the REQ channel via its advertised IPC address.
        """
        if os.name != 'posix':
            return None, None  # Return early if we're not on POSIX

        named_pipe: Path | None = None
        socket_type = zmq.SocketType(cast('int', zmq_socket.type)).name
        try:
            if sys.platform.startswith('linux'):  # Use abstract sockets on Linux
                address = f'ipc://@{identifier}'

            else:  # Otherwise use filesystem Unix domain sockets
                runtime_path = platformdirs.user_runtime_path(ensure_exists=True)
                named_pipe = runtime_path / f'{identifier}.ipc'
                named_pipe.unlink(missing_ok=True)  # pre-unlink to avoid collisions
                address = 'ipc://' + named_pipe.as_posix()
            zmq_socket.bind(address)
            logger.debug("Bound %s socket to '%s'", socket_type, address)

        except (zmq.ZMQError, OSError) as e:
            logger.warning(
                "Failed to bind %s socket to '%s'", socket_type, address, exc_info=e
            )
            if address is not None:
                with contextlib.suppress(zmq.ZMQError):
                    zmq_socket.unbind(address)
            if named_pipe is not None:
                with contextlib.suppress(Exception):
                    named_pipe.unlink(missing_ok=True)
            return None, None

        else:
            return address, named_pipe

    @staticmethod
    def _bind_tcp(
        zmq_socket: zmq.Socket,
        preferred_port: int | None,
        bind_ip: str,
        advertised_ip: str,
    ) -> tuple[str, int]:
        """Bind a socket to a TCP address, falling back to a random port if taken.

        The socket listens on ``bind_ip`` (wildcard or loopback); the returned
        address is built from ``advertised_ip``, which must be concrete and routable.
        """
        tcp_port = preferred_port
        socket_type = zmq.SocketType(cast('int', zmq_socket.type)).name
        if preferred_port is not None:
            try:
                zmq_socket.bind(f'tcp://{bind_ip}:{preferred_port}')
            except zmq.ZMQError as e:
                if e.errno != zmq.EADDRINUSE:
                    raise
                tcp_port = None
        if tcp_port is None:
            tcp_port = zmq_socket.bind_to_random_port(f'tcp://{bind_ip}')
            if preferred_port is not None:
                logger.warning(
                    'Preferred port %d unavailable; bound %s socket to random port %d',
                    preferred_port,
                    socket_type,
                    tcp_port,
                )
        address = f'tcp://{advertised_ip}:{tcp_port}'
        logger.debug("Bound %s socket to '%s'", socket_type, address)
        return address, tcp_port

    @classmethod
    def _recall_ports(cls, uuid: UUID) -> tuple[int | None, int | None]:
        """Return the (REP, PUB) TCP ports last bound by the service ``uuid``."""
        try:
            data = msgspec.json.decode(
                cls._ports_file.read_bytes(), type=dict[str, tuple[int, int]]
            )
            return data[uuid.hex]
        except (OSError, msgspec.DecodeError, KeyError):
            return None, None

    @classmethod
    def _remember_ports(cls, uuid: UUID, rep_port: int, pub_port: int) -> None:
        """Store the bound TCP ports for ``uuid``, to be reused on the next start."""
        with contextlib.suppress(Exception):
            cls._ports_file.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(cls._ports_file.with_suffix('.lock')):
                try:
                    data = msgspec.json.decode(
                        cls._ports_file.read_bytes(), type=dict[str, tuple[int, int]]
                    )
                except (OSError, msgspec.DecodeError):
                    data = {}
                data[uuid.hex] = (rep_port, pub_port)
                temp_file = cls._ports_file.with_suffix('.tmp')
                temp_file.write_bytes(msgspec.json.encode(data))
                temp_file.replace(cls._ports_file)

    def _start_event_loop(
        self, request_handler: Callable[[Q], Any], handshake_data: _WelcomeData
    ) -> None:
        """Start the background thread processing incoming requests."""
        self._event_thread = threading.Thread(
            target=ServiceHost._event_loop,
            kwargs={
                'stop_event': self._stop_event_loop,
                'rep_socket': self._rep_socket,
                'pub_socket': self._pub_socket,
                'pub_lock': self._pub_lock,
                'req_decoder': self._req_decoder,
                'rep_encoder': self._rep_encoder,
                'request_handler': request_handler,
                'handshake_data': handshake_data,
                'has_subscribers': self._has_subscribers,
            },
            daemon=True,
        )
        self._event_thread.start()

    def _advertise(
        self,
        service_name: str,
        service_type: str,
        properties: Mapping[str, str | None] | None,
        ipc_addr: str | None,
        advertised_ip: str,
        *,
        use_zeroconf: bool,
    ) -> None:
        """Advertise the service locally and - if ``use_zeroconf`` - via Zeroconf.

        The Zeroconf record advertises ``advertised_ip``, which must be concrete
        and routable.
        """
        # advertise service locally
        self._local_advertisement = LocalServiceAdvertisement(
            service_name=service_name,
            service_type=service_type,
            address=ipc_addr or self.rep_tcp_addr,
            pid=os.getpid(),
            uuid=self._uuid,
            properties=properties,
        )

        # advertise service via Zeroconf for remote discovery
        if not use_zeroconf:
            return
        service_info = ServiceInfo(
            type_=_format_zeroconf_service_type(service_type),
            name=_format_zeroconf_service_name(service_name, service_type),
            port=self.rep_tcp_port,
            parsed_addresses=[advertised_ip],
            properties=dict(properties or {}),
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
            args=(self._zeroconf, service_info),
            daemon=True,
        ).start()

    @staticmethod
    def _finalize(
        *,
        event_thread: threading.Thread | None,
        stop_event: threading.Event,
        sockets: Iterable[zmq.Socket],
        zmq_context: zmq.Context,
        local_advertisement: LocalServiceAdvertisement | None,
        zeroconf: Zeroconf | None,
        named_pipes: Iterable[Path | None],
    ) -> None:
        """Unadvertise the service and release all resources held by the host."""
        # remove local advertisement
        if local_advertisement is not None:
            with contextlib.suppress(Exception):
                local_advertisement.close()

        # close zeroconf; this also unregisters the advertised service
        if zeroconf is not None:
            with contextlib.suppress(Exception):
                logger.debug('Closing Zeroconf')
                zeroconf.close()

        # call base class finalizer
        ServiceBase._finalize_base(  # noqa: SLF001
            event_thread,
            stop_event,
            sockets,
            zmq_context,
        )

        # named pipes
        for pipe in named_pipes:
            if pipe is not None:
                with contextlib.suppress(Exception):
                    pipe.unlink(missing_ok=True)

    @overload
    @staticmethod
    def get_metadata(option: int | str) -> int | bytes | str | None: ...

    @overload
    @staticmethod
    def get_metadata(
        option: int | str, default: _DefaultT
    ) -> int | bytes | str | _DefaultT: ...

    @staticmethod
    def get_metadata(
        option: int | str, default: int | bytes | str | None = None
    ) -> int | bytes | str | Any:
        """
        Read a metadata value from the request currently being handled.

        Intended to be called from within a ``request_handler`` to access per-request
        connection information, such as the client's address (``'Peer-Address'``) or
        application metadata (e.g. ``'X-Hostname'``).

        Parameters
        ----------
        option : int or str
            An integer frame property (e.g. :attr:`zmq.SRCFD
            <zmq.MessageOption.SRCFD>`) or a string metadata
            key (e.g. ``'Peer-Address'``).
        default : int or bytes or str, optional
            The value to return if no request is currently being handled or the
            metadata option is unavailable. Defaults to ``None``.

        Returns
        -------
        int or bytes or str
            The requested value, or `default` if no request is currently being
            handled or the option is unavailable.
        """
        frame = _current_frame.get()
        if frame is None:
            return default
        try:
            return frame.get(option)  # type: ignore[arg-type]
        except zmq.ZMQError:
            return default

    @property
    def has_subscribers(self) -> bool:
        """
        Whether anyone is subscribed to the PUB/SUB channel.

        True while at least one subscriber is connected, or briefly after a client
        handshake (a subscription typically follows within the grace period). The
        flag is refreshed by the event loop and may lag actual subscription changes
        by up to one poll interval. It signals "someone subscribed to something" -
        there is no per-topic granularity.
        """
        return self._has_subscribers.is_set()

    def wait_for_subscribers(self, timeout: float) -> bool:
        """
        Block until someone subscribes to the PUB/SUB channel.

        Returns as soon as :attr:`has_subscribers` becomes True - i.e., once a
        subscription registers or a client handshake starts the grace period - or when
        the timeout expires.

        Parameters
        ----------
        timeout : float
            Maximum time to wait, in seconds.

        Returns
        -------
        bool
            True if a subscriber registered within the timeout, False otherwise.
        """
        return self._has_subscribers.wait(timeout)

    def publish(self, data: E) -> None:
        """
        Broadcast a message to all subscribers over the PUB/SUB channel.

        If no subscriber is connected (see :attr:`has_subscribers`), the message is
        dropped without being encoded - mirroring what the socket would do with it.

        Parameters
        ----------
        data : E
            The payload to broadcast. Encoded with the host's serialization protocol.

        Raises
        ------
        ServiceError
            If the message cannot be encoded or published.
        """
        with self._pub_lock:
            if self._closed:
                logger.warning('Cannot publish: the host has been closed')
                return
            if not self._has_subscribers.is_set():
                return
            try:
                payload = self._pub_encoder.encode(data)
            except Exception as e:
                raise ServiceError('Error encoding message to publish') from e
            try:
                self._pub_socket.send(payload, copy=False)
            except zmq.ZMQError as e:
                raise ServiceError('Error publishing message') from e

    @staticmethod
    def _event_loop(
        *,
        stop_event: threading.Event,
        rep_socket: zmq.Socket,
        pub_socket: zmq.Socket,
        pub_lock: threading.Lock,
        req_decoder: msgspec.msgpack.Decoder[Q] | msgspec.json.Decoder[Q],
        rep_encoder: msgspec.msgpack.Encoder | msgspec.json.Encoder,
        request_handler: Callable[[Q], Any],
        handshake_data: _WelcomeData,
        has_subscribers: threading.Event,
    ) -> None:
        # avoid overhead of attribute lookups by storing them in local variables
        send_multipart = rep_socket.send_multipart
        recv_multipart = rep_socket.recv_multipart
        decode = req_decoder.decode
        encode = rep_encoder.encode
        reply_kind: _MessageKind
        reply_data: Any

        # WELCOME replies are static, so they can be prepared once ahead of the loop
        welcome_frames = [_MessageKind.WELCOME.byte_value, encode(handshake_data)]

        # subscription tracking: the XPUB socket surfaces subscription changes as frames
        # (0x01 = first subscriber of a topic, 0x00 = last one gone, sent also on
        # disconnect). These frames are balanced per topic, so a plain counter of
        # subscribed topics - or a recent HELLO, which precedes a client's
        # subscription - determines has_subscribers.
        n_subscribed_topics = 0
        hello_grace_until = 0.0

        def update_subscriber_flag() -> None:
            """Drain pending subscription frames and refresh the subscriber flag."""
            nonlocal n_subscribed_topics
            with pub_lock:
                while pub_socket.poll(0):
                    frame = pub_socket.recv(zmq.NOBLOCK)
                    n_subscribed_topics += 1 if frame[:1] == b'\x01' else -1
            if n_subscribed_topics or time.monotonic() < hello_grace_until:
                has_subscribers.set()
            else:
                has_subscribers.clear()

        def encode_and_send(kind: _MessageKind, data: Any) -> None:
            """Encode `data` and send it as a two-frame reply over `rep_socket`.

            If encoding fails, sends an ERROR reply with the serialized exception
            instead. ZMQ send errors are logged but otherwise suppressed.

            Parameters
            ----------
            kind : _MessageKind
                Message kind byte to use as the first reply frame.
            data : Any
                Payload to encode and send as the second reply frame.
            """
            try:
                reply_frames = [kind.byte_value, encode(data)]
            except Exception:
                logger.exception('Error encoding reply to client')
                reply_frames = [
                    _MessageKind.ERROR.byte_value,
                    encode(ErrorData.from_exception()),
                ]
            try:
                send_multipart(reply_frames, copy=False)
            except zmq.ZMQError:
                logger.exception('Error sending reply to client')

        # receive with a timeout instead of poll + receive: this saves a syscall
        # per message, and the periodic timeouts allow checking stop_event
        rep_socket.setsockopt(zmq.RCVTIMEO, _EVENT_LOOP_POLL_MS)

        while not stop_event.is_set():
            # refresh the subscriber flag once per iteration - i.e., at least once
            # per poll interval when idle, and once per request when busy
            update_subscriber_flag()

            # receive request from client
            try:
                request_frames = recv_multipart(copy=False)
            except zmq.Again:  # timed out - check stop_event and wait again
                continue
            except zmq.ZMQError:
                if not stop_event.is_set():  # silent when closing down
                    logger.exception('Error receiving request from client')
                break

            # validate framing and determine the kind of request. Once a request has
            # been received, a reply MUST be sent - otherwise the REP socket gets
            # stuck in its send state and the channel deadlocks.
            try:
                request_kind = _MessageKind(request_frames[0].buffer[0])
                request_data = request_frames[1].buffer
            except (IndexError, ValueError):
                logger.exception('Received malformed request from client')
                encode_and_send(_MessageKind.ERROR, ErrorData.from_exception())
                continue

            # handle request depending on the request type
            match request_kind:
                case _MessageKind.REQUEST:  # general request
                    # decode request
                    try:
                        request_data = decode(request_data)
                    except msgspec.DecodeError:
                        logger.exception('Error decoding request from client')
                        encode_and_send(_MessageKind.ERROR, ErrorData.from_exception())
                        continue
                    reply_kind = _MessageKind.REPLY

                    # call request_handler
                    # expose the request frame to get_metadata() during the call
                    token = _current_frame.set(request_frames[1])
                    try:
                        reply_data = request_handler(request_data)
                    except Exception:
                        logger.exception('Error during request handler call')
                        reply_kind = _MessageKind.ERROR
                        reply_data = ErrorData.from_exception()
                    finally:
                        _current_frame.reset(token)

                case _MessageKind.HELLO:
                    # a subscription may be imminent (it follows the handshake):
                    # presume a subscriber right away rather than dropping messages
                    # until the next drain notices the actual subscription
                    hello_grace_until = time.monotonic() + _HELLO_GRACE_S
                    has_subscribers.set()
                    try:
                        send_multipart(welcome_frames, copy=False)
                    except zmq.ZMQError:
                        logger.exception('Error sending reply to client')
                    continue

                case _:  # unexpected request type
                    logger.error('Unexpected request type: %s', request_kind)
                    reply_kind = _MessageKind.ERROR
                    reply_data = ErrorData.from_exception(
                        ValueError(f'Unexpected request type: {request_kind}')
                    )

            # encode and send reply
            encode_and_send(reply_kind, reply_data)

    @override
    def close(self) -> None:
        """Close the host and clean up resources."""
        with self._lock_close:
            if self._closed:
                return
            with self._pub_lock:  # wait for an in-flight publish()
                self._closed = True
            self._finalizer()


def _is_local_address(address: str) -> bool:
    """Check whether a ZMQ address points at the local machine."""
    try:
        address_components = urlsplit(address)
        return (
            address_components.scheme == 'ipc'
            or address_components.hostname == 'localhost'
            or ip_address(address_components.hostname or '').is_loopback
        )
    except ValueError:
        return False


class ServiceClient(ServiceBase, Generic[Q, R, E]):
    """
    A client for communicating with :class:`ServiceHost`.

    The client connects to the host's REQ/REP channel - either directly via
    ``address`` or by discovering the service - and adopts the host's serialization
    format during an initial handshake. If an ``event_handler`` is given, the client
    additionally subscribes to the host's PUB/SUB channel and forwards each decoded
    event to the handler from a background thread.

    The type parameters are the request payload type accepted by :meth:`request`
    (``Q``, set via annotation), the default reply type (``R``, inferred from
    ``default_reply_type``), and the event type decoded from PUB messages
    (``E``, inferred from ``event_type``). All default to :data:`~typing.Any`.

    Parameters
    ----------
    service_type : str
        The service type to discover or connect to.
    address : str, optional
        The direct connection address for the REQ channel. If None (default), the
        service is discovered via ``service_type`` and ``txt_properties``.
    event_handler : Callable, optional
        A callback to handle PUB messages, by default None. Called with the decoded
        message (``E``, see ``event_type``); its return value is ignored.
        Runs on a background thread and should not block; exceptions it raises are
        logged but not propagated.
    discovery_timeout : float, default: 10.0
        Timeout in seconds for service discovery.
    txt_properties : Mapping, optional
        Properties for service filtering during discovery, by default None.
    default_reply_type
        The default data type for incoming replies (``R``); the client is
        parameterized on this type. May be a tagged union; ``Any`` by default.
    event_type
        The data type for decoding incoming PUB messages (``E``), independent of
        ``default_reply_type``; ``Any`` by default. Pass a tagged union here to
        dispatch published messages on a msgspec ``tag``. Requires an
        ``event_handler``, whose parameter must accept this type.
    remote : bool, default: True
        Whether to use Zeroconf for discovering remote services, by default True.

    Raises
    ------
    ValueError
        If ``event_type`` is given without an ``event_handler``.
    TimeoutError
        If no matching service is discovered within ``discovery_timeout``, or the
        host does not answer the handshake in time.
    RemoteError
        If the host reports an error during the handshake.
    ServiceError
        If communication with the host fails during the handshake.
    """

    is_local: bool
    """Whether the client is connected to a service on localhost."""

    _serialization: Serialization
    _req_encoder: msgspec.msgpack.Encoder | msgspec.json.Encoder
    _rep_decoder: msgspec.msgpack.Decoder[R] | msgspec.json.Decoder[R]
    _event_decoder: msgspec.msgpack.Decoder[E] | msgspec.json.Decoder[E]
    _default_rep_type: TypeForm[R]
    _event_type: TypeForm[E]
    _serialization_module: ModuleType
    _req_socket: zmq.Socket
    _sub_socket: zmq.Socket

    def __init__(
        self,
        service_type: str,
        *,
        address: str | None = None,
        event_handler: Callable[[E], object] | None = None,
        discovery_timeout: float = 10.0,
        txt_properties: Mapping[str, str | None] | None = None,
        default_reply_type: TypeForm[R] = Any,
        event_type: TypeForm[E] = Any,
        remote: bool = True,
    ) -> None:
        # without an event handler the SUB socket never connects, so an event
        # type could never take effect - reject the combination early
        if event_type is not Any and event_handler is None:
            raise ValueError('event_type requires an event_handler')

        # initialize base class
        super().__init__()

        # define msgspec encoder/decoder; building the decoders also validates
        # default_reply_type and event_type before any resources exist. The
        # serialization format is provisional until the handshake, which may
        # switch it to the host's format
        self._default_rep_type = default_reply_type
        self._event_type = event_type
        self._set_serialization('msgpack')

        # ZeroMQ sockets; RELAXED lets the REQ socket send again after an abandoned
        # (e.g. timed-out) request, and CORRELATE discards the stale reply to it
        self._req_socket = self._zmq_context.socket(zmq.REQ)
        self._sub_socket = self._zmq_context.socket(zmq.SUB)
        self._req_socket.setsockopt(zmq.REQ_RELAXED, 1)
        self._req_socket.setsockopt(zmq.REQ_CORRELATE, 1)
        self._req_socket.setsockopt_string(
            zmq.METADATA, f'X-Hostname:{socket.gethostname()}'
        )

        try:
            # connect REQ channel
            self._lock_req = threading.Lock()
            if address is not None:
                self._address_req = address
            else:
                self._address_req, _ = discover(
                    service_type=service_type,
                    properties=txt_properties,
                    timeout=discovery_timeout,
                    remote=remote,
                )
            logger.debug("Connecting REQ socket to '%s'", self._address_req)
            self._req_socket.connect(self._address_req)
            self.is_local = _is_local_address(self._address_req)

            # perform handshake
            self._handshake()

            # connect SUB channel and start the event loop for subscription handling
            if event_handler is not None:
                self._start_event_loop(event_handler)
            else:
                logger.debug('Not connecting SUB socket for lack of event handler')

            # register finalizer to clean up resources on exit
            self._finalizer = weakref.finalize(
                self,
                ServiceBase._finalize_base,  # noqa: SLF001
                self._event_thread,
                self._stop_event_loop,
                [self._req_socket, self._sub_socket],
                self._zmq_context,
            )
        except BaseException:
            # unwind partially-acquired resources when construction fails
            ServiceBase._finalize_base(  # noqa: SLF001
                self._event_thread,
                self._stop_event_loop,
                [self._req_socket, self._sub_socket],
                self._zmq_context,
            )
            raise

    def _start_event_loop(self, event_handler: Callable[[E], object]) -> None:
        """Connect the SUB socket and start the thread processing PUB messages.

        The thread receives the event decoder by value - correct only because it
        starts after :meth:`_handshake` has settled the serialization format.
        """
        logger.debug("Connecting SUB socket to '%s'", self._address_sub)
        self._sub_socket.connect(self._address_sub)
        self._sub_socket.setsockopt_string(zmq.SUBSCRIBE, '')
        self._event_thread = threading.Thread(
            target=ServiceClient._event_loop,
            args=(
                self._stop_event_loop,
                self._sub_socket,
                self._event_decoder,
                event_handler,
            ),
            daemon=True,
        )
        self._event_thread.start()

    @staticmethod
    def _event_loop(
        stop_event: threading.Event,
        socket_sub: zmq.Socket,
        pub_decoder: msgspec.msgpack.Decoder[E] | msgspec.json.Decoder[E],
        event_handler: Callable[[E], object],
    ) -> None:
        """Process incoming PUB messages."""
        # receive with a timeout instead of poll + receive: this saves a syscall per
        # message, and the periodic timeouts allow checking stop_event
        socket_sub.setsockopt(zmq.RCVTIMEO, _EVENT_LOOP_POLL_MS)

        # the first receive of a burst blocks (with timeout); once a message has
        # arrived, the queue is drained without blocking before waiting again
        flags = 0
        while not stop_event.is_set():
            try:
                frame = socket_sub.recv(flags, copy=False)
            except zmq.Again:  # recv timed out / queue drained -> wait again
                flags = 0
                continue
            except zmq.ZMQError:
                if not stop_event.is_set():  # silent when closing down
                    logger.exception('Error receiving published message')
                break
            flags = zmq.NOBLOCK

            try:
                message = pub_decoder.decode(frame.buffer)
            except msgspec.DecodeError:
                logger.exception('Error decoding published message')
                continue
            try:
                event_handler(message)
            except Exception as e:
                logger.exception('Subscription handler raised an exception', exc_info=e)

    def _set_serialization(self, serialization: Serialization) -> None:
        """Build the encoder and decoders for the given serialization format."""
        self._serialization = serialization
        self._serialization_module = getattr(msgspec, serialization)
        self._req_encoder = self._serialization_module.Encoder()
        self._rep_decoder = self._serialization_module.Decoder(
            type=self._default_rep_type
        )
        self._event_decoder = self._serialization_module.Decoder(type=self._event_type)

    def _handshake(self) -> None:
        """Perform handshake with the host and adopt its serialization format."""
        _, reply_data = self._req(
            _MessageKind.HELLO, reply_type=_WelcomeData, timeout=_HANDSHAKE_TIMEOUT_S
        )
        if isinstance(reply_data, ErrorData):
            raise RemoteError(reply_data)
        if reply_data.serialization != self._serialization:
            logger.debug('Switching to %s serialization', reply_data.serialization)
            self._set_serialization(reply_data.serialization)
        if (
            self.is_local
            and os.name == 'posix'
            and reply_data.ipc_req_rep is not None
            and reply_data.ipc_pub_sub is not None
        ):
            if self._address_req.startswith('tcp://'):
                self._req_socket.disconnect(self._address_req)
                self._address_req = reply_data.ipc_req_rep
                logger.debug("Reconnecting REQ socket to '%s'", self._address_req)
                self._req_socket.connect(self._address_req)
            self._address_sub = reply_data.ipc_pub_sub
        else:
            # build the PUB address from the host IP that already proved reachable:
            # the one used to reach the REQ socket. When connected via IPC, the host
            # is local and its TCP sockets are reachable via loopback.
            host_ip = urlsplit(self._address_req).hostname or IPV4_LOOPBACK
            self._address_sub = f'tcp://{host_ip}:{reply_data.tcp_port_pub}'

    @overload
    def _req(
        self,
        request_kind: _MessageKind,
        request_data: Q | None = None,
        *,
        reply_type: type[T],
        timeout: float | None = None,
    ) -> tuple[_MessageKind, T]: ...

    @overload
    def _req(
        self,
        request_kind: _MessageKind,
        request_data: Q | None = None,
        *,
        reply_type: None = None,
        timeout: float | None = None,
    ) -> tuple[_MessageKind, R]: ...

    def _req(
        self,
        request_kind: _MessageKind,
        request_data: Q | None = None,
        *,
        reply_type: type[T] | None = None,
        timeout: float | None = None,
    ) -> tuple[_MessageKind, Any]:
        with self._lock_req:  # acquire lock
            if self._closed:
                raise ServiceError('The client has been closed')

            # encode request
            try:
                request_kind_bytes = request_kind.byte_value
                request_data_bytes = self._req_encoder.encode(request_data)
            except Exception as e:
                raise ServiceError('Error encoding request to host') from e

            # send request to host, then poll for its reply; polling (rather than a
            # blocking receive) keeps the call responsive to close() and enforces
            # the optional timeout
            deadline = None if timeout is None else time.monotonic() + timeout
            try:
                self._req_socket.send_multipart(
                    [request_kind_bytes, request_data_bytes], copy=False
                )
                while not self._req_socket.poll(_EVENT_LOOP_POLL_MS):
                    if self._closed:
                        raise ServiceError('The client has been closed')
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError('Timed out awaiting reply from host')
                reply_frames = self._req_socket.recv_multipart(copy=False)
            except zmq.ZMQError as e:
                raise ServiceError('Error communicating with host') from e

            # validate framing and determine the kind of reply
            try:
                reply_kind = _MessageKind(reply_frames[0].buffer[0])
                reply_data_buffer = reply_frames[1].buffer
            except (IndexError, ValueError) as e:
                raise ServiceError('Received malformed reply from host') from e

            # decode the host's reply
            try:
                if reply_kind == _MessageKind.REPLY:
                    if reply_type is None or reply_type is self._default_rep_type:
                        # use the fast prebuilt decoder for the default reply type
                        reply_data = self._rep_decoder.decode(reply_data_buffer)
                    else:
                        # otherwise, use the slower msgspec decode method
                        reply_data = self._serialization_module.decode(
                            reply_data_buffer, type=reply_type
                        )
                elif reply_kind == _MessageKind.ERROR:
                    reply_data = self._serialization_module.decode(
                        reply_data_buffer, type=ErrorData
                    )
                elif reply_kind == _MessageKind.WELCOME:
                    # WELCOME replies use the host's serialization format, which is not
                    # yet known at this point. Being a struct, _WelcomeData only decodes
                    # with the matching format - so we can safely try our current format
                    # first and fall back to the other one.
                    try:
                        reply_data = self._serialization_module.decode(
                            reply_data_buffer, type=_WelcomeData
                        )
                    except msgspec.DecodeError:
                        other_serialization_module = (
                            msgspec.json
                            if self._serialization == 'msgpack'
                            else msgspec.msgpack
                        )
                        reply_data = other_serialization_module.decode(
                            reply_data_buffer, type=_WelcomeData
                        )
                else:
                    raise ServiceError(f"Unexpected reply kind: '{reply_kind}'")
            except msgspec.DecodeError as e:
                raise ServiceError('Error decoding reply from host') from e

            # return reply kind and data
            return reply_kind, reply_data

    @overload
    def request(
        self,
        request_data: Q,
        reply_type: type[T],
        timeout: float | None = None,
    ) -> T: ...

    @overload
    def request(
        self,
        request_data: Q,
        reply_type: None = None,
        timeout: float | None = None,
    ) -> R: ...

    def request(
        self,
        request_data: Q,
        reply_type: type[T] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """
        Send a generic request to the server.

        Parameters
        ----------
        request_data : Q
            The request payload.
        reply_type
            Override the expected reply type.
        timeout : float, optional
            Maximum time in seconds to await the reply. By default, the call blocks
            until a reply arrives or the client is closed.

        Returns
        -------
        Any
            The deserialized reply from the server. If ``reply_type`` is given, returns
            an instance of ``reply_type``; otherwise returns an instance of
            ``default_reply_type`` defined during instantiation of the class.

        Raises
        ------
        RemoteError
            If an error occurred on the host side.
        ServiceError
            If the host sent an unexpected reply kind or the client has been closed.
        TimeoutError
            If no reply arrived within `timeout` seconds.
        """
        reply_kind, reply_data = self._req(
            request_kind=_MessageKind.REQUEST,
            request_data=request_data,
            reply_type=reply_type,
            timeout=timeout,
        )
        if reply_kind == _MessageKind.REPLY:
            return reply_data
        if reply_kind == _MessageKind.ERROR:
            error_data = cast('ErrorData', reply_data)
            raise RemoteError(error_data)
        raise ServiceError(f"Received unexpected reply type: '{reply_kind}'")

    @override
    def close(self) -> None:
        """Close the client and clean up resources."""
        with self._lock_close:
            if self._closed:
                return
            self._closed = True
            with self._lock_req:  # wait for an in-flight request to notice _closed
                pass
            self._finalizer()

    @property
    def address_req(self) -> str:
        """The address for the REQ channel."""
        return self._address_req

    @property
    def address_sub(self) -> str:
        """The address for the SUB channel."""
        return self._address_sub


class _ServiceListenerIterator(ServiceListener):
    """A Zeroconf :class:`ServiceListener` used with :class:`ServiceIterator`."""

    def __init__(
        self,
        q: 'queue.Queue[ServiceEvent | None]',
        properties: Mapping[str, str | None],
    ) -> None:
        self._queue = q
        self._properties = properties
        self._seen_remote: dict[str, ServiceEvent] = {}
        self._local_ipv4 = get_local_ipv4()

    @override
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

    @override
    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        if name in self._seen_remote:
            _, address, properties = self._seen_remote.pop(name)
            event = ServiceEvent('removed', address, properties)
            self._queue.put(event)

    @override
    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        logger.debug('Ignoring update for service: %s', name)


class _Yielded:
    """Sentinel marking an address whose latest event was already yielded."""


class ServiceIterator(Iterator[ServiceEvent]):
    """
    Lazy iterator for service discovery events.

    Monitors both local (IPC) and remote (Zeroconf/TCP) services, yielding
    :class:`ServiceEvent` instances as services appear and disappear. Local services are
    always preferred — if a service is reachable both via IPC and TCP, only the IPC
    address is yielded.

    Parameters
    ----------
    service_type : str
        The service type to discover, e.g., ``'bpod'``.
    properties : dict, optional
        Dictionary of expected service properties to match.
    timeout : float or None, default: 10.0
        How many seconds to monitor.
        Pass ``None`` to monitor indefinitely until the iterator is closed.
    poll_interval : float, default: 1.0
        How often to poll for local service changes, in seconds.
    local : bool, default: True
        Whether to search for services on the local machine.
    remote : bool, default: True
        Whether to also search for services on the network.

    Notes
    -----
    Prefer :func:`iter_services` over instantiating this class directly.
    """

    _YIELDED = _Yielded()

    def __init__(
        self,
        service_type: str,
        properties: Mapping[str, str | None] | None = None,
        *,
        timeout: float | None = 10.0,
        poll_interval: float = 1.0,
        local: bool = True,
        remote: bool = True,
    ) -> None:
        if not local and not remote:
            raise ValueError('at least one of local or remote must be True')

        # a None on the queue is the close sentinel: it unblocks a consumer
        # waiting in __next__ and marks the iterator as exhausted
        self._q: queue.Queue[ServiceEvent | None] = queue.Queue()
        self._stop = threading.Event()
        self._deadline = None if timeout is None else time.monotonic() + timeout
        self._zc: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._state: dict[str, ServiceEvent | _Yielded] = {}  # per-address state
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
            self._q,
            self._stop,
            self._watcher,
            self._zc,
            self._browser,
        )

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

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
                    self._handle(self._q.get_nowait())
            except queue.Empty:
                pass

            # Yield next pending event
            while self._pending:
                address = self._pending.popleft()
                event = self._state.pop(address, None)
                if event is None or isinstance(event, _Yielded):
                    continue
                self._state[address] = self._YIELDED
                return event

            # Handle timeout / blocking
            remaining = (
                None if self._deadline is None else self._deadline - time.monotonic()
            )
            if remaining is not None and remaining <= 0:
                self.close()
                raise StopIteration

            try:
                self._handle(self._q.get(timeout=remaining))
            except queue.Empty as e:
                self.close()
                raise StopIteration from e

    def _handle(self, event: ServiceEvent | None) -> None:
        """Feed a queued event to :meth:`_process`; end iteration on the sentinel."""
        if event is None:  # close sentinel enqueued by _cleanup()
            self._q.put(None)  # keep subsequent __next__ calls exhausted
            raise StopIteration
        self._process(event)

    @staticmethod
    def _cleanup(
        q: 'queue.Queue[ServiceEvent | None]',
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
        # unblock a consumer waiting in __next__ and mark the iterator exhausted
        q.put(None)

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
    properties: Mapping[str, str | None] | None = None,
    *,
    timeout: float | None = 10.0,
    poll_interval: float = 1.0,
    local: bool = True,
    remote: bool = True,
) -> ServiceIterator:
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
    timeout : float or None, default: 10.0
        How many seconds to monitor.
        Pass ``None`` to monitor indefinitely until the iterator is closed.
    poll_interval : float, default: 1.0
        How often to poll for local service changes, in seconds.
    local : bool, default: True
        Whether to search for services on the local machine.
    remote : bool, default: True
        Whether to also search for services on the network.

    Yields
    ------
    ServiceEvent
        A named tuple with the following fields:

        - kind: str, either ``'added'`` or ``'removed'``
        - address: str, the service address, e.g., ``'tcp://192.168.1.10:1234'``
        - properties: dict, the service properties, e.g., ``{'name': 'MyDevice'}``

    Examples
    --------
    Print events as services appear and disappear::

        for event in iter_services('bpod', timeout=None):
            print(event.kind, event.address)
    """
    return ServiceIterator(
        service_type=service_type,
        properties=properties,
        timeout=timeout,
        poll_interval=poll_interval,
        local=local,
        remote=remote,
    )


def discover(
    service_type: str,
    properties: Mapping[str, str | None] | None = None,
    *,
    timeout: float = 10.0,
    poll_interval: float = 1.0,
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
    timeout : float, default: 10.0
        How many seconds to wait for a matching service before timing out.
    poll_interval : float, default: 1.0
        How often to poll for local service changes, in seconds.
    local : bool, default: True
        Whether to search for a matching service on the local machine.
    remote : bool, default: True
        Whether to search for a matching service on the network.

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
