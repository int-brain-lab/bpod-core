"""Tests for the _bpod_cli entry point."""

import logging
import signal
import threading
from unittest.mock import MagicMock, patch

import pytest

from bpod_core._cli import _bpod_cli
from bpod_core.bpod import Bpod


@pytest.fixture
def mock_bpod_cls(mocker):
    """Mock the Bpod class used inside _bpod_cli."""
    mock_instance = MagicMock(spec=Bpod)
    mock_instance.__enter__ = MagicMock(return_value=mock_instance)
    mock_instance.__exit__ = MagicMock(return_value=False)
    mock_cls = mocker.patch('bpod_core._cli.Bpod', return_value=mock_instance)
    return mock_cls, mock_instance


def _run_cli_with_args(args: list[str], mock_bpod_cls) -> int:
    """Patch sys.argv and immediately trigger shutdown, then call _bpod_cli."""
    _mock_cls, _mock_instance = mock_bpod_cls

    original_event_init = threading.Event.__init__

    def patched_event_init(self, *a, **kw):
        original_event_init(self, *a, **kw)
        self.set()  # Trigger immediate shutdown so the CLI doesn't block

    with (
        patch('sys.argv', ['bpod', *args]),
        patch.object(threading.Event, '__init__', patched_event_init),
    ):
        return _bpod_cli()


class TestBpodCli:
    def test_default_args(self, mock_bpod_cls):
        """CLI with no arguments uses default port/serial/remote and returns 0."""
        mock_cls, _ = mock_bpod_cls
        result = _run_cli_with_args([], mock_bpod_cls)
        assert result == 0
        mock_cls.assert_called_once_with(port=None, serial_number=None, remote=False)

    def test_port_arg(self, mock_bpod_cls):
        """CLI passes --port value to Bpod constructor."""
        mock_cls, _ = mock_bpod_cls
        result = _run_cli_with_args(['--port', 'COM3'], mock_bpod_cls)
        assert result == 0
        mock_cls.assert_called_once_with(port='COM3', serial_number=None, remote=False)

    def test_serial_number_arg(self, mock_bpod_cls):
        """CLI passes --serial-number value to Bpod constructor."""
        mock_cls, _ = mock_bpod_cls
        result = _run_cli_with_args(['--serial-number', '12345'], mock_bpod_cls)
        assert result == 0
        mock_cls.assert_called_once_with(port=None, serial_number='12345', remote=False)

    def test_remote_flag(self, mock_bpod_cls):
        """CLI passes remote=True when --remote flag is set."""
        mock_cls, _ = mock_bpod_cls
        result = _run_cli_with_args(['--remote'], mock_bpod_cls)
        assert result == 0
        mock_cls.assert_called_once_with(port=None, serial_number=None, remote=True)

    def test_led_enabled_by_default(self, mock_bpod_cls):
        """CLI calls set_status_led(True) on entry and set_status_led(True) on exit."""
        _, mock_instance = mock_bpod_cls
        _run_cli_with_args([], mock_bpod_cls)
        calls = mock_instance.set_status_led.call_args_list
        # First call enables LED (True by default), last call restores it
        assert calls[0].args == (True,)
        assert calls[-1].args == (True,)

    def test_no_led_flag(self, mock_bpod_cls):
        """CLI calls set_status_led(False) when --no-led is passed."""
        _, mock_instance = mock_bpod_cls
        _run_cli_with_args(['--no-led'], mock_bpod_cls)
        calls = mock_instance.set_status_led.call_args_list
        assert calls[0].args == (False,)
        # LED is always restored to True on exit
        assert calls[-1].args == (True,)

    def test_exception_returns_1(self, mock_bpod_cls):
        """CLI returns 1 and logs the error when Bpod raises an exception."""
        mock_cls, _ = mock_bpod_cls
        mock_cls.side_effect = RuntimeError('connection failed')
        with patch('sys.argv', ['bpod']):
            result = _bpod_cli()
        assert result == 1

    def test_port_and_serial_mutually_exclusive(self):
        """CLI rejects simultaneous --port and --serial-number arguments."""
        with (
            patch('sys.argv', ['bpod', '--port', 'COM3', '--serial-number', '12345']),
            pytest.raises(SystemExit),
        ):
            _bpod_cli()

    def test_version_flag(self, caplog):
        """--version logs the version string and returns 0 without opening a device."""
        with (
            patch('sys.argv', ['bpod', '--version']),
            patch('bpod_core._cli.bpod_core_version', '1.2.3'),
            caplog.at_level(logging.INFO, logger='bpod_cli'),
        ):
            result = _bpod_cli()
        assert result == 0
        assert 'bpod-core 1.2.3' in caplog.text

    def test_signal_handler_triggers_shutdown(self, mock_bpod_cls):
        """Registered signal handler sets the shutdown event."""
        captured_handlers = {}
        mock_event = MagicMock(spec=threading.Event)

        with (
            patch('sys.argv', ['bpod']),
            patch(
                'bpod_core._cli.signal.signal',
                side_effect=lambda s, h: captured_handlers.update({s: h}),
            ),
            patch('bpod_core._cli.threading.Event', return_value=mock_event),
        ):
            _bpod_cli()

        assert captured_handlers, 'no signal handlers were registered'
        handler = next(iter(captured_handlers.values()))
        handler(signal.SIGINT, None)
        mock_event.set.assert_called_once()
