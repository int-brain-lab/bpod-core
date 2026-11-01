"""Upstream firmware bugs, most fixed in a pull request the test's marker links to."""

import random
import statistics
from time import perf_counter_ns, sleep

import pytest
import serial

from bpod_core.bpod._compiler import _name_channels
from bpod_core.bpod.constants import (
    CHANNEL_TYPES_INPUT,
    FlexIOChannelType,
    FlexIOThresholdPolarity,
)
from bpod_core.fsm import StateMachine

from ._helpers import (
    FIRMWARE_PRS,
    _cancel_trial,
    _chain,
    _counter_trial,
    _large,
    _probe_module,
    _pulse,
    _request_identification,
    _timed,
    _timer_across_states,
    _timer_trial,
    _trial_events,
    _trial_gaps,
)


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/2')
def test_deferred_state_machine_start(bpod, fuzz_seed):
    """Every state machine queued during a trial starts once that trial ends.

    The bug needs a trial to end while the next state machine is being transferred.
    Trials of a few cycles hit it within 200 trials on a Bpod 2.5; the fix measured
    1 in 300 to 1 in 1000 trials of a few milliseconds on a 2+.
    """
    rng = random.Random(f'{fuzz_seed}:deferred')  # noqa: S311
    fsm = StateMachine()
    fsm.add_state('A', transitions={'Tup': '>exit'})

    try:
        for _ in range(500):
            fsm.states['A'].timer = rng.uniform(0, 0.0005)
            bpod.run(fsm)  # sent while the previous trial runs
        bpod.wait(timeout=0.1)
    except TimeoutError:  # from run(), waiting for the machine before to start
        pytest.fail('a queued state machine never started')


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/3')
def test_back_flag(bpod, run_guarded):
    """A state machine queued after one using `>back` doesn't inherit `>back`."""
    a = StateMachine()  # uses '>back', ends after 60 ms
    a.set_global_timer(index=0, duration=0.06)
    a.add_state('Start', transitions={'Tup': 'A'}, actions={'GlobalTimerTrig': 0})
    a.add_state('A', timer=0.01, transitions={'Tup': 'B', 'GlobalTimer0_End': '>exit'})
    a.add_state(
        'B', timer=0.01, transitions={'Tup': '>back', 'GlobalTimer0_End': '>exit'}
    )
    b = StateMachine()  # doesn't use '>back'
    b.add_state('C', transitions={'Tup': '>exit'})

    def trials() -> None:
        bpod.run(a)
        bpod.run(b)  # with the bug, trial a never ends: this never returns
        bpod.wait()

    run_guarded(trials, timeout=0.15)


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/6')
def test_global_timer_on_flex_output(bpod, flex_loopback):
    """A state change leaves a Flex output driven by a global timer alone."""
    edges = _timed(_trial_events(bpod, _timer_across_states('Flex1')), 'Flex4_')
    assert [name for name, _ in edges] == ['Flex4_High', 'Flex4_Low'], edges
    (_, on), (_, off) = edges
    assert abs(off - on - 200) <= 2  # the timer's 20 ms, in cycles


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/7')
def test_analog_threshold_tables(bpod, flex_io):
    """A state machine arms no analog threshold unless one of its states does."""
    flex_io['Flex1'].channel_type = FlexIOChannelType.ANALOG_INPUT
    threshold = flex_io['Flex1'].thresholds[0]
    threshold.polarity = FlexIOThresholdPolarity.FALLING
    threshold.voltage = 5000  # crossed by any reading
    a = StateMachine()  # arms the threshold, which fires and disarms
    a.add_state(
        'A',
        timer=0.05,
        transitions={'Flex1_Trig0': '>exit', 'Tup': '>exit'},
        actions={'AnalogThreshEnable': 1},
    )
    b = StateMachine()  # arms nothing
    b.add_state('A', timer=0.05, transitions={'Tup': '>exit'})
    bpod.run(a)
    bpod.wait()
    assert [event for _, event, _ in _trial_events(bpod, b)] == ['Tup', 'TrialEnd']


# without the fix, the Flex input ignores its wire until the Bpod is power-cycled
@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/11', run=False)
def test_flex_input_virtual_event_lasts_one_trial(bpod, flex_loopback):
    """After a trial with a virtual event on a Flex input, the input reads its wire.

    A virtual event overrides the input until the next state machine starts. The
    firmware cleared the override only for the inputs after the Flex inputs, so a Flex
    input ignored its hardware from then on.
    """
    inputs = _name_channels(bpod._hardware.input_description, CHANNEL_TYPES_INPUT)
    flex4 = [name for _, name in inputs].index('Flex4')

    def send() -> None:  # a virtual event, as Input.override sends it
        sleep(0.005)  # once the trial runs
        bpod.serial0.write_struct('<c2B', b'V', flex4, 1)

    wait = _chain(('Wait', 0.05, {}))
    wait.states['Wait'].transitions['Flex4_High'] = '>exit'
    assert 'Flex4_High' in [e for _, e, _ in _trial_events(bpod, wait, during=send)]
    assert _timed(_trial_events(bpod, _pulse('Flex1')), 'Flex4') == [
        ('Flex4_High', 21),
        ('Flex4_Low', 71),
    ]


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/14')
def test_global_counter_counts_tup(bpod):
    """A global counter attached to Tup counts it."""
    fsm = StateMachine()
    fsm.set_global_counter(0, event='Tup', threshold=1)
    fsm.add_state('A', timer=0.001, transitions={'Tup': 'B'})
    fsm.add_state(
        'B', timer=0.01, transitions={'GlobalCounter0_End': '>exit', 'Tup': '>exit'}
    )
    assert 'GlobalCounter0_End' in [event for _, event, _ in _trial_events(bpod, fsm)]


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/15')
def test_clock_reset_acknowledged_at_once(bpod):
    """A session clock reset is acknowledged without waiting for the USB flush timer.

    The unflushed acknowledgement takes a round trip of about 200 µs instead of 100 µs
    on a Teensy 4 (Bpod 2.5 and 2+).
    """
    round_trips = []
    for _ in range(20):
        start = perf_counter_ns()
        assert bpod.reset_session_clock()
        round_trips.append(perf_counter_ns() - start)
    assert statistics.median(round_trips) < 150_000


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/18', run=False)  # hangs the Bpod
def test_command_split_across_usb_packets(bpod):
    """A command whose bytes arrive in separate USB packets is answered.

    Without the fix, a Teensy 4 Bpod (2.5, 2+) waits for the missing bytes forever and
    has to be power-cycled, so the test only runs with --fixed-firmware.
    """
    bpod.serial0.write(b'I')  # read an input: the opcode, ...
    sleep(0.01)
    bpod.serial0.write(bytes([bpod.inputs['Port1'].index]))  # ... then its channel
    with bpod.serial0.temporary_timeout(0.2):
        assert len(bpod.serial0.read(1)) == 1


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/21')
def test_analog_sampling_starts_with_the_first_trial(bpod, flex_io):
    """After a session without analog inputs, the next one samples from its first trial.

    The first trial after a session clock reset turns analog sampling on, also without
    analog inputs, and only the stop command turns it off. A session with analog inputs
    that follows samples before its first trial, and one time in ten gets an extra
    sample at its start: all its analog timestamps are then a sample period late.
    """
    trial = StateMachine()
    trial.add_state('A', timer=0.01, transitions={'Tup': '>exit'})
    bpod.run(trial)  # a session without analog inputs
    bpod.wait()
    bpod.close()
    bpod.open()
    bpod.flex_io['Flex1'].channel_type = FlexIOChannelType.ANALOG_INPUT
    try:
        with serial.Serial(bpod._flexio_serial.port, timeout=0.2) as port:
            assert port.read(4) == b'', 'analog samples before the first trial'
    finally:
        bpod.run(trial)  # records, so that closing stops the sampling
        bpod.wait()


@pytest.mark.known_firmware_issue()
def test_queued_large_machine_starts_on_time(bpod, run_guarded):
    """A large queued machine starts one cycle after the trial before it ends.

    The firmware loaded a queued machine in the timer interrupt when the trial before
    it ended, losing the cycles it took for a large machine.
    """
    fsm = _large()

    def trials() -> None:
        for _ in range(10):
            bpod.run(fsm)  # loaded while the previous trial runs
        bpod.wait()

    run_guarded(trials, timeout=5)
    gaps = _trial_gaps(bpod.get_data())
    cycle = bpod._hardware.cycle_period_us
    assert max(gaps) < 1.5 * cycle, f'µs from each trial end to the next start: {gaps}'


@pytest.mark.known_firmware_issue()
def test_timer_starts_in_the_trigger_cycle(bpod):
    """A global timer that a state starts at once reports its start in that cycle.

    The firmware reports it a cycle late, so the start and end are a cycle short of
    the duration apart.
    """
    fsm = StateMachine()
    fsm.set_global_timer(0, duration=0.003)
    assert _timed(_trial_events(bpod, _timer_trial(fsm)), 'GlobalTimer') == [
        ('GlobalTimer0_Start', 0),
        ('GlobalTimer0_End', 30),
    ]


@pytest.mark.known_firmware_issue()
def test_cancelled_timer_ends_in_the_cancel_cycle(bpod):
    """A cancelled global timer reports its end in the cycle of the cancel.

    The firmware reports it a cycle late.
    """
    ends = _timed(_trial_events(bpod, _cancel_trial()), 'GlobalTimer0_End')
    assert ends == [('GlobalTimer0_End', 30)]  # when B begins


@pytest.mark.known_firmware_issue()
def test_counter_ends_with_the_counted_event(bpod):
    """A global counter reports its threshold in the cycle of the event reaching it.

    The firmware reports it a cycle late.
    """
    events = _trial_events(bpod, _counter_trial())
    fifth = _timed(events, 'GlobalTimer1_End')[4][1]
    assert _timed(events, 'GlobalCounter') == [('GlobalCounter0_End', fifth)]


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/22')
def test_global_timer_without_events(bpod):
    """A global timer set not to send events sends none, also if it doesn't loop.

    The firmware applies the setting to looping timers only.
    """
    fsm = StateMachine()
    fsm.set_global_timer(0, duration=0.003, send_events=False)
    assert _timed(_trial_events(bpod, _timer_trial(fsm)), 'GlobalTimer') == []


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/4')
def test_module_relay_off_after_reconnecting(bpod, module):
    """Connecting turns every module relay off.

    The firmware turns off only the relay of the channel its scratch variable last
    held, here that of the input read before disconnecting.
    """
    module.set_relay(True)
    bpod.inputs['Port1'].read()  # leaves the input's index in the scratch variable
    bpod.close()
    bpod.open()
    try:
        assert _probe_module(bpod, module) == b'', 'the module is still relayed'
    finally:
        module.set_relay(False)  # else it stays on, the fix missing, until power-off


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/20')
def test_module_bytes_cleared_at_trial_start(bpod, module_beyond_port1):
    """A trial starts without the bytes that modules sent before it.

    The firmware moves on to the next module by counting the bytes it discards, so it
    clears only the module on port 1, unless that one sent one or two bytes. A module
    further along keeps its bytes, which turn into events once the trial starts.
    """
    module = module_beyond_port1  # its answer to the identification stays on the Bpod
    assert _probe_module(bpod, module) == b'', f'the relay of {module} is on'
    events = _trial_events(bpod, _chain(('A', 0.01, {})))
    assert [event for _, event, _ in events] == ['Tup', 'TrialEnd']


@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/13')
def test_module_byte_zero_is_no_other_modules_event(bpod, module_beyond_port1):
    """A module's byte 0 is no event of another module.

    The firmware takes byte N from a module as its event N, counting from 1, but also
    takes 0: as the last event of the module port before, or on port 1 as event code
    255, which marks the end of a trial. Asked to identify itself during a trial, a
    module sends zeros in its firmware version. Only ports 2 and up are tested, since
    on port 1 the trial would seem to end on the host.
    """
    module = module_beyond_port1

    def ask() -> None:
        sleep(0.005)  # once the trial runs
        _request_identification(bpod, module)

    trial = _trial_events(bpod, _chain(('A', 0.05, {})), during=ask)
    events = [event for _, event, _ in trial]
    senders = {event.rpartition('_')[0] for event in events[:-2]}  # not Tup, TrialEnd
    assert senders == {module.name}, f'module events {events[:-2]}'
