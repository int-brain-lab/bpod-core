import json
import os

import pytest

from bpod_core import ipc


@pytest.fixture
def mock_zeroconf(mocker):
    return mocker.patch('bpod_core.ipc.Zeroconf')


class TestLocalServiceAdvertisement:
    """Tests for LocalServiceAdvertisement class."""

    @pytest.fixture
    def temp_runtime_dir(self, tmp_path, mocker):
        """Override runtime directory to use a temp path."""
        mocker.patch.object(
            ipc.LocalServiceAdvertisement, 'runtime_directory', tmp_path
        )
        return tmp_path

    def test_creates_service_file(self, temp_runtime_dir):
        """Advertisement creates a JSON file in the runtime directory."""
        ad = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        )
        assert ad.service_file.exists()
        assert ad.service_file.suffix == '.json'

    def test_file_contains_correct_data(self, temp_runtime_dir):
        """Service file contains correct JSON data."""
        ad = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=12345,
            properties={'key': 'value'},
        )
        data = json.loads(ad.service_file.read_text())
        assert data['service_type'] == 'test_service'
        assert data['address'] == 'tcp://127.0.0.1:5555'
        assert data['pid'] == 12345
        assert data['properties'] == {'key': 'value'}
        assert 'uuid' in data

    def test_stop_removes_file(self, temp_runtime_dir):
        """stop() removes the service file."""
        ad = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        )
        service_file = ad.service_file
        assert service_file.exists()

        ad.stop()

        assert not service_file.exists()

    def test_stop_cleans_up_empty_directories(self, temp_runtime_dir):
        """stop() removes empty parent directories."""
        ad = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        )
        service_dir = ad.service_file.parent
        assert service_dir.exists()

        ad.stop()

        assert not service_dir.exists()

    def test_discover_finds_service(self, temp_runtime_dir):
        """discover() yields advertised services."""
        ad = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
            properties={'name': 'test'},
        )

        results = list(ipc.LocalServiceAdvertisement.discover('test_service'))

        assert len(results) == 1
        assert results[0].address == 'tcp://127.0.0.1:5555'
        assert results[0].properties == {'name': 'test'}

        ad.stop()

    def test_discover_filters_by_properties(self, temp_runtime_dir):
        """discover() filters services by matching properties."""
        ad1 = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
            properties={'name': 'first'},
        )
        ad2 = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5556',
            pid=os.getpid(),
            properties={'name': 'second'},
        )

        results = list(
            ipc.LocalServiceAdvertisement.discover(
                'test_service', properties={'name': 'second'}
            )
        )

        assert len(results) == 1
        assert results[0].address == 'tcp://127.0.0.1:5556'

        ad1.stop()
        ad2.stop()

    def test_discover_removes_stale_advertisements(self, temp_runtime_dir, mocker):
        """discover() removes files for dead processes."""
        ad = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=99999999,  # non-existent PID
        )
        service_file = ad.service_file
        assert service_file.exists()

        # Mock pid_exists to return False
        mocker.patch('bpod_core.ipc.pid_exists', return_value=False)

        results = list(ipc.LocalServiceAdvertisement.discover('test_service'))

        assert len(results) == 0
        assert not service_file.exists()

    def test_discover_returns_empty_for_nonexistent_service(self, temp_runtime_dir):
        """discover() returns empty iterator for unknown service types."""
        results = list(ipc.LocalServiceAdvertisement.discover('nonexistent'))
        assert results == []

    def test_service_type_sanitization(self, temp_runtime_dir):
        """Service types with special characters are sanitized."""
        ad = ipc.LocalServiceAdvertisement(
            service_type='test-service.local',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        )

        # Directory name should be sanitized
        assert '_' in ad.service_file.parent.name

        ad.stop()

    def test_multiple_services_same_type(self, temp_runtime_dir):
        """Multiple services of the same type can be advertised."""
        ad1 = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        )
        ad2 = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5556',
            pid=os.getpid(),
        )

        results = list(ipc.LocalServiceAdvertisement.discover('test_service'))
        assert len(results) == 2

        ad1.stop()
        ad2.stop()

    def test_handles_corrupted_file(self, temp_runtime_dir):
        """discover() skips corrupted JSON files."""
        # Create a valid advertisement first to get the directory
        ad = ipc.LocalServiceAdvertisement(
            service_type='test_service',
            address='tcp://127.0.0.1:5555',
            pid=os.getpid(),
        )
        service_dir = ad.service_file.parent

        # Create a corrupted file
        corrupted_file = service_dir / 'corrupted.json'
        corrupted_file.write_text('not valid json')

        results = list(ipc.LocalServiceAdvertisement.discover('test_service'))

        # Should still find the valid service
        assert len(results) == 1
        assert results[0].address == 'tcp://127.0.0.1:5555'

        ad.stop()


@pytest.fixture
def mock_service_browser(mocker):
    return mocker.patch('bpod_core.ipc.ServiceBrowser')


class TestClient:
    """Tests for the DualChannelClient class."""

    @pytest.fixture
    def host(self, mock_zeroconf):
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
        # client should downgrade serialization to host's json
        assert client._serialization == 'json'

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

    @pytest.fixture
    def mock_service(self, mock_zeroconf):
        with ipc.DualChannelHost('test', 'testservice') as service:
            yield service

    def test_basic_init_and_properties(self, mock_service):
        """Check ports, addresses, and Zeroconf objects are initialized."""
        assert mock_service.rep_tcp_port > 0
        assert mock_service.rep_tcp_addr.startswith('tcp://')
        assert mock_service._zeroconf is not None
        assert mock_service._zeroconf_service_info is not None

    @pytest.mark.parametrize('remote', [True, False])
    def test_bind_address_matches_local_flag(self, mock_zeroconf, remote):
        """Validate bind address switches between 0.0.0.0 and 127.0.0.1."""
        service = ipc.DualChannelHost('test', 'testservice', remote=remote)
        ip = service._bind_ip
        expected_ip = '0.0.0.0' if remote else '127.0.0.1'
        assert ip == expected_ip


class TestDiscover:
    """Tests for the discover function."""

    def test_discover_timeout(self, mocker, mock_zeroconf, mock_service_browser):
        """Timeout when no matching service is discovered within deadline."""
        mocker.patch('threading.Event.wait', return_value=False)
        with pytest.raises(TimeoutError):
            ipc.discover('_svc._tcp.local.', properties=None, timeout=0)
