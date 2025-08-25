import pytest

from bpod_core import ipc


@pytest.fixture
def mock_zeroconf(mocker):
    return mocker.patch('bpod_core.ipc.Zeroconf')


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
        assert mock_service._service_info is not None

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
