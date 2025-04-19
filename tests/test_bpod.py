import logging
from unittest.mock import MagicMock, patch

import pytest
from serial import SerialException

from bpod_core.bpod import Bpod, BpodError


class TestBpodInit:
    @pytest.fixture
    def mock_serial(self):
        """Fixture to mock serial communication."""
        mock_serial_instance = MagicMock()
        with patch(
            'bpod_core.bpod.ExtendedSerial', return_value=mock_serial_instance
        ) as mock_serial:
            yield mock_serial

    @patch('bpod_core.bpod.Bpod._identify_bpod', return_value=('COM3', '12345'))
    def test_bpod_init(self, mock_id_bpod, mock_serial):
        Bpod()


class TestBpodIdentifyBpod:
    @pytest.fixture
    def mock_comports(self):
        """Fixture to mock available COM ports."""
        mock_port_info = MagicMock()
        mock_port_info.device = 'COM3'
        mock_port_info.serial_number = '12345'
        mock_port_info.vid = 0x16C0  # supported VID
        with patch('bpod_core.bpod.comports') as mock_comports:
            mock_comports.return_value = [mock_port_info]
            yield mock_comports

    @pytest.fixture
    def mock_serial(self):
        """Fixture to mock serial communication."""
        mock_serial_instance = MagicMock()
        mock_serial_instance.read.return_value = bytes([222])
        mock_serial_instance.validate_response.return_value = True
        mock_serial_instance.__enter__.return_value = mock_serial_instance
        with patch(
            'bpod_core.bpod.ExtendedSerial', return_value=mock_serial_instance
        ) as mock_serial:
            yield mock_serial

    def test_automatic_success(self, mock_serial, mock_comports):
        """Test successful identification of Bpod without specifying port or serial."""
        port, serial_number = Bpod._identify_bpod()
        assert port == 'COM3'
        assert serial_number == '12345'
        mock_serial.assert_called_once_with('COM3', timeout=0.15)

    def test_automatic_unsupported_vid(self, mock_serial, mock_comports):
        """Test failure to auto identify Bpod when only device has unsupported VID."""
        mock_port_info = mock_comports.return_value
        mock_port_info[0].vid = 0x0000  # unsupported VID
        with pytest.raises(BpodError, match=r'No .* Bpod found'):
            Bpod._identify_bpod()
        mock_serial.assert_not_called()

    def test_automatic_no_devices(self, mock_serial, mock_comports):
        """Test failure to auto identify Bpod when no COM ports are available."""
        mock_comports.return_value = []
        with pytest.raises(BpodError, match=r'No .* Bpod found'):
            Bpod._identify_bpod()
        mock_serial.assert_not_called()

    def test_automatic_no_discovery_byte(self, mock_serial, mock_comports):
        """Test failure to auto identify Bpod when no discovery byte is received."""
        mock_serial_instance = mock_serial.return_value
        mock_serial_instance.read.return_value = b''
        with pytest.raises(BpodError, match='No .* Bpod found'):
            Bpod._identify_bpod()
        mock_serial.assert_called_once_with('COM3', timeout=0.15)

    def test_automatic_serial_exception(self, mock_serial, mock_comports):
        """Test failure to auto identify Bpod when serial read raises exception."""
        mock_serial_instance = mock_serial.return_value
        mock_serial_instance.read.side_effect = SerialException
        with pytest.raises(BpodError, match='No .* Bpod found'):
            Bpod._identify_bpod()
        mock_serial.assert_called_once_with('COM3', timeout=0.15)

    def test_serial_success(self, mock_serial, mock_comports):
        """Test successful identification of Bpod when specifying serial (non-eager)."""
        port, serial_number = Bpod._identify_bpod(serial_number='12345')
        assert port == 'COM3'
        assert serial_number == '12345'  # existing serial
        mock_serial.assert_not_called()

    def test_serial_incorrect_serial(self, mock_serial, mock_comports):
        """Test failure to identify Bpod when specifying incorrect serial."""
        with pytest.raises(BpodError, match='No .* serial number'):
            Bpod._identify_bpod(serial_number='00000')
        mock_serial.assert_not_called()

    def test_serial_unsupported_vid(self, mock_serial, mock_comports):
        """Test failure to identify Bpod by serial if device has incompatible VID."""
        mock_port_info = mock_comports.return_value
        mock_port_info[0].vid = 0x0000  # unsupported VID
        with pytest.raises(BpodError, match='.* not .* supported Bpod'):
            Bpod._identify_bpod(serial_number='12345')
        mock_serial.assert_not_called()

    def test_port_success(self, mock_serial, mock_comports):
        """Test successful identification of Bpod when specifying port."""
        port, serial_number = Bpod._identify_bpod(port='COM3')
        assert port == 'COM3'
        assert serial_number == '12345'  # existing serial
        mock_serial.assert_not_called()

    def test_port_incorrect_port(self, mock_serial, mock_comports):
        """Test failure to identify Bpod when specifying incorrect port."""
        with pytest.raises(BpodError, match='Port not found'):
            Bpod._identify_bpod(port='incorrect_port')
        mock_serial.assert_not_called()


class TestBpodHandshake:
    @pytest.fixture
    def mock_bpod(self):
        mock_bpod = MagicMock(spec=Bpod)
        mock_bpod.serial0 = MagicMock()
        mock_bpod.port0 = 'COM3'
        yield mock_bpod

    def test_handshake_success(self, mock_bpod, caplog):
        caplog.set_level(logging.DEBUG)
        mock_bpod.serial0.validate_response.return_value = True
        Bpod._handshake(mock_bpod)
        mock_bpod.serial0.validate_response.assert_called_once_with(b'6', b'5')
        mock_bpod.serial0.reset_input_buffer.assert_called_once()
        assert len(caplog.records) == 1
        assert caplog.records[0].levelname == 'DEBUG'
        assert 'successful' in caplog.records[0].message

    def test_handshake_failure_1(self, mock_bpod):
        mock_bpod.serial0.validate_response.return_value = False
        with pytest.raises(BpodError, match='Handshake .* failed'):
            Bpod._handshake(mock_bpod)
        mock_bpod.serial0.reset_input_buffer.assert_called_once()

    def test_handshake_failure_2(self, mock_bpod):
        mock_bpod.serial0.validate_response.side_effect = SerialException
        with pytest.raises(BpodError, match='Handshake .* failed'):
            Bpod._handshake(mock_bpod)
        mock_bpod.serial0.reset_input_buffer.assert_called_once()
