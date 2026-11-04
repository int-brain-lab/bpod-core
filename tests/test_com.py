import logging
import os
import re
import struct
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, call

import numpy as np
import pytest
from serial import SerialException
from serial.tools.list_ports_common import numsplit

from bpod_core import com
from bpod_core.constants import STRUCT_UINT64_LE, TeensyPID

from .conftest import held_by_another_thread, take_replies


@pytest.fixture
def mock_serial(mocker):
    """Fixture to mock serial communication."""
    mock_serial = com.ExtendedSerial()
    patched_object_base = 'bpod_core.com.Serial'
    mock_serial.super_write = mocker.patch(f'{patched_object_base}.write')
    mock_serial.super_read = mocker.patch(f'{patched_object_base}.read')
    return mock_serial


class TestExtendedSerial:
    """Tests for ExtendedSerial helpers and semantics."""

    def test_write(self, mock_serial):
        """Send bytes and ensure underlying serial.write is called."""
        mock_serial.write(b'x')
        mock_serial.super_write.assert_called_with(b'x')

    def test_read_waiting_takes_all_pending_bytes(self, pty_port):
        """On POSIX, one call returns everything pending on the port; EOF raises."""
        master, port = pty_port
        os.write(master, b'abc')
        os.write(master, b'de')
        assert port.read_waiting() == b'abcde'
        os.close(master)
        with pytest.raises(SerialException):  # hung up
            port.read_waiting()

    def test_read_waiting_ends_on_cancel(self, pty_port):
        """cancel_read() from another thread ends the wait with ReadCancelled."""
        master, port = pty_port
        threading.Timer(0.05, port.cancel_read).start()
        with pytest.raises(com.ReadCancelled):
            port.read_waiting()
        os.write(master, b'a')
        assert port.read_waiting() == b'a'  # reading goes on afterwards

    def test_discard_cancel_read(self, pty_port):
        """A cancel no read consumed is withdrawn; with none pending, it's a no-op."""
        master, port = pty_port
        port.cancel_read()
        port.discard_cancel_read()
        port.discard_cancel_read()  # nothing pending: returns at once
        os.write(master, b'a')
        assert port.read_waiting() == b'a'

    def test_read_waiting_falls_back_to_read(self, mock_serial, mocker):
        """Without descriptors, read() takes what is waiting or waits for one byte."""
        mocker.patch('bpod_core.com._DIRECT_READ', False)
        mocker.patch(
            'bpod_core.com.Serial.in_waiting',
            new_callable=PropertyMock,
            side_effect=[3, 0],
        )
        mock_serial.super_read.return_value = b'xyz'
        assert mock_serial.read_waiting() == b'xyz'
        mock_serial.read_waiting()
        assert mock_serial.super_read.call_args_list == [call(3), call(1)]

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

    def test_read_struct_precompiled(self, mock_serial):
        """Pre-compiled Struct yields the same result as a format string."""
        mock_serial.super_read.return_value = b'\x01\x02\x00\x03\x00\x00\x00'
        s = struct.Struct('<BHI')
        a, b, c = mock_serial.read_struct(s)
        assert a == 1
        assert b == 2
        assert c == 3
        mock_serial.super_read.assert_called_with(s.size)

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

    def test_query_struct_precompiled(self, mock_serial):
        """Pre-compiled Struct yields the same result as a format string."""
        mock_serial.super_read.return_value = b'\x01\x02\x00\x03\x00\x00\x00'
        s = struct.Struct('<BHI')
        a, b, c = mock_serial.query_struct(b'x', s)
        assert a == 1
        assert b == 2
        assert c == 3
        mock_serial.super_write.assert_called_with(b'x')
        mock_serial.super_read.assert_called_with(s.size)

    def test_verify(self, mock_serial):
        """Verify compares read bytes against expected pattern."""
        mock_serial.super_read.return_value = b'\x01\x02\x00\x03\x00\x00\x00'
        result = mock_serial.verify(b'x', b'\x01\x02\x00\x03\x00\x00\x00')
        assert result is True
        mock_serial.super_write.assert_called_with(b'x')
        mock_serial.super_read.assert_called_with(7)
        result = mock_serial.verify(b'x', b'\x01')
        assert result is False
        mock_serial.super_read.assert_called_with(1)

    def test_verify_timeout(self, mock_serial):
        """Verify applies the given timeout for the duration of the call only."""
        mock_serial.timeout = 5
        mock_serial.super_read.side_effect = lambda _: bytes([mock_serial.timeout])
        assert mock_serial.verify(b'x', b'\x02', timeout=2) is True
        assert mock_serial.timeout == 5

    def test_write_waits_for_the_write_lock(self, pty_port):
        """A write waits until another thread's hold on the write lock ends."""
        master, port = pty_port
        writer = threading.Thread(target=port.write, args=(b'x',), daemon=True)
        with held_by_another_thread(port.write_lock):
            writer.start()
            writer.join(0.05)
            assert writer.is_alive()  # waiting for the lock
        writer.join(1)
        assert os.read(master, 10) == b'x'

    def test_replies_owned_by_another_thread_are_refused(self, mock_serial):
        """Reads and queries raise while another thread owns the replies; writes go."""
        with held_by_another_thread(mock_serial.exclusive_reads()):
            for read in (
                lambda: mock_serial.read(1),
                lambda: mock_serial.query(b'x'),
                lambda: mock_serial.verify(b'x'),
                mock_serial.read_uint8,
            ):
                with pytest.raises(com.PortBusyError):
                    read()
            mock_serial.super_write.assert_not_called()  # a refused query sends nothing
            mock_serial.write(b'y')
        mock_serial.super_write.assert_called_once_with(b'y')

    def test_exclusive_reads_nests_for_the_owner(self, mock_serial):
        """The owner's reads and queries go through, however deeply nested."""
        mock_serial.super_read.return_value = b'\x01'
        with mock_serial.exclusive_reads():
            with mock_serial.exclusive_reads():
                assert mock_serial.query(b'x') == b'\x01'
            assert mock_serial.read(1) == b'\x01'
            assert mock_serial.read_lock.locked()
        assert not mock_serial.read_lock.locked()

    def test_claim_replies(self, mock_serial):
        """Taken when free; joined when a streaming thread holds them; else refused."""
        assert mock_serial.claim_replies()
        assert not mock_serial.claim_replies()  # a streaming thread has them
        mock_serial.read_lock.release()
        with (
            held_by_another_thread(mock_serial.exclusive_reads()),
            pytest.raises(com.PortBusyError),
        ):
            mock_serial.claim_replies()

    def test_read_waiting_ignores_the_locks(self, pty_port):
        """A reader streams on, and can be cancelled, whatever the locks' state."""
        master, port = pty_port
        take_replies(port)
        with held_by_another_thread(port.write_lock):
            os.write(master, b'abc')
            assert port.read_waiting() == b'abc'
            threading.Timer(0.02, port.cancel_read).start()
            with pytest.raises(com.ReadCancelled):
                port.read_waiting()
        port.read_lock.release()

    def test_temporary_timeout(self, mock_serial):
        """Temporary timeout is applied within the context and restored after."""
        mock_serial.timeout = 5
        with mock_serial.temporary_timeout(2):
            assert mock_serial.timeout == 2
        assert mock_serial.timeout == 5

    def test_temporary_timeout_none(self, mock_serial):
        """A timeout of None leaves the port's timeout untouched."""
        mock_serial.timeout = 5
        with mock_serial.temporary_timeout(None):
            assert mock_serial.timeout == 5
        assert mock_serial.timeout == 5

    def test_temporary_timeout_exception(self, mock_serial):
        """The original timeout is restored even if the context body raises."""
        mock_serial.timeout = 5
        with pytest.raises(RuntimeError), mock_serial.temporary_timeout(2):
            raise RuntimeError
        assert mock_serial.timeout == 5

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
            with pytest.raises(struct.error):
                getattr(mock_serial, fcn)(value + (1 if value_type == 'max' else -1))

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


class _Port(SimpleNamespace):
    """Minimal port stub that supports ordering by device name."""

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _Port):
            return NotImplemented
        return bool(numsplit(self.device) < numsplit(other.device))


class TestFindPorts:
    """Tests for find_ports() filtering functionality."""

    @pytest.fixture
    def mock_ports(self, mocker):
        """Fixture providing mock serial ports."""
        port1 = _Port(
            device='/dev/ttyACM0', vid=0x16C0, pid=0x0483, serial_number='ABC123'
        )
        port2 = _Port(
            device='/dev/ttyUSB0', vid=0x0403, pid=0x6001, serial_number='DEF456'
        )
        port3 = _Port(
            device='/dev/ttyACM1', vid=0x16C0, pid=0x048B, serial_number='GHI789'
        )

        ports = [port1, port2, port3]
        mocker.patch('bpod_core.com.comports', return_value=ports)
        return ports

    def test_no_filters(self, mock_ports):
        """Returns all ports when no filters specified."""
        result = com.find_ports()
        assert result == sorted(mock_ports, key=lambda p: numsplit(p.device))

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

    def test_none_filter_ignored(self, mock_ports):
        """Filters set to None do not constrain the result."""
        result = com.find_ports(vid=0x16C0, serial_number=None)
        assert len(result) == 2

    def test_unknown_filter_raises(self, mock_ports):
        """Unknown filter names raise instead of silently matching nothing."""
        with pytest.raises(TypeError):
            com.find_ports(nonexistent_attr='value')

    def test_list_with_regex(self, mock_ports):
        """List can contain regex patterns."""
        result = com.find_ports(device=[re.compile(r'ttyACM'), '/dev/ttyUSB0'])
        assert len(result) == 3


@pytest.mark.parametrize(
    ('location', 'expected'),
    [
        ('3-2:1.0', 0),  # Linux
        ('3-2.1:1.4', 4),  # Linux, behind a hub
        ('1-2:x.2', 2),  # Windows
        ('20-1.2', None),  # macOS
        ('3-2', None),  # a device with a single interface
        (None, None),
    ],
)
def test_usb_interface_number(make_port_info, location, expected):
    """The interface number is read from the end of pyserial's location."""
    port = make_port_info('/dev/ttyACM0')
    port.location = location
    assert com._usb_interface_number(port) == expected


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

    def test_dtr_raised_after_open(self, mock_serial, mocker):
        """With `raise_dtr`, DTR is low on opening and raised before reading."""
        dtr = mocker.PropertyMock()
        type(mock_serial).dtr = dtr
        result = com.verify_serial_discovery(
            port='COM1', expected_message=b'A', raise_dtr=True
        )
        assert result is True
        assert dtr.call_args_list == [mocker.call(False), mocker.call(True)]

    def test_dtr_untouched_by_default(self, mock_serial, mocker):
        """By default, the port is opened as is."""
        dtr = mocker.PropertyMock()
        type(mock_serial).dtr = dtr
        assert com.verify_serial_discovery(port='COM1', expected_message=b'A') is True
        dtr.assert_not_called()

    def test_serial_exception(self, mock_serial):
        mock_serial.read.side_effect = SerialException()
        result = com.verify_serial_discovery(
            port='COM1',
            expected_message=b'A',
        )
        assert result is False


class TestReadBcdDevice:
    """Tests for _read_bcd_device(), the USB device release number."""

    @pytest.fixture(autouse=True)
    def no_cache(self, mocker):
        """Start each test without release numbers found by earlier tests."""
        mocker.patch.dict(com._BCD_DEVICES, clear=True)

    @pytest.fixture
    def port(self, make_port_info):
        """The port of a Bpod 2+, a Teensy 4.1 with three serial ports."""
        return make_port_info(
            '/dev/ttyACM0', serial_number='14414540', pid=TeensyPID.TRIPLE_SERIAL
        )

    @pytest.fixture
    def sysfs(self, mocker, port, tmp_path):
        """Linux, with the sysfs directory of the port's USB device (no bcdDevice)."""
        mocker.patch.object(com, 'sys', SimpleNamespace(platform='linux'))
        port.usb_device_path = str(tmp_path)  # type: ignore[attr-defined]
        (tmp_path / 'serial').write_text(f'{port.serial_number}\n')
        return tmp_path

    @pytest.fixture
    def winreg(self, mocker):
        """Windows, with a fake winreg listing a Teensy 4.1's hardware IDs."""
        mocker.patch.object(com, 'sys', SimpleNamespace(platform='win32'))
        winreg = MagicMock()
        winreg.OpenKey.return_value.__enter__.return_value = 'key'
        hardware_ids = [r'USB\VID_16C0&PID_048C&REV_0280', r'USB\VID_16C0&PID_048C']
        winreg.QueryValueEx.return_value = (hardware_ids, 7)
        mocker.patch.dict(sys.modules, {'winreg': winreg})
        return winreg

    @pytest.fixture
    def iokit(self, mocker, port):
        """macOS, with fake IOKit helpers: two serial ports, one the port's."""
        mocker.patch.object(com, 'sys', SimpleNamespace(platform='darwin'))
        iokit = MagicMock(kUSBSerialNumberString='USB Serial Number')
        properties = {
            ('other', 'IOCalloutDevice'): '/dev/cu.other',
            ('bpod', 'IOCalloutDevice'): port.device,
            ('usb', 'USB Serial Number'): port.serial_number,
        }
        iokit.GetIOServicesByType.return_value = ['other', 'bpod']
        iokit.get_string_property.side_effect = lambda obj, key: properties.get(
            (obj, key)
        )
        iokit.GetParentDeviceByType.return_value = 'usb'
        iokit.get_int_property.return_value = 0x0280
        # an attribute of serial.tools: on macOS, pyserial has imported the real one
        mocker.patch('serial.tools.list_ports_osx', iokit, create=True)
        return iokit

    def test_found_number_is_kept(self, port, sysfs):
        """Reads bcdDevice from sysfs, and keeps the number for the device."""
        (sysfs / 'bcdDevice').write_text('0280\n')
        assert com._read_bcd_device(port) == 0x0280
        (sysfs / 'bcdDevice').unlink()
        assert com._read_bcd_device(port) == 0x0280

    def test_failed_lookup_is_repeated(self, port, sysfs):
        """A failed lookup is repeated next time, e.g., once the device is back."""
        assert com._read_bcd_device(port) is None
        (sysfs / 'bcdDevice').write_text('0280\n')
        assert com._read_bcd_device(port) == 0x0280

    def test_no_serial_number(self, mocker, port):
        """A port without a serial number isn't looked up: its device can't be told."""
        look_up = mocker.patch('bpod_core.com._look_up_bcd_device')
        port.serial_number = None
        assert com._read_bcd_device(port) is None
        look_up.assert_not_called()

    def test_linux_not_usb(self, port, sysfs):
        """A port without a USB device has no release number."""
        port.usb_device_path = None  # type: ignore[attr-defined]
        assert com._read_bcd_device(port) is None

    def test_linux_unplugged(self, port, sysfs):
        """A device unplugged since the ports were listed has no release number."""
        port.usb_device_path = str(sysfs / 'gone')  # type: ignore[attr-defined]
        assert com._read_bcd_device(port) is None

    def test_linux_other_device(self, port, sysfs):
        """A device that has taken over the port's USB path isn't the port's."""
        (sysfs / 'bcdDevice').write_text('0277\n')
        (sysfs / 'serial').write_text('99999999\n')
        assert com._read_bcd_device(port) is None

    def test_windows(self, port, winreg):
        """Reads the revision from the hardware IDs of the USB device's registry key."""
        assert com._read_bcd_device(port) == 0x0280
        winreg.OpenKey.assert_called_once_with(
            winreg.HKEY_LOCAL_MACHINE,
            r'SYSTEM\CurrentControlSet\Enum\USB\VID_16C0&PID_048C\14414540',
        )
        winreg.QueryValueEx.assert_called_once_with('key', 'HardwareID')

    def test_windows_unknown_device(self, port, winreg):
        """A device without a registry key has no release number."""
        winreg.OpenKey.side_effect = FileNotFoundError
        assert com._read_bcd_device(port) is None

    def test_macos(self, port, iokit):
        """Reads bcdDevice from the USB device the port's serial service belongs to."""
        assert com._read_bcd_device(port) == 0x0280
        iokit.GetParentDeviceByType.assert_called_once_with('bpod', 'IOUSBHostDevice')
        iokit.get_int_property.assert_called_once_with(
            'usb', 'bcdDevice', iokit.kCFNumberSInt32Type
        )

    def test_macos_unknown_port(self, port, iokit):
        """A port without a serial service has no release number."""
        port.device = '/dev/cu.gone'
        assert com._read_bcd_device(port) is None

    def test_macos_not_usb(self, port, iokit):
        """A serial service without a USB device has no release number."""
        iokit.GetParentDeviceByType.return_value = None
        assert com._read_bcd_device(port) is None

    def test_macos_other_device(self, port, iokit):
        """A device that has taken over the port's name isn't the port's."""
        port.serial_number = '99999999'
        assert com._read_bcd_device(port) is None

    def test_unknown_device_on_this_platform(self, make_port_info):
        """The real lookup of this platform finds nothing for an unknown device."""
        port = make_port_info(
            '/dev/nonexistent', serial_number='NONE', vid=0xFFFF, pid=0xFFFF
        )
        assert com._read_bcd_device(port) is None


class TestSerialDevice:
    """Tests for SerialDevice class."""

    @pytest.fixture
    def mock_port_info(self, make_port_info):
        """Fixture to create a port info object."""
        return make_port_info('/dev/ttyACM0')

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

    def test_init_with_port_info(
        self, mock_comports, mock_port_info, mock_extended_serial
    ):
        """An already enumerated port is used without enumerating again."""
        device = com.SerialDevice(mock_port_info)
        mock_comports.assert_not_called()
        assert device.port == '/dev/ttyACM0'

    def test_init_without_opening_connection(self, mock_comports, mock_extended_serial):
        """Device can be initialized without opening connection."""
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        mock_extended_serial.open.assert_not_called()
        assert device.port == '/dev/ttyACM0'

    def test_init_port_not_found(self, mock_comports):
        """Raises SerialException when port does not exist."""
        mock_comports.return_value = []
        with pytest.raises(SerialException, match='serial port not found'):
            com.SerialDevice('/dev/ttyACM0')

    def test_init_port_none(self, mock_comports):
        """Anything but a path or a port info is reported as a missing port."""
        with pytest.raises(SerialException, match='serial port not found: None'):
            com.SerialDevice(None)  # type: ignore[arg-type]

    def test_bcd_device(
        self, mocker, mock_comports, mock_port_info, mock_extended_serial
    ):
        """The USB device release number is read for the device's port."""
        read = mocker.patch('bpod_core.com._read_bcd_device', return_value=0x0280)
        device = com.SerialDevice('/dev/ttyACM0', open_connection=False)
        read.assert_not_called()
        assert device._bcd_device == 0x0280
        read.assert_called_once_with(mock_port_info)

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
