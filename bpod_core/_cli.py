"""Command line interface tools."""

# ruff: noqa: T201

import argparse
import logging
import os
import signal
import threading
from types import FrameType

from bpod_core.bpod import Bpod


def _bpod_cli() -> int:
    # Simple logging setup for CLI output
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    # Configure CLI interface and arguments
    transport = 'TCP or Unix sockets' if os.name == 'posix' else 'TCP'
    parser = argparse.ArgumentParser(
        prog='bpod',
        description=f'Launches a Bpod process that can be connected to via {transport}',
        epilog='Press Ctrl+C to interrupt',
    )

    # Only one of port or serial number should be provided
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        '-p',
        '--port',
        help="The device's serial port",
        default=None,
    )
    group.add_argument(
        '-s',
        '--serial-number',
        help="The device's USB serial number",
        default=None,
    )

    # Optional flags
    parser.add_argument(
        '-r',
        '--remote',
        help='Make the device available on the local network.',
        action='store_true',
    )
    parser.add_argument(
        '--no-led',
        dest='led',
        action='store_false',
        help="Disable the device's status LED",
    )
    parser.set_defaults(led=True)
    cli_arguments = parser.parse_args()

    shutdown_event = threading.Event()

    def handle_shutdown(_signum: int, _frame: FrameType | None) -> None:
        shutdown_event.set()

    for sig_name in ('SIGINT', 'SIGTERM', 'SIGBREAK', 'SIGHUP'):
        if hasattr(signal, sig_name):
            signal.signal(getattr(signal, sig_name), handle_shutdown)

    try:
        with Bpod(
            port=cli_arguments.port,
            serial_number=cli_arguments.serial_number,
            remote=cli_arguments.remote,
        ) as bpod:
            bpod.set_status_led(cli_arguments.led)
            print('Press Ctrl+C to exit')
            try:
                shutdown_event.wait()  # Block until a shutdown signal is received
            finally:
                bpod.set_status_led(True)  # Always restore LED state on exit

    except Exception as e:
        # Report error without traceback for cleaner CLI UX
        logging.error(str(e))  # noqa:LOG015, TRY400
        return 1

    else:
        return 0
