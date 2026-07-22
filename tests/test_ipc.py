import json
import logging
import os
import socket
import threading
import time
from typing import Any, NamedTuple
from uuid import uuid4

import msgspec
import pytest
import zeroconf
import zmq
from zeroconf import ServiceBrowser

from bpod_core import ipc


def _noop_handler(_):
    """Request handler stub for hosts whose REQ channel is not under test."""
    return {}


class _EventA(msgspec.Struct, tag=True):
    value: int


class _EventB(msgspec.Struct, tag=True):
    text: str


class _Echo(msgspec.Struct):
    echo: Any


def _publish_until(host, message, predicate, timeout=2.0):
    """Publish `message` repeatedly until `predicate()` is true (slow-joiner safe)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        host.publish(message)
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('published message was not received in time')


def _wait_until(predicate, timeout=2.0):
    """Poll `predicate` until it is true or `timeout` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture(autouse=True)
def fast_event_loop(mocker):
    mocker.patch('bpod_core.ipc._EVENT_LOOP_POLL_MS', 5)


class TestLocalServiceAdvertisement:
    """Tests for LocalServiceAdvertisement class."""

    def test_context_manager(self, mock_local_discovery_dir):
        """Advertisement can be used as a context manager."""
        with ipc.LocalServiceAdvertisement(
            service_name='service_name',
            service_type='service_type',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        ) as ad:
            assert ad.service_file.is_relative_to(mock_local_discovery_dir)
            assert ad.service_file.exists()
            assert ad.service_file.suffix == '.json'
        assert not ad.service_file.exists()
        assert not mock_local_discovery_dir.exists()

    def test_close_removes_file(self, mock_local_discovery_dir):
        """close() removes the service file and removes empty parent directories."""
        ad = ipc.LocalServiceAdvertisement(
            service_name='service_name',
            service_type='service_type',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        )
        assert ad.service_file.exists()
        ad.close()
        assert not ad.service_file.exists()
        assert not mock_local_discovery_dir.exists()

    def test_file_contains_correct_data(self, mock_local_discovery_dir):
        """Service file contains correct JSON data."""
        uuid = uuid4()
        with ipc.LocalServiceAdvertisement(
            service_name='service_name',
            service_type='service_type',
            address='tcp://127.0.0.1:5555',
            pid=12345,
            uuid=uuid,
            properties={'key': 'value'},
        ) as ad:
            data = json.loads(ad.service_file.read_text())
            assert data['service_name'] == 'service_name'
            assert data['service_type'] == 'service_type'
            assert data['address'] == 'tcp://127.0.0.1:5555'
            assert data['pid'] == 12345
            assert data['uuid'] == str(uuid)
            assert data['properties'] == {'key': 'value'}

    def test_discover_finds_service(self, mock_local_discovery_dir):
        """discover() yields advertised services."""
        with ipc.LocalServiceAdvertisement(
            service_name='service_name',
            service_type='service_type',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
            properties={'name': 'test'},
        ):
            results = list(ipc.LocalServiceAdvertisement.discover('service_type'))
            assert len(results) == 1
            assert results[0].service_name == 'service_name'
            assert results[0].address == 'tcp://127.0.0.1:5555'
            assert results[0].properties == {'name': 'test'}

    def test_discover_filters_by_properties(self, mock_local_discovery_dir):
        """discover() filters services by matching properties."""
        with (
            ipc.LocalServiceAdvertisement(
                service_name='service_name_1',
                service_type='service_type',
                address='tcp://127.0.0.1:5555',
                pid=os.getpid(),
                properties={'name': 'first'},
            ),
            ipc.LocalServiceAdvertisement(
                service_name='service_name_2',
                service_type='service_type',
                address='tcp://127.0.0.1:5556',
                pid=os.getpid(),
                properties={'name': 'second'},
            ),
        ):
            results = list(ipc.LocalServiceAdvertisement.discover('service_type'))
            assert len(results) == 2
            results = list(
                ipc.LocalServiceAdvertisement.discover(
                    'service_type', properties={'name': 'second'}
                )
            )
            assert len(results) == 1
            assert results[0].address == 'tcp://127.0.0.1:5556'

    def test_discover_removes_stale_advertisements(
        self, mock_local_discovery_dir, mocker
    ):
        """discover() removes files for dead processes."""
        with ipc.LocalServiceAdvertisement(
            service_name='service_name',
            service_type='service_type',
            address='tcp://127.0.0.1:5555',
            pid=99999999,  # non-existent PID
        ) as ad:
            assert ad.service_file.exists()
            mocker.patch('bpod_core.ipc.pid_exists', return_value=False)
            results = list(ipc.LocalServiceAdvertisement.discover('service_type'))
            assert len(results) == 0
        assert not ad.service_file.exists()
        assert not mock_local_discovery_dir.exists()

    def test_discover_nonexistent_service(self, mock_local_discovery_dir):
        """discover() returns empty iterator for unknown service types."""
        assert list(ipc.LocalServiceAdvertisement.discover('nonexistent')) == []

    def test_service_type_sanitization(self, mock_local_discovery_dir):
        """Service types with special characters are sanitized."""
        with ipc.LocalServiceAdvertisement(
            service_name='service_name',
            service_type='service_type.local',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        ) as ad:
            assert '.' not in ad.service_file.parent.name

    def test_handles_corrupted_file(self, mock_local_discovery_dir):
        """discover() skips corrupted JSON files."""
        # Create a valid advertisement first to get the directory
        with ipc.LocalServiceAdvertisement(
            service_name='service_name',
            service_type='service_type',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        ) as ad:
            service_dir = ad.service_file.parent
            corrupted_file = service_dir / 'corrupted.json'
            corrupted_file.write_text('not valid json')
            results = list(ipc.LocalServiceAdvertisement.discover('service_type'))
            assert len(results) == 1
            assert results[0].address == 'tcp://127.0.0.1:5555'


@pytest.fixture
def mock_service_browser(mocker):
    return mocker.patch('bpod_core.ipc.ServiceBrowser')


@pytest.mark.parametrize(
    ('address', 'expected'),
    [
        ('ipc://@REQ_REP_abc', True),
        ('ipc:///tmp/foo.ipc', True),
        ('tcp://127.0.0.1:5555', True),
        ('tcp://127.0.0.99:5555', True),
        ('tcp://localhost:5555', True),
        ('tcp://[::1]:5555', True),
        ('tcp://192.168.1.10:5555', False),
        ('tcp://rig-pc.local:5555', False),  # DNS hostname, not an IP literal
        ('tcp://:5555', False),
    ],
)
def test_is_local_address(address, expected):
    """_is_local_address() detects loopback and IPC addresses without raising."""
    assert ipc._is_local_address(address) is expected


class TestErrorData:
    """Tests for ErrorData and RemoteError."""

    def test_from_active_exception(self):
        """Without an argument, the active exception is serialized."""
        try:
            raise KeyError('boom')  # noqa: TRY301
        except KeyError:
            error_data = ipc.ErrorData.from_exception()
        assert error_data.name == 'KeyError'
        assert error_data.args == ('boom',)
        assert 'KeyError' in error_data.traceback

    def test_no_active_exception_raises(self):
        """Without an argument and no active exception, ValueError is raised."""
        with pytest.raises(ValueError, match='No exception'):
            ipc.ErrorData.from_exception()

    def test_broken_str_method(self):
        """Exceptions with a broken __str__ are serialized with a placeholder."""

        class BrokenStrError(Exception):
            def __str__(self):
                raise RuntimeError('nope')

        error_data = ipc.ErrorData.from_exception(BrokenStrError())
        assert error_data.name == 'BrokenStrError'
        assert 'unprintable' in error_data.message

    def test_traceback_formatting_failure(self, mocker):
        """If traceback formatting fails, ErrorData serializes without one."""
        mocker.patch.object(ipc.traceback, 'format_exception', side_effect=RuntimeError)
        error_data = ipc.ErrorData.from_exception(ValueError('boom'))
        assert error_data.traceback is None
        assert 'ValueError: boom' in str(ipc.RemoteError(error_data))

    def test_remote_error_str_includes_traceback(self):
        """RemoteError renders the remote traceback when available."""
        try:
            raise ValueError('boom')  # noqa: TRY301
        except ValueError as e:
            error_data = ipc.ErrorData.from_exception(e)
        message = str(ipc.RemoteError(error_data))
        assert 'occurred on the remote side' in message
        assert 'Traceback' in message


class TestClient:
    """Tests for the ServiceClient class."""

    @pytest.fixture
    def host(self, mock_advertisement):
        with ipc.ServiceHost(
            service_name='TestService',
            service_type='dualtest',
            request_handler=lambda data: {'echo': data},
            serialization='json',
            remote=False,
        ) as host:
            yield host

    @pytest.fixture
    def client(self, host, mock_service_browser):
        with ipc.ServiceClient(
            service_type='dualtest',
            address=host.rep_tcp_addr,
            discovery_timeout=0,
            default_reply_type=dict,
        ) as client:
            yield client

    def test_handshake(self, client):
        """Verify handshake exchanges addresses and negotiates serialization."""
        assert client.address_req.startswith(('tcp://', 'ipc://'))
        assert client.address_sub.startswith(('tcp://', 'ipc://'))
        assert client._serialization == 'json', 'Serialization should be JSON'

    def test_sub_address_built_from_req_ip(self, mock_advertisement, mocker):
        """The SUB address combines the client's REQ IP with the handshake port."""
        # skip the host's IPC binds so the handshake falls back to the TCP path
        mocker.patch.object(ipc.ServiceHost, '_bind_ipc', return_value=(None, None))
        with (
            ipc.ServiceHost(
                service_name='TestService',
                service_type='dualtest',
                request_handler=_noop_handler,
                remote=False,
            ) as host,
            ipc.ServiceClient(
                service_type='dualtest',
                address=host.rep_tcp_addr,
                discovery_timeout=0,
            ) as client,
        ):
            assert client.address_sub == f'tcp://127.0.0.1:{host.pub_tcp_port}'

    def test_event_type_requires_handler(self):
        """event_type without event_handler raises ValueError."""
        with pytest.raises(ValueError, match='requires an event_handler'):
            ipc.ServiceClient(
                service_type='dualtest',
                address='tcp://127.0.0.1:1',
                event_type=dict,
            )

    def test_request_response(self, client):
        """Round-trip a request to the host and validate payload."""
        reply = client.request({'foo': 'bar'})
        assert reply == {'echo': {'foo': 'bar'}}

    def test_explicit_reply_type(self, client):
        """A non-default reply_type is decoded with the flexible slow path."""
        reply = client.request({'foo': 'bar'}, reply_type=_Echo)
        assert isinstance(reply, _Echo)
        assert reply.echo == {'foo': 'bar'}

    def test_unencodable_request_raises(self, client):
        """A request payload that cannot be encoded raises ServiceError."""
        with pytest.raises(ipc.ServiceError, match='encoding'):
            client.request(object())
        assert client.request('ok') == {'echo': 'ok'}, 'channel should stay usable'

    def test_error_response(self, mock_advertisement, mock_service_browser):
        """Verify server exceptions are logged and client gets empty dict."""

        def bad_handler(_):
            raise RuntimeError('boom')

        with (
            ipc.ServiceHost('Test', 'service', request_handler=bad_handler) as host,
            ipc.ServiceClient('service', host.rep_tcp_addr) as client,
            pytest.raises(ipc.RemoteError, match='boom'),
        ):
            client.request({'foo': 'bar'})

    def test_unencodable_reply(self, mock_advertisement, mock_service_browser):
        """An unencodable reply yields a RemoteError; the channel stays usable."""

        def handler(data):
            return object() if data == 'bad' else {'echo': data}

        with (
            ipc.ServiceHost('Test', 'service', handler, remote=False) as host,
            ipc.ServiceClient('service', host.rep_tcp_addr) as client,
        ):
            with pytest.raises(ipc.RemoteError, match='unsupported'):
                client.request('bad')
            assert client.request('good') == {'echo': 'good'}

    def test_unencodable_exception_args(self, mock_advertisement, mock_service_browser):
        """Exceptions with non-primitive args are delivered as RemoteError."""

        def handler(_):
            raise ValueError('boom', object())

        with (
            ipc.ServiceHost('Test', 'service', handler, remote=False) as host,
            ipc.ServiceClient('service', host.rep_tcp_addr) as client,
            pytest.raises(ipc.RemoteError, match='boom'),
        ):
            client.request({'foo': 'bar'})

    def test_request_timeout_and_recovery(
        self, mock_advertisement, mock_service_browser
    ):
        """A timed-out request raises TimeoutError; the channel recovers after."""

        def handler(data):
            if data == 'slow':
                time.sleep(0.1)
            return {'echo': data}

        with (
            ipc.ServiceHost('Test', 'service', handler, remote=False) as host,
            ipc.ServiceClient('service', host.rep_tcp_addr) as client,
        ):
            with pytest.raises(TimeoutError):
                client.request('slow', timeout=0.05)
            assert client.request('fast') == {'echo': 'fast'}

    def test_request_after_close_raises(self, mock_advertisement, mock_service_browser):
        """Requests on a closed client raise ServiceError."""
        with (
            ipc.ServiceHost('Test', 'service', _noop_handler, remote=False) as host,
            ipc.ServiceClient('service', host.rep_tcp_addr) as client,
        ):
            client.close()
            with pytest.raises(ipc.ServiceError, match='closed'):
                client.request({'foo': 'bar'})


class TestPublish:
    """Tests for the PUB/SUB channel."""

    @pytest.fixture
    def host(self, mock_advertisement):
        with ipc.ServiceHost(
            service_name='TestService',
            service_type='pubtest',
            request_handler=lambda data: {'echo': data},
            remote=False,
        ) as host:
            yield host

    @pytest.fixture
    def received(self):
        return []

    @pytest.fixture
    def client(self, host, mock_service_browser, received):
        with ipc.ServiceClient(
            service_type='pubtest',
            address=host.rep_tcp_addr,
            event_handler=received.append,
            discovery_timeout=0,
            default_reply_type=dict,
            event_type=_EventA | _EventB,
        ) as client:
            yield client

    def test_publish_delivers_typed_message(self, host, client, received):
        """A published struct is delivered and decoded with event_type."""
        _publish_until(host, _EventA(value=7), lambda: bool(received))
        assert isinstance(received[0], _EventA), 'event decoder, not reply decoder'
        assert received[0].value == 7

    def test_tagged_union_dispatch(self, host, client, received):
        """Distinct struct types decode to their respective union members."""
        _publish_until(host, _EventA(value=1), lambda: bool(received))
        received.clear()
        _publish_until(
            host,
            _EventB(text='hi'),
            lambda: any(isinstance(m, _EventB) for m in received),
        )
        assert any(isinstance(m, _EventB) and m.text == 'hi' for m in received)

    def test_decode_failure_keeps_thread_alive(self, host, client, received):
        """An undecodable message is logged and does not kill the subscription."""
        _publish_until(host, _EventA(value=1), lambda: bool(received))
        received.clear()
        host.publish({'no': 'tag'})  # cannot decode as the tagged union
        _publish_until(
            host,
            _EventB(text='ok'),
            lambda: any(isinstance(m, _EventB) for m in received),
        )

    def test_handler_exception_keeps_thread_alive(self, host, mock_service_browser):
        """A raising event handler is logged and does not kill the subscription."""
        received = []

        def handler(message):
            received.append(message)
            raise RuntimeError('handler boom')

        with ipc.ServiceClient(
            'pubtest',
            address=host.rep_tcp_addr,
            event_handler=handler,
            discovery_timeout=0,
            event_type=_EventA | _EventB,
        ):
            _publish_until(host, _EventA(value=1), lambda: bool(received))
            received.clear()
            _publish_until(
                host,
                _EventB(text='ok'),
                lambda: any(isinstance(m, _EventB) for m in received),
            )

    def test_publish_after_close_warns(self, mock_advertisement, caplog):
        """Publishing on a closed host logs a warning and returns silently."""
        host = ipc.ServiceHost('test', 'pubtest', _noop_handler, remote=False)
        host.close()
        with caplog.at_level(logging.WARNING, logger='bpod_core.ipc'):
            host.publish({'x': 1})
        assert any('closed' in record.message for record in caplog.records)

    def test_publish_encode_error_raises(self, host, client):
        """A non-encodable payload raises ServiceError (given a subscriber)."""
        with pytest.raises(ipc.ServiceError, match='encoding'):
            host.publish(object())

    def test_no_subscribers_skips_encoding(self, host, mocker):
        """Without subscribers, publish() drops the message before encoding."""
        assert host.has_subscribers is False
        encoder = mocker.Mock()
        host._pub_encoder = encoder
        host.publish({'x': 1})
        encoder.encode.assert_not_called()

    def test_has_subscribers_lifecycle(self, host, mock_service_browser, mocker):
        """Flag rises with a handshake, persists while subscribed, falls on close."""
        mocker.patch('bpod_core.ipc._HELLO_GRACE_S', 0.05)
        assert host.has_subscribers is False
        with ipc.ServiceClient(
            'pubtest',
            address=host.rep_tcp_addr,
            event_handler=lambda _: None,
            discovery_timeout=0,
        ):
            # set synchronously with the handshake, before the client is done
            assert host.has_subscribers is True
            # outlives the HELLO grace period thanks to the actual subscription
            time.sleep(0.2)
            assert host.has_subscribers is True
        assert _wait_until(lambda: not host.has_subscribers), (
            'flag should clear after the subscriber disconnects'
        )

    def test_wait_for_subscribers(self, host, mock_service_browser):
        """wait_for_subscribers blocks until a subscription (or grace) registers."""
        assert host.wait_for_subscribers(timeout=0.05) is False
        with ipc.ServiceClient(
            'pubtest',
            address=host.rep_tcp_addr,
            event_handler=lambda _: None,
            discovery_timeout=0,
        ):
            assert host.wait_for_subscribers(timeout=2.0) is True

    def test_hello_grace_expires_without_subscription(
        self, host, mock_service_browser, mocker
    ):
        """A handshake from a non-subscribing client raises the flag only briefly."""
        mocker.patch('bpod_core.ipc._HELLO_GRACE_S', 0.05)
        with ipc.ServiceClient(
            'pubtest', address=host.rep_tcp_addr, discovery_timeout=0
        ):
            assert host.has_subscribers is True, 'HELLO grace should raise the flag'
            assert _wait_until(lambda: not host.has_subscribers), (
                'flag should clear once the grace period expires'
            )


class TestHost:
    """Tests for the ServiceHost class."""

    def test_basic_init_and_properties(self, mock_advertisement):
        """Check ports, addresses, and Zeroconf objects are initialized."""
        with ipc.ServiceHost('test', 'test_service', _noop_handler) as host:
            assert host.rep_tcp_addr.startswith('tcp://')
            assert host._zeroconf is not None

    def test_preferred_port_used_when_free(self, mock_advertisement):
        """A free preferred port is used as-is."""
        with ipc.ServiceHost('a', 'test_service', _noop_handler, remote=False) as first:
            port = first.rep_tcp_port
        with ipc.ServiceHost(
            'b', 'test_service', _noop_handler, port_rep=port, remote=False
        ) as host:
            assert host.rep_tcp_port == port

    def test_ports_remembered_for_uuid(self, mock_advertisement, mock_ports_file):
        """A caller-supplied UUID reuses the ports bound on the previous start."""
        uuid = uuid4()
        with ipc.ServiceHost(
            'test', 'test_service', _noop_handler, uuid=uuid, remote=False
        ) as first:
            ports = (first.rep_tcp_port, first.pub_tcp_port)
        with ipc.ServiceHost(
            'test', 'test_service', _noop_handler, uuid=uuid, remote=False
        ) as second:
            assert (second.rep_tcp_port, second.pub_tcp_port) == ports

    def test_explicit_port_overrides_remembered(
        self, mock_advertisement, mock_ports_file
    ):
        """An explicit port argument wins over and updates the remembered ports."""
        uuid = uuid4()
        with ipc.ServiceHost(
            'test', 'test_service', _noop_handler, uuid=uuid, remote=False
        ) as first:
            remembered = first.rep_tcp_port
            # pick a free port while `first` still holds its own
            with socket.socket() as free_socket:
                free_socket.bind(('127.0.0.1', 0))
                free_port = free_socket.getsockname()[1]
        assert free_port != remembered
        with ipc.ServiceHost(
            'test',
            'test_service',
            _noop_handler,
            uuid=uuid,
            port_rep=free_port,
            remote=False,
        ) as second:
            assert second.rep_tcp_port == free_port
        with ipc.ServiceHost(
            'test', 'test_service', _noop_handler, uuid=uuid, remote=False
        ) as third:
            assert third.rep_tcp_port == free_port

    def test_random_uuid_writes_no_state(self, mock_advertisement, mock_ports_file):
        """Hosts without a caller-supplied UUID do not persist ports."""
        with ipc.ServiceHost('test', 'test_service', _noop_handler, remote=False):
            pass
        assert not mock_ports_file.exists()

    def test_preferred_port_fallback_warns(self, mock_advertisement, caplog):
        """An occupied preferred port falls back to a random port with a warning."""
        with ipc.ServiceHost('a', 'test_service', _noop_handler, remote=False) as first:
            occupied = first.rep_tcp_port
            with caplog.at_level(logging.WARNING, logger='bpod_core.ipc'):
                host = ipc.ServiceHost(
                    'b', 'test_service', _noop_handler, port_rep=occupied, remote=False
                )
                host.close()
        assert host.rep_tcp_port != occupied
        assert any('Preferred port' in record.message for record in caplog.records)

    @pytest.mark.parametrize('remote', [True, False], ids=['remote', 'local'])
    def test_bind_address(self, mock_advertisement, mocker, remote):
        """Validate bind address switches between 0.0.0.0 and 127.0.0.1."""
        # skip the IPC binds so LAST_ENDPOINT reflects the TCP bind
        mocker.patch.object(ipc.ServiceHost, '_bind_ipc', return_value=(None, None))
        with ipc.ServiceHost(
            'test', 'service_type', _noop_handler, remote=remote
        ) as host:
            expected_ip = '0.0.0.0' if remote else '127.0.0.1'
            for zmq_socket in (host._rep_socket, host._pub_socket):
                endpoint = zmq_socket.get_string(zmq.LAST_ENDPOINT)
                assert endpoint.startswith(f'tcp://{expected_ip}:'), (
                    f'Socket should be bound to {expected_ip}'
                )

    def test_remote_true_creates_zeroconf_and_local(self, mock_advertisement):
        """remote=True creates both zeroconf and local advertisement."""
        with ipc.ServiceHost(
            'test', 'test_service', _noop_handler, remote=True
        ) as host:
            assert host._zeroconf is not None
            # registration runs on a background thread
            register_service = host._zeroconf.register_service
            assert _wait_until(lambda: register_service.call_count == 1)
            host._zeroconf.close.assert_not_called()
            assert host._local_advertisement is not None
            assert host._local_advertisement.service_file.exists()
        host._zeroconf.close.assert_called_once()
        assert not host._local_advertisement.service_file.exists()
        assert not host._local_advertisement._runtime_directory.exists()

    def test_remote_false_creates_only_local(self, mock_advertisement):
        """remote=False creates only local advertisement, no zeroconf."""
        with ipc.ServiceHost(
            'test', 'test_service', _noop_handler, remote=False
        ) as host:
            assert host._zeroconf is None
            assert host._local_advertisement is not None
            assert host._local_advertisement.service_file.exists()
        assert not host._local_advertisement.service_file.exists()
        assert not host._local_advertisement._runtime_directory.exists()

    @pytest.mark.parametrize(
        'request_frames',
        [[b'Q'], [b'X', b'x'], [b''], [b'R', b'x']],
        ids=['missing_payload', 'unknown_kind', 'empty_frame', 'reply_as_request'],
    )
    def test_malformed_request_gets_error_reply(
        self, mock_advertisement, request_frames
    ):
        """A malformed request yields an ERROR reply and keeps the host serving."""
        with ipc.ServiceHost(
            'test', 'test_service', _noop_handler, remote=False
        ) as host:
            context = zmq.Context()
            try:
                sock = context.socket(zmq.REQ)
                sock.connect(host.rep_tcp_addr)
                sock.send_multipart(request_frames)
                assert sock.poll(2000), 'host did not reply to malformed request'
                frames = sock.recv_multipart()
                assert frames[0] == ipc._MessageKind.ERROR.byte_value
            finally:
                sock.close(linger=0)
                context.term()
            assert host._event_thread.is_alive()

    def test_undecodable_request_gets_error_reply(
        self, mock_advertisement, mock_service_browser
    ):
        """A request failing typed decoding yields RemoteError; host stays usable."""
        with (
            ipc.ServiceHost(
                'test',
                'test_service',
                _noop_handler,
                request_type=_EventA,
                remote=False,
            ) as host,
            ipc.ServiceClient('test_service', host.rep_tcp_addr) as client,
        ):
            with pytest.raises(ipc.RemoteError) as exc_info:
                client.request({'not': 'an event'})
            assert exc_info.value.original_error.name == 'ValidationError'
            assert client.request({'type': '_EventA', 'value': 1}) == {}

    def test_get_metadata(self, mock_advertisement, mock_service_browser):
        """get_metadata() exposes per-request connection metadata to the handler."""

        def handler(_):
            return {
                'hostname': ipc.ServiceHost.get_metadata('X-Hostname'),
                'bogus': ipc.ServiceHost.get_metadata('X-Bogus-Option', 'fallback'),
            }

        with (
            ipc.ServiceHost('test', 'test_service', handler, remote=False) as host,
            ipc.ServiceClient('test_service', host.rep_tcp_addr) as client,
        ):
            reply = client.request(None)
        assert reply['hostname'] == socket.gethostname()
        assert reply['bogus'] == 'fallback'

    def test_get_metadata_outside_request_returns_default(self):
        """get_metadata() returns the default when no request is being handled."""
        assert ipc.ServiceHost.get_metadata('X-Hostname') is None
        assert ipc.ServiceHost.get_metadata('X-Hostname', 'fallback') == 'fallback'

    def test_unsupported_serialization_raises(self, mock_advertisement):
        """An unsupported serialization protocol raises ValueError."""
        with pytest.raises(ValueError, match='serialization'):
            ipc.ServiceHost('test', 'test_service', _noop_handler, serialization='xml')

    def test_close_is_idempotent(self, mock_advertisement):
        """close() can be called multiple times without error."""
        host = ipc.ServiceHost('test', 'test_service', _noop_handler, remote=False)
        host.close()
        host.close()

    def test_close_removes_local_advertisement(self, mock_advertisement):
        """close() removes the local advertisement file."""
        host = ipc.ServiceHost('test', 'test_service', _noop_handler, remote=False)
        service_file = host._local_advertisement.service_file
        assert service_file.exists()
        host.close()
        assert not service_file.exists()


class TestBindIpc:
    """Tests for ServiceHost._bind_ipc."""

    def test_non_posix_returns_none(self, mocker):
        """IPC is skipped entirely on non-POSIX platforms."""
        mocker.patch.object(ipc.os, 'name', 'nt')
        zmq_socket = mocker.Mock()
        assert ipc.ServiceHost._bind_ipc(zmq_socket, 'ID') == (None, None)
        zmq_socket.bind.assert_not_called()

    @pytest.mark.skipif(os.name != 'posix', reason='POSIX only')
    def test_abstract_socket_on_linux(self, mocker, caplog):
        """On linux, sockets bind to abstract IPC addresses without named pipes."""
        mocker.patch.object(ipc.sys, 'platform', 'linux')
        zmq_socket = mocker.Mock()
        zmq_socket.type = zmq.REP
        with caplog.at_level(logging.DEBUG, logger='bpod_core.ipc'):
            address, pipe = ipc.ServiceHost._bind_ipc(zmq_socket, 'ID')
        assert address == 'ipc://@ID'
        assert pipe is None
        zmq_socket.bind.assert_called_once_with('ipc://@ID')
        assert f"Bound REP socket to '{address}'" in caplog.text

    @pytest.mark.skipif(os.name != 'posix', reason='POSIX only')
    def test_named_pipe_on_other_posix(self, mocker, tmp_path, caplog):
        """On non-linux POSIX platforms, sockets bind to filesystem named pipes."""
        mocker.patch.object(ipc.sys, 'platform', 'darwin')
        mocker.patch.object(
            ipc.platformdirs, 'user_runtime_path', return_value=tmp_path
        )
        stale_pipe = tmp_path / 'ID.ipc'
        stale_pipe.touch()  # stale named pipe from a previous run
        zmq_socket = mocker.Mock()
        zmq_socket.type = zmq.REP
        with caplog.at_level(logging.DEBUG, logger='bpod_core.ipc'):
            address, pipe = ipc.ServiceHost._bind_ipc(zmq_socket, 'ID')
        assert address == 'ipc://' + stale_pipe.as_posix()
        assert pipe == stale_pipe
        assert not stale_pipe.exists()  # pre-unlinked to avoid collisions
        zmq_socket.bind.assert_called_once_with(address)
        assert f"Bound REP socket to '{address}'" in caplog.text

    @pytest.mark.skipif(os.name != 'posix', reason='POSIX only')
    def test_bind_failure_falls_back_to_tcp(self, mocker, tmp_path, caplog):
        """A failed bind is unwound, logged, and reported as (None, None)."""
        mocker.patch.object(ipc.sys, 'platform', 'darwin')
        mocker.patch.object(
            ipc.platformdirs, 'user_runtime_path', return_value=tmp_path
        )
        zmq_socket = mocker.Mock()
        zmq_socket.type = zmq.REP
        zmq_socket.bind.side_effect = zmq.ZMQError()
        with caplog.at_level(logging.WARNING, logger='bpod_core.ipc'):
            assert ipc.ServiceHost._bind_ipc(zmq_socket, 'ID') == (None, None)
        zmq_socket.unbind.assert_called_once()
        assert not (tmp_path / 'ID.ipc').exists()
        assert 'Failed to bind REP socket' in caplog.text


class TestConstructorCleanup:
    """Failed construction must release all partially-acquired resources."""

    @pytest.fixture
    def tracked_contexts(self, mocker):
        """Record all ZMQ contexts created during the test."""
        contexts = []
        original_context = zmq.Context

        class TrackingContext(original_context):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                contexts.append(self)

        mocker.patch.object(zmq, 'Context', TrackingContext)
        return contexts

    def test_client_failed_discovery(self, tracked_contexts, mock_local_discovery_dir):
        """A discovery timeout during construction terminates the ZMQ context."""
        with pytest.raises(TimeoutError):
            ipc.ServiceClient('no-such-service', discovery_timeout=0.2, remote=False)
        assert len(tracked_contexts) == 1
        assert tracked_contexts[0].closed

    def test_host_failed_advertisement(self, tracked_contexts, mocker):
        """An advertisement failure stops the event thread and the ZMQ context."""
        mocker.patch.object(
            ipc, 'LocalServiceAdvertisement', side_effect=RuntimeError('boom')
        )
        threads_before = threading.active_count()
        with pytest.raises(RuntimeError, match='boom'):
            ipc.ServiceHost('name', 'type', _noop_handler, remote=False)
        assert len(tracked_contexts) == 1
        assert tracked_contexts[0].closed
        deadline = time.monotonic() + 2.0
        while threading.active_count() > threads_before:
            if time.monotonic() > deadline:
                raise AssertionError('host event thread still running')
            time.sleep(0.01)


class TestLocalDiscovery:
    """Tests for local vs remote discovery behavior."""

    def test_client_discovers_host_locally(self, mock_advertisement):
        """Client discovers host via local advertisement without zeroconf."""
        with (
            ipc.ServiceHost('test', 'service', request_handler=lambda d: {'req': d}),
            ipc.ServiceClient(service_type='service', remote=False) as client,
        ):
            assert client._address_req.startswith(('tcp://', 'ipc://'))
            reply = client.request({'test': 'value'})
            assert reply == {'req': {'test': 'value'}}

    def test_discover_prefers_local_over_zeroconf(
        self, mock_advertisement, mock_service_browser
    ):
        """discover() returns local service without invoking zeroconf."""
        with ipc.LocalServiceAdvertisement(
            service_name='service_name',
            service_type='service_type',
            address='tcp://127.0.0.1:9999',
            pid=os.getpid(),
        ):
            address, _ = ipc.discover('service_type', timeout=0, remote=True)
            assert address == 'tcp://127.0.0.1:9999'

    def test_discover_remote_false_raises_if_no_local(self, mock_advertisement):
        """discover() with remote=False raises if no local service found."""
        with pytest.raises(TimeoutError):
            ipc.discover('nonexistent', timeout=0, remote=False)

    def test_discover_falls_back_to_zeroconf(
        self, mock_advertisement, mock_service_browser
    ):
        """discover() falls back to zeroconf when no local service exists."""
        with pytest.raises(TimeoutError):
            ipc.discover('test_service', timeout=0, poll_interval=0.01, remote=True)
        mock_advertisement['zeroconf'].assert_called_once()

    def test_client_remote_false_uses_only_local(self, mock_advertisement):
        """Client with remote=False only uses local discovery."""
        with (
            ipc.ServiceHost('test', 'localonly', _noop_handler, remote=False),
            ipc.ServiceClient(service_type='localonly', remote=False),
        ):
            mock_advertisement['zeroconf'].assert_not_called()

    def test_client_remote_false_raises_if_no_local(self, mock_advertisement):
        """Client with remote=False raises if no local service found."""
        with pytest.raises(TimeoutError):
            ipc.ServiceClient(
                service_type='nonexistent', discovery_timeout=0, remote=False
            )

    def test_discover_timeout(self, mock_advertisement, mock_service_browser):
        """Timeout when no matching service is discovered within deadline."""
        with pytest.raises(TimeoutError):
            ipc.discover(
                '_svc._tcp.local.', properties=None, timeout=0, poll_interval=0.01
            )


def test_service_iterator_requires_local_or_remote():
    """At least one of local or remote must be enabled."""
    with pytest.raises(ValueError, match='local or remote'):
        ipc.ServiceIterator('service_type', local=False, remote=False)


class TestIterServices:
    """Tests for iter_services()."""

    @pytest.fixture
    def mock_local_advertisement(self, mock_local_discovery_dir):
        with ipc.LocalServiceAdvertisement(
            service_name='service_name',
            service_type='service_type',
            address='tcp://127.0.0.1:5555',
            properties={'a': 'b'},
        ) as advertisement:
            yield advertisement

    @pytest.fixture
    def mock_remote_advertisement(
        self, mocker, mock_local_discovery_dir, mock_service_browser
    ):
        """Simulate a remote service advertised via Zeroconf."""
        mocker.patch('bpod_core.ipc.get_local_ipv4', return_value='192.168.1.50')

        mock_info = mocker.MagicMock(spec=zeroconf.ServiceInfo)
        mock_info.parsed_addresses.return_value = ['192.168.1.100']
        mock_info.port = 5555
        mock_info.decoded_properties = {'a': 'b'}

        mock_zc = mocker.MagicMock(spec=zeroconf.Zeroconf)
        mock_zc.get_service_info.return_value = mock_info
        mocker.patch('bpod_core.ipc.Zeroconf', return_value=mock_zc)

        class RemoteAdvertisement:
            def __init__(self):
                self._zc = None
                self._type = None
                self._name = None
                self._listener = None

            def close(self):
                self._listener.remove_service(self._zc, self._type, self._name)

        advertisement = RemoteAdvertisement()

        def make_browser(zc, type_, listener, *_, **__):
            advertisement._zc = zc
            advertisement._type = type_
            advertisement._name = f'service.{type_}'
            advertisement._listener = listener
            listener.add_service(zc, type_, advertisement._name)
            return mocker.MagicMock(spec=ServiceBrowser)

        mock_service_browser.side_effect = make_browser
        return advertisement

    class MockAdvertisement(NamedTuple):
        fixture: Any
        is_local: bool
        address: str

    @pytest.fixture(
        params=[
            pytest.param(('mock_local_advertisement', True), id='local'),
            pytest.param(('mock_remote_advertisement', False), id='remote'),
        ],
    )
    def mock_advertisement(self, request):
        fixture_name, is_local = request.param
        address = 'tcp://127.0.0.1:5555' if is_local else 'tcp://192.168.1.100:5555'
        fixture = request.getfixturevalue(fixture_name)
        return self.MockAdvertisement(fixture, is_local, address)

    def test_yields_added_event(self, mock_advertisement):
        """`added` event is yielded as a local service appears."""
        iterator = ipc.iter_services('service_type', poll_interval=0.01)
        event = next(iterator)
        assert event.kind == 'added'
        assert event.address == mock_advertisement.address
        assert event.properties == {'a': 'b'}

    def test_yields_removed_event(self, mock_advertisement):
        """`removed` event is yielded as a local service disappears."""
        iterator = ipc.iter_services('service_type', poll_interval=0.01)
        next(iterator)
        mock_advertisement.fixture.close()
        event = next(iterator)
        assert event.kind == 'removed'
        assert event.address == mock_advertisement.address
        assert event.properties == {'a': 'b'}

    def test_exhausted_when_no_services(
        self, mock_local_advertisement, mock_service_browser, mock_zeroconf
    ):
        """timeout=0 with no services yields nothing."""
        iterator = ipc.iter_services('nonexistent', timeout=0, remote=True)
        assert list(iterator) == []

    def test_filters_by_properties(self, mock_local_advertisement):
        """Only services matching the given properties are yielded."""
        with ipc.LocalServiceAdvertisement(
            service_name='another_service_name',
            service_type='service_type',
            address='tcp://127.0.0.1:6666',
            properties={'a': 'b', 'x': 'y'},
        ):
            kwargs = {'timeout': 0, 'local': True, 'remote': True}
            iterator_1 = ipc.iter_services('service_type', {'x': 'y'}, **kwargs)
            iterator_2 = ipc.iter_services('service_type', {'a': 'b'}, **kwargs)
            events_1 = list(iterator_1)
            events_2 = list(iterator_2)
        assert len(events_1) == 1
        assert len(events_2) == 2
        assert events_1[0].address == 'tcp://127.0.0.1:6666'

    def test_remote_false_no_zeroconf(self, mock_zeroconf, mock_local_discovery_dir):
        """remote=False never instantiates Zeroconf."""
        list(ipc.iter_services('nonexistent', timeout=0, remote=False))
        mock_zeroconf.assert_not_called()

    def test_close_ends_blocking_iteration(
        self, mock_zeroconf, mock_local_discovery_dir
    ):
        """close() unblocks a consumer waiting in __next__ (timeout=None)."""
        iterator = ipc.iter_services('nonexistent', timeout=None, remote=False)
        received = []
        consumer = threading.Thread(target=lambda: received.extend(iterator))
        consumer.start()
        time.sleep(0.05)  # let the consumer block in __next__
        iterator.close()
        consumer.join(timeout=2)
        assert not consumer.is_alive(), 'close() should end a blocked iteration'
        assert received == []
        # the iterator stays exhausted on subsequent calls
        with pytest.raises(StopIteration):
            next(iterator)

    @pytest.mark.parametrize(
        ('kinds', 'expected_len', 'expected_event'),
        [
            pytest.param('+', 1, ('added', 0), id='add'),
            pytest.param('-', 0, None, id='remove'),
            pytest.param('+-', 0, None, id='add, remove'),
            pytest.param('+-+', 1, ('added', 2), id='add, remove, add'),
            pytest.param('+-+-', 0, None, id='add, remove, add, remove'),
            pytest.param('+Y-', 1, ('removed', 1), id='add, yield, remove'),
        ],
    )
    def test_collapse(
        self, kinds, expected_len, expected_event, mock_local_discovery_dir
    ):
        """test collapsing various combinations of added/removed events."""
        iterator = ipc.ServiceIterator(
            service_type='nonexistent',
            timeout=0,
            poll_interval=0.01,
            local=True,
            remote=False,
        )
        i = 0
        for k in kinds:
            if k == 'Y':
                next(iterator)
                continue
            kind = 'added' if k == '+' else 'removed'
            iterator._q.put(ipc.ServiceEvent(kind, 'tcp://127.0.0.1:0', {'v': str(i)}))
            i += 1
        events = list(iterator)
        assert len(events) == expected_len, 'incorrect number of events'
        if events:
            assert events[0].kind == expected_event[0], '1st event has wrong kind'
            assert events[0].properties == {'v': str(expected_event[1])}
        else:
            assert expected_event is None
