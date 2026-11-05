"""Randomly generated state machines, run in quick succession.

A robustness test for the firmware and for bpod-core's reader, event and softcode
threads. Each case runs N_TRIALS machines from a seeded generator, either one at a time
or queued on the Bpod while the previous one still runs, on a Bpod 2+ also queued while
Flex1 records analog samples. It checks what breaks when robustness fails: truncated
trial data, lost or misordered softcodes, wrong output resets, visits that don't match
the machine, and timing violations. It also predicts the events of the features the
machines use: the host's answers to softcodes, outputs read back on the inputs wired to
them (Flex1 to Flex4, TTLOut1 to TTLIn1), global timers that loop a set number of times
or are cancelled, and global counters that are reset, and the app softcodes received on
the secondary port. While the machines run, the host and an app on the secondary port
also send softcodes no state reacts to, for more traffic during the uploads. A watchdog
ends a machine the Bpod never finishes. Failures name the seed and dump the batch's
machines.
"""

import json
import random
import threading
import time
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Iterator
from itertools import pairwise
from pathlib import Path
from typing import NamedTuple

import polars as pl
import pytest
import serial

from bpod_core.bpod import Bpod
from bpod_core.constants import UINT8_MAX
from bpod_core.fsm import StateMachine

from ._helpers import FIRMWARE_PRS, _wire_flex

N_TRIALS = 25
"""Machines per level and mode."""
TOLERANCE_CYCLES = 1
"""Allowed deviation of a global timer span, and of a trial ended by one, in cycles.

State visits along a `Tup` path, and trials made of them, measure exactly.
"""
ANSWER_TIMEOUT_CYCLES = 2000
"""How long a state waits for the host to answer its softcode."""

Rng = random.Random


class _Limits(NamedTuple):
    cycle_us: int
    max_states: int  # usable without `>back`
    n_timers: int
    n_counters: int
    n_conditions: int
    n_softcodes: int  # that the host can send
    leds: list[str]
    """The PWM output channels, used as actions besides softcodes: harmless unwired."""
    loopbacks: dict[str, str]
    """The outputs wired to inputs, mapped to the inputs: states and timers set them."""
    app_port: bool = False
    """Whether the Bpod has a secondary USB serial port for app softcodes."""

    @property
    def dead_event(self) -> str:
        """An event that never fires: the end of a global timer nobody triggers."""
        return f'GlobalTimer{self.n_timers - 1}_End'

    @property
    def answered(self) -> int:
        """The first softcode the host answers: the top ones, one per answer."""
        return UINT8_MAX + 1 - self.n_softcodes  # softcodes 1 to 255

    def answer(self, softcode: int) -> int | None:
        """The host's answer to `softcode`, if it answers it."""
        return softcode - self.answered if softcode >= self.answered else None

    @property
    def spare_softcode(self) -> int:
        """A host softcode no state reacts to, sent as extra traffic: no answer."""
        return self.n_softcodes - 1


class _Case(NamedTuple):
    fsm: StateMachine
    path: list[str] | None
    """The exact visit order, or None when global timer 0 ends the trial."""


class _Visit(NamedTuple):
    state: str
    start: int  # cycles from the trial's start
    end: int
    row: int  # of the StateStart row
    end_row: int

    @property
    def cycles(self) -> int:
        return self.end - self.start


class _Event(NamedTuple):
    name: str
    channel: str | None
    value: int | None
    cycle: int  # from the trial's start


class _Trial(NamedTuple):
    """What a checked trial contributes to the batch's figures."""

    softcodes: list[int]
    deviations: list[int]
    start: int  # µs
    end: int
    control: int
    n_events: int
    app_softcodes: list[int]


class _Fuzzer(NamedTuple):
    bpod: Bpod
    limits: _Limits
    seed: int
    softcodes: list[int]
    dump_dir: Path


@pytest.fixture
def fuzzer(bpod: Bpod, fuzz_seed: int, tmp_path: Path) -> _Fuzzer:
    hardware = bpod._hardware
    limits = _Limits(
        cycle_us=hardware.cycle_period_us,
        max_states=hardware.max_states - 1,
        n_timers=hardware.n_global_timers,
        n_counters=hardware.n_global_counters,
        n_conditions=hardware.n_conditions,
        n_softcodes=bpod._compiler.n_softcodes,
        leds=sorted(name for name, out in bpod.outputs.items() if out.io_type == b'P'),
        loopbacks={},
        app_port=bpod.serial1 is not None,
    )
    softcodes: list[int] = []

    def handle(softcode: int) -> None:
        softcodes.append(softcode)
        if (answer := limits.answer(softcode)) is not None:  # a state waits for it
            bpod.send_softcode(answer)

    bpod.set_softcode_handler(handle)
    return _Fuzzer(bpod, limits, fuzz_seed, softcodes, tmp_path)


@pytest.fixture
def app_port(bpod: Bpod) -> Iterator[serial.Serial | None]:
    """The Bpod's secondary USB serial port as an app opens it, if it has one."""
    if bpod.serial1 is None:
        yield None
        return
    with serial.Serial(bpod.serial1.port, timeout=0.05) as port:
        port.reset_input_buffer()
        yield port


def _seconds(cycles: int, limits: _Limits) -> float:
    return cycles * limits.cycle_us / 1e6


def _cycles(seconds: float, limits: _Limits) -> int:
    return round(seconds * 1e6 / limits.cycle_us)


def _nominal(case: _Case, state: str, limits: _Limits) -> int:
    """A state's timer in cycles, as the generator drew it."""
    return _cycles(case.fsm.states[state].timer, limits)


def _nominal_trial(case: _Case, limits: _Limits) -> int:
    """A trial's length in cycles: its path's state timers, or global timer 0's.

    A state waiting for the host counts its timeout.
    """
    if case.path is not None:
        return sum(_nominal(case, state, limits) for state in case.path)
    return _cycles(case.fsm.global_timers[0].duration, limits)


def _walk(rng: Rng, names: list[str], last: str) -> tuple[list[str], dict[str, str]]:
    """A random visit order through the states, and each state's successor in it."""
    order = names[:1] + rng.sample(names[1:], len(names) - 1)
    return order, dict(zip(order, [*order[1:], last], strict=True))


def _extras(rng: Rng, limits: _Limits, p_softcode: float = 0.5) -> dict[str, int]:
    """Random actions: a softcode with the given probability, LEDs, wired outputs."""
    actions: dict[str, int] = {}
    if rng.random() < p_softcode:
        actions['SoftCode'] = rng.randrange(1, limits.answered)  # left unanswered
    if limits.app_port and rng.random() < p_softcode / 2:
        actions['SoftCodeApp'] = rng.randint(1, UINT8_MAX)
    n_leds = min(len(limits.leds), rng.choice((0, 0, 1, 2)))
    for led in rng.sample(limits.leds, n_leds):
        actions[led] = rng.randint(1, 255)  # never 0, which a reset row also carries
    for output in limits.loopbacks:
        if rng.random() < 0.3:
            actions[output] = 1
    return actions


def _random_chain(
    rng: Rng,
    limits: _Limits,
    n_states: int,
    timer_cycles: tuple[int, int],
    *,
    p_softcode: float = 0.5,
) -> _Case:
    """States visited in a random order, each timing out into the next; the last exits.

    Some states instead send a softcode the host answers, and move on with the answer.
    Some also carry a dead transition, a transition table entry that must not fire.
    """
    names = [f's{i}' for i in range(n_states)]
    path, following = _walk(rng, names, '>exit')
    fsm = StateMachine()
    for name in names:  # added in name order, so the path crosses the state table
        transitions = {'Tup': following[name]}
        actions = _extras(rng, limits, p_softcode)
        if rng.random() < 0.1:
            answer = rng.randrange(limits.spare_softcode)
            actions['SoftCode'] = limits.answered + answer
            transitions[f'SoftCode{answer}'] = following[name]
            cycles = ANSWER_TIMEOUT_CYCLES
        else:
            cycles = rng.randint(*timer_cycles)
        if rng.random() < 0.2:
            transitions[limits.dead_event] = rng.choice(names)
        fsm.add_state(
            name,
            timer=_seconds(cycles, limits),
            transitions=transitions,
            actions=actions,
        )
    return _Case(fsm, path)


def _level_chain(rng: Rng, limits: _Limits) -> _Case:
    return _random_chain(rng, limits, rng.randint(2, 60), (1, 20))


def _level_softcodes(rng: Rng, limits: _Limits) -> _Case:
    """A softcode from every state, one per 100 to 300 µs."""
    n_states = rng.randint(50, limits.max_states)
    return _random_chain(rng, limits, n_states, (1, 3), p_softcode=1)


def _level_big(rng: Rng, limits: _Limits) -> _Case:
    """As many states as the Bpod takes."""
    return _random_chain(rng, limits, limits.max_states, (1, 20))


def _level_mixed(rng: Rng, limits: _Limits) -> _Case:
    """Small and large machines at random: a large one may load as a trial ends."""
    return rng.choice((_level_chain, _level_big))(rng, limits)


def _arm(fsm: StateMachine, first: str, limits: _Limits) -> None:
    """Add an entry state that starts global timer 0 once; nothing may lead back to it.

    Re-triggering a running timer restarts it, so a revisited trigger state would
    postpone the end of the trial indefinitely.
    """
    fsm.add_state(
        'arm',
        timer=_seconds(1, limits),
        transitions={'Tup': first},
        actions={'GlobalTimerTrig': 0},
    )


def _level_cycles(rng: Rng, limits: _Limits) -> _Case:
    """Arbitrary `Tup` transitions, cycles and `>back` included; timer 0 exits.

    A third of the `Tup` targets of a ring are rewired at random; a dead transition to
    each state's ring successor keeps every state reachable. The first ring state never
    uses `>back`, which would lead back to the arming state.
    """
    names = [f's{i}' for i in range(rng.randint(2, 40))]
    _, successor = _walk(rng, names, names[0])
    targets = dict(successor)
    for name in rng.sample(names, len(names) // 3):
        back = name != names[0] and rng.random() < 0.3
        targets[name] = '>back' if back else rng.choice(names)
    exit_cycles = rng.randint(50, 2000)
    fsm = StateMachine()
    fsm.set_global_timer(0, duration=_seconds(exit_cycles, limits))
    _arm(fsm, names[0], limits)
    for name in names:
        fsm.add_state(
            name,
            timer=_seconds(rng.randint(1, 50), limits),
            transitions={
                'Tup': targets[name],
                'GlobalTimer0_End': '>exit',
                limits.dead_event: successor[name],
            },
            actions=_extras(rng, limits),
        )
    return _Case(fsm, None)


def _level_timers(rng: Rng, limits: _Limits) -> _Case:
    """Global timers, counters and conditions driving a small ring of states.

    Timer 0 starts all other timers through its onset trigger and ends the trial. They
    loop until it ends, or a set number of times that fit into it. One state may cancel
    a timer, and states reset counters. A wired TTL output is a timer's, which states
    leave alone; a FlexIO output isn't, since states reset it (see
    test_global_timer_on_flex_output).
    The firmware takes one transition per cycle, conditions first, and discards the
    cycle's other events: a condition could steal the cycle of timer 0's end event
    and lose the exit. Condition 0, true once timer 0 is inactive, exits instead.
    """
    timers = rng.sample(range(1, limits.n_timers), rng.randint(1, limits.n_timers - 1))
    exit_cycles = rng.randint(200, 2000)
    fsm = StateMachine()
    fsm.set_global_timer(
        0,
        duration=_seconds(exit_cycles, limits),
        onset_trigger=sum(1 << i for i in timers),
    )
    timed = next((o for o in limits.loopbacks if not o.startswith('Flex')), None)
    timed_by = rng.choice(timers) if timed else None
    state_limits = limits._replace(
        loopbacks={o: wired for o, wired in limits.loopbacks.items() if o != timed}
    )
    for i in timers:
        duration, interval = rng.randint(1, 20), rng.randint(0, 20)
        delay = rng.randint(0, 50)
        # a loop restarts a cycle after its end at the earliest
        fit = (exit_cycles - delay) // (duration + max(interval, 1)) - 1
        loop = rng.randint(2, min(fit, 10)) if fit >= 2 and rng.random() < 0.5 else 1
        if i == timed_by:
            channel, value_on = timed, 1
        elif limits.leds and rng.random() < 0.3:
            channel, value_on = rng.choice(limits.leds), rng.randint(1, 255)
        else:
            channel, value_on = None, 0
        fsm.set_global_timer(
            i,
            duration=_seconds(duration, limits),
            onset_delay=_seconds(delay, limits),
            loop=loop,
            loop_interval=_seconds(interval, limits),
            channel=channel,
            value_on=value_on,
        )
    events = [f'GlobalTimer{i}_{edge}' for i in timers for edge in ('Start', 'End')]
    counters = range(min(limits.n_counters, rng.randint(0, 2)))
    for j in counters:
        counted = f'GlobalTimer{rng.choice(timers)}_End'
        fsm.set_global_counter(j, event=counted, threshold=rng.randint(1, 20))
        events.append(f'GlobalCounter{j}_End')
    fsm.set_condition(0, channel='GlobalTimer0', value=False)
    for k in range(1, min(limits.n_conditions, rng.randint(1, 3))):
        channel = f'GlobalTimer{rng.choice(timers)}'
        fsm.set_condition(k, channel=channel, value=rng.random() < 0.5)
        events.append(f'Condition{k}')
    names = [f's{i}' for i in range(rng.randint(2, 8))]
    _, successor = _walk(rng, names, names[0])
    canceller = rng.choice(names) if rng.random() < 0.5 else None
    _arm(fsm, names[0], limits)
    for name in names:
        transitions = {'Condition0': '>exit', 'GlobalTimer0_End': '>exit'}
        chosen = rng.sample(events, min(len(events), rng.randint(1, 3)))
        for event in chosen:
            transitions[event] = rng.choice(names)
        transitions[chosen[0]] = successor[name]  # every state is reachable
        if rng.random() < 0.5:
            transitions['Tup'] = rng.choice(names)
        actions = _extras(rng, state_limits)
        if name == canceller:
            actions['GlobalTimerCancel'] = rng.choice(timers)
        if counters and rng.random() < 0.3:
            actions['GlobalCounterReset'] = rng.choice(counters)
        fsm.add_state(
            name,
            timer=_seconds(rng.randint(1, 200), limits),
            transitions=transitions,
            actions=actions,
        )
    return _Case(fsm, None)


LEVELS: dict[str, Callable[[Rng, _Limits], _Case]] = {
    'chain': _level_chain,
    'softcodes': _level_softcodes,
    'big': _level_big,
    'mixed': _level_mixed,
    'cycles': _level_cycles,
    'timers': _level_timers,
}


def _visits(
    types: list[str], states: list[str | None], cycles: list[int]
) -> list[_Visit]:
    starts = [row for row, kind in enumerate(types) if kind == 'StateStart']
    ends = [row for row, kind in enumerate(types) if kind == 'StateEnd']
    kinds = [kind for kind in types if kind in ('StateStart', 'StateEnd')]
    assert kinds == ['StateStart', 'StateEnd'] * len(starts), 'state rows out of order'
    return [
        _Visit(str(states[s]), cycles[s], cycles[e], s, e)
        for s, e in zip(starts, ends, strict=True)
    ]


def _check_outputs(
    case: _Case, frame: pl.DataFrame, visits: list[_Visit], leds: Collection[str]
) -> tuple[list[int], list[int]]:
    """Check the output rows of every visit; return the softcodes and app softcodes."""
    rows = frame.with_row_index('row').filter(pl.col('Type') == 'OutputAction')
    outputs = list(zip(rows['row'], rows['Channel'], rows['Value'], strict=True))
    visit_rows = [visit.row for visit in visits]
    segments: list[list[tuple[str, int]]] = [[] for _ in visits]
    for row, channel, value in outputs:  # the rows following each StateStart
        if (i := bisect_right(visit_rows, row) - 1) >= 0:
            segments[i].append((channel, value))
    softcodes: dict[str, list[int]] = {'SoftCode': [], 'SoftCodeApp': []}
    lit: set[str] = set()
    for visit, segment in zip(visits, segments, strict=True):
        actions = case.fsm.states[visit.state].actions
        for channel, recorded in softcodes.items():
            codes = [val for ch, val in segment if ch == channel]
            expected = [actions[channel]] if channel in actions else []
            assert codes == expected, (
                f'{channel} rows on entering {visit.state}: {codes}'
            )
            recorded.extend(codes)
        seen: dict[str, int] = {}
        for ch, val in segment:
            if ch in leds:
                seen.setdefault(ch, val)  # later rows are resets, checked below
        expected_leds = {ch: val for ch, val in actions.items() if ch in leds}
        expected_leds |= dict.fromkeys(lit - expected_leds.keys(), 0)
        assert seen == expected_leds, f'LED rows on entering {visit.state}: {seen}'
        lit = {ch for ch, val in expected_leds.items() if val}
    last_end = visits[-1].end_row if visits else -1
    reset_at_end = {ch for row, ch, val in outputs if row > last_end and val == 0}
    assert reset_at_end >= lit, (
        f'LEDs left on at the end of the trial: {lit - reset_at_end}'
    )
    return softcodes['SoftCode'], softcodes['SoftCodeApp']


def _actions(case: _Case, visits: list[_Visit], action: str) -> list[tuple[int, int]]:
    """The visits taking `action`: when each started, and the action's value."""
    return [
        (visit.start, actions[action])
        for visit in visits
        if action in (actions := case.fsm.states[visit.state].actions)
    ]


def _answer(case: _Case, state: str, limits: _Limits) -> int | None:
    """The host's answer that `state` waits for, if it waits for one."""
    return limits.answer(case.fsm.states[state].actions.get('SoftCode', -1))


def _check_answers(
    case: _Case, visits: list[_Visit], events: list[_Event], limits: _Limits
) -> None:
    """Check that the host's answers end the states waiting for them, and only those."""
    expected = [
        (answer, visit.end)
        for visit in visits
        if (answer := _answer(case, visit.state, limits)) is not None
    ]
    answers = [(e.value, e.cycle) for e in events if e.channel == 'SoftCode']
    assert answers == expected, f'host softcodes {answers}, not {expected}'


def _first_cancels(case: _Case, visits: list[_Visit]) -> dict[int, int]:
    """The cycle each cancelled global timer is first cancelled in."""
    first: dict[int, int] = {}
    for t, i in _actions(case, visits, 'GlobalTimerCancel'):
        first.setdefault(i, t)
    return first


def _edges(levels: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Where a level starting at 0 changes, as (level, cycle), from (cycle, level)."""
    edges, current = [], 0
    for cycle, level in levels:
        if level != current:
            edges.append((level, cycle))
            current = level
    return edges


def _timer_output(
    case: _Case, i: int, visits: list[_Visit], events: list[_Event], trial_end: int
) -> list[tuple[int, int]]:
    """The edges of the input wired to global timer `i`'s output, as (level, cycle).

    The output follows the timer's events; the firmware reports those a cycle late for a
    timer starting with the trial and for a cancel. Two changes within a cycle cancel
    out, and an input reads its level in the next cycle.
    """
    late_start = 1 if case.fsm.global_timers[i].onset_delay == 0 else None
    cancel = _first_cancels(case, visits).get(i)
    late_end = None if cancel is None else cancel + 1
    levels: dict[int, int] = {}  # the output's level at the end of each cycle
    for e in events:
        if e.channel == f'GlobalTimer{i}' and e.value is not None:
            late = e.cycle == (late_start if e.value else late_end)
            levels[e.cycle - 1 if late else e.cycle] = e.value
    changes = sorted(levels.items())
    return _edges((t + 1, level) for t, level in changes if t < trial_end)


def _check_loopbacks(
    case: _Case,
    visits: list[_Visit],
    events: list[_Event],
    limits: _Limits,
    trial_end: int,
) -> None:
    """Check that each wired input follows its output a cycle later.

    A state sets the output as it begins, or a global timer while it runs.
    """
    timers = {t.channel: i for i, t in case.fsm.global_timers.items() if t.channel}
    for output, wired in limits.loopbacks.items():
        if (i := timers.get(output)) is not None:
            expected = _timer_output(case, i, visits, events, trial_end)
        else:
            expected = _edges(
                (visit.start + 1, case.fsm.states[visit.state].actions.get(output, 0))
                for visit in visits
            )
        edges = [(e.value, e.cycle) for e in events if e.channel == wired]
        assert edges == expected, f'{wired} edges {edges}, not {expected}'


def _check_timers(
    case: _Case, visits: list[_Visit], events: list[_Event], limits: _Limits
) -> list[int]:
    """Check each looping timer's events; return the span deviations.

    A timer alternates between Start and End, as often as it loops, until a state
    cancels it. A timer that is on then ends a cycle later, as the firmware reports the
    events of a state's actions in the next cycle (see
    test_cancelled_timer_ends_in_the_cancel_cycle), and nothing follows.
    """
    cancelled = _first_cancels(case, visits)
    deviations = []
    for i, timer in case.fsm.global_timers.items():
        if not timer.loop:
            continue
        edges = [(e.value, e.cycle) for e in events if e.channel == f'GlobalTimer{i}']
        levels = [value for value, _ in edges]
        assert levels == ([1, 0] * len(levels))[: len(levels)], (
            f'timer {i} events out of order: {levels[:6]}...'
        )
        natural = edges
        if (cancel := cancelled.get(i)) is not None:
            natural = [(value, t) for value, t in edges if t <= cancel]
            on = bool(natural) and natural[-1][0] == 1
            after = edges[len(natural) :]
            assert after == ([(0, cancel + 1)] if on else []), (
                f'timer {i} cancelled in cycle {cancel}, then {after}'
            )
            starts = levels.count(1)
            assert timer.loop == 1 or starts <= timer.loop, f'timer {i} over-looped'
        elif timer.loop > 1:
            assert levels == [1, 0] * timer.loop, (
                f'timer {i} ran {levels.count(1)} times, not {timer.loop}'
            )
        nominal = _cycles(timer.duration, limits), _cycles(timer.loop_interval, limits)
        for n, ((_, a), (_, b)) in enumerate(pairwise(natural)):
            deviations.append(b - a - nominal[n % 2])
    return deviations


def _check_counters(
    case: _Case, visits: list[_Visit], events: list[_Event], trial_end: int
) -> None:
    """Check that each global counter ends whenever it reaches its threshold.

    A state resets its counter as it begins. The firmware checks the threshold before
    counting a cycle's events, so a counter ends in the cycle after the event reaching
    it (see test_counter_ends_with_the_counted_event), unless reset in that cycle.
    """
    resets = _actions(case, visits, 'GlobalCounterReset')
    for j, counter in case.fsm.global_counters.items():
        counted = Counter(e.cycle for e in events if e.name == counter.event)
        reset_at = {t for t, reset in resets if reset == j}
        expected, count = [], 0
        for t in sorted(counted.keys() | reset_at):
            count += counted[t]
            if t in reset_at:
                count = 0
            elif count == counter.threshold and t < trial_end:
                expected.append(t + 1)
        ends = [e.cycle for e in events if e.name == f'GlobalCounter{j}_End']
        assert ends == expected, f'counter {j} ended in cycles {ends}, not {expected}'


def _check_trial(case: _Case, frame: pl.DataFrame, limits: _Limits) -> _Trial:
    """Check one trial's data against its machine.

    The deviations returned are those of all measured spans from their nominal
    lengths, in cycles, for calibrating TOLERANCE_CYCLES.
    """
    types: list[str] = frame['Type'].to_list()
    for kind in ('TrialStart', 'TrialEnd', 'TrialEndControl'):
        assert types.count(kind) == 1, (
            f'{types.count(kind)} {kind} rows (truncated trial?)'
        )
    times: list[int] = frame['Time'].dt.epoch('us').to_list()
    # TrialEndControl is on the Bpod's µs clock, the other rows count cycles
    for row, (a, b) in enumerate(pairwise(times), 1):
        assert a <= b or types[row] == 'TrialEndControl', (
            f'time runs backwards at row {row}:\n{frame.slice(row - 3, 6)}'
        )
    assert frame['State Machine'][0] == case.fsm.hash.hex(), 'data of another machine'
    states: list[str | None] = frame['State'].to_list()
    unknown = {state for state in states if state is not None} - set(case.fsm.states)
    assert not unknown, f'unknown states {unknown}'

    end_row = types.index('TrialEnd')
    start, end = times[types.index('TrialStart')], times[end_row]
    cycles = [round((t - start) / limits.cycle_us) for t in times]
    visits = _visits(types, states, cycles)
    trial_cycles = cycles[end_row]
    columns = (frame[c].to_list() for c in ('Event', 'Channel', 'Value'))
    events = [
        _Event(str(name), channel, value, t)
        for kind, name, channel, value, t in zip(types, *columns, cycles, strict=True)
        if kind == 'InputEvent'
        and channel != 'SoftCodeApp'  # sent as extra traffic: no state reacts to them
        and not (channel == 'SoftCode' and value == limits.spare_softcode)
    ]
    deviations: list[int] = []
    if case.path is not None:
        assert [visit.state for visit in visits] == case.path, (
            'visits differ from the path'
        )
        for visit in visits:
            if _answer(case, visit.state, limits) is None:  # else ended by the answer
                assert visit.cycles == _nominal(case, visit.state, limits), (
                    f'{visit.state} lasted {visit.cycles} cycles, not its timer'
                )
        spent = sum(visit.cycles for visit in visits)
        assert trial_cycles == spent, f'trial of {trial_cycles} cycles, not {spent}'
    else:
        for visit in visits:
            target = case.fsm.states[visit.state].transitions.get('Tup')
            timer = _nominal(case, visit.state, limits)
            if target not in (None, visit.state):
                assert visit.cycles <= timer + TOLERANCE_CYCLES, (
                    f'{visit.state} outlived its timer: {visit.cycles} cycles'
                )
        deviations.append(trial_cycles - _nominal_trial(case, limits))
    deviations += _check_timers(case, visits, events, limits)
    _check_counters(case, visits, events, trial_cycles)
    _check_answers(case, visits, events, limits)
    _check_loopbacks(case, visits, events, limits, trial_cycles)
    assert all(abs(d) <= TOLERANCE_CYCLES for d in deviations), (
        f'spans off by {sorted(set(deviations))} cycles'
    )
    softcodes, app_softcodes = _check_outputs(case, frame, visits, limits.leds)
    return _Trial(
        softcodes,
        deviations,
        start,
        end,
        times[types.index('TrialEndControl')],
        len(events),
        app_softcodes,
    )


def _softcode_mismatch(received: list[int], results: list[_Trial]) -> str:
    """Where the softcodes received first differ from those recorded, by trial."""
    recorded = [
        (i, n, code)
        for i, t in enumerate(results)
        for n, code in enumerate(t.softcodes)
    ]
    codes = [code for _, _, code in recorded]
    index = next(
        (
            k
            for k, (got, code) in enumerate(zip(received, codes, strict=False))
            if got != code
        ),
        min(len(received), len(codes)),
    )
    if index == len(recorded):
        return f'{len(received) - index} softcode(s) received beyond those recorded'
    trial, n, code = recorded[index]
    got = received[index] if index < len(received) else 'nothing'
    return (
        f'softcode {code} of trial {trial} (its softcode {n + 1} of '
        f'{len(results[trial].softcodes)}) was received as {got}; '
        f'{len(received)} received, {len(recorded)} recorded'
    )


class _Batch:
    """Runs a case's machines on a worker thread and collects their trial data."""

    def __init__(
        self,
        bpod: Bpod,
        cases: list[_Case],
        mode: str,
        spare_softcode: int,
        app_port: serial.Serial | None = None,
    ) -> None:
        self.bpod, self.cases, self.mode = bpod, cases, mode
        self.spare_softcode = spare_softcode
        self.app_port = app_port
        self.app_received = bytearray()
        """The app softcodes received on the app port."""
        self.frames: list[pl.DataFrame] = []
        self.current = 0
        """Index of the machine being run, or queued."""
        self.cancelled = threading.Event()
        self.wall = self.cpu = 0.0

    def _send_spare_softcodes(self, done: threading.Event) -> None:
        """Send spare softcodes, also as an app, every 0.5-5 ms while a trial runs."""
        rng = random.Random()  # noqa: S311 - timing only, never part of a check
        message = bytes((ord('~'), self.spare_softcode))
        while not done.wait(rng.uniform(0.0005, 0.005)):
            if self.bpod.is_running:  # send_softcode() would warn between trials
                self.bpod.serial0.write(message)
                if self.app_port is not None:  # one of the app's softcode events
                    self.app_port.write(bytes((rng.randrange(self.spare_softcode),)))

    def _read_app_port(self, done: threading.Event) -> None:
        """Collect the app softcodes, including those still arriving after `done`."""
        port = self.app_port
        assert port is not None
        while not done.is_set():
            self.app_received += port.read(max(1, port.in_waiting))
        time.sleep(0.1)  # the last trial's, still in the USB buffers
        self.app_received += port.read(port.in_waiting)

    def run(self) -> None:
        bpod = self.bpod
        done = threading.Event()
        threads = [threading.Thread(target=self._send_spare_softcodes, args=(done,))]
        if self.app_port is not None:
            threads.append(threading.Thread(target=self._read_app_port, args=(done,)))
        for thread in threads:
            thread.start()
        wall, cpu = time.perf_counter(), time.process_time()
        try:
            for index, case in enumerate(self.cases):
                if self.cancelled.is_set():
                    break
                self.current = index
                bpod.run(case.fsm)  # queued: returns once the machine is sent
                if self.mode == 'paced':
                    bpod.wait()
            bpod.wait()  # queued: the last machine; paced: nothing left
        finally:
            self.wall, self.cpu = time.perf_counter() - wall, time.process_time() - cpu
            done.set()
            for thread in threads:
                thread.join()
        # the machines run so far, collected outside the timing
        self.frames = [bpod.get_data(concat=False) for _ in range(self.current + 1)]


def _known_issues(level: str, mode: str) -> list[pytest.MarkDecorator]:
    """The known firmware issues a level fails in a mode, on the stock firmware."""
    if level == 'cycles' and mode != 'paced':
        # a queued machine without `>back` after one with it never ends
        return [pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/3')]
    if level in ('softcodes', 'big', 'mixed') and mode == 'analog':
        # loading a large queued machine in the timer interrupt loses cycles, which
        # delays the analog samples against the trials (no pull request yet)
        return [pytest.mark.known_firmware_issue()]
    return []


@pytest.mark.parametrize(
    ('level', 'mode'),
    [
        pytest.param(
            level, mode, id=f'{level}-{mode}', marks=_known_issues(level, mode)
        )
        for level in LEVELS
        for mode in ('paced', 'queued', 'analog')
    ],
)
def test_random_machines(  # noqa: PLR0917 - fixtures
    level: str,
    mode: str,
    fuzzer: _Fuzzer,
    loopbacks: dict[str, str],
    app_port: serial.Serial | None,
    logged_warnings,
    record_property,
    run_guarded,
    request: pytest.FixtureRequest,
):
    """Random machines of one level run back to back, their data checked afterwards.

    Their states and timers also set the outputs wired to inputs, but not Flex1 in
    'analog' mode, which runs queued while recording it (and skips without FlexIO
    channels).
    """
    bpod, limits = fuzzer.bpod, fuzzer.limits
    wired = dict(loopbacks)
    check_analog = None
    if mode == 'analog':
        check_analog = request.getfixturevalue('analog_recording')
        wired.pop('Flex1', None)
    elif 'Flex1' in wired:
        _wire_flex(bpod.flex_io)
    limits = limits._replace(loopbacks=wired)
    rng = random.Random(f'{fuzzer.seed}:{level}:{mode}')  # noqa: S311
    cases = [LEVELS[level](rng, limits) for _ in range(N_TRIALS)]
    batch = _Batch(bpod, cases, mode, limits.spare_softcode, app_port)
    expected_s = _seconds(sum(_nominal_trial(case, limits) for case in cases), limits)

    def describe(index: int) -> str:
        """Name the failing trial; dump the batch's machines for replay, in order.

        A queued failure may depend on the machines before it, so the batch is dumped.
        """
        for i, case in enumerate(cases):
            path = fuzzer.dump_dir / f'{level}-{mode}-{i:02d}.json'
            case.fsm.to_file(path, overwrite=True)
        where = f'seed {fuzzer.seed}, {level}/{mode}, trial {index}'
        return f'{where}; machines dumped to {fuzzer.dump_dir}'

    timeout = 2 * expected_s + 0.05 * N_TRIALS + 2
    run_guarded(
        batch.run,
        timeout,
        describe=lambda: describe(batch.current),
        cancel=batch.cancelled.set,
    )
    assert len(batch.frames) == N_TRIALS
    trials = [frame['Trial'][0] for frame in batch.frames]
    assert trials == list(range(trials[0], trials[0] + N_TRIALS)), 'trial numbers'

    results: list[_Trial] = []
    for i, (case, frame) in enumerate(zip(cases, batch.frames, strict=True)):
        try:
            results.append(_check_trial(case, frame, limits))
        except AssertionError as e:
            raise AssertionError(describe(i)) from e
    recorded = [code for trial in results for code in trial.softcodes]
    cycles = [
        limits.cycle_us * (t.control - t.start) / (t.end - t.start) for t in results
    ]
    n_states = [len(case.fsm.states) for case in cases]
    n_events = [trial.n_events for trial in results]
    record_property('states', f'{min(n_states)}-{max(n_states)}')
    record_property('events', f'{min(n_events)}-{max(n_events)}')
    record_property('softcodes', len(recorded))
    record_property('app', sum(len(trial.app_softcodes) for trial in results))
    record_property(
        'gap', f'{max(b.start - a.end for a, b in pairwise(results)):.0f} µs'
    )
    record_property('cycle', f'{max(cycles):.2f} µs')
    record_property('host CPU', f'{batch.cpu / batch.wall:.0%}')
    record_property('span dev', sorted({d for t in results for d in t.deviations}))

    if fuzzer.softcodes != recorded:
        where = describe(0)  # dumps the machines
        recorded_by_trial = [trial.softcodes for trial in results]
        softcodes = {'received': fuzzer.softcodes, 'recorded': recorded_by_trial}
        (fuzzer.dump_dir / 'softcodes.json').write_text(json.dumps(softcodes))
        pytest.fail(f'{_softcode_mismatch(fuzzer.softcodes, results)}: {where}')
    sent_app = bytes(code for trial in results for code in trial.app_softcodes)
    if bytes(batch.app_received) != sent_app:
        received = bytes(batch.app_received)
        first = next(
            (
                i
                for i, (a, b) in enumerate(zip(received, sent_app, strict=False))
                if a != b
            ),
            min(len(received), len(sent_app)),
        )
        pytest.fail(
            f'app port received {len(received)} app softcodes, {len(sent_app)} '
            f'recorded; first difference at {first}: {describe(0)}'
        )
    if check_analog is not None:
        check_analog(pl.concat(batch.frames))
    # e.g. the library's timing violation, but not a late start: machines are sent as
    # fast as they can be, also behind short trials, and the table shows the gaps
    if warnings := [w for w in logged_warnings() if ' ms late: ' not in w]:
        worst = cycles.index(max(cycles))
        pytest.fail(
            f'{warnings}\nworst cycle {cycles[worst]:.2f} µs: {describe(worst)}'
        )


def test_watchdog_stops_a_runaway_machine(fuzzer: _Fuzzer, run_guarded):
    """A machine that never ends is stopped by the watchdog; the Bpod stays usable."""
    bpod = fuzzer.bpod
    forever = StateMachine()
    forever.add_state('forever')  # no transitions: runs until stopped
    batch = _Batch(bpod, [_Case(forever, None)], 'paced', fuzzer.limits.spare_softcode)
    with pytest.raises(
        pytest.fail.Exception, match=r'within 0\.1 s: a state without transitions'
    ):
        run_guarded(
            batch.run,
            0.1,
            describe=lambda: 'a state without transitions',
            cancel=batch.cancelled.set,
        )
    assert not bpod.is_running
    assert batch.frames[0]['Type'].to_list().count('TrialEnd') == 1  # reported normally
    chain = _random_chain(random.Random(0), fuzzer.limits, 2, (10, 10))  # noqa: S311
    bpod.run(chain.fsm)
    bpod.wait()
    _check_trial(chain, bpod.get_data(), fuzzer.limits)
