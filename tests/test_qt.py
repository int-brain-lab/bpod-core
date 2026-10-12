"""Tests for bpod_core.qt."""

import pytest
from qtpy.QtCore import QObject

from bpod_core.bpod.structs import (
    EventInput,
    EventOutput,
    EventStateEnd,
    EventStateStart,
    EventTrialEnd,
    EventTrialEndControl,
    EventTrialStart,
)
from bpod_core.qt import QBpod


class TestOnBpodEvent:
    """Tests for the match-and-emit handler translating messages to signals."""

    @pytest.fixture
    def qbpod(self):
        """A QBpod with only its QObject base initialized (no hardware)."""
        instance = QBpod.__new__(QBpod)
        QObject.__init__(instance)
        return instance

    @pytest.mark.parametrize(
        ('message', 'signal_name', 'expected_args'),
        [
            pytest.param(
                EventTrialStart(time_us=1, trial=3, fsm_hash='ab'),
                'trialStart',
                [1, 3, 'ab'],
                id='trial_start',
            ),
            pytest.param(
                EventTrialEnd(time_us=2, trial=3),
                'trialEnd',
                [2, 3],
                id='trial_end',
            ),
            pytest.param(
                EventTrialEndControl(time_us=3, trial=3, n_events=7),
                'trialEndControl',
                [3, 3, 7],
                id='trial_end_control',
            ),
            pytest.param(
                EventStateStart(time_us=4, state='S1'),
                'stateStart',
                [4, 'S1'],
                id='state_start',
            ),
            pytest.param(
                EventStateEnd(time_us=5, state='S1'),
                'stateEnd',
                [5, 'S1'],
                id='state_end',
            ),
            pytest.param(
                EventInput(time_us=6, event='Port1_High', channel='Port1', value=1),
                'inputEvent',
                [6, 'Port1_High', 'Port1', 1],
                id='input_event',
            ),
            pytest.param(
                EventInput(time_us=7, event='Port1_Low', channel='Port1', value=0),
                'inputEvent',
                [7, 'Port1_Low', 'Port1', 0],
                id='input_event_value_zero',
            ),
            pytest.param(
                EventInput(time_us=8, event='Tup', channel=None, value=None),
                'inputEvent',
                [8, 'Tup', '', -1],
                id='input_event_without_channel_or_value',
            ),
            pytest.param(
                EventOutput(time_us=9, channel='Valve1', value=255),
                'outputAction',
                [9, 'Valve1', 255],
                id='output_action',
            ),
        ],
    )
    def test_message_emits_signal(
        self, qbpod, qtbot, message, signal_name, expected_args
    ):
        """Each message kind emits its signal with the expected arguments."""
        with qtbot.waitSignal(getattr(qbpod, signal_name), timeout=1000) as blocker:
            qbpod._on_bpod_event(message)
        assert blocker.args == expected_args
