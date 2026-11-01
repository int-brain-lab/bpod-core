"""Helpers for the tests that run on a connected Bpod."""

import datetime as dt
from collections.abc import Callable
from itertools import pairwise
from time import monotonic, sleep

import polars as pl

from bpod_core.bpod import _IDLE_STOP_REPLY_HEADER, _IDLE_STOP_REPLY_SIZE, Bpod, Module
from bpod_core.bpod.abc import AbstractFlexIO
from bpod_core.bpod.constants import FlexIOChannelType
from bpod_core.fsm import StateMachine

FIRMWARE_PRS = 'https://github.com/sanworks/Bpod_StateMachine_Firmware/pull'
"""Where the fixes of known firmware issues are, by pull request number."""

Events = list[tuple[str | None, str, int]]
"""A trial's events and its end, as (state, event, cycles from the trial's start)."""


def _chain(*steps: tuple[str, float, dict[str, int]]) -> StateMachine:
    """States visited in order, each with a timer and actions, then the exit."""
    fsm = StateMachine()
    names = [name for name, _, _ in steps]
    for (name, timer, actions), target in zip(
        steps, [*names[1:], '>exit'], strict=True
    ):
        fsm.add_state(name, timer=timer, transitions={'Tup': target}, actions=actions)
    return fsm


def _pulse(output: str) -> StateMachine:
    """`output` on for 5 ms, from 2 ms into a 10 ms trial: an input wired to it changes
    in cycles 21 and 71.
    """
    return _chain(('Off', 0.002, {}), ('On', 0.005, {output: 1}), ('End', 0.003, {}))


def _timer_across_states(channel: str) -> StateMachine:
    """Global timer 0 setting `channel` for 20 ms: A starts it, B follows after 5 ms."""
    fsm = _chain(('A', 0.005, {'GlobalTimerTrig': 0}), ('B', 0.03, {}))
    fsm.set_global_timer(0, duration=0.02, channel=channel, value_on=1)
    return fsm


def _wire_flex(flex_io: AbstractFlexIO) -> None:
    """Set up Flex1 as digital output and Flex4 as digital input, for a wire between."""
    flex_io['Flex1'].channel_type = FlexIOChannelType.DIGITAL_OUTPUT
    flex_io['Flex4'].channel_type = FlexIOChannelType.DIGITAL_INPUT


def _busy_timers(fsm: StateMachine, duration: float) -> StateMachine:
    """Global timer 0, of `duration`, starting 15 timers that end in every cycle."""
    loops = range(1, 16)
    fsm.set_global_timer(0, duration=duration, onset_trigger=sum(1 << i for i in loops))
    for i in loops:
        fsm.set_global_timer(i, duration=1e-4, loop=1, loop_interval=1e-4)
    return fsm


def _timer_trial(fsm: StateMachine) -> StateMachine:
    """One state of 20 ms that triggers global timer 0."""
    fsm.add_state(
        'A', timer=0.02, transitions={'Tup': '>exit'}, actions={'GlobalTimerTrig': 0}
    )
    return fsm


def _cancel_trial() -> StateMachine:
    """Global timer 0, of 10 ms, triggered when A begins and cancelled when B does."""
    fsm = StateMachine()
    fsm.set_global_timer(0, duration=0.01)
    fsm.add_state(
        'A', timer=0.003, transitions={'Tup': 'B'}, actions={'GlobalTimerTrig': 0}
    )
    fsm.add_state(
        'B', timer=0.015, transitions={'Tup': '>exit'}, actions={'GlobalTimerCancel': 0}
    )
    return fsm


def _counted(fsm: StateMachine) -> StateMachine:
    """Global timer 1 ending every millisecond once triggered, counted to 5."""
    fsm.set_global_timer(1, duration=0.0005, loop=1, loop_interval=0.0005)
    fsm.set_global_counter(0, event='GlobalTimer1_End', threshold=5)
    return fsm


def _counter_trial() -> StateMachine:
    """Global counter 0 reaching 5 ends of global timer 1, in a trial of 20 ms."""
    fsm = _counted(StateMachine())
    fsm.add_state(
        'A', timer=0.02, transitions={'Tup': '>exit'}, actions={'GlobalTimerTrig': 1}
    )
    return fsm


def _trial_events(
    bpod: Bpod, fsm: StateMachine, during: Callable[[], object] | None = None
) -> Events:
    """Run `fsm`: its events, and the trial's end, as (state, event, cycle)."""
    bpod.run(fsm)
    if during is not None:
        during()
    bpod.wait()
    data = bpod.get_data()
    data = data.filter(pl.col('Trial') == data['Trial'].max())
    start = data.filter(pl.col('Type') == 'TrialStart')['Time'][0]
    rows = data.filter(pl.col('Type').is_in(['InputEvent', 'TrialEnd'])).select(
        pl.col('State').cast(pl.String),
        pl.when(pl.col('Type') == 'TrialEnd')
        .then(pl.lit('TrialEnd'))
        .otherwise(pl.col('Event').cast(pl.String)),
        (pl.col('Time') - start).dt.total_microseconds(),
    )
    cycle = bpod._hardware.cycle_period_us
    return [(state, event, round(us / cycle)) for state, event, us in rows.rows()]


def _timed(events: Events, prefix: str) -> list[tuple[str, int]]:
    """The events starting with `prefix`, with their cycles."""
    return [(event, cycle) for _, event, cycle in events if event.startswith(prefix)]


def _trial_gaps(data: pl.DataFrame) -> list[int]:
    """The µs from each trial's end to the next trial's start."""
    times = {
        kind: data.filter(pl.col('Type') == kind)['Time'].dt.epoch('us').to_list()
        for kind in ('TrialStart', 'TrialEnd')
    }
    ends, next_starts = times['TrialEnd'][:-1], times['TrialStart'][1:]
    return [b - a for a, b in zip(ends, next_starts, strict=True)]


def _request_identification(bpod: Bpod, module: Module) -> None:
    """Ask `module` to identify itself, from the host.

    Every Bpod module answers byte 255 with `A`, its firmware version and its name.
    """
    bpod.serial0.write_struct('<c3B', b'T', module.index + 1, 1, 255)


def _probe_module(bpod: Bpod, module: Module) -> bytes:
    """Ask `module` to identify itself; what comes back, which only a relay brings."""
    _request_identification(bpod, module)
    with bpod.serial0.temporary_timeout(0.05):  # the Bpod waits 10 ms for a module
        reply = bpod.serial0.read(1)
    if reply:
        with bpod.serial0.temporary_timeout(0.02):
            reply += bpod.serial0.read(1024)
    return reply


def _stop_analog_sampling(bpod: Bpod) -> None:
    """End analog sampling that the firmware still runs for an earlier session.

    It can add a sample to the first trial (see
    test_analog_sampling_starts_with_the_first_trial); stopped, the recording starts
    with the first trial.
    """
    reply = bpod.serial0.query(b'X', _IDLE_STOP_REPLY_SIZE)
    assert reply.startswith(_IDLE_STOP_REPLY_HEADER), f'stop answered {reply.hex()}'


def _analog_data(bpod: Bpod, until: dt.datetime) -> pl.DataFrame:
    """The analog data, once bpod-core has passed on the samples up to `until`.

    It passes them on in batches, every 100 ms; this waits for at most a second. It
    takes only batches passed on already: getting one blocks until there is one.
    """
    frames: list[pl.DataFrame] = []
    deadline = monotonic() + 1
    while monotonic() < deadline:
        if bpod._analog_data.empty():
            sleep(0.01)
            continue
        frames.append(bpod.get_analog_data())
        if frames[-1].select(pl.col('Time').max() >= until).item():
            break
    assert frames, 'no analog samples within a second'
    return pl.concat(frames)


def _check_analog_samples(bpod: Bpod, data: pl.DataFrame) -> None:
    """Check that no analog sample of the trials in `data` is lost, but the last's.

    A trial's samples are those taken from its start until the next trial's: as many
    as fit, the first one at most a sample period after the start, or a cycle before
    it (trial starts are timed in microseconds, samples in cycles). bpod-core times
    the samples by counting them, so a lost one shifts all later ones by a sample
    period, which each later trial's first sample shows nine times in ten.
    """
    period, cycle = bpod._analog_sample_period_us, bpod._hardware.cycle_period_us
    assert period, 'no analog recording'
    trial_starts = data.filter(pl.col('Type') == 'TrialStart')
    times = pl.col('Time').dt.epoch('us')
    samples = (
        _analog_data(bpod, until=trial_starts.select(pl.col('Time').max()).item())
        .group_by('Trial')
        .agg(pl.len(), times.min().alias('first'), times.max().alias('last'))
        .rows_by_key('Trial', unique=True)
    )
    starts = dict(trial_starts.select('Trial', times).iter_rows())
    for (trial, start), (_, next_start) in pairwise(sorted(starts.items())):
        n, first, last = samples.get(trial, (0, None, None))
        expected = (next_start - start) / period
        span = '' if first is None else f', {first - start} to {last - start} µs in'
        assert abs(n - expected) <= 1, (
            f'trial {trial}: {n} analog samples in {expected:.1f} sample periods'
            f'{span}; the next trial starts {next_start - start} µs after it'
        )
        assert first is not None and -cycle < first - start <= period + cycle, (
            f'trial {trial}: first analog sample {first - start} µs after its start'
        )
