"""Command line interface tools."""

import argparse
import logging
import os
import signal
import threading
from types import FrameType

from bpod_core.bpod import Bpod, bpod_core_version


def _bpod_cli() -> int:
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
    parser.add_argument(
        '-v',
        '--verbose',
        action='store_true',
        help='Use verbose output',
    )
    parser.add_argument(
        '--version',
        action='store_true',
        help='Show the version of bpod-core and exit',
    )
    parser.set_defaults(led=True)
    cli_arguments = parser.parse_args()

    # logging
    log_level = logging.DEBUG if cli_arguments.verbose else logging.INFO
    logging.basicConfig(level=log_level, format='%(message)s')
    logger = logging.getLogger('bpod_cli')

    if cli_arguments.version:
        logger.info('bpod-core %s', bpod_core_version)
        return 0

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
            logger.info('Press Ctrl+C to exit')
            try:
                shutdown_event.wait()  # Block until a shutdown signal is received
            finally:
                bpod.set_status_led(True)  # Always restore LED state on exit

    except Exception as e:
        # Report error without traceback for cleaner CLI UX
        logger.error(str(e))  # noqa: TRY400
        return 1

    else:
        return 0
