import errno
import socket
from unittest.mock import MagicMock, call, patch

import numpy as np
import pytest
from zeroconf import NonUniqueNameException

from bpod_core import com


@pytest.fixture
def mock_serial():
    """Fixture to mock serial communication."""
    mock_serial = com.ExtendedSerial()
    patched_object_base = 'bpod_core.com.Serial'
    with (
        patch(f'{patched_object_base}.write') as mock_write,
        patch(f'{patched_object_base}.read') as mock_read,
    ):
        mock_serial.super_write = mock_write
        mock_serial.super_read = mock_read
        yield mock_serial


class TestEnhancedSerial:
    def test_write(self, mock_serial):
        mock_serial.write(b'x')
        mock_serial.super_write.assert_called_with(b'x')

    def test_write_struct(self, mock_serial):
        mock_serial.write_struct('<BHI', 1, 2, 3)
        mock_serial.super_write.assert_called_with(b'\x01\x02\x00\x03\x00\x00\x00')

    def test_read_struct(self, mock_serial):
        mock_serial.super_read.return_value = b'\x01\x02\x00\x03\x00\x00\x00'
        a, b, c = mock_serial.read_struct('<BHI')
        assert a == 1
        assert b == 2
        assert c == 3

    def test_query(self, mock_serial):
        mock_serial.query(b'x', size=4)
        mock_serial.super_write.assert_called_with(b'x')
        mock_serial.super_read.assert_called_with(4)

    def test_query_struct(self, mock_serial):
        mock_serial.super_read.return_value = b'\x01\x02\x00\x03\x00\x00\x00'
        a, b, c = mock_serial.query_struct(b'x', '<BHI')
        assert a == 1
        assert b == 2
        assert c == 3

    def test_verify(self, mock_serial):
        mock_serial.super_read.return_value = b'\x01\x02\x00\x03\x00\x00\x00'
        result = mock_serial.verify(b'x', b'\x01\x02\x00\x03\x00\x00\x00')
        assert result is True
        result = mock_serial.verify(b'x', b'\x01')
        assert result is False


class TestChunkedSerialReader:
    def test_initial_buffer_size(self):
        reader = com.ChunkedSerialReader(chunk_size=2)
        assert len(reader) == 0

    def test_put_data(self):
        reader = com.ChunkedSerialReader(chunk_size=2)
        reader.put(b'\x01\x02\x03\x04')
        assert len(reader) == 4

    def test_get_data(self):
        reader = com.ChunkedSerialReader(chunk_size=2)
        reader.put(b'\x01\x02\x03\x04')
        data = reader.get(4)
        assert data == bytearray(b'\x01\x02\x03\x04')
        assert len(reader) == 0

    def test_get_partial_data(self):
        reader = com.ChunkedSerialReader(chunk_size=2)
        reader.put(b'\x01\x02\x03\x04')
        data = reader.get(2)
        assert data == bytearray(b'\x01\x02')
        assert len(reader) == 2

    def test_data_received(self):
        reader = com.ChunkedSerialReader(chunk_size=4)
        reader.process = MagicMock()
        reader.data_received(b'\x01\x00\x00\x00\x02\x00\x00\x00')
        assert len(reader) == 0
        reader.process.assert_has_calls(
            [
                call(b'\x01\x00\x00\x00'),
                call(b'\x02\x00\x00\x00'),
            ],
        )

    def test_multiple_data_received(self):
        reader = com.ChunkedSerialReader(chunk_size=4)
        reader.process = MagicMock()
        reader.data_received(b'\x01\x00')
        assert len(reader) == 2
        reader.data_received(b'\x00\x00')
        assert len(reader) == 0
        reader.data_received(b'\x02\x00')
        assert len(reader) == 2
        reader.data_received(b'\x00\x00')
        assert len(reader) == 0
        reader.process.assert_has_calls(
            [
                call(b'\x01\x00\x00\x00'),
                call(b'\x02\x00\x00\x00'),
            ],
        )


class TestToBytes:
    def test_to_bytes_with_bytes(self):
        assert com.to_bytes(b'test') == b'test'

    def test_to_bytes_with_bytearray(self):
        assert com.to_bytes(bytearray([1, 2, 3])) == b'\x01\x02\x03'

    def test_to_bytes_with_memoryview(self):
        data = bytearray([1, 2, 3])
        assert com.to_bytes(memoryview(data)) == b'\x01\x02\x03'

    def test_to_bytes_with_int(self):
        assert com.to_bytes(255) == b'\xff'
        with pytest.raises(ValueError, match='bytes must be in range'):
            com.to_bytes(256)

    def test_to_bytes_with_numpy_array(self):
        array = np.array([1, 2, 3], dtype=np.uint8)
        assert com.to_bytes(array) == b'\x01\x02\x03'

    def test_to_bytes_with_numpy_scalar(self):
        scalar = np.uint8(42)
        assert com.to_bytes(scalar) == b'*'

    def test_to_bytes_with_string(self):
        assert com.to_bytes('test') == b'test'

    def test_to_bytes_with_list(self):
        assert com.to_bytes([1, 2, 3]) == b'\x01\x02\x03'
        with pytest.raises(ValueError, match='bytes must be in range'):
            com.to_bytes([1, 2, 256])

    def test_to_bytes_with_float(self):
        with pytest.raises(TypeError):
            com.to_bytes(42.0)


class TestGetLocalIPv4:
    def test_returns_valid_ipv4(self):
        ip = com.get_local_ipv4()
        parts = ip.split('.')
        assert len(parts) == 4
        assert all(0 <= int(p) < 256 for p in parts)

    def test_fallback_to_loopback_on_unreachable(self, monkeypatch):
        class DummySocket:
            def connect(self, addr):
                raise OSError(errno.ENETUNREACH, 'Network unreachable')

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr(socket, 'socket', lambda *a, **k: DummySocket())
        ip = com.get_local_ipv4()
        assert ip == '127.0.0.1'


class TestZMQServiceBasic:
    def test_basic_init_and_properties(self):
        service = com.ZMQService('testservice', 'desc', advertise=False)
        assert service.port > 0
        assert service.bind_address.startswith('tcp://')
        assert service._zeroconf is None
        assert service._service_info is None
        service.close()

    def test_close_idempotent(self):
        service = com.ZMQService('testservice', 'desc', advertise=False)
        service.close()
        # Calling close again should do nothing / not error
        service.close()

    def test_context_manager_closes(self):
        with com.ZMQService('testservice', 'desc', advertise=False) as service:
            assert not service._closed
        assert service._closed

    @pytest.mark.parametrize('local', [True, False])
    def test_bind_address_matches_local_flag(self, local):
        service = com.ZMQService('testservice', 'desc', local=local, advertise=False)
        ip = service.bind_address.split('://')[1]
        expected_ip = '127.0.0.1' if local else com.get_local_ipv4()
        assert ip == expected_ip
        service.close()


class TestZMQServiceAdvertise:
    def test_register_service_with_name_conflict_retry(self, monkeypatch):
        call_count = {'count': 0}

        def fake_register(service_info):
            call_count['count'] += 1
            if call_count['count'] == 1:
                raise NonUniqueNameException()

        # Patch Zeroconf and ServiceInfo
        monkeypatch.setattr('bpod_core.com.Zeroconf', MagicMock())
        monkeypatch.setattr('bpod_core.com.ServiceInfo', MagicMock())
        zeroconf_instance = com.Zeroconf.return_value
        zeroconf_instance.register_service.side_effect = fake_register
        com.ServiceInfo.side_effect = lambda **kwargs: MagicMock(
            name=kwargs.get('name')
        )

        service = com.ZMQService('testservice', 'desc')
        assert call_count['count'] == 2
        service.close()
