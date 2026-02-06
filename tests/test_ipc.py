import json
import os
from uuid import uuid4

import pytest

from bpod_core import ipc


@pytest.fixture
def mock_zeroconf(mocker):
    """Mock Zeroconf class."""
    return mocker.patch('bpod_core.ipc.Zeroconf', spec=ipc.Zeroconf)


@pytest.fixture
def mock_runtime_dir(tmp_path, mocker):
    """Mock runtime directory for advertisements."""
    mocker.patch.object(ipc.LocalServiceAdvertisement, 'runtime_directory', tmp_path)
    return tmp_path


@pytest.fixture
def mock_advertisement(mock_zeroconf, mock_runtime_dir):
    """Mock, both, zeroconf and local advertisement."""
    yield {'zeroconf': mock_zeroconf, 'runtime_dir': mock_runtime_dir}


class TestLocalServiceAdvertisement:
    """Tests for LocalServiceAdvertisement class."""

    def test_context_manager(self, mock_runtime_dir):
        """Advertisement can be used as a context manager."""
        with ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        ) as ad:
            assert ad.service_file.is_relative_to(mock_runtime_dir)
            assert ad.service_file.exists()
            assert ad.service_file.suffix == '.json'
        assert not ad.service_file.exists()
        assert not mock_runtime_dir.exists()

    def test_close_removes_file(self, mock_runtime_dir):
        """close() removes the service file and removes empty parent directories."""
        ad = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        )
        assert ad.service_file.exists()
        ad.close()
        assert not ad.service_file.exists()
        assert not mock_runtime_dir.exists()

    def test_file_contains_correct_data(self, mock_runtime_dir):
        """Service file contains correct JSON data."""
        uuid = uuid4()
        with ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=12345,
            uuid=uuid,
            properties={'key': 'value'},
        ) as ad:
            data = json.loads(ad.service_file.read_text())
            assert data['service_type'] == 'test_service'
            assert data['address'] == 'tcp://127.0.0.1:5555'
            assert data['pid'] == 12345
            assert data['uuid'] == str(uuid)
            assert data['properties'] == {'key': 'value'}

    def test_discover_finds_service(self, mock_runtime_dir):
        """discover() yields advertised services."""
        with ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
            properties={'name': 'test'},
        ):
            results = list(ipc.LocalServiceAdvertisement.discover('test_service'))
            assert len(results) == 1
            assert results[0].address == 'tcp://127.0.0.1:5555'
            assert results[0].properties == {'name': 'test'}

    def test_discover_filters_by_properties(self, mock_runtime_dir):
        """discover() filters services by matching properties."""
        with (
            ipc.LocalServiceAdvertisement(
                service_type='test_service',
                address='tcp://127.0.0.1:5555',
                pid=os.getpid(),
                properties={'name': 'first'},
            ),
            ipc.LocalServiceAdvertisement(
                service_type='test_service',
                address='tcp://127.0.0.1:5556',
                pid=os.getpid(),
                properties={'name': 'second'},
            ),
        ):
            results = list(ipc.LocalServiceAdvertisement.discover('test_service'))
            assert len(results) == 2
            results = list(
                ipc.LocalServiceAdvertisement.discover(
                    'test_service', properties={'name': 'second'}
                )
            )
            assert len(results) == 1
            assert results[0].address == 'tcp://127.0.0.1:5556'

    def test_discover_removes_stale_advertisements(self, mock_runtime_dir, mocker):
        """discover() removes files for dead processes."""
        with ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=99999999,  # non-existent PID
        ) as ad:
            assert ad.service_file.exists()
            mocker.patch('bpod_core.ipc.pid_exists', return_value=False)
            results = list(ipc.LocalServiceAdvertisement.discover('test_service'))
            assert len(results) == 0
            assert not ad.service_file.exists()
            assert not mock_runtime_dir.exists()

    def test_discover_returns_empty_for_nonexistent_service(self, mock_runtime_dir):
        """discover() returns empty iterator for unknown service types."""
        assert list(ipc.LocalServiceAdvertisement.discover('nonexistent')) == []

    def test_service_type_sanitization(self, mock_runtime_dir):
        """Service types with special characters are sanitized."""
        with ipc.LocalServiceAdvertisement(
            service_type='test-service.local',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        ) as ad:
            assert '-' not in ad.service_file.parent.name

    def test_handles_corrupted_file(self, mock_runtime_dir):
        """discover() skips corrupted JSON files."""
        # Create a valid advertisement first to get the directory
        with ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        ) as ad:
            service_dir = ad.service_file.parent
            corrupted_file = service_dir / 'corrupted.json'
            corrupted_file.write_text('not valid json')
            results = list(ipc.LocalServiceAdvertisement.discover('test_service'))
            assert len(results) == 1
            assert results[0].address == 'tcp://127.0.0.1:5555'


@pytest.fixture
def mock_service_browser(mocker):
    return mocker.patch('bpod_core.ipc.ServiceBrowser')


class TestClient:
    """Tests for the DualChannelClient class."""

    @pytest.fixture
    def host(self, mock_advertisement):
        with ipc.DualChannelHost(
            service_name='TestService',
            service_type='dualtest',
            event_handler=lambda data: {'echo': data},
            remote=False,
            serialization='json',
        ) as host:
            yield host

    @pytest.fixture
    def client(self, host, mock_service_browser):
        with ipc.DualChannelClient(
            service_type='dualtest', address=host.rep_tcp_addr, discovery_timeout=0
        ) as client:
            yield client

    def test_handshake(self, client):
        """Verify handshake exchanges addresses and negotiates serialization."""
        assert client._address_req.startswith(('tcp://', 'ipc://'))
        assert client._address_sub.startswith(('tcp://', 'ipc://'))
        assert client._serialization == 'json', 'Serialization should be JSON'

    def test_request_response(self, client):
        """Round-trip a request to the host and validate payload."""
        reply = client.request(foo='bar')
        assert reply == {'echo': {'foo': 'bar'}}

    def test_unknown_request_type(self, client, caplog):
        """Ensure unknown request type is logged as an error by the host."""
        with caplog.at_level('ERROR'):
            client._req(request_type='invalid')

    def test_error_response(self, host, client, caplog):
        """Verify server exceptions are logged and client gets empty dict."""

        def bad_handler(_):
            raise RuntimeError('boom')

        host._user_event_handler = bad_handler
        with caplog.at_level('ERROR'):
            reply = client.request(foo='bar')
        assert reply == {}
        error_logs = [rec for rec in caplog.records if rec.levelname == 'ERROR']
        assert any(
            'RuntimeError' in rec.message and 'boom' in rec.message
            for rec in error_logs
        )


class TestHost:
    """Tests for the DualChannelHost class."""

    def test_basic_init_and_properties(self, mock_advertisement):
        """Check ports, addresses, and Zeroconf objects are initialized."""
        with ipc.DualChannelHost('test', 'test_service') as host:
            assert host.rep_tcp_port > 0
            assert host.rep_tcp_addr.startswith('tcp://')
            assert host._zeroconf is not None
            assert host._zeroconf_service_info is not None

    @pytest.mark.parametrize('remote', [True, False], ids=['remote', 'local'])
    def test_bind_address(self, mock_advertisement, remote):
        """Validate bind address switches between 0.0.0.0 and 127.0.0.1."""
        with ipc.DualChannelHost('test', 'test_service', remote=remote) as host:
            expected_ip = '0.0.0.0' if remote else '127.0.0.1'
            assert host._bind_ip == expected_ip, f'Bind IP should be {expected_ip}'

    def test_remote_true_creates_zeroconf_and_local(self, mock_advertisement):
        """remote=True creates both zeroconf and local advertisement."""
        with ipc.DualChannelHost('test', 'test_service', remote=True) as host:
            assert host._zeroconf is not None
            assert host._zeroconf_service_info is not None
            host._zeroconf.register_service.assert_called_once()
            host._zeroconf.close.assert_not_called()
            assert host._local_advertisement is not None
            assert host._local_advertisement.service_file.exists()
        host._zeroconf.close.assert_called_once()
        assert not host._local_advertisement.service_file.exists()
        assert not host._local_advertisement.runtime_directory.exists()

    def test_remote_false_creates_only_local(self, mock_advertisement):
        """remote=False creates only local advertisement, no zeroconf."""
        with ipc.DualChannelHost('test', 'test_service', remote=False) as host:
            assert host._zeroconf is None
            assert host._zeroconf_service_info is None
            assert host._local_advertisement is not None
            assert host._local_advertisement.service_file.exists()
        assert not host._local_advertisement.service_file.exists()
        assert not host._local_advertisement.runtime_directory.exists()

    def test_close_removes_local_advertisement(self, mock_advertisement):
        """close() removes the local advertisement file."""
        host = ipc.DualChannelHost('test', 'test_service', remote=False)
        service_file = host._local_advertisement.service_file
        assert service_file.exists()
        host.close()
        assert not service_file.exists()


class TestLocalDiscovery:
    """Tests for local vs remote discovery behavior."""

    def test_client_discovers_host_locally(self, mock_advertisement):
        """Client discovers host via local advertisement without zeroconf."""
        with (
            ipc.DualChannelHost('test', 'localtest', remote=False) as host,
            ipc.DualChannelClient(service_type='localtest', remote=False) as client,
        ):
            assert client._address_req.startswith(('tcp://', 'ipc://'))
            host._user_event_handler = lambda d: {'received': d}
            reply = client.request(test='value')
            assert reply == {'received': {'test': 'value'}}

    def test_discover_prefers_local_over_zeroconf(
        self, mock_advertisement, mock_service_browser
    ):
        """discover() returns local service without invoking zeroconf."""
        with ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:9999',
            pid=os.getpid(),
        ):
            address, properties = ipc.discover('test_service', remote=True, timeout=0)
            assert address == 'tcp://127.0.0.1:9999'
            mock_advertisement['zeroconf'].assert_not_called()

    def test_discover_remote_false_raises_if_no_local(self, mock_advertisement):
        """discover() with remote=False raises if no local service found."""
        with pytest.raises(RuntimeError, match='No matching service found locally'):
            ipc.discover('nonexistent', remote=False, timeout=0)

    def test_discover_falls_back_to_zeroconf(
        self, mocker, mock_advertisement, mock_service_browser
    ):
        """discover() falls back to zeroconf when no local service exists."""
        mocker.patch('threading.Event.wait', return_value=False)
        with pytest.raises(TimeoutError):
            ipc.discover('test_service', remote=True, timeout=0)
        mock_advertisement['zeroconf'].assert_called_once()

    def test_client_remote_false_uses_only_local(self, mock_advertisement):
        """Client with remote=False only uses local discovery."""
        with (
            ipc.DualChannelHost('test', 'localonly', remote=False),
            ipc.DualChannelClient(service_type='localonly', remote=False),
        ):
            mock_advertisement['zeroconf'].assert_not_called()

    def test_client_remote_false_raises_if_no_local(self, mock_advertisement):
        """Client with remote=False raises if no local service found."""
        with pytest.raises(RuntimeError, match='No matching service found locally'):
            ipc.DualChannelClient(service_type='nonexistent', remote=False)

    def test_discover_timeout(self, mocker, mock_advertisement, mock_service_browser):
        """Timeout when no matching service is discovered within deadline."""
        mocker.patch('threading.Event.wait', return_value=False)
        with pytest.raises(TimeoutError):
            ipc.discover('_svc._tcp.local.', properties=None, timeout=0)
