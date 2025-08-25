from unittest.mock import MagicMock, call

import numpy as np
import pytest

from bpod_core import com


@pytest.fixture
def mock_serial(mocker):
    """Fixture to mock serial communication."""
    mock_serial = com.ExtendedSerial()
    patched_object_base = 'bpod_core.com.Serial'
    mock_serial.super_write = mocker.patch(f'{patched_object_base}.write')
    mock_serial.super_read = mocker.patch(f'{patched_object_base}.read')
    return mock_serial


class TestEnhancedSerial:
    """Tests for ExtendedSerial helpers and semantics."""

    def test_write(self, mock_serial):
        """Send bytes and ensure underlying serial.write is called."""
        mock_serial.write(b'x')
        mock_serial.super_write.assert_called_with(b'x')

    def test_write_struct(self, mock_serial):
        """Pack values with struct format and write exact bytes."""
        mock_serial.write_struct('<BHI', 1, 2, 3)
        mock_serial.super_write.assert_called_with(b'\x01\x02\x00\x03\x00\x00\x00')

    def test_read_struct(self, mock_serial):
        """Read bytes and unpack into expected integer tuple."""
        mock_serial.super_read.return_value = b'\x01\x02\x00\x03\x00\x00\x00'
        a, b, c = mock_serial.read_struct('<BHI')
        assert a == 1
        assert b == 2
        assert c == 3

    def test_query(self, mock_serial):
        """Write request then read exact number of bytes for reply."""
        mock_serial.query(b'x', size=4)
        mock_serial.super_write.assert_called_with(b'x')
        mock_serial.super_read.assert_called_with(4)

    def test_query_struct(self, mock_serial):
        """Send request and unpack structured reply into integers."""
        mock_serial.super_read.return_value = b'\x01\x02\x00\x03\x00\x00\x00'
        a, b, c = mock_serial.query_struct(b'x', '<BHI')
        assert a == 1
        assert b == 2
        assert c == 3

    def test_verify(self, mock_serial):
        """Verify compares read bytes against expected pattern."""
        mock_serial.super_read.return_value = b'\x01\x02\x00\x03\x00\x00\x00'
        result = mock_serial.verify(b'x', b'\x01\x02\x00\x03\x00\x00\x00')
        assert result is True
        result = mock_serial.verify(b'x', b'\x01')
        assert result is False


class TestChunkedSerialReader:
    """Tests for ChunkedSerialReader buffer and processing behavior."""

    def test_initial_buffer_size(self):
        """New reader starts empty with zero buffered bytes."""
        reader = com.ChunkedSerialReader(chunk_size=2)
        assert len(reader) == 0

    def test_custom_buffer(self):
        """Supplied buffer object is used internally by the reader."""
        buffer = bytearray()
        reader = com.ChunkedSerialReader(chunk_size=2, buffer=buffer)
        assert buffer is reader._buf

    def test_call(self):
        """Reader is callable and returns itself for chaining."""
        reader = com.ChunkedSerialReader(chunk_size=2)
        assert reader.__call__() is reader

    def test_put_data(self):
        """Appending data increases buffered length accordingly."""
        reader = com.ChunkedSerialReader(chunk_size=2)
        reader.put(b'\x01\x02\x03\x04')
        assert len(reader) == 4

    def test_get_data(self):
        """Reading all data returns the full bytes and clears buffer."""
        reader = com.ChunkedSerialReader(chunk_size=2)
        reader.put(b'\x01\x02\x03\x04')
        data = reader.get(4)
        assert data == bytearray(b'\x01\x02\x03\x04')
        assert len(reader) == 0

    def test_get_partial_data(self):
        """Partial reads return requested length and retain remainder."""
        reader = com.ChunkedSerialReader(chunk_size=2)
        reader.put(b'\x01\x02\x03\x04')
        data = reader.get(2)
        assert data == bytearray(b'\x01\x02')
        assert len(reader) == 2

    def test_data_received(self):
        """Exact chunk-size frames trigger process() calls with frames."""
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
        """Accumulated partial frames are processed once complete."""
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
    """Tests for to_bytes helper function."""

    def test_to_bytes_with_bytes(self):
        """Bytes input returns unchanged bytes payload."""
        assert com.to_bytes(b'test') == b'test'

    def test_to_bytes_with_bytearray(self):
        """Bytearray converts to identical bytes sequence."""
        assert com.to_bytes(bytearray([1, 2, 3])) == b'\x01\x02\x03'

    def test_to_bytes_with_memoryview(self):
        """Memoryview is supported and converted to bytes."""
        data = bytearray([1, 2, 3])
        assert com.to_bytes(memoryview(data)) == b'\x01\x02\x03'

    def test_to_bytes_with_int(self):
        """Single int 0–255 converts to one-byte sequence; >255 errors."""
        assert com.to_bytes(255) == b'\xff'
        with pytest.raises(ValueError, match='bytes must be in range'):
            com.to_bytes(256)

    def test_to_bytes_with_numpy_array(self):
        """Numpy uint8 array converts element-wise to bytes."""
        array = np.array([1, 2, 3], dtype=np.uint8)
        assert com.to_bytes(array) == b'\x01\x02\x03'

    def test_to_bytes_with_numpy_scalar(self):
        """Numpy uint8 scalar converts to a single byte."""
        scalar = np.uint8(42)
        assert com.to_bytes(scalar) == b'*'

    def test_to_bytes_with_string(self):
        """String is encoded to bytes using ASCII."""
        assert com.to_bytes('test') == b'test'

    def test_to_bytes_with_list(self):
        """List of integers converts to bytes sequence."""
        assert com.to_bytes([1, 2, 3]) == b'\x01\x02\x03'
        with pytest.raises(ValueError, match='bytes must be in range'):
            com.to_bytes([1, 2, 256])

    def test_to_bytes_with_float(self):
        """Unsupported type raises TypeError."""
        with pytest.raises(TypeError):
            com.to_bytes(42.0)
