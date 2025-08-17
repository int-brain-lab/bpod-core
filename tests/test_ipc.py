from unittest.mock import patch

import pytest

from bpod_core import ipc


@pytest.fixture
def host():
    with ipc.DualChannelHost(
        name='TestService',
        service_type='dualtest',
        event_handler=lambda data: {'echo': data},
        remote=False,
    ) as h:
        yield h


@pytest.fixture
def client(host):
    with ipc.DualChannelClient(
        service_type='dualtest',
        address=host.rep_tcp_addr,
    ) as c:
        yield c


def test_handshake(client):
    assert client.req_address.startswith(('tcp://', 'ipc://'))
    assert client.sub_address.startswith(('tcp://', 'ipc://'))


def test_request_response(client):
    reply = client.request(foo='bar')
    assert reply == {'echo': {'foo': 'bar'}}


def test_error_response(host, client, caplog):
    def bad_handler(_):
        raise RuntimeError('boom')

    host._user_event_handler = bad_handler
    with caplog.at_level('ERROR'):
        reply = client.request(foo='bar')
    assert reply == {}
    error_logs = [rec for rec in caplog.records if rec.levelname == 'ERROR']
    assert any(
        'RuntimeError' in rec.message and 'boom' in rec.message for rec in error_logs
    )


@pytest.fixture
def mock_service():
    with (
        patch('bpod_core.ipc.Zeroconf'),
        ipc.DualChannelHost('test', 'testservice') as service,
    ):
        yield service


def test_basic_init_and_properties(mock_service):
    assert mock_service.rep_tcp_port > 0
    assert mock_service.rep_tcp_addr.startswith('tcp://')
    assert mock_service._zeroconf is not None
    assert mock_service._service_info is not None


@pytest.mark.parametrize('remote', [True, False])
@patch('bpod_core.ipc.Zeroconf')
def test_bind_address_matches_local_flag(_, remote):
    service = ipc.DualChannelHost(
        'test',
        'testservice',
        remote=remote,
    )
    ip = service.bind_ip
    expected_ip = '0.0.0.0' if remote else '127.0.0.1'
    assert ip == expected_ip
