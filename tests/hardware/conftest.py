"""Fixtures and hooks for the tests that run on a connected Bpod."""

import contextlib
import functools
import logging
import os
import random
import threading
from collections.abc import Callable, Iterator
from importlib.metadata import version

import polars as pl
import pytest
from packaging.version import Version

from bpod_core.bpod import Bpod, Module, discover_bpod
from bpod_core.bpod.abc import AbstractFlexIO
from bpod_core.bpod.constants import FlexIOChannelType
from bpod_core.bpod.structs import BpodInfo

from ._helpers import (
    _chain,
    _check_analog_samples,
    _stop_analog_sampling,
    _trial_events,
    _wire_flex,
)

# pytest-benchmark 5.3.0 reads the `_nodeid` slot that pytest 9 removed; fixed upstream
# (ionelmc/pytest-benchmark#323) but not yet released. Drop with 5.3.1.
if pytest.version_tuple >= (9,) and (
    Version(version('pytest-benchmark')) <= Version('5.3.0')
):
    pytest.Function._nodeid = property(lambda self: self.nodeid)  # type: ignore[attr-defined]


@pytest.fixture(scope='session')
def bpod_info() -> BpodInfo:
    """The idle Bpod to test on: the first found, or the one the environment selects."""
    devices = discover_bpod(
        port=os.getenv('BPOD_OVERRIDE_PORT'),
        serial_number=os.getenv('BPOD_OVERRIDE_SERIAL_NUMBER'),
    )
    if (device := next(devices, None)) is None:
        pytest.skip('no idle Bpod found')
    return device


@pytest.fixture
def bpod(bpod_info: BpodInfo, pytestconfig: pytest.Config) -> Iterator[Bpod]:
    """A fresh connection to the Bpod for each test."""
    with Bpod(port=bpod_info.port) as bpod:
        if _DEVICE not in pytestconfig.stash:
            info = bpod.version
            pytestconfig.stash[_DEVICE] = (
                f'Bpod {info.machine_str} (PCB {info.pcb}), firmware '
                f'{info.firmware[0]}.{info.firmware[1]}, '
                f'serial number {bpod_info.serial_number} at {bpod_info.port}'
            )
        yield bpod
        if bpod.is_running:  # left running by a failed test
            with contextlib.suppress(TimeoutError):  # given up; replug if it persists
                bpod.stop_state_machine()


@pytest.fixture(scope='session')
def fuzz_seed(pytestconfig: pytest.Config) -> int:
    """The seed of the randomized tests: from --seed, or drawn and printed."""
    seed: int | None = pytestconfig.getoption('--seed')
    if seed is None:
        seed = random.SystemRandom().randrange(2**32)
    pytestconfig.stash[_SEED] = seed
    return seed


@pytest.fixture
def logged_warnings(caplog: pytest.LogCaptureFixture) -> Callable[[], list[str]]:
    """The messages logged at WARNING or above so far.

    Not the notice of a Bpod whose support is experimental, logged on every connection.
    """
    return lambda: [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.msg != 'Support for %s is experimental'
    ]


@pytest.fixture
def flex_io(bpod: Bpod) -> AbstractFlexIO:
    """The Bpod's FlexIO channels; skips on a Bpod without them."""
    if not bpod._hardware.n_flexio:  # flex_io raises then
        pytest.skip('needs FlexIO channels (Bpod 2+)')
    return bpod.flex_io


def _first_module(bpod: Bpod, port: int) -> Module:
    """The module on the first module port from `port` on that has one; skips without.

    Connecting identifies the modules, so this asks nothing of the Bpod.
    """
    modules = bpod.modules.values()
    connected = (m for m in modules if m.is_connected and m.index + 1 >= port)
    if (module := next(connected, None)) is None:
        where = f' on module port {port} or higher' if port > 1 else ''
        pytest.skip(f'needs a module{where}')
    return module


@pytest.fixture
def module(bpod: Bpod) -> Module:
    """The module on the first module port that has one; skips without one."""
    return _first_module(bpod, port=1)


@pytest.fixture
def module_beyond_port1(bpod: Bpod) -> Module:
    """The module on the first module port after port 1 that has one; skips without."""
    return _first_module(bpod, port=2)


LOOPBACKS = {'Flex1': 'Flex4', 'TTLOut1': 'TTLIn1'}
"""The outputs wires may connect to inputs, for the loopback tests."""


@pytest.fixture(scope='session')
def loopbacks(bpod_info: BpodInfo) -> dict[str, str]:
    """The outputs wired to inputs: probed once, on a connection of its own.

    The probe runs before the test that first asks, so before that test connects.
    """
    with Bpod(port=bpod_info.port) as bpod:
        has_flex_io = bpod._hardware.n_flexio > 0  # else flex_io raises
        if has_flex_io:
            _wire_flex(bpod.flex_io)
        outputs = [o for o in LOOPBACKS if has_flex_io or not o.startswith('Flex')]
        probe = _chain(('Probe', 0.02, dict.fromkeys(outputs, 1)))
        edges = {(event, t) for _, event, t in _trial_events(bpod, probe)}
        # a cycle after the output, not, e.g., mains hum on an unwired input
        return {o: i for o, i in LOOPBACKS.items() if (f'{i}_High', 1) in edges}


@pytest.fixture
def flex_loopback(flex_io: AbstractFlexIO, loopbacks: dict[str, str]) -> None:
    """Flex1 as digital output, wired to Flex4 as digital input; skips if unwired."""
    if 'Flex1' not in loopbacks:
        pytest.skip('needs a wire from Flex1 to Flex4')
    _wire_flex(flex_io)


@pytest.fixture
def ttl_loopback(loopbacks: dict[str, str]) -> None:
    """TTLOut1 wired to TTLIn1, centre and shell; skips if unwired."""
    if 'TTLOut1' not in loopbacks:
        pytest.skip('needs wires from TTLOut1 to TTLIn1, centre and shell')


@pytest.fixture
def analog_recording(
    bpod: Bpod, flex_io: AbstractFlexIO
) -> Callable[[pl.DataFrame], None]:
    """Record Flex1 as analog input; returns a check that no sample is lost."""
    _stop_analog_sampling(bpod)
    flex_io['Flex1'].channel_type = FlexIOChannelType.ANALOG_INPUT
    return functools.partial(_check_analog_samples, bpod)


def _run_guarded(
    bpod: Bpod,
    work: Callable[[], object],
    timeout: float,
    *,
    describe: Callable[[], str] | None = None,
    cancel: Callable[[], object] | None = None,
) -> None:
    """Run `work` on a worker thread; stop the Bpod and fail if it takes too long.

    Bounds what the library can't: `work` may block in the handoff inside ``run()``,
    for a trial that never ends. On expiry, `cancel` tells `work` not to go on, and the
    running trial is stopped - or given up, if the Bpod ignores the stop. An error
    raised by `work` is re-raised, as the cause of `describe`'s text if given.
    """
    errors: list[BaseException] = []

    def target() -> None:
        try:
            work()
        except BaseException as e:  # noqa: BLE001 - re-raised on the test's thread
            errors.append(e)

    worker = threading.Thread(target=target, name='guarded', daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        if cancel is not None:
            cancel()
        note = ''
        try:
            bpod.stop_state_machine()  # the worker then returns from its wait
        except TimeoutError as e:  # the Bpod ignored the stop: the trial was given up
            note = f'; {e}'
        worker.join(2.0)
        where = '' if describe is None else f': {describe()}'
        pytest.fail(f'the Bpod did not finish within {timeout:.1f} s{where}{note}')
    if errors:
        if describe is None:
            raise errors[0]
        # a failed check stays one; any other error isn't taken for a firmware issue
        failed = isinstance(errors[0], (AssertionError, pytest.fail.Exception))
        raise (AssertionError if failed else RuntimeError)(describe()) from errors[0]


@pytest.fixture
def run_guarded(bpod: Bpod) -> Callable[..., None]:
    """Run work on the test's Bpod that may hang, failing instead (see _run_guarded)."""
    return functools.partial(_run_guarded, bpod)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Expect the known firmware issues to fail, unless --fixed-firmware has the fix."""
    fixed_firmware = config.getoption('--fixed-firmware')
    for item in items:
        if (marker := item.get_closest_marker('known_firmware_issue')) is None:
            continue
        fix = marker.args[0] if marker.args else None
        if fix is not None and fixed_firmware:
            continue
        reason = 'known firmware issue, ' + (f'fixed by {fix}' if fix else 'no fix yet')
        run = marker.kwargs.get('run', True)  # False: the failure hangs the Bpod
        # only a failed check is the issue; an error elsewhere, e.g. in bpod-core, isn't
        expected = (AssertionError, pytest.fail.Exception)
        xfail = pytest.mark.xfail(reason=reason, strict=False, run=run, raises=expected)
        item.add_marker(xfail)


def _outcome(report: pytest.TestReport) -> str:
    """A report's outcome, telling expected failures and passes apart."""
    if hasattr(report, 'wasxfail'):
        return 'xfailed' if report.skipped else 'xpassed'
    return report.outcome


_DEVICE = pytest.StashKey[str]()
_SEED = pytest.StashKey[int]()
_REPORTS: list[pytest.TestReport] = []
"""One report per hardware test, in the order the tests ran."""


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Keep the report that decided each test's outcome: its setup or its call."""
    if report.when == 'call' or (report.when == 'setup' and not report.passed):
        _REPORTS.append(report)


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter, config: pytest.Config
) -> None:
    """Tabulate the tests that recorded properties: one table per test, a row per id."""
    if all(_outcome(report) == 'skipped' for report in _REPORTS):
        return
    terminalreporter.section('hardware test results')
    if (device := config.stash.get(_DEVICE, None)) is not None:
        terminalreporter.write_line(device)
    if (seed := config.stash.get(_SEED, None)) is not None:
        terminalreporter.write_line(f'fuzz seed {seed} (replay with --seed {seed})')

    tables: dict[str, list[tuple[str, pytest.TestReport]]] = {}
    for report in _REPORTS:
        if not report.user_properties:
            continue
        name, _, level = report.nodeid.rpartition('::')[2].partition('[')
        tables.setdefault(name, []).append((level.rstrip(']') or '-', report))
    for name, rows in tables.items():
        columns = list(dict.fromkeys(k for _, r in rows for k, _ in r.user_properties))
        lines = [[name, 'outcome', *columns]]
        for level, report in rows:
            properties = dict(report.user_properties)
            lines.append(
                [
                    level,
                    _outcome(report),
                    *(str(properties.get(c, '')) for c in columns),
                ]
            )
        widths = [max(map(len, column)) for column in zip(*lines, strict=True)]
        terminalreporter.write_line('')
        for line in lines:
            cells = [
                cell.ljust(width) for cell, width in zip(line, widths, strict=True)
            ]
            terminalreporter.write_line('  '.join(cells).rstrip())
