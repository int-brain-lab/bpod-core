import logging
import re
import struct
from unittest.mock import MagicMock, call

import numpy as np
import pytest
from serial import SerialException

from bpod_core import com
from bpod_core.constants import STRUCT_UINT64_LE


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

    @pytest.mark.parametrize(
        'fcn',
        [
            'write_int8',
            'write_int16',
            'write_int32',
            'write_int64',
            'write_uint8',
            'write_uint16',
            'write_uint32',
            'write_uint64',
        ],
    )
    def test_write_int(self, mock_serial, fcn):
        """Test writing integer values."""
        dtype = getattr(np, fcn.split('_')[1])
        info = np.iinfo(dtype)
        length = info.bits // 8
        signed = info.kind == 'i'
        for value_type in ['min', 'max']:
            value = getattr(info, value_type)
            expected = value.to_bytes(length, 'little', signed=signed)
            getattr(mock_serial, fcn)(value)
            mock_serial.super_write.assert_called_with(expected)

    @pytest.mark.parametrize(
        'fcn',
        [
            'read_int8',
            'read_int16',
            'read_int32',
            'read_int64',
            'read_uint8',
            'read_uint16',
            'read_uint32',
            'read_uint64',
        ],
    )
    def test_read_int(self, mock_serial, fcn):
        """Test reading integer values."""
        dtype = getattr(np, fcn.split('_')[1])
        info = np.iinfo(dtype)
        length = info.bits // 8
        signed = info.kind == 'i'
        for value_type in ['min', 'max']:
            value = getattr(info, value_type)
            mock_serial.super_read.return_value = value.to_bytes(
                length, 'little', signed=signed
            )
            value_out = getattr(mock_serial, fcn)()
            mock_serial.super_read.assert_called_with(length)
            assert value_out == value

    def test_write_bool(self, mock_serial):
        """Test reading single character."""
        mock_serial.write_bool(True)
        mock_serial.super_write.assert_called_with(b'\x01')

    def test_read_bool(self, mock_serial):
        """Test reading single character."""
        mock_serial.super_read.return_value = b'\x01'
        value_out = mock_serial.read_bool()
        mock_serial.super_read.assert_called_with(1)
        assert value_out is True


class TestReadStructIter:
    """Tests for ExtendedSerial.read_struct_iter."""

    def test_yields_tuples(self, mock_serial):
        """Yields one tuple per record when flatten=False."""
        mock_serial.super_read.return_value = struct.pack('<QQ', 100, 200)
        result = list(mock_serial.read_struct_iter(STRUCT_UINT64_LE, 2))
        assert result == [(100,), (200,)]

    def test_flatten(self, mock_serial):
        """Yields individual values when flatten=True."""
        mock_serial.super_read.return_value = struct.pack('<QQ', 100, 200)
        r1, r2 = list(mock_serial.read_struct_iter(STRUCT_UINT64_LE, 2, flatten=True))
        assert r1 == 100
        assert r2 == 200

    def test_reads_correct_byte_count(self, mock_serial):
        """Reads n * fmt.size bytes in a single call."""
        mock_serial.super_read.return_value = struct.pack('<QQQ', 1, 2, 3)
        list(mock_serial.read_struct_iter(STRUCT_UINT64_LE, 3))
        mock_serial.super_read.assert_called_once_with(24)

    def test_format_string(self, mock_serial):
        """Accepts a format string in place of a pre-compiled Struct."""
        mock_serial.super_read.return_value = struct.pack('<QQ', 100, 200)
        result = list(mock_serial.read_struct_iter('<Q', 2))
        assert result == [(100,), (200,)]

    def test_format_string_flatten(self, mock_serial):
        """Accepts a format string and flattens values."""
        mock_serial.super_read.return_value = struct.pack('<QQ', 100, 200)
        result = list(mock_serial.read_struct_iter('<Q', 2, flatten=True))
        assert result == [100, 200]


class TestStreamStruct:
    """Tests for ExtendedSerial.stream_struct."""

    @pytest.fixture
    def mock_readinto(self, mocker, request):
        """Mock Serial.readinto to fill buffer from a list of byte responses."""
        responses = iter(request.param)

        def side_effect(buf):
            data = next(responses)
            buf[:] = data
            return len(data)

        return mocker.patch('bpod_core.com.Serial.readinto', side_effect=side_effect)

    @pytest.mark.parametrize(
        'mock_readinto',
        [[struct.pack('<Q', 100), struct.pack('<Q', 200)]],
        indirect=True,
    )
    def test_yields_tuples(self, mock_serial, mock_readinto):
        """Yields one tuple per record when flatten=False."""
        result = list(mock_serial.stream_struct(STRUCT_UINT64_LE, 2, flatten=False))
        assert result == [(100,), (200,)]

    @pytest.mark.parametrize(
        'mock_readinto',
        [[struct.pack('<Q', 100), struct.pack('<Q', 200)]],
        indirect=True,
    )
    def test_flatten(self, mock_serial, mock_readinto):
        """Yields individual values when flatten=True (default)."""
        result = list(mock_serial.stream_struct(STRUCT_UINT64_LE, 2))
        assert result == [100, 200]

    @pytest.mark.parametrize(
        'mock_readinto',
        [[struct.pack('<Q', i) for i in range(3)]],
        indirect=True,
    )
    def test_calls_readinto_per_record(self, mock_serial, mock_readinto):
        """Calls readinto once per record, not once for all data."""
        list(mock_serial.stream_struct(STRUCT_UINT64_LE, 3))
        assert mock_readinto.call_count == 3

    @pytest.mark.parametrize(
        'mock_readinto',
        [[struct.pack('<Q', 100), struct.pack('<Q', 200)]],
        indirect=True,
    )
    def test_format_string(self, mock_serial, mock_readinto):
        """Accepts a format string in place of a pre-compiled Struct."""
        result = list(mock_serial.stream_struct('<Q', 2, flatten=False))
        assert result == [(100,), (200,)]

    @pytest.mark.parametrize(
        'mock_readinto',
        [[struct.pack('<Q', 100), struct.pack('<Q', 200)]],
        indirect=True,
    )
    def test_format_string_flatten(self, mock_serial, mock_readinto):
        """Accepts a format string and flattens values."""
        result = list(mock_serial.stream_struct('<Q', 2))
        assert result == [100, 200]


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
    @pytest.fixture
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
        mock_serial.read.side_effect = SerialException()
        result = com.verify_serial_discovery(
            port='COM1',
            expected_message=b'A',
        )
        assert result is False


class TestSerialDevice:
    """Tests for SerialDevice class."""

    @pytest.fixture
    def mock_port_info(self, mocker):
        """Fixture to create a mock port info object."""
        port_info = mocker.MagicMock()
        port_info.device = '/dev/ttyACM0'
        port_info.serial_number = '12345'
        port_info.vid = 0x16C0
        port_info.pid = 0x0483
        return port_info

    @pytest.fixture
    def mock_comports(self, mocker, mock_port_info):
        """Fixture to mock available COM ports."""
        mock = mocker.patch('bpod_core.com.comports')
        mock.return_value = [mock_port_info]
        return mock

    @pytest.fixture
    def mock_extended_serial(self, mocker):
        """Fixture to mock ExtendedSerial."""
        mock = mocker.MagicMock(spec=com.ExtendedSerial)
        mock.is_open = False
        mock.open.side_effect = lambda: setattr(mock, 'is_open', True)
        mock.close.side_effect = lambda: setattr(mock, 'is_open', False)
        mocker.patch('bpod_core.com.ExtendedSerial', return_value=mock)
        return mock

    def test_init_opens_connection_by_default(
        self, mock_comports, mock_extended_serial
    ):
        """Device opens serial connection by default on init."""
        device = com.SerialDevice('/dev/ttyACM0')
        mock_extended_serial.open.assert_called_once()
        assert device.port == '/dev/ttyACM0'

    def test_init_without_opening_connection(self, mock_comports, mock_extended_serial):
        """Device can be initialized without opening connection."""
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        mock_extended_serial.open.assert_not_called()
        assert device.port == '/dev/ttyACM0'

    def test_init_port_not_found(self, mock_comports):
        """Raises SerialException when port does not exist."""
        mock_comports.return_value = []
        with pytest.raises(SerialException, match='Serial port not found'):
            com.SerialDevice('/dev/ttyACM0')

    def test_context_manager_enter(self, mock_comports, mock_extended_serial):
        """Context manager __enter__ returns the device instance."""
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        result = device.__enter__()
        assert result is device

    def test_context_manager_exit(self, mock_comports, mock_extended_serial):
        """Context manager __exit__ closes the connection."""
        mock_extended_serial.is_open = True
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        device.__exit__(None, None, None)
        mock_extended_serial.close.assert_called_once()

    def test_context_manager_with_statement(self, mock_comports, mock_extended_serial):
        """Device works correctly with 'with' statement."""
        mock_extended_serial.is_open = True
        with com.SerialDevice('/dev/ttyACM0', open_connection=False) as device:
            assert device.port == '/dev/ttyACM0'
        mock_extended_serial.close.assert_called_once()

    def test_open_when_closed(self, mock_comports, mock_extended_serial, caplog):
        """open() opens connection when not already open."""
        mock_extended_serial.is_open = False
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        with caplog.at_level(logging.DEBUG):
            device.open()
        mock_extended_serial.open.assert_called_once()
        assert 'Opening connection' in caplog.text

    def test_open_when_already_open(self, mock_comports, mock_extended_serial):
        """open() does nothing when connection is already open."""
        mock_extended_serial.is_open = True
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        device.open()
        mock_extended_serial.open.assert_not_called()

    def test_open_raises_on_failure(self, mock_comports, mock_extended_serial):
        """open() raises SerialException when connection fails."""
        mock_extended_serial.is_open = False
        mock_extended_serial.open.side_effect = Exception('Connection failed')
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        with pytest.raises(SerialException, match='Failed to open connection'):
            device.open()

    def test_close_when_open(self, mock_comports, mock_extended_serial, caplog):
        """close() closes connection when open."""
        mock_extended_serial.is_open = True
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        with caplog.at_level(logging.DEBUG):
            device.close()
        mock_extended_serial.close.assert_called_once()
        assert 'Closing connection' in caplog.text

    def test_close_when_already_closed(self, mock_comports, mock_extended_serial):
        """close() does nothing when connection is already closed."""
        mock_extended_serial.is_open = False
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        device.close()
        mock_extended_serial.close.assert_not_called()

    def test_close_raises_on_failure(self, mock_comports, mock_extended_serial):
        """close() raises SerialException when closing fails."""
        mock_extended_serial.is_open = True
        mock_extended_serial.close.side_effect = Exception('Close failed')
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        with pytest.raises(SerialException, match='Failed to close connection'):
            device.close()

    def test_close_detaches_finalizer(self, mock_comports, mock_extended_serial):
        """close() detaches the live finalizer registered during open()."""
        device = com.SerialDevice('/dev/ttyACM0', open_connection=True)
        finalizer = device._serial_device_finalizer
        assert finalizer is not None and finalizer.alive
        device.close()
        assert not finalizer.alive

    def test_port_property(self, mock_comports, mock_extended_serial):
        """port property returns the device path."""
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        assert device.port == '/dev/ttyACM0'

    def test_rename_updates_name(self, mock_comports, mock_extended_serial):
        """_rename_serial_device updates _serial_device_name."""
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        device._rename_serial_device('my device')
        assert device._serial_device_name == 'my device'

    def test_rename_before_open_registers_finalizer(
        self, mock_comports, mock_extended_serial
    ):
        """_rename_serial_device before open() registers a finalizer."""
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        assert device._serial_device_finalizer is None
        device._rename_serial_device('my device')
        assert device._serial_device_finalizer is not None

    def test_rename_after_open_replaces_finalizer(
        self, mock_comports, mock_extended_serial
    ):
        """after open(), rename detaches old finalizer and registers a new one."""
        device = com.SerialDevice('/dev/ttyACM0', open_connection=True)
        old_finalizer = device._serial_device_finalizer
        device._rename_serial_device('renamed device')
        assert not old_finalizer.alive
        assert device._serial_device_finalizer is not None
        assert device._serial_device_finalizer.alive

    def test_rename_gc_uses_new_name(self, mock_comports, mock_extended_serial, caplog):
        """after rename, GC-triggered cleanup logs the updated device name."""
        device = com.SerialDevice('/dev/ttyACM0', open_connection=True)
        device._rename_serial_device('fancy device')
        with caplog.at_level(logging.DEBUG):
            del device
        assert 'fancy device' in caplog.text
        mock_extended_serial.close.assert_called_once()

    def test_finalizer_closes_on_gc(self, mock_comports, mock_extended_serial):
        """Finalizer closes the serial connection during garbage collection."""
        device = com.SerialDevice('/dev/ttyACM0', open_connection=True)
        del device
        mock_extended_serial.close.assert_called_once()

    def test_finalizer_swallows_errors(self, mock_comports, mock_extended_serial):
        """Finalizer does not raise when serial.close() fails."""
        mock_extended_serial.close.side_effect = Exception('Close failed')
        device = com.SerialDevice('/dev/ttyACM0', open_connection=True)
        finalizer = device._serial_device_finalizer
        assert finalizer.alive
        del device
        assert not finalizer.alive

    def test_exit_swallows_close_errors(self, mock_comports, mock_extended_serial):
        """__exit__ does not raise when closing fails."""
        mock_extended_serial.is_open = True
        mock_extended_serial.close.side_effect = Exception('Close failed')
        with com.SerialDevice('/dev/ttyACM0', open_connection=False):
            pass  # should not raise on exit

    def test_exit_preserves_original_exception(
        self, mock_comports, mock_extended_serial
    ):
        """__exit__ does not mask the original exception with a close error."""
        mock_extended_serial.is_open = True
        mock_extended_serial.close.side_effect = Exception('Close failed')
        with (
            pytest.raises(ValueError, match='original error'),
            com.SerialDevice('/dev/ttyACM0', open_connection=False),
        ):
            raise ValueError('original error')
