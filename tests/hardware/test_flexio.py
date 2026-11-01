"""The FlexIO channels of a Bpod 2+: analog recording, thresholds and loopbacks.

The loopback tests need a wire from Flex1 to Flex4, and skip without it.
"""

import polars as pl
import pytest

from bpod_core.bpod import Bpod
from bpod_core.bpod.abc import AbstractFlexIO
from bpod_core.bpod.constants import (
    FlexIOChannelType,
    FlexIOThresholdMode,
    FlexIOThresholdPolarity,
)
from bpod_core.fsm import StateMachine

from ._helpers import FIRMWARE_PRS, _analog_data, _busy_timers, _chain

DURATIONS_S = (0.05, 0.0131, 0.2, 0.0011, 0.08, 0.001)
"""Trial lengths for the analog recording, the last one only ends the one before."""


@pytest.fixture
def analog_loopback(
    analog_recording, flex_io: AbstractFlexIO, loopbacks: dict[str, str]
) -> None:
    """Flex4 as analog output, wired to Flex1 as analog input; skips if unwired.

    Thresholds are only reliable on an analog input with no other kind before it, so
    the input is Flex1.
    """
    if 'Flex1' not in loopbacks:
        pytest.skip('needs a wire from Flex1 to Flex4')
    flex_io['Flex4'].channel_type = FlexIOChannelType.ANALOG_OUTPUT


def _busy(duration: float) -> StateMachine:
    """A trial of `duration` with an event in every cycle: 15 looping global timers."""
    fsm = _busy_timers(StateMachine(), duration)
    fsm.add_state(
        'Arm', timer=1e-4, transitions={'Tup': 'Run'}, actions={'GlobalTimerTrig': 0}
    )
    fsm.add_state('Run', transitions={'GlobalTimer0_End': '>exit'})
    return fsm


def _events(bpod: Bpod, prefix: str) -> list[tuple[str, str, int]]:
    """The last trial's events starting with `prefix`, timed from their state."""
    data = bpod.get_data()
    data = data.filter(pl.col('Trial') == data['Trial'].max()).with_columns(
        pl.col('Event', 'State').cast(pl.String), t=pl.col('Time').dt.epoch('us')
    )
    starts = dict(
        data.filter(pl.col('Type') == 'StateStart').select('State', 't').rows()
    )
    events = data.filter(pl.col('Event').str.starts_with(prefix))
    return [
        (state, event, t - starts[state])
        for state, event, t in events.select('State', 'Event', 't').rows()
    ]


@pytest.mark.parametrize('load', ['idle', 'busy'])
def test_analog_samples_follow_the_trials(bpod, analog_recording, load):
    """No analog sample is lost across queued trials, also with an event every cycle."""
    for duration in DURATIONS_S:
        bpod.run(_busy(duration) if load == 'busy' else _chain(('A', duration, {})))
    bpod.wait()
    analog_recording(bpod.get_data())


def test_threshold_fires_once_until_rearmed(bpod, flex_io):
    """A threshold disarms when it fires, until a state arms it again."""
    flex1 = flex_io['Flex1']
    flex1.channel_type = FlexIOChannelType.ANALOG_INPUT
    flex1.thresholds[0].polarity = FlexIOThresholdPolarity.FALLING
    flex1.thresholds[0].voltage = 5000  # crossed by any reading
    arm = {'AnalogThreshEnable': 1}
    bpod.run(_chain(('A', 0.02, arm), ('B', 0.02, {}), ('C', 0.02, arm)))
    bpod.wait()
    assert [state for state, _, _ in _events(bpod, 'Flex1_Trig')] == ['A', 'C']


def test_digital_loopback(bpod, flex_loopback):
    """Flex4 follows Flex1 one cycle later, for exactly as long as a state sets it."""
    on = {'Flex1': 1}
    steps = [
        ('On1', 0.005, on),
        ('Off1', 0.003, {}),
        ('On2', 0.007, on),
        ('Off2', 0.002, {}),
    ]
    bpod.run(_chain(*steps))
    bpod.wait()
    cycle = bpod._hardware.cycle_period_us
    assert _events(bpod, 'Flex4') == [
        ('On1', 'Flex4_High', cycle),
        ('Off1', 'Flex4_Low', cycle),
        ('On2', 'Flex4_High', cycle),
        ('Off2', 'Flex4_Low', cycle),
    ]


def test_analog_loopback(bpod, analog_loopback):
    """Voltages written to Flex4 read back on Flex1 within a few millivolts."""
    levels = (500, 2500, 4500)  # full scale reads some 20 mV low
    bpod.run(_chain(*((f'V{level}', 0.03, {'Flex4': level}) for level in levels)))
    bpod.wait()
    data = bpod.get_data()
    analog = _analog_data(bpod, until=data.select(pl.col('Time').max()).item())
    starts = data.filter(pl.col('Type') == 'StateStart')['Time'].to_list()
    for level, start in zip(levels, starts, strict=True):
        within = analog.filter(
            pl.col('Time').is_between(start, start + pl.duration(milliseconds=25))
        )
        assert abs(within['Flex1'].median() - level) < 10, (
            f'{level} mV read back as {within["Flex1"].median()} mV'
        )


# a state arming thresholds in an earlier machine arms them in this one's first state
@pytest.mark.known_firmware_issue(f'{FIRMWARE_PRS}/7')
@pytest.mark.parametrize(
    'mode',
    [FlexIOThresholdMode.MANUAL, FlexIOThresholdMode.LINKED],
    ids=['manual', 'linked'],
)
def test_thresholds_on_a_stepped_signal(bpod, flex_io, analog_loopback, mode):
    """A rising and a falling threshold fire when a stepped signal passes them.

    Only the rising one is armed at first. In MANUAL mode it fires once; in LINKED
    mode, each one firing arms the other.
    """
    flex1 = flex_io['Flex1']
    flex1.threshold_mode = mode
    rising, falling = flex1.thresholds
    rising.voltage, rising.polarity = 2500, FlexIOThresholdPolarity.RISING
    falling.voltage, falling.polarity = 1500, FlexIOThresholdPolarity.FALLING
    rising.enabled = True
    low, high = {'Flex4': 1000}, {'Flex4': 3000}
    steps = [
        ('Low1', 0.01, low),
        ('High1', 0.01, high),
        ('Low2', 0.01, low),
        ('High2', 0.01, high),
        ('Low3', 0.01, low),
    ]
    bpod.run(_chain(*steps))
    bpod.wait()
    events = _events(bpod, 'Flex1_Trig')
    expected = [('High1', 'Flex1_Trig0')]
    if mode == FlexIOThresholdMode.LINKED:
        expected += [
            ('Low2', 'Flex1_Trig1'),
            ('High2', 'Flex1_Trig0'),
            ('Low3', 'Flex1_Trig1'),
        ]
    assert [(state, event) for state, event, _ in events] == expected
    period = bpod._analog_sample_period_us
    assert all(t < 2 * period for _, _, t in events), f'late threshold events: {events}'
