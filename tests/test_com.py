from unittest.mock import MagicMock, call, patch

import numpy as np
import pytest

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
        reader = com.ChunkedSerialReader()
        assert len(reader) == 0

    def test_put_data(self):
        reader = com.ChunkedSerialReader()
        reader.put(b'\x01\x02\x03\x04')
        assert len(reader) == 4

    def test_get_data(self):
        reader = com.ChunkedSerialReader()
        reader.put(b'\x01\x02\x03\x04')
        data = reader.get(4)
        assert data == bytearray(b'\x01\x02\x03\x04')
        assert len(reader) == 0

    def test_get_partial_data(self):
        reader = com.ChunkedSerialReader()
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
            ]
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
            ]
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
        with pytest.raises(ValueError):
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
        with pytest.raises(ValueError):
            com.to_bytes([1, 2, 256])

    def test_to_bytes_with_float(self):
        with pytest.raises(TypeError):
            com.to_bytes(42.0)
