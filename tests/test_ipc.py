import json
import os
from typing import Any, NamedTuple
from uuid import uuid4

import pytest
import zeroconf
from zeroconf import ServiceBrowser

from bpod_core import ipc


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


class TestClient:
    """Tests for the ServiceClient class."""

    @pytest.fixture
    def host(self, mock_advertisement):
        with ipc.ServiceHost(
            service_name='TestService',
            service_type='dualtest',
            event_handler=lambda data: {'echo': data},
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
            default_data_type=dict,
        ) as client:
            yield client

    def test_handshake(self, client):
        """Verify handshake exchanges addresses and negotiates serialization."""
        assert client._address_req.startswith(('tcp://', 'ipc://'))
        assert client._address_sub.startswith(('tcp://', 'ipc://'))
        assert client._serialization == 'json', 'Serialization should be JSON'

    def test_request_response(self, client):
        """Round-trip a request to the host and validate payload."""
        reply = client.request({'foo': 'bar'})
        assert reply == {'echo': {'foo': 'bar'}}

    def test_error_response(self, mock_advertisement, mock_service_browser):
        """Verify server exceptions are logged and client gets empty dict."""

        def bad_handler(_):
            raise RuntimeError('boom')

        with (
            ipc.ServiceHost('Test', 'service', event_handler=bad_handler) as host,
            ipc.ServiceClient('service', host.rep_tcp_addr) as client,
            pytest.raises(ipc.RemoteError, match='boom'),
        ):
            client.request({'foo': 'bar'})


class TestHost:
    """Tests for the ServiceHost class."""

    def test_basic_init_and_properties(self, mock_advertisement):
        """Check ports, addresses, and Zeroconf objects are initialized."""
        with ipc.ServiceHost('test', 'test_service') as host:
            assert host.rep_tcp_addr.startswith('tcp://')
            assert host._zeroconf is not None
            assert host._zeroconf_service_info is not None

    @pytest.mark.parametrize('remote', [True, False], ids=['remote', 'local'])
    def test_bind_address(self, mock_advertisement, remote):
        """Validate bind address switches between 0.0.0.0 and 127.0.0.1."""
        with ipc.ServiceHost('test', 'service_type', remote=remote) as host:
            expected_ip = '0.0.0.0' if remote else '127.0.0.1'
            assert host._bind_ip == expected_ip, f'Bind IP should be {expected_ip}'

    def test_remote_true_creates_zeroconf_and_local(self, mock_advertisement):
        """remote=True creates both zeroconf and local advertisement."""
        with ipc.ServiceHost('test', 'test_service', remote=True) as host:
            assert host._zeroconf is not None
            assert host._zeroconf_service_info is not None
            host._zeroconf.register_service.assert_called_once()
            host._zeroconf.close.assert_not_called()
            assert host._local_advertisement is not None
            assert host._local_advertisement.service_file.exists()
        host._zeroconf.close.assert_called_once()
        assert not host._local_advertisement.service_file.exists()
        assert not host._local_advertisement._runtime_directory.exists()

    def test_remote_false_creates_only_local(self, mock_advertisement):
        """remote=False creates only local advertisement, no zeroconf."""
        with ipc.ServiceHost('test', 'test_service', remote=False) as host:
            assert host._zeroconf is None
            assert host._zeroconf_service_info is None
            assert host._local_advertisement is not None
            assert host._local_advertisement.service_file.exists()
        assert not host._local_advertisement.service_file.exists()
        assert not host._local_advertisement._runtime_directory.exists()

    def test_close_removes_local_advertisement(self, mock_advertisement):
        """close() removes the local advertisement file."""
        host = ipc.ServiceHost('test', 'test_service', remote=False)
        service_file = host._local_advertisement.service_file
        assert service_file.exists()
        host.close()
        assert not service_file.exists()


class TestLocalDiscovery:
    """Tests for local vs remote discovery behavior."""

    def test_client_discovers_host_locally(self, mock_advertisement):
        """Client discovers host via local advertisement without zeroconf."""
        with (
            ipc.ServiceHost('test', 'service', event_handler=lambda d: {'req': d}),
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
            ipc.ServiceHost('test', 'localonly', remote=False),
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
