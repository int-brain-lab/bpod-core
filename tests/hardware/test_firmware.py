"""The Bpod firmware's features, one small test each: a baseline for firmware changes.

Most tests run a short trial and check the events the Bpod reports, timed in cycles
from the trial's start. They pass on the stock firmware.
"""

import random
import struct
import time

import polars as pl
import pytest
import serial

from bpod_core.fsm import StateMachine

from ._helpers import (
    _busy_timers,
    _cancel_trial,
    _chain,
    _counted,
    _counter_trial,
    _large,
    _largest,
    _probe_module,
    _pulse,
    _timed,
    _timer_across_states,
    _timer_trial,
    _trial_events,
    _trial_gaps,
)


class TestStates:
    """States, their timers and `>back`."""

    def test_state_timer(self, bpod):
        """A state's timer ends it after exactly its duration."""
        events = _trial_events(bpod, _chain(('A', 0.005, {})))
        assert events == [('A', 'Tup', 50), (None, 'TrialEnd', 50)]

    def test_state_chain(self, bpod):
        """States follow each other on their timers, each for its own duration."""
        events = _trial_events(
            bpod, _chain(('A', 0.002, {}), ('B', 0.003, {}), ('C', 0.0001, {}))
        )
        assert events == [
            ('A', 'Tup', 20),
            ('B', 'Tup', 50),
            ('C', 'Tup', 51),
            (None, 'TrialEnd', 51),
        ]

    def test_zero_timer(self, bpod):
        """A state with a timer of 0 lasts one cycle."""
        events = _trial_events(bpod, _chain(('A', 0, {}), ('B', 0.001, {})))
        assert events == [('A', 'Tup', 1), ('B', 'Tup', 11), (None, 'TrialEnd', 11)]

    def test_back(self, bpod):
        """`>back` returns to the state visited before."""
        fsm = StateMachine()
        fsm.set_global_timer(0, duration=0.0045)
        end = {'GlobalTimer0_End': '>exit'}
        fsm.add_state(
            'Arm',
            timer=0.0001,
            transitions={'Tup': 'A'},
            actions={'GlobalTimerTrig': 0},
        )
        fsm.add_state('A', timer=0.001, transitions={'Tup': 'B', **end})
        fsm.add_state('B', timer=0.001, transitions={'Tup': '>back', **end})
        visits = [
            state for state, event, _ in _trial_events(bpod, fsm) if event == 'Tup'
        ]
        assert visits == ['Arm', 'A', 'B', 'A', 'B']

    def test_largest_machine(self, bpod):
        """A machine of as many states as the Bpod takes visits them all."""
        n_states = bpod._hardware.max_states - 1  # state 255 stands for `>exit`
        events = _trial_events(
            bpod, _chain(*((f's{i}', 0.0001, {}) for i in range(n_states)))
        )
        assert [state for state, event, _ in events if event == 'Tup'] == [
            f's{i}' for i in range(n_states)
        ]
        assert events[-1] == (None, 'TrialEnd', n_states)


class TestGlobalTimers:
    """Global timers."""

    def test_global_timer(self, bpod):
        """A global timer starts when triggered and ends its duration after the trigger.

        When its start is reported: see test_timer_starts_in_the_trigger_cycle.
        """
        fsm = StateMachine()
        fsm.set_global_timer(0, duration=0.003)
        timed = _timed(_trial_events(bpod, _timer_trial(fsm)), 'GlobalTimer')
        assert [event for event, _ in timed] == [
            'GlobalTimer0_Start',
            'GlobalTimer0_End',
        ]
        assert timed[-1] == ('GlobalTimer0_End', 30)

    def test_global_timer_onset_delay(self, bpod):
        """A global timer with an onset delay starts after it."""
        fsm = StateMachine()
        fsm.set_global_timer(0, duration=0.003, onset_delay=0.002)
        events = _trial_events(bpod, _timer_trial(fsm))
        assert _timed(events, 'GlobalTimer') == [
            ('GlobalTimer0_Start', 20),
            ('GlobalTimer0_End', 50),
        ]

    def test_global_timer_loops(self, bpod):
        """A looping global timer repeats its duration and interval, as often as set."""
        fsm = StateMachine()
        fsm.set_global_timer(0, duration=0.001, loop=3, loop_interval=0.002)
        timed = _timed(_trial_events(bpod, _timer_trial(fsm)), 'GlobalTimer')
        assert [event for event, _ in timed] == [
            'GlobalTimer0_Start',
            'GlobalTimer0_End',
        ] * 3
        # the first start: see test_timer_starts_in_the_trigger_cycle
        assert [cycle for _, cycle in timed[1:]] == [10, 30, 40, 60, 70]

    def test_global_timer_cancel(self, bpod):
        """Cancelling a global timer ends it early.

        In which cycle: see test_cancelled_timer_ends_in_the_cancel_cycle.
        """
        ends = _timed(_trial_events(bpod, _cancel_trial()), 'GlobalTimer0_End')
        assert [cycle for _, cycle in ends] in ([30], [31])

    def test_global_timer_onset_trigger(self, bpod):
        """A global timer's onset triggers the timers in its onset trigger."""
        fsm = StateMachine()
        fsm.set_global_timer(0, duration=0.002, onset_trigger=1 << 1)
        fsm.set_global_timer(1, duration=0.004)
        timed = dict(_timed(_trial_events(bpod, _timer_trial(fsm)), 'GlobalTimer'))
        assert timed['GlobalTimer1_Start'] == timed['GlobalTimer0_Start']
        assert (timed['GlobalTimer0_End'], timed['GlobalTimer1_End']) == (20, 40)

    def test_global_timer_retrigger_restarts(self, bpod):
        """Triggering a running global timer again starts its duration over."""
        fsm = StateMachine()
        fsm.set_global_timer(0, duration=0.005)
        trig = {'GlobalTimerTrig': 0}
        fsm.add_state('A', timer=0.002, transitions={'Tup': 'B'}, actions=trig)
        fsm.add_state('B', timer=0.01, transitions={'Tup': '>exit'}, actions=trig)
        events = _timed(_trial_events(bpod, fsm), 'GlobalTimer0_End')
        assert events == [('GlobalTimer0_End', 70)]

    def test_global_timer_loops_until_cancelled(self, bpod):
        """A global timer looping without a count loops until cancelled."""
        fsm = StateMachine()
        fsm.set_global_timer(0, duration=0.0005, loop=1, loop_interval=0.0005)
        fsm.add_state(
            'A', timer=0.005, transitions={'Tup': 'B'}, actions={'GlobalTimerTrig': 0}
        )
        fsm.add_state(
            'B',
            timer=0.005,
            transitions={'Tup': '>exit'},
            actions={'GlobalTimerCancel': 0},
        )
        events = _timed(_trial_events(bpod, fsm), 'GlobalTimer0')
        assert len(events) >= 9
        assert max(cycle for _, cycle in events) <= 51  # none after the cancel

    def test_looping_global_timer_without_events(self, bpod):
        """A looping timer without events still runs: a condition sees it."""
        fsm = StateMachine()
        fsm.set_global_timer(
            0,
            duration=0.005,
            onset_delay=0.002,
            loop=2,
            loop_interval=0.001,
            send_events=False,
        )
        fsm.set_condition(0, channel='GlobalTimer0', value=True)
        fsm.add_state(
            'A',
            timer=0.02,
            transitions={'Condition0': '>exit', 'Tup': '>exit'},
            actions={'GlobalTimerTrig': 0},
        )
        events = _trial_events(bpod, fsm)
        assert _timed(events, 'GlobalTimer') == []
        assert events[0][1] == 'Condition0'


class TestGlobalCounters:
    """Global counters."""

    def test_global_counter(self, bpod):
        """A global counter reports reaching its threshold of counted events.

        In which cycle: see test_counter_ends_with_the_counted_event.
        """
        events = _trial_events(bpod, _counter_trial())
        ends = [cycle for event, cycle in _timed(events, 'GlobalTimer1_End')]
        counter = _timed(events, 'GlobalCounter')
        assert counter in (
            [('GlobalCounter0_End', ends[4])],
            [('GlobalCounter0_End', ends[4] + 1)],
        )

    def test_global_counter_reset(self, bpod):
        """Resetting a global counter starts its count over."""
        fsm = _counted(StateMachine())
        fsm.add_state(
            'A', timer=0.003, transitions={'Tup': 'B'}, actions={'GlobalTimerTrig': 1}
        )
        fsm.add_state(
            'B',
            timer=0.02,
            transitions={'Tup': '>exit'},
            actions={'GlobalCounterReset': 0},
        )
        events = _trial_events(bpod, fsm)
        ends = [cycle for event, cycle in _timed(events, 'GlobalTimer1_End')]
        after_reset = [cycle for cycle in ends if cycle > 30]
        counter = [cycle for _, cycle in _timed(events, 'GlobalCounter')]
        assert counter in ([after_reset[4]], [after_reset[4] + 1])


class TestConditions:
    """Conditions."""

    def test_condition_on_global_timer(self, bpod):
        """A condition on a global timer moves on while the timer is active."""
        fsm = StateMachine()
        fsm.set_global_timer(0, duration=0.005, onset_delay=0.002)
        fsm.set_condition(0, channel='GlobalTimer0', value=True)
        fsm.add_state(
            'A',
            timer=0.02,
            transitions={'Condition0': 'B', 'Tup': '>exit'},
            actions={'GlobalTimerTrig': 0},
        )
        fsm.add_state('B', timer=0.001, transitions={'Tup': '>exit'})
        events = _trial_events(bpod, fsm)
        [(_, start)] = _timed(events, 'GlobalTimer0_Start')
        moves = [(s, c - start) for s, e, c in events if e == 'Condition0']
        assert moves in ([('A', 0)], [('A', 1)])  # in or a cycle after its start

    def test_condition_on_inactive_global_timer(self, bpod):
        """A condition on a global timer being inactive holds before it starts."""
        fsm = StateMachine()
        fsm.set_global_timer(0, duration=0.005)
        fsm.set_condition(0, channel='GlobalTimer0', value=False)
        fsm.add_state('A', timer=0.002, transitions={'Tup': 'B'})
        fsm.add_state(
            'B', timer=0.02, transitions={'Condition0': '>exit', 'Tup': '>exit'}
        )
        events = _trial_events(bpod, fsm)
        assert [(state, event) for state, event, _ in events][:2] == [
            ('A', 'Tup'),
            ('B', 'Condition0'),
        ]


class TestSoftcodes:
    """Softcodes, to and from the host and an app."""

    def test_softcodes_to_the_host(self, bpod):
        """A state's softcode reaches the host's softcode handler."""
        received: list[int] = []
        bpod.set_softcode_handler(received.append)
        _trial_events(
            bpod, _chain(('A', 0.001, {'SoftCode': 5}), ('B', 0.001, {'SoftCode': 7}))
        )
        assert received == [5, 7]

    def test_softcode_from_the_host(self, bpod):
        """A softcode from the host is an event that moves the state machine on."""
        fsm = StateMachine()
        fsm.add_state(
            'Wait', timer=1, transitions={'SoftCode3': '>exit', 'Tup': '>exit'}
        )
        events = _trial_events(bpod, fsm, during=lambda: bpod.send_softcode(3))
        assert [(state, event) for state, event, _ in events] == [
            ('Wait', 'SoftCode3'),
            (None, 'TrialEnd'),
        ]

    @pytest.fixture
    def app_port(self, bpod):
        """The Bpod's secondary USB serial port, as a third-party app opens it."""
        if bpod.serial1 is None:
            pytest.skip('needs a secondary USB serial port')
        with serial.Serial(bpod.serial1.port, timeout=0.3) as port:
            port.reset_input_buffer()
            yield port

    def test_softcode_to_the_app_port(self, bpod, app_port):
        """A state's app softcode is sent to the secondary port, as is."""
        _trial_events(bpod, _chain(('A', 0.001, {'SoftCodeApp': 9})))
        assert app_port.read(1) == bytes([9])
        assert app_port.read_all() == b''

    def test_softcode_from_the_app_port(self, bpod, app_port):
        """A byte from an app on the secondary port is an event."""
        fsm = StateMachine()
        fsm.add_state(
            'Wait', timer=0.3, transitions={'SoftCodeApp4': '>exit', 'Tup': '>exit'}
        )

        def send() -> None:
            time.sleep(0.01)
            app_port.write(bytes([4]))

        events = _trial_events(bpod, fsm, during=send)
        assert events[0][:2] == ('Wait', 'SoftCodeApp4')


class TestInputs:
    """Input channels."""

    @pytest.mark.parametrize('channel', ['Port1', 'TTLIn1'])
    def test_virtual_input_event(self, bpod, channel):
        """Overriding an input during a trial reports its edge as an event."""
        fsm = StateMachine()
        fsm.add_state(
            'Wait', timer=1, transitions={f'{channel}_High': '>exit', 'Tup': '>exit'}
        )

        def override() -> None:
            bpod.inputs[channel].override(False)
            bpod.inputs[channel].override(True)

        events = _trial_events(bpod, fsm, during=override)
        assert ('Wait', f'{channel}_High') in [
            (state, event) for state, event, _ in events
        ]

    def test_input_read(self, bpod):
        """Reading an input while idle answers with its level."""
        assert isinstance(bpod.inputs['Port1'].read(), bool)

    def test_disabled_input_ignores_virtual_events(self, bpod):
        """A disabled input reports no events, virtual ones included."""
        bpod.inputs['TTLIn1'].enabled = False
        fsm = StateMachine()
        fsm.add_state(
            'Wait', timer=0.05, transitions={'TTLIn1_High': '>exit', 'Tup': '>exit'}
        )

        def override() -> None:
            bpod.inputs['TTLIn1'].override(False)
            bpod.inputs['TTLIn1'].override(True)

        events = _trial_events(bpod, fsm, during=override)
        assert [(state, event) for state, event, _ in events] == [
            ('Wait', 'Tup'),
            (None, 'TrialEnd'),
        ]


class TestFlexLoopback:
    """With a wire from Flex1 to Flex4."""

    def test_global_timer_drives_flex_output(self, bpod, flex_loopback):
        """A global timer sets its channel while it runs."""
        fsm = StateMachine()
        fsm.set_global_timer(
            0, duration=0.003, onset_delay=0.002, channel='Flex1', value_on=1
        )
        fsm.add_state(
            'A',
            timer=0.01,
            transitions={'Tup': '>exit'},
            actions={'GlobalTimerTrig': 0},
        )
        assert _timed(_trial_events(bpod, fsm), 'Flex4') == [
            ('Flex4_High', 21),
            ('Flex4_Low', 51),
        ]

    def test_condition_on_flex_input(self, bpod, flex_loopback):
        """A condition on an input moves on once the input has its value."""
        fsm = StateMachine()
        fsm.set_condition(0, channel='Flex4', value=True)
        fsm.add_state('Off', timer=0.002, transitions={'Tup': 'On'})
        fsm.add_state(
            'On',
            timer=0.01,
            transitions={'Condition0': '>exit', 'Tup': '>exit'},
            actions={'Flex1': 1},
        )
        events = _trial_events(bpod, fsm)
        assert ('On', 'Condition0') in [(state, event) for state, event, _ in events]


class TestTTLLoopback:
    """With wires from TTLOut1 to TTLIn1, centre and shell."""

    def test_state_output(self, bpod, ttl_loopback):
        """TTLIn1 follows TTLOut1 a cycle later, for as long as a state sets it."""
        assert _timed(_trial_events(bpod, _pulse('TTLOut1')), 'TTLIn1') == [
            ('TTLIn1_High', 21),
            ('TTLIn1_Low', 71),
        ]

    def test_override_and_read(self, bpod, ttl_loopback):
        """An output the host sets is read back on the input wired to it."""
        for level in (True, False, True, False):
            bpod.outputs['TTLOut1'].override(level)
            assert bpod.inputs['TTLIn1'].read() is level

    def test_global_timer_output_outlasts_a_state(self, bpod, ttl_loopback):
        """A global timer's TTL output stays on across a state change.

        A state sets the channels it has no action for to 0, but not one a timer holds
        (unlike a FlexIO output, see test_global_timer_on_flex_output).
        """
        events = _trial_events(bpod, _timer_across_states('TTLOut1'))
        assert _timed(events, 'TTLIn1') == [
            ('TTLIn1_High', 1),
            ('TTLIn1_Low', 201),
        ]


class TestModule:
    """With a module on any module port."""

    def test_module_identified(self, module):
        """A module answers the identification with its name, firmware and events."""
        assert module.firmware_version > 0
        assert module.n_events > 0
        assert len(module.event_names) == module.n_events

    def test_module_identification_repeats(self, bpod, module):
        """Identifying the modules again finds the same module."""
        before = (module.name, module.firmware_version, module.event_names)
        bpod.update_modules()
        again = next(m for m in bpod.modules.values() if m.index == module.index)
        assert (again.name, again.firmware_version, again.event_names) == before

    def test_module_relay(self, bpod, module):
        """A module's bytes reach the host while, and only while, its relay is on."""
        module.set_relay(True)
        try:
            reply = _probe_module(bpod, module)
        finally:
            module.set_relay(False)
        assert reply[:5] == b'A' + struct.pack('<I', module.firmware_version), reply
        name_length = reply[5]
        assert module.name.startswith(reply[6 : 6 + name_length].decode())
        assert _probe_module(bpod, module) == b''


class TestTrials:
    """Running, queueing and stopping trials."""

    def test_stop_ends_the_trial(self, bpod):
        """Stopping a state without way out ends the trial, with its data."""
        fsm = StateMachine()
        fsm.add_state('Forever')
        bpod.run(fsm)
        bpod.stop_state_machine()
        data = bpod.get_data()
        assert data.filter(pl.col('Type') == 'TrialEnd').height == 1

    def test_queued_trials_follow_one_cycle_apart(self, bpod, logged_warnings):
        """A queued trial starts one cycle after the trial before, without warning."""
        fsm = _chain(('A', 0.003, {}))
        for _ in range(5):
            bpod.run(fsm)
        bpod.wait()
        gaps = _trial_gaps(bpod.get_data())
        cycle = bpod._hardware.cycle_period_us
        assert all(abs(gap - cycle) <= 2 for gap in gaps), f'µs between trials: {gaps}'
        assert logged_warnings() == []

    def test_machine_sent_while_the_one_before_loads(self, bpod, run_guarded):
        """Machines run back to back from idle, the first slow to load, both run.

        The Bpod holds one state machine besides the running one, so `run()` waits for
        the first to start before sending the second; sent earlier, the second reset
        the first one's confirmation and the event stream got out of step. One try in
        four or so got through regardless, so the test tries five times.
        """
        large, small = _large(), _chain(('A', 0.005, {}))

        def trials() -> None:
            for _ in range(5):
                bpod.run(large)  # from idle: returns once it is sent
                bpod.run(small)  # sent once the large machine has started
                bpod.wait()

        run_guarded(trials, timeout=2)
        data = bpod.get_data()
        assert (data['Type'] == 'TrialEnd').sum() == 10, 'trials read to their end'

    def test_largest_machine_runs_intact(self, bpod, run_guarded):
        """The largest state machine the Bpod takes runs as sent, alone and queued.

        Its trial ends every state in turn on its timer: a table loaded wrong, or a
        dead transition that fired, would leave that path.
        """
        fsm = _largest(bpod)

        def trials() -> None:
            for _ in range(4):
                bpod.run(fsm)  # the first from idle, the others queued
            bpod.wait()

        run_guarded(trials, timeout=5)
        tups = bpod.get_data().filter(pl.col('Event').cast(pl.String) == 'Tup')
        assert tups['State'].cast(pl.String).to_list() == list(fsm.states) * 4

    def test_queued_machine_loads_intact(self, bpod, fuzz_seed, run_guarded):
        """A machine loaded during a busy trial sends exactly its own softcodes.

        A firmware that loads queued machines outside the timer interrupt must not share
        the interrupt's working variables: an interrupt at the wrong moment changes the
        machine's outputs. The trial sends an event every cycle, and every state has a
        softcode, to make such a race likely; it can't make it certain.
        """
        rng = random.Random(f'{fuzz_seed}:intact')  # noqa: S311
        received: list[int] = []
        bpod.set_softcode_handler(received.append)
        machines = [_softcode_chain(rng) for _ in range(20)]

        def trials() -> None:
            for fsm in machines:
                bpod.run(fsm)  # loaded while the previous trial runs
            bpod.wait()

        run_guarded(trials, timeout=10)
        data = bpod.get_data()
        sent = data.filter(
            (pl.col('Type') == 'OutputAction') & (pl.col('Channel') == 'SoftCode')
        )
        assert received == sent['Value'].to_list(), 'softcodes differ from the machines'


def _softcode_chain(rng: random.Random) -> StateMachine:
    """A softcode from each of 250 states, a cycle each, with an event per cycle.

    Fifteen global timers looping every cycle keep the timer interrupt sending events
    while the next machine loads.
    """
    fsm = _busy_timers(StateMachine(), 1)
    names = [f's{i}' for i in range(250)]
    for name, target in zip(names, [*names[1:], '>exit'], strict=True):
        fsm.add_state(
            name,
            timer=1e-4,
            transitions={'Tup': target},
            actions={'SoftCode': rng.randint(1, 254)},
        )
    fsm.states['s0'].actions['GlobalTimerTrig'] = 0
    return fsm


class TestStatusLed:
    """The status LED."""

    def test_status_led(self, bpod):
        """The status LED can be switched off and on again."""
        assert bpod.set_status_led(False)
        assert bpod.set_status_led(True)
