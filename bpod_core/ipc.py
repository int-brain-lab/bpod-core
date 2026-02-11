"""Inter-process Communication, service discovery and related."""

import contextlib
import json
import logging
import os
import socket
import sys
import threading
import weakref
from abc import abstractmethod
from collections.abc import Callable, Iterator
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import msgspec
import platformdirs
import zmq
from platformdirs import user_runtime_path
from psutil import pid_exists
from pydantic import UUID4, validate_call
from zeroconf import (
    InterfaceChoice,
    IPVersion,
    ServiceBrowser,
    ServiceInfo,
    ServiceStateChange,
    Zeroconf,
)

from bpod_core.constants import IP_ANY, IP_LOOPBACK
from bpod_core.misc import (
    RE_NON_ALPHANUMERIC,
    get_local_ipv4,
    prune_empty_parent_directories,
    to_snake_case,
)

logger = logging.getLogger(__name__)


class DualChannelMessage(msgspec.Struct, omit_defaults=True, array_like=True):
    type: str = msgspec.field(name='T')  # message type
    data: Any | None = msgspec.field(default=None, name='D')  # message data


class DualChannelHandshake(msgspec.Struct, kw_only=True):
    ipc_pub_sub: str | None = None
    ipc_req_rep: str | None = None
    tcp_pub_sub: str | None = None
    tcp_req_rep: str | None = None


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
            logger.debug("Removed local service advertisement '%s'", service_file)
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
                # Load service info
                try:
                    data = json.loads(service_file.read_text())
                    info = msgspec.convert(data, LocalServiceInfo)
                except (
                    json.JSONDecodeError,
                    msgspec.ValidationError,
                    OSError,
                ):
                    continue

                # Remove service file if process no longer exists
                if not pid_exists(info.pid):
                    service_file.unlink(missing_ok=True)
                    continue

                # Check if properties match
                if all(info.properties.get(k) == v for k, v in properties.items()):
                    yield info

        # Clean up empty directories
        with contextlib.suppress(OSError, ValueError):
            prune_empty_parent_directories(
                service_dir,
                LocalServiceAdvertisement.runtime_directory,
                remove_root=True,
            )


class DualChannelBase(contextlib.AbstractContextManager):
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
    def close(self) -> None: ...


class DualChannelHost(DualChannelBase):
    """
    A ZeroMQ host providing REQ/REP and PUB/SUB sockets with service discovery.

    Provides two communication channels: a REQ/REP channel for synchronous
    request-reply messaging and a PUB/SUB channel for broadcasting events to
    subscribers. Incoming requests are dispatched to a user-provided
    ``event_handler`` callback.

    The service is automatically advertised for discovery by
    :class:`DualChannelClient`. When ``remote=True``, the service is advertised
    via Zeroconf (mDNS) for network-wide discovery. When ``remote=False``, it is
    advertised locally via a file in the user's runtime directory for IPC-only use
    cases. On POSIX systems, clients on the same machine transparently upgrade to IPC
    for lower latency.
    """

    _zeroconf: Zeroconf | None = None
    _zeroconf_service_info: ServiceInfo | None = None
    _named_pipe_rep: Path | None = None
    _named_pipe_pub: Path | None = None

    def __init__(
        self,
        service_name: str,
        service_type: str,
        properties: dict[str, str | None] | None = None,
        event_handler: Callable[[Any], Any] | None = None,
        remote: bool = True,
        port_pub: int | None = None,
        port_rep: int | None = None,
        serialization: Literal['json', 'msgpack'] = 'msgpack',
    ) -> None:
        """
        Initialize the DualChannelHost.

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
        remote : bool, default=True
            If True, binds TCP sockets to '0.0.0.0'. Otherwise, binds to '127.0.0.1'.
        port_pub : int, optional
            TCP port to bind the PUB socket. If None, a random available port is chosen.
        port_rep : int, optional
            TCP port to bind the REP socket. If None, a random available port is chosen.
        serialization : {'json', 'msgpack'}, default='msgpack'
            Serialization format for message encoding.
        """
        # initialize base class
        super().__init__()

        self.uuid = uuid4()
        self._bind_ip = IP_ANY if remote else IP_LOOPBACK
        self._local_ip = get_local_ipv4() if remote else IP_LOOPBACK

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
                rep_ipc_addr = f'ipc://@REQ_REP_{self.uuid.hex}'
                pub_ipc_addr = f'ipc://@PUB_SUB_{self.uuid.hex}'
            else:
                # On other POSIX platforms we use filesystem Unix domain sockets
                runtime_path = platformdirs.user_runtime_path(ensure_exists=True)
                self._named_pipe_rep = runtime_path / f'REQ_REP_{self.uuid.hex}.ipc'
                self._named_pipe_pub = runtime_path / f'PUB_SUB_{self.uuid.hex}.ipc'
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
            """Helper function binding socket to TCP address with preferred port."""
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
            self._decoder = msgspec.msgpack.Decoder(type=DualChannelMessage)
        elif serialization == 'json':
            self._encoder = msgspec.json.Encoder()
            self._decoder = msgspec.json.Decoder(type=DualChannelMessage)
        else:
            raise ValueError(f'Unsupported serialization protocol: {serialization}')

        # start event loop for request handling
        self._event_handler_lock = threading.Lock()
        handshake_data = DualChannelHandshake(
            ipc_pub_sub=pub_ipc_addr,
            ipc_req_rep=rep_ipc_addr,
            tcp_pub_sub=self.pub_tcp_addr,
            tcp_req_rep=self.rep_tcp_addr,
        )
        self._event_thread = threading.Thread(
            target=DualChannelHost._event_loop,
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
            uuid=self.uuid,
            properties=properties,
        )

        # advertise service via Zeroconf for remote discovery
        if remote:
            zeroconf_type = f'_{to_snake_case(service_type)}._tcp.local.'
            zeroconf_name = f'{service_name}.{zeroconf_type}'
            self._zeroconf_service_info = ServiceInfo(
                type_=zeroconf_type,
                name=zeroconf_name,
                port=self.rep_tcp_port,
                addresses=[socket.inet_aton(self._local_ip)],
                properties=properties or {},
                server=f'{socket.gethostname()}.local.',
            )
            self._zeroconf = Zeroconf(
                interfaces=InterfaceChoice.Default,
                ip_version=IPVersion.V4Only,
            )
            self._zeroconf.register_service(
                self._zeroconf_service_info, allow_name_change=True
            )
            self._zeroconf_service_name = self._zeroconf_service_info.name
            logger.debug(
                "Registering Zeroconf service '%s'", self._zeroconf_service_name
            )

        # register finalizer to clean up resources on exit
        self._finalizer = weakref.finalize(
            self,
            DualChannelHost._finalize,
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
                    logger.debug("Unregistering Zeroconf service '%s'", service.name)
                    zeroconf.unregister_service(service)
            with contextlib.suppress(Exception):
                zeroconf.close()

        # call base class finalizer
        DualChannelBase._finalize_base(
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
    def _event_loop(  # noqa: PLR0913
        stop_event: threading.Event,
        req_rep_socket: zmq.Socket,
        decoder: msgspec.msgpack.Decoder | msgspec.json.Decoder,
        encoder: msgspec.msgpack.Encoder | msgspec.json.Encoder,
        serialization_protocol: str,
        event_handler: Callable[[Any], dict],
        event_handler_lock: threading.Lock,
        handshake_data: DualChannelHandshake,
    ) -> None:
        """
        Handle incoming REQ messages.

        Notes
        -----
        - Decodes incoming messages using the configured serialization.
        - Calls the registered event handler for type 'R'.
        - Responds with handshake data for type 'H'.
        - Sends an error for unknown types.
        """
        format_error = DualChannelHost._format_error

        # avoid overhead of attribute lookups
        send = req_rep_socket.send
        recv = req_rep_socket.recv
        decode = decoder.decode
        encode = encoder.encode

        while not stop_event.is_set():
            # wait for incoming requests (short poll so we can check stop_event)
            if not req_rep_socket.poll(100):
                continue

            # guard the actual recv with try/except so shutdown can't hang us
            try:
                request_frame = recv(copy=False)
            except zmq.ZMQError as e:
                logger.exception('Error receiving request from client', exc_info=e)
                continue

            # try to decode the request
            try:
                req: DualChannelMessage = decode(request_frame.buffer)
            except msgspec.DecodeError as e1:
                # try the other serialization as a fallback
                try:
                    if serialization_protocol == 'msgpack':
                        req = msgspec.json.decode(
                            request_frame.buffer, type=DualChannelMessage
                        )
                    else:
                        req = msgspec.msgpack.decode(
                            request_frame.buffer, type=DualChannelMessage
                        )
                except msgspec.DecodeError:
                    logger.exception('Error decoding request from client', exc_info=e1)
                    reply = format_error(type(e1).__name__, str(e1))
                    try:
                        reply_bytes = encode(reply)
                        send(reply_bytes, copy=False)
                    except (msgspec.EncodeError, zmq.ZMQError) as e2:
                        logger.exception('Error sending reply to client', exc_info=e2)
                    continue

            # handle request depending on request type
            match req.type:
                case 'R':  # general request
                    try:
                        with event_handler_lock:
                            reply_data = event_handler(req.data)
                    except Exception as e:
                        logger.exception(
                            'Event handler raised an exception', exc_info=e
                        )
                        reply = format_error(type(e).__name__, str(e))
                    else:
                        reply = DualChannelMessage('R', reply_data)

                case 'H':  # handshake for communicating TCP and IPC addresses
                    reply = DualChannelMessage(type='H', data=handshake_data)

                case _:  # unknown request type
                    message = f'Unknown request type: {req.type}'
                    logger.error(message)
                    reply = format_error('RequestError', message)

            # encode reply
            try:
                reply_bytes = encode(reply)
            except msgspec.EncodeError as e:
                logger.exception('Error encoding reply to client', exc_info=e)
                reply = format_error(type(e).__name__, str(e))
                reply_bytes = encode(reply)

            # send reply
            try:
                send(reply_bytes, copy=False)
            except zmq.ZMQError as e:
                logger.exception('Error sending reply to client', exc_info=e)

    @staticmethod
    def _format_error(name: str, message: str) -> DualChannelMessage:
        """
        Format an error response message.

        Parameters
        ----------
        name : str
            The error type name.
        message : str
            Human-readable error message.

        Returns
        -------
        DualChannelMessage
            A message with type 'E' containing error details.
        """
        return DualChannelMessage(type='E', data={'name': name, 'message': message})

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


class DualChannelClient(DualChannelBase):
    """A client for communicating with a DualChannelHost."""

    def __init__(
        self,
        service_type: str,
        address: str | None = None,
        event_handler: Callable[[dict], Any] | None = None,
        discovery_timeout: float = 10.0,
        txt_properties: dict | None = None,
        remote: bool = True,
    ) -> None:
        """
        Initialize a DualChannelClient instance.

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
        remote : bool, optional
            Whether to use Zeroconf for discovering remote services, by default True.
        """
        # initialize base class
        super().__init__()

        # ZeroMQ sockets
        self._socket_req_rep = self._zmq_context.socket(zmq.REQ)
        self._socket_pub_sub = self._zmq_context.socket(zmq.SUB)

        # define msgspec encoder/decoder
        serialization_module = getattr(msgspec, self._serialization)
        self._encoder = serialization_module.Encoder()
        self._decoder = serialization_module.Decoder(type=DualChannelMessage)

        # connect REQ channel
        if address is not None:
            self._address_req = address
        else:
            self._address_req, _ = discover(
                service_type, txt_properties, remote, discovery_timeout
            )
        self._socket_req_rep.connect(self._address_req)
        self._lock_req = threading.Lock()
        logger.debug("Connecting REQ socket to '%s'", self._address_req)
        self.is_local = any(x in self._address_req for x in ('127.0.0.1', 'ipc://'))

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
            target=DualChannelClient._event_loop,
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
            DualChannelBase._finalize_base,
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
            msg = socket_sub.recv()
            msg = decoder.decode(msg)
            try:
                event_handler(msg)
            except Exception as e:
                logger.exception('Subscription handler raised an exception', exc_info=e)

    def _handshake(self) -> None:
        """Perform handshake with the host."""
        reply_type, reply_data = self._req('H')
        if (
            self.is_local
            and os.name == 'posix'
            and reply_data.get('ipc_req_rep') is not None
            and reply_data.get('ipc_pub_sub') is not None
        ):
            if self._address_req.startswith(('tcp://', 'tcp4://', 'tcp6://')):
                self._socket_req_rep.disconnect(self._address_req)
                self._address_req = reply_data.get('ipc_req_rep')
                logger.debug("Reconnecting REQ socket to '%s'", self._address_req)
                self._socket_req_rep.connect(self._address_req)
            self._address_sub = reply_data.get('ipc_pub_sub')
        else:
            self._address_sub = reply_data.get('tcp_pub_sub')

    def _req(self, request_type: str, data: Any | None = None) -> tuple[str, Any]:
        with self._lock_req:  # acquire lock
            # encode request
            request = DualChannelMessage(type=request_type, data=data)
            try:
                request_bytes = self._encoder.encode(request)
            except msgspec.EncodeError as e:
                raise ValueError('Error encoding request to host') from e

            # send request
            try:
                self._socket_req_rep.send(request_bytes)
            except zmq.ZMQError as e:
                raise RuntimeError('Error sending request to host') from e

            # receive reply
            try:
                reply_frame = self._socket_req_rep.recv(copy=False)
            except zmq.ZMQError as e:
                raise RuntimeError('Error receiving reply from host') from e

            # receive and decode reply
            try:
                reply: DualChannelMessage = self._decoder.decode(reply_frame.buffer)

            # switch serialization format
            except msgspec.DecodeError as e:
                new_format = 'msgpack' if self._serialization == 'json' else 'json'
                new_serialization_module = getattr(msgspec, new_format)
                new_decoder = new_serialization_module.Decoder(type=DualChannelMessage)
                try:
                    reply = new_decoder.decode(reply_frame.buffer)
                except msgspec.DecodeError:
                    raise ValueError('Error decoding reply from host') from e
                logger.debug('Switching to %s serialization', new_format)
                self._encoder = new_serialization_module.Encoder()
                self._decoder = new_decoder
                self._serialization = cast('Literal["json", "msgpack"]', new_format)

            # return reply type and data
            return reply.type, reply.data

    def request(self, **kwargs: Any) -> Any:
        """
        Send a generic request to the server.

        Parameters
        ----------
        **kwargs : Any
            Key-value pairs to be sent as the request payload.

        Returns
        -------
        Any
            The reply data from the server. Returns an empty dictionary if an error
            occurs.
        """
        reply_type, reply_data = self._req('R', kwargs)
        match reply_type:
            case 'R':  # general request
                return reply_data
            case 'E':  # error
                logger.error(
                    'Remote %s: %s',
                    reply_data.get('name', 'Error'),
                    reply_data.get('message', ''),
                )
            case _:
                logger.error("Received unknown reply type: '%s'", reply_type)
        return {}

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


def discover(
    service_type: str,
    properties: dict[str, str | None] | None = None,
    remote: bool = True,
    timeout: float = 10,
) -> tuple[str, dict[str, str | None]]:
    """
    Discover a device/service on the local network matching given properties.

    Parameters
    ----------
    service_type : str
        The service type to discover, e.g., 'bpod'
    properties : dict, optional
        Dictionary of expected service properties to match.
    remote : bool, optional
        Whether to search for a matching service on the network, by default True.
    timeout : float, optional
        How many seconds to wait for a matching service before timing out.
        Default is 10.

    Returns
    -------
    str
        The Zeroconf service address, e.g., 'tcp://192.168.1.10:1234'.
    dict
        A dictionary of service properties.

    Raises
    ------
    TimeoutError
        If no matching device/service is found within the timeout period.
    """
    properties = properties or {}
    for local_info in LocalServiceAdvertisement.discover(service_type, properties):
        return local_info.address, local_info.properties
    if not remote:
        raise RuntimeError('No matching service found locally')

    event = threading.Event()
    zc_service_type = f'_{to_snake_case(service_type)}._tcp.local.'
    address: str | None = None
    txt_record: dict[str, str | None] = {}

    def on_state_change(
        *, name: str, state_change: ServiceStateChange, **_: Any
    ) -> None:
        nonlocal address, txt_record
        if event.is_set():
            return
        if state_change is ServiceStateChange.Added:
            remote_info = zc.get_service_info(zc_service_type, name)
            if not remote_info or not remote_info.addresses:
                return
            for k, v in properties.items():
                if remote_info.decoded_properties.get(k) != v:
                    return
            port = remote_info.port
            ip = socket.inet_ntoa(remote_info.addresses[0])
            ip = '127.0.0.1' if ip == get_local_ipv4() else ip
            address = f'tcp://{ip}:{port}'
            txt_record = remote_info.decoded_properties
            event.set()

    with Zeroconf() as zc:
        found = False
        browser = None
        try:
            browser = ServiceBrowser(zc, zc_service_type, handlers=[on_state_change])
            found = event.wait(timeout)
        finally:
            if browser is not None:
                with contextlib.suppress(Exception):
                    browser.cancel()
    if not found or address is None:
        raise TimeoutError('No matching service found')
    return address, txt_record
