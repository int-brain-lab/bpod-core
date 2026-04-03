"""Tests for bpod_core.bpod.threads."""

from __future__ import annotations

import logging
import struct
import time
from queue import SimpleQueue

import numpy as np
import polars as pl
import pytest

from bpod_core.bpod.structs import (
    CompiledStateMachine,
    RawEvent,
    TimeReferences,
    _InputEvents,
)
from bpod_core.bpod.threads import (
    _INITIAL_BUFFER_SIZE,
    EventThread,
    ReadThread,
    SoftcodeThread,
    _build_event_lookup,
    _EventID,
)

_CONFIRM_OK = b'\x01'
_CONFIRM_FAIL = b'\x00'


def _start(micros_us: int = 0) -> bytes:
    """Encode start_micros_us as read by read_uint64."""
    return struct.pack('<Q', micros_us)


def _opcode1(events: list[int], n_cycles: int) -> bytes:
    """Encode an opcode-1 (hardware events) packet."""
    return bytes([1, len(events), *events]) + struct.pack('<I', n_cycles)


def _opcode2(softcode: int) -> bytes:
    """Encode an opcode-2 (softcode) packet. param is 1-based on the wire."""
    return bytes([2, softcode + 1])


def _exit_data(n_cycles: int, end_micros_us: int) -> bytes:
    """Encode the 12-byte post-loop exit payload."""
    return struct.pack('<IQ', n_cycles, end_micros_us)


def _drain(q: SimpleQueue) -> list:
    items = []
    while not q.empty():
        items.append(q.get())
    return items


class TestReadThread:
    @pytest.fixture
    def make_thread(self, mock_ext_serial):
        """Factory: loads raw bytes into the mock serial and returns a ReadThread."""

        def _make(data: bytes, *, cycle_period_us: int = 1):
            mock_ext_serial.response_buffer.extend(data)
            q_events: SimpleQueue[RawEvent] = SimpleQueue()
            q_softcodes: SimpleQueue[int] = SimpleQueue()
            thread = ReadThread(
                serial=mock_ext_serial,
                trial=0,
                cycle_period_us=cycle_period_us,
                queue_events=q_events,
                queue_softcodes=q_softcodes,
            )
            return thread, q_events, q_softcodes

        return _make

    def test_initial_events_enqueued(self, make_thread):
        """START_FSM and START_STATE are the first two events enqueued."""
        data = _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        events = _drain(q_events)
        assert events[0].event_id == _EventID.START_FSM
        assert events[1].event_id == _EventID.START_STATE

    def test_hardware_event_enqueued(self, make_thread):
        """A hardware event in an opcode-1 packet is placed on the event queue."""
        data = (
            _CONFIRM_OK
            + _start()
            + _opcode1([42], n_cycles=5)
            + _opcode1([255], n_cycles=10)
            + _exit_data(10, 10)
        )
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        event_indices = [e.event_id for e in _drain(q_events)]
        assert 42 in event_indices

    def test_event_timestamp(self, make_thread):
        """Event bpod_count_us equals start_micros + n_cycles * cycle_period."""
        data = (
            _CONFIRM_OK
            + _start(micros_us=100)
            + _opcode1([10], n_cycles=5)
            + _opcode1([255], n_cycles=10)
            + _exit_data(10, 110)
        )
        thread, q_events, _ = make_thread(data, cycle_period_us=2)
        thread.start()
        thread.join(timeout=2)
        event = next(e for e in _drain(q_events) if e.event_id == 10)
        assert event.micros_us == 110  # 100 + 5 * 2

    def test_softcode_enqueued(self, make_thread):
        """Opcode-2 packet places zero-based softcode on the softcode queue."""
        data = (
            _CONFIRM_OK
            + _start()
            + _opcode2(3)
            + _opcode1([255], n_cycles=0)
            + _exit_data(0, 0)
        )
        thread, _, q_softcodes = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        assert _drain(q_softcodes) == [3]

    def test_end_events_enqueued(self, make_thread):
        """END_FSM_CYCLES, END_FSM_MICROS, and STOP_SENTINEL are enqueued after exit."""
        data = _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        event_indices = [e.event_id for e in _drain(q_events)]
        assert _EventID.END_FSM_CYCLES in event_indices
        assert _EventID.END_FSM_MICROS in event_indices
        assert event_indices[-1] == _EventID.STOP_SENTINEL

    def test_fsm_confirmation_success(self, make_thread):
        """Proceeds normally when Bpod responds with a truthy confirmation byte."""
        data = _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        event_indices = [e.event_id for e in _drain(q_events)]
        assert _EventID.START_FSM in event_indices

    @pytest.mark.filterwarnings('ignore::pytest.PytestUnhandledThreadExceptionWarning')
    def test_fsm_confirmation_failure(self, make_thread):
        """Sends STOP_SENTINEL and exits when Bpod sends a zero confirmation byte."""
        data = _CONFIRM_FAIL  # no further data needed
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        event_indices = [e.event_id for e in _drain(q_events)]
        assert event_indices == [_EventID.STOP_SENTINEL]

    def test_timing_violation_warning(self, make_thread, caplog):
        """Logs a WARNING when cycle/micros discrepancy exceeds 1 ms."""
        # cycle-based duration: 5 * 1 = 5 µs; actual: 2000 µs; discrepancy: 1995 > 1000
        data = (
            _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(5, 2000)
        )
        thread, _, _ = make_thread(data, cycle_period_us=1)
        with caplog.at_level(logging.WARNING):
            thread.start()
            thread.join(timeout=2)
        assert 'timing' in caplog.text.lower()


def _make_fsm(
    n_states: int = 2,
    transitions: dict[tuple[int, int], int] | None = None,
    state_actions: list[dict[str, int]] | None = None,
) -> CompiledStateMachine:
    """Build a minimal CompiledStateMachine for testing."""
    mat = np.arange(n_states, dtype=np.uint8)[:, np.newaxis] * np.ones(
        (1, 255), dtype=np.uint8
    )
    for (state, event), target in (transitions or {}).items():
        mat[state][event] = target
    state_names = [f'S{i}' for i in range(n_states)]
    return CompiledStateMachine(
        state_names=state_names,
        state_transitions=mat,
        state_actions=state_actions or [{} for _ in range(n_states)],
        use_back_op=False,
        state_lookup=pl.DataFrame(
            {
                'state_id': pl.Series(range(n_states), dtype=pl.Int16),
                'state': pl.Series(state_names, dtype=pl.Categorical),
            }
        ),
    )


class TestEventThread:
    @pytest.fixture
    def make_thread(self):
        """Factory: builds an EventThread from a minimal FSM and starts it."""
        threads = []

        def _make(
            fsm: CompiledStateMachine | None = None,
            action_names: list[str] | None = None,
            event_names: list[str] | None = None,
            time_reference: TimeReferences | None = None,
        ) -> tuple[EventThread, SimpleQueue[pl.LazyFrame]]:
            action_names = action_names or []
            event_names = event_names or ['Ev0', 'Tup']
            input_events = _InputEvents(
                names=event_names,
                channels=[None] * len(event_names),
                values=[None] * len(event_names),
            )
            data_queue: SimpleQueue[pl.LazyFrame] = SimpleQueue()
            thread = EventThread(
                trial=0,
                fsm=fsm or _make_fsm(),
                data_queue=data_queue,
                event_lookup=_build_event_lookup(input_events, action_names),
                action_names=action_names,
                time_reference=time_reference or TimeReferences(0, 0, 0),
            )
            thread.start()
            threads.append(thread)
            return thread, data_queue

        yield _make

        for t in threads:
            t.stop()
            t.join(timeout=2)

    def _collect(self, data_queue: SimpleQueue[pl.LazyFrame]) -> pl.DataFrame:
        return data_queue.get(timeout=2).collect()

    def test_stop_exits_thread(self, make_thread):
        """stop() causes the thread to exit cleanly."""
        thread, _ = make_thread()
        thread.stop()
        thread.join(timeout=2)
        assert not thread.is_alive()

    def test_data_enqueued_on_stop(self, make_thread):
        """A LazyFrame is pushed to data_queue when the thread exits."""
        thread, data_queue = make_thread()
        thread.stop()
        thread.join(timeout=2)
        assert not data_queue.empty()

    def test_hardware_event_recorded(self, make_thread):
        """Hardware event appears in output with correct event name."""
        thread, data_queue = make_thread(event_names=['Ev0', 'Tup'])
        thread.queue.put(RawEvent(micros_us=0, event_id=0))
        thread.stop()
        thread.join(timeout=2)
        df = self._collect(data_queue)
        assert 'Ev0' in df.filter(pl.col('type') == 'InputEvent')['event'].to_list()

    def test_event_timestamp(self, make_thread):
        """Event timestamp equals reset_system_time_ns // 1000 + micros_us."""
        time_ref = TimeReferences(0, 0, reset_system_time_ns=5_000_000)
        thread, data_queue = make_thread(
            event_names=['Ev0', 'Tup'], time_reference=time_ref
        )
        thread.queue.put(RawEvent(micros_us=100, event_id=0))
        thread.stop()
        thread.join(timeout=2)
        df = self._collect(data_queue)
        ev = df.filter(pl.col('event') == 'Ev0')
        assert (
            ev['time'][0] == pl.Series([5100], dtype=pl.Datetime('us'))[0]
        )  # 5000 + 100 µs

    def test_state_transition_generates_state_events(self, make_thread):
        """State transition generates StateEnd for old state and StateStart for new."""
        # event 0 triggers S0 → S1
        fsm = _make_fsm(transitions={(0, 0): 1})
        thread, data_queue = make_thread(fsm=fsm)
        thread.queue.put(RawEvent(micros_us=0, event_id=0))
        thread.stop()
        thread.join(timeout=2)
        df = self._collect(data_queue)
        types = df['type'].cast(pl.String).to_list()
        assert 'StateEnd' in types
        assert 'StateStart' in types

    def test_state_names_in_output(self, make_thread):
        """State column contains the correct state names after a transition."""
        fsm = _make_fsm(transitions={(0, 0): 1})
        thread, data_queue = make_thread(fsm=fsm)
        thread.queue.put(RawEvent(micros_us=0, event_id=0))
        thread.stop()
        thread.join(timeout=2)
        df = self._collect(data_queue)
        states = df['state'].drop_nulls().cast(pl.String).to_list()
        assert 'S0' in states
        assert 'S1' in states

    def test_output_action_recorded(self, make_thread):
        """Output actions for the current state are recorded on START_STATE."""
        fsm = _make_fsm(state_actions=[{'PWM1': 128}, {}])
        thread, data_queue = make_thread(fsm=fsm, action_names=['PWM1'])
        # START_STATE triggers initial output recording for S0
        thread.queue.put(RawEvent(micros_us=0, event_id=_EventID.START_STATE))
        thread.stop()
        thread.join(timeout=2)
        df = self._collect(data_queue)
        output = df.filter(pl.col('type') == 'OutputAction')
        assert 'PWM1' in output['channel'].cast(pl.String).to_list()

    def test_ttl_reset_on_state_transition(self, make_thread):
        """TTL output is reset to 0."""
        # S0 sets TTL1=1; S1 has no actions; event 0 triggers S0→S1
        fsm = _make_fsm(
            transitions={(0, 0): 1},
            state_actions=[{'TTL1': 1}, {}],
        )
        thread, data_queue = make_thread(fsm=fsm, action_names=['TTL1'])
        # START_STATE seeds active_outputs with TTL1=1 for S0
        thread.queue.put(RawEvent(micros_us=0, event_id=_EventID.START_STATE))
        thread.queue.put(RawEvent(micros_us=0, event_id=0))  # S0 → S1
        thread.stop()
        thread.join(timeout=2)
        df = self._collect(data_queue)
        ttl = df.filter(
            (pl.col('type') == 'OutputAction') & (pl.col('channel') == 'TTL1')
        )
        assert 0 in ttl['value'].to_list()

    def test_peek_data_mid_trial(self, make_thread):
        """peek_data returns a non-empty snapshot while the trial is running."""
        thread, _ = make_thread()
        thread.queue.put(RawEvent(micros_us=0, event_id=_EventID.START_FSM))
        deadline = time.monotonic() + 2.0
        while thread._n_events == 0 and time.monotonic() < deadline:
            time.sleep(0.001)
        df = thread.peek_data().collect()
        assert len(df) > 0

    def test_buffer_growth(self, make_thread):
        """Buffer doubles correctly."""
        n = _INITIAL_BUFFER_SIZE + 10
        thread, data_queue = make_thread(event_names=['Ev0', 'Tup'])
        for _ in range(n):
            thread.queue.put(RawEvent(micros_us=0, event_id=0))
        thread.stop()
        thread.join(timeout=2)
        df = self._collect(data_queue)
        assert len(df.filter(pl.col('event') == 'Ev0')) == n


class TestSoftcodeThread:
    @pytest.fixture
    def make_thread(self):
        """Factory: returns a started SoftcodeThread and stops it after the test."""
        threads = []

        def _make(handler=None):
            thread = SoftcodeThread(softcode_handler=handler)
            thread.start()
            threads.append(thread)
            return thread

        yield _make

        for t in threads:
            t.stop()
            t.join(timeout=2)

    def test_handler_called(self, make_thread):
        """Handler is called with the correct softcode value."""
        received = []
        thread = make_thread(handler=received.append)
        thread.queue.put(7)
        thread.stop()
        thread.join(timeout=2)
        assert received == [7]

    def test_handler_called_in_order(self, make_thread):
        """Handler is called once per softcode in the order received."""
        received = []
        thread = make_thread(handler=received.append)
        for code in [1, 5, 3]:
            thread.queue.put(code)
        thread.stop()
        thread.join(timeout=2)
        assert received == [1, 5, 3]

    def test_no_handler_logs_warning(self, make_thread, caplog):
        """A warning is logged when no handler is defined."""
        thread = make_thread(handler=None)
        with caplog.at_level(logging.WARNING):
            thread.queue.put(4)
            thread.stop()
            thread.join(timeout=2)
        assert '4' in caplog.text

    def test_handler_exception_logged_and_continues(self, make_thread, caplog):
        """An exception in the handler is logged and processing continues."""
        received = []

        def handler(code):
            if code == 0:
                raise ValueError('bad softcode')
            received.append(code)

        thread = make_thread(handler=handler)
        with caplog.at_level(logging.ERROR):
            thread.queue.put(0)
            thread.queue.put(9)
            thread.stop()
            thread.join(timeout=2)
        assert received == [9]
        assert 'bad softcode' in caplog.text

    def test_stop_terminates_thread(self, make_thread):
        """stop() causes the thread to exit cleanly."""
        thread = make_thread()
        thread.stop()
        thread.join(timeout=2)
        assert not thread.is_alive()
