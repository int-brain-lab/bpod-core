"""Connecting to a Bpod: discovery, handshake, sibling ports and clock alignment."""

import contextlib
import threading
from time import time_ns

import pytest

from bpod_core.bpod import _SESSION_CLOCK_MAX_ERROR_NS, Bpod, discover_bpod
from bpod_core.bpod.constants import DISCOVERY_BYTE
from bpod_core.bpod.errors import BpodError
from bpod_core.com import find_ports, verify_serial_discovery
from bpod_core.fsm import StateMachine

from ._helpers import FIRMWARE_PRS

MAX_REPLY_S = 0.020
"""Longest acceptable wait for a reply to the handshake or to raising DTR."""
MAX_CONNECT_S = 0.5
"""Longest acceptable time to connect, or to disconnect."""
MAX_SLACK_NS = 50_000_000
"""Longest acceptable gap between the host's run / wait calls and the trial's events."""


def _slowest(benchmark) -> float:
    """The slowest round in seconds, or 0 with benchmarking disabled."""
    return benchmark.stats.stats.max if benchmark.stats is not None else 0.0


class _Connection(contextlib.ExitStack):
    """One connection to the Bpod at a time: open() connects, close() disconnects."""

    def __init__(self, port: str) -> None:
        super().__init__()
        self.port = port

    def open(self) -> None:
        self.enter_context(Bpod(port=self.port))


def test_discovery_lists_the_bpod(bpod_info):
    """A scan for idle Bpods finds the device."""
    assert bpod_info.serial_number in {d.serial_number for d in discover_bpod()}


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/17')
def test_discovery_byte_answers_dtr(benchmark, bpod_info):
    """Raising DTR makes the Bpod send its discovery byte right away."""

    def probe():
        assert verify_serial_discovery(
            bpod_info.port, DISCOVERY_BYTE, timeout=MAX_REPLY_S, raise_dtr=True
        )

    benchmark.pedantic(probe, rounds=5, warmup_rounds=1)
    assert _slowest(benchmark) < MAX_REPLY_S  # not the periodic byte


def test_connect(benchmark, bpod_info, logged_warnings):
    """Connecting is quick and quiet, and leaves no threads behind."""
    threads_before = set(threading.enumerate())
    with _Connection(bpod_info.port) as connection:
        benchmark.pedantic(
            connection.open, setup=connection.close, rounds=20, warmup_rounds=1
        )
    assert _slowest(benchmark) < MAX_CONNECT_S
    assert logged_warnings() == []
    assert set(threading.enumerate()) == threads_before


def test_disconnect(benchmark, bpod_info):
    """Leaving the context manager, i.e. disconnecting, is quick."""
    with _Connection(bpod_info.port) as connection:  # closed also if a round fails
        benchmark.pedantic(
            connection.close, setup=connection.open, rounds=10, warmup_rounds=1
        )
    assert _slowest(benchmark) < MAX_CONNECT_S


def test_close_releases_service_addresses(bpod_info, logged_warnings):
    """After close(), the next connection binds its service addresses right away."""
    bpod = Bpod(port=bpod_info.port)  # stays referenced, as by a GUI
    bpod.close()
    with Bpod(port=bpod_info.port):
        pass
    assert logged_warnings() == []


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/16')
def test_reopen_handshake(benchmark, bpod):
    """Reopening the port is answered within a few milliseconds."""
    benchmark.pedantic(bpod.open, setup=bpod.close, rounds=10, warmup_rounds=1)
    assert _slowest(benchmark) < MAX_REPLY_S


def test_clock_reset_round_trip(benchmark, bpod, logged_warnings):
    """A session clock reset is answered fast enough to align the clocks."""

    def reset():
        assert bpod.reset_session_clock()

    benchmark.pedantic(reset, rounds=20, warmup_rounds=1)
    assert _slowest(benchmark) < 2 * _SESSION_CLOCK_MAX_ERROR_NS / 1e9
    assert logged_warnings() == []


def test_sibling_ports(bpod, bpod_info):
    """The secondary port, and the FlexIO port of a 2+, belong to the same Bpod."""
    if bpod.serial1 is None:
        pytest.skip('needs a secondary USB serial port')
    ports = [bpod.serial1.port]
    if bpod.version.machine == 4:
        assert bpod._flexio_serial is not None
        ports.append(bpod._flexio_serial.port)
    else:
        assert bpod._flexio_serial is None
    for port in ports:
        assert port != bpod.port
        assert find_ports(device=port)[0].serial_number == bpod_info.serial_number


def _assert_trial_aligned(bpod: Bpod) -> None:
    """Run a trial of two 5 ms states; its events fall between run() and wait()."""
    fsm = StateMachine()
    fsm.add_state('a', timer=0.005, transitions={'Tup': 'b'})
    fsm.add_state('b', timer=0.005, transitions={'Tup': '>exit'})
    t_run = time_ns()
    bpod.run(fsm)
    bpod.wait()
    t_wait = time_ns()
    times = bpod.get_data()['Time'].dt.epoch('ns').to_list()
    assert t_run <= min(times) <= t_run + MAX_SLACK_NS
    assert t_wait - MAX_SLACK_NS <= max(times) <= t_wait


def test_event_times_are_host_aligned(bpod):
    """Event times fall between the host's run and wait calls, with little slack."""
    _assert_trial_aligned(bpod)


def test_reopen_realigns_clock(bpod):
    """After closing and reopening, event times are aligned to the host clock again."""
    bpod.close()
    bpod.open()
    _assert_trial_aligned(bpod)


def test_command_from_another_thread_is_refused_during_a_trial(bpod):
    """A command needing a reply, sent mid-trial from another thread, is refused.

    The trial's reader owns the port's replies; the trial's data stays intact.
    """
    fsm = StateMachine()
    fsm.add_state('a', timer=0.2, transitions={'Tup': '>exit'})
    bpod.run(fsm)
    errors: list[BaseException] = []

    def command() -> None:
        try:
            bpod.update_modules()
        except BpodError as e:
            errors.append(e)

    thread = threading.Thread(target=command)
    thread.start()
    thread.join(2)
    bpod.wait()
    assert len(errors) == 1
    assert 'while a state machine is running' in str(errors[0])
    types = bpod.get_data()['Type'].to_list()
    assert types[0] == 'TrialStart'
    assert types[-1] == 'TrialEndControl'
