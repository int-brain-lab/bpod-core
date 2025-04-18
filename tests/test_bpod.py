import logging
from unittest.mock import MagicMock, patch

import pytest
from serial import SerialException

from bpod_core.bpod import Bpod, BpodError


class TestBpodInit:
    def test_bpod_init_no_device(self):
        with (
            patch('bpod_core.bpod.find_bpod_ports', return_value=iter([])),
            pytest.raises(BpodError, match=r'No Bpod found'),
        ):
            Bpod()

    def test_bpod_init_invalid_port(self):
        with (
            patch('bpod_core.bpod.list_ports.grep', return_value=iter([])),
            pytest.raises(BpodError, match=r'Port not found'),
        ):
            Bpod(port='invalid_port')

    def test_bpod_init_unsupported_device(self):
        mock_info = MagicMock()
        mock_info.vid = 0x1234  # Unsupported VID
        with (
            patch('bpod_core.bpod.list_ports.grep', return_value=iter([mock_info])),
            pytest.raises(BpodError, match=r'not a .* Bpod'),
        ):
            Bpod(port='COM3')


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
