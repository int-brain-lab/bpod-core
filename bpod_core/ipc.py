"""Inter-process Communication, service discovery and related."""

import contextlib
import os
import re
import socket
import sys
import threading
import uuid
import weakref
from abc import ABC, abstractmethod
from collections.abc import Callable
from types import TracebackType
from typing import Any, Literal, cast

import msgspec
import zmq
from typing_extensions import Self
from zeroconf import (
    InterfaceChoice,
    IPVersion,
    ServiceBrowser,
    ServiceInfo,
    ServiceStateChange,
    Zeroconf,
)

from bpod_core.com import logger
from bpod_core.misc import convert_to_snake_case, get_local_ipv4

IP_LOOPBACK = '127.0.0.1'
IP_ANY = '0.0.0.0'


class DualChannelMessage(msgspec.Struct, omit_defaults=True, array_like=True):
    type: str = msgspec.field(name='T')  # message type
    data: Any | None = msgspec.field(default=None, name='D')  # message data


class DualChannelBase(ABC):
    _serialization: Literal['json', 'msgpack'] = 'msgpack'
    _encoder: msgspec.msgpack.Encoder | msgspec.json.Encoder
    _decoder: msgspec.msgpack.Decoder | msgspec.json.Decoder
    _event_thread: threading.Thread
    _socket_req_rep: zmq.Socket
    _socket_pub_sub: zmq.Socket

    def __init__(self) -> None:
        self._closed = False
        self._lock_close = threading.Lock()
        self._finalizer = weakref.finalize(self, self.close)
        self._zmq_context = zmq.Context()
        self._stop_event_loop = threading.Event()

    def __enter__(self) -> Self:
        """Enter context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool | None:
        """Exit context manager."""
        self.close()
        return None

    @abstractmethod
    def _event_loop(self): ...

    def close(self) -> bool:
        """
        Close the instance.

        Releases resources, stops the event loop, and terminates ZeroMQ sockets and
        context.

        Returns
        -------
        bool
            Returns True if the instance was successfully closed, False if it was
            already closed.
        """
        with self._lock_close:
            if self._closed:
                return False
            self._closed = True

            if self._event_thread and self._event_thread.is_alive():
                self._stop_event_loop.set()
                self._event_thread.join()

            # close sockets and terminate context
            self._socket_req_rep.close(linger=0)
            self._socket_pub_sub.close(linger=0)
            self._zmq_context.term()
            return True


class DualChannelHost(DualChannelBase):
    """A ZeroMQ host providing REQ/REP and PUB/SUB sockets with Zeroconf discovery."""

    _rep_ipc_addr: str | None = None
    _pub_ipc_addr: str | None = None

    def __init__(
        self,
        service_name: str,
        service_type: str,
        txt_record: dict[str | bytes, str | bytes | None] | None = None,
        event_handler: Callable[[Any | None], Any] | None = None,
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
            Zeroconf service type (e.g., 'my_service').
        txt_record : dict, optional
            Additional TXT records for Zeroconf service advertisement.
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

        self.name = convert_to_snake_case(service_name).strip('_')
        self.uuid = uuid.uuid4()
        self._bind_ip = IP_ANY if remote else IP_LOOPBACK
        self._local_ip = get_local_ipv4() if remote else IP_LOOPBACK

        # ZeroMQ sockets
        self._socket_req_rep = self._zmq_context.socket(zmq.REP)
        self._socket_pub_sub = self._zmq_context.socket(zmq.PUB)

        # socket options / high-water marks
        self._socket_req_rep.setsockopt(zmq.SNDHWM, 1000)
        self._socket_req_rep.setsockopt(zmq.RCVHWM, 1000)
        self._socket_pub_sub.setsockopt(zmq.SNDHWM, 1000)

        # bind sockets to IPC addresses (POSIX only)
        # clients on localhost can upgrade to IPC (named pipes) for improved performance
        if 'win' not in sys.platform:
            self._rep_ipc_addr = f'ipc:///tmp/REQ_REP_{self.uuid.hex}.ipc'
            self._pub_ipc_addr = f'ipc:///tmp/PUB_SUB_{self.uuid.hex}.ipc'
            try:
                self._socket_req_rep.bind(self._rep_ipc_addr)
                self._socket_pub_sub.bind(self._pub_ipc_addr)
                logger.debug("Binding REP socket to '%s'", self._rep_ipc_addr)
                logger.debug("Binding PUB socket to '%s'", self._pub_ipc_addr)
            except zmq.ZMQError:
                logger.warning('Failed to bind IPC sockets; continuing without IPC')
                self._rep_ipc_addr = None
                self._pub_ipc_addr = None

        def bind_tcp(zmq_socket: zmq.Socket, tcp_port: int | None) -> tuple[str, int]:
            """Bind socket to TCP address with preferred port."""
            bind_address = f'tcp://{self._bind_ip}'
            service_address = f'tcp://{self._local_ip}'
            if tcp_port is not None:
                try:
                    zmq_socket.bind(f'{bind_address}:{tcp_port}')
                except zmq.ZMQError:
                    tcp_port = None
            if tcp_port is None:
                tcp_port = zmq_socket.bind_to_random_port(bind_address)
            return f'{service_address}:{tcp_port}', tcp_port

        # bind sockets to TCP addresses
        self.rep_tcp_addr, self.rep_tcp_port = bind_tcp(self._socket_req_rep, port_rep)
        self.pub_tcp_addr, self.pub_tcp_port = bind_tcp(self._socket_pub_sub, port_pub)
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
        self._user_event_handler = event_handler or self._empty_event_handler
        self._event_handler_lock = threading.RLock()
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._event_thread.start()

        # advertise service via Zeroconf
        self._service_type = f'_{service_type.strip("_")}._tcp.local.'
        self._service_name = f'{self.name}.{self._service_type}'
        self._service_info = ServiceInfo(
            type_=self._service_type,
            name=self._service_name,
            port=self.rep_tcp_port,
            addresses=[socket.inet_aton(self._local_ip)],
            properties=txt_record or {},
            server=f'{socket.gethostname()}.local.',
        )
        self._zeroconf = Zeroconf(
            interfaces=InterfaceChoice.Default if remote else IP_LOOPBACK,
            ip_version=IPVersion.V4Only,
        )
        self._zeroconf.register_service(self._service_info, allow_name_change=True)
        self._service_name = self._service_info.name
        logger.debug("Registering Zeroconf service '%s'", self._service_name)

    @staticmethod
    def _empty_event_handler(_: Any) -> dict:
        """Default event handler that returns an empty dict."""
        return {}

    def _event_loop(self) -> None:
        """
        Handle incoming REQ messages.

        Notes
        -----
        - Decodes incoming messages using the configured serialization.
        - Calls the registered event handler for type 'R'.
        - Responds with handshake data for type 'H'.
        - Sends an error for unknown types.
        """
        while not self._stop_event_loop.is_set():
            # wait for incoming requests (short poll so we can check stop_event)
            if not self._socket_req_rep.poll(100):
                continue

            # guard the actual recv with try/except so shutdown can't hang us
            try:
                request_frame = self._socket_req_rep.recv(copy=False)
            except zmq.ZMQError as e:
                logger.exception('Error receiving request from client', exc_info=e)
                continue

            # try to decode the request
            try:
                request: DualChannelMessage = self._decoder.decode(request_frame.bytes)
            except msgspec.DecodeError as e1:
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
                        exc_info=e1,
                    )
                    reply = self._format_error(type(e1).__name__, e1.args[0])
                    try:
                        reply_bytes = self._encoder.encode(reply)
                        self._socket_req_rep.send(reply_bytes, copy=False)
                    except (msgspec.EncodeError, zmq.ZMQError) as e2:
                        logger.exception('Error sending reply to client', exc_info=e2)
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
                            'ipc_pub_sub': self._pub_ipc_addr,
                            'ipc_req_rep': self._rep_ipc_addr,
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
                self._socket_req_rep.send(reply_bytes, copy=False)
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

    def close(self) -> bool:
        """
        Close the host and clean up resources.

        Returns
        -------
        bool
            True if the host closed successfully, False otherwise.
        """
        if not super().close():
            return False

        # Unregister Zeroconf service
        logger.debug("Unregistering Zeroconf service '%s'", self._service_name)
        self._zeroconf.unregister_service(self._service_info)
        self._zeroconf.close()

        # remove IPC files
        with contextlib.suppress(FileNotFoundError):
            if self._rep_ipc_addr is not None:
                os.remove(self._rep_ipc_addr[6:])
            if self._pub_ipc_addr is not None:
                os.remove(self._pub_ipc_addr[6:])

        return True


class DualChannelClient(DualChannelBase):
    """A client for communicating with a DualChannelHost."""

    def __init__(
        self,
        service_type: str,
        address: str | None = None,
        event_handler: Callable[[dict], Any] | None = None,
        discovery_timeout: float = 10.0,
        txt_properties: dict | None = None,
    ):
        """
        Initialize a DualChannelClient instance.

        Parameters
        ----------
        service_type : str
            The mDNS service type to discover or connect to.
        address : str, optional
            The direct connection address for the REQ channel, by default None.
        event_handler : callable, optional
            A callback to handle PUB messages, by default None.
        discovery_timeout : float, optional
            Timeout in seconds for service discovery, by default 10.0.
        txt_properties : dict, optional
            Properties for service filtering during discovery, by default None.
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
                service_type, txt_properties, discovery_timeout
            )
        self._socket_req_rep.connect(self._address_req)
        self._lock_req = threading.Lock()
        logger.debug("Binding REQ socket to '%s'", self._address_req)
        self.is_local = '127.0.0.1' in self._address_req

        # perform handshake
        self._handshake()

        # connect SUB channel
        self._socket_pub_sub.connect(self._address_sub)
        self._socket_pub_sub.setsockopt_string(zmq.SUBSCRIBE, '')
        logger.debug("Binding SUB socket to '%s'", self._address_sub)

        # start event loop for subscription handling
        self._event_handler = event_handler
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._event_thread.start()

    def _event_loop(self):
        """Process incoming PUB messages."""
        while not self._stop_event_loop.is_set():
            if not self._socket_pub_sub.poll(100):
                continue
            msg = self._socket_pub_sub.recv()
            msg = self._decoder.decode(msg)
            try:
                self._event_handler(msg)
            except Exception as e:
                logger.exception('Subscription handler raised an exception', exc_info=e)

    def _handshake(self):
        """Perform handshake with the host."""
        reply_type, reply_data = self._req('H')
        if (
            self.is_local
            and sys.platform in ('darwin', 'linux')
            and reply_data.get('ipc_req_rep') is not None
            and reply_data.get('ipc_pub_sub') is not None
        ):
            self._socket_req_rep.disconnect(self._address_req)
            self._address_req = reply_data.get('ipc_req_rep')
            logger.debug("Rebinding REQ socket to '%s'", self._address_req)
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
                reply: DualChannelMessage = self._decoder.decode(reply_frame.bytes)

            # switch serialization format
            except msgspec.DecodeError as e:
                new_format = 'msgpack' if self._serialization == 'json' else 'json'
                new_serialization_module = getattr(msgspec, new_format)
                new_decoder = new_serialization_module.Decoder(type=DualChannelMessage)
                try:
                    reply = new_decoder.decode(reply_frame.bytes)
                except msgspec.DecodeError:
                    raise ValueError('Error decoding reply from host') from e
                logger.debug('Switching to %s serialization', new_format)
                self._encoder = new_serialization_module.Encoder()
                self._decoder = new_decoder
                self._serialization = cast('Literal["json", "msgpack"]', new_format)

            # return reply type and data
            return reply.type, reply.data

    def request(self, **kwargs) -> Any:
        """
        Send a generic request to the server.

        Parameters
        ----------
        **kwargs : dict
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


def discover(
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
