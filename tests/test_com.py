import logging
import re
from unittest.mock import MagicMock, call

import pytest
from serial import SerialException

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
        reader = com.ChunkedSerialReader(chunk_size=2, callback=MagicMock())
        assert len(reader._buffer) == 0
        assert len(reader._buffer) == 0

    def test_custom_buffer(self):
        """Supplied buffer object is used internally by the reader."""
        buffer = bytearray()
        reader = com.ChunkedSerialReader(
            chunk_size=2, callback=MagicMock(), buffer=buffer
        )
        assert buffer is reader._buffer

    def test_call(self):
        """Reader is callable and returns itself for chaining."""
        reader = com.ChunkedSerialReader(chunk_size=2, callback=MagicMock())
        assert reader.__call__() is reader

    def test_connection_made(self, caplog):
        """connection_made() logs that the serial reader thread has started."""
        reader = com.ChunkedSerialReader(chunk_size=4, callback=lambda _: None)
        transport = MagicMock()
        transport.serial.portstr = 'COM3'
        with caplog.at_level(logging.DEBUG):
            reader.connection_made(transport)
        assert 'Starting serial reader thread for COM3' in caplog.text
        assert reader._port == 'COM3'

    def test_connection_lost(self, caplog):
        """connection_lost() logs when the reader thread stops."""
        reader = com.ChunkedSerialReader(chunk_size=4, callback=lambda _: None)
        reader._port = 'COM3'
        with caplog.at_level(logging.DEBUG):
            reader.connection_lost(None)
        assert 'Stopping serial reader thread for COM3' in caplog.text
        with pytest.raises(RuntimeError):
            reader.connection_lost(RuntimeError())

    def test_data_received(self):
        """Exact chunk-size frames trigger process() calls with frames."""
        callback = MagicMock()
        reader = com.ChunkedSerialReader(chunk_size=4, callback=callback)
        reader.data_received(b'\x01\x00\x00\x00\x02\x00\x00\x00')
        assert len(reader._buffer) == 0
        callback.assert_has_calls(
            [
                call(b'\x01\x00\x00\x00'),
                call(b'\x02\x00\x00\x00'),
            ],
        )

    def test_multiple_data_received(self):
        """Accumulated partial frames are processed once complete."""
        callback = MagicMock()
        reader = com.ChunkedSerialReader(chunk_size=4, callback=callback)
        reader.data_received(b'\x01\x00')
        assert len(reader._buffer) == 2
        reader.data_received(b'\x00\x00')
        assert len(reader._buffer) == 0
        reader.data_received(b'\x02\x00')
        assert len(reader._buffer) == 2
        reader.data_received(b'\x00\x00')
        assert len(reader._buffer) == 0
        callback.assert_has_calls(
            [
                call(b'\x01\x00\x00\x00'),
                call(b'\x02\x00\x00\x00'),
            ],
        )

    def test_partial_chunk_remains_buffered(self):
        """Incomplete chunk data stays in the buffer."""
        callback = MagicMock()
        reader = com.ChunkedSerialReader(chunk_size=4, callback=callback)
        reader.data_received(b'\x01\x00')
        assert len(reader._buffer) == 2
        callback.assert_not_called()

    def test_non_multiple_chunk_data(self):
        """Extra data beyond full chunks stays buffered."""
        callback = MagicMock()
        reader = com.ChunkedSerialReader(chunk_size=4, callback=callback)
        reader.data_received(b'\x01\x02\x03\x04\x05')
        callback.assert_called_once_with(b'\x01\x02\x03\x04')
        assert reader._buffer == bytearray(b'\x05')


class TestFindPorts:
    """Tests for find_ports() filtering functionality."""

    @pytest.fixture
    def mock_ports(self, mocker):
        """Fixture providing mock serial ports."""
        port1 = MagicMock()
        port1.device = '/dev/ttyACM0'
        port1.vid = 0x16C0
        port1.pid = 0x0483
        port1.serial_number = 'ABC123'

        port2 = MagicMock()
        port2.device = '/dev/ttyUSB0'
        port2.vid = 0x0403
        port2.pid = 0x6001
        port2.serial_number = 'DEF456'

        port3 = MagicMock()
        port3.device = '/dev/ttyACM1'
        port3.vid = 0x16C0
        port3.pid = 0x048B
        port3.serial_number = 'GHI789'

        ports = [port1, port2, port3]
        mocker.patch('bpod_core.com.comports', return_value=ports)
        return ports

    def test_no_filters(self, mock_ports):
        """Returns all ports when no filters specified."""
        result = com.find_ports()
        assert result == mock_ports

    def test_scalar_filter(self, mock_ports):
        """Exact match on single attribute returns matching ports."""
        result = com.find_ports(vid=0x16C0)
        assert len(result) == 2
        assert mock_ports[0] in result
        assert mock_ports[2] in result

    def test_scalar_filter_no_match(self, mock_ports):
        """Returns empty list when no ports match filter."""
        result = com.find_ports(vid=0x9999)
        assert result == []

    def test_list_filter(self, mock_ports):
        """List filter matches any item (OR logic)."""
        result = com.find_ports(pid=[0x0483, 0x6001])
        assert len(result) == 2
        assert mock_ports[0] in result
        assert mock_ports[1] in result

    def test_regex_filter(self, mock_ports):
        """Regex pattern filter matches ports with matching strings."""
        result = com.find_ports(device=re.compile(r'/dev/ttyACM\d+'))
        assert len(result) == 2
        assert mock_ports[0] in result
        assert mock_ports[2] in result

    def test_combined_filters(self, mock_ports):
        """Multiple filters combine with AND logic."""
        result = com.find_ports(vid=0x16C0, pid=0x0483)
        assert len(result) == 1
        assert mock_ports[0] in result

    def test_nonexistent_attribute(self, mock_ports):
        """Filter on missing attribute matches None values."""
        result = com.find_ports(nonexistent_attr='value')
        assert result == []

    def test_regex_on_non_string(self, mock_ports):
        """Regex filter on non-string attribute returns no match."""
        result = com.find_ports(vid=re.compile(r'16C0'))
        assert result == []

    def test_list_with_regex(self, mock_ports):
        """List can contain regex patterns."""
        result = com.find_ports(device=[re.compile(r'ttyACM'), '/dev/ttyUSB0'])
        assert len(result) == 3


class TestVerifySerialDiscovery:
    @pytest.fixture()
    def mock_serial(self, mocker):
        mock_serial = mocker.MagicMock()
        mock_serial.read.return_value = b'A'
        mock_serial.__enter__.return_value = mock_serial
        mocker.patch('bpod_core.com.Serial', return_value=mock_serial)
        return mock_serial

    def test_success(self, mock_serial):
        result = com.verify_serial_discovery(
            port='COM1',
            expected_message=b'A',
        )
        assert result is True
        mock_serial.read.assert_called_once_with(1)

    def test_wrong_message(self, mock_serial):
        result = com.verify_serial_discovery(
            port='COM1',
            expected_message=b'B',
        )
        assert result is False

    def test_trigger_called(self, mock_serial, mocker):
        trigger = mocker.Mock()
        result = com.verify_serial_discovery(
            port='COM1',
            expected_message=b'A',
            trigger=trigger,
        )
        assert result is True
        trigger.assert_called_once()

    def test_serial_exception(self, mock_serial):
        mock_serial.side_effect = SerialException
        result = com.verify_serial_discovery(
            port='COM1',
            expected_message=b'DISCOVERY_OK',
        )
        assert result is False
