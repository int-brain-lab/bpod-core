"""Tests for bpod_core.bpod.threads."""

from __future__ import annotations

import logging
import struct
from queue import Empty, Queue
from typing import TYPE_CHECKING

import pytest

from bpod_core.bpod.threads import EventID, ReadThread

if TYPE_CHECKING:
    from bpod_core.bpod.structs import RawEvent


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


def _drain(q: Queue) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except Empty:
            return items


class TestReadThread:
    @pytest.fixture
    def make_thread(self, mock_ext_serial):
        """Factory: loads raw bytes into the mock serial and returns a ReadThread."""

        def _make(data: bytes, *, confirm_fsm: bool = False, cycle_period_us: int = 1):
            mock_ext_serial.response_buffer.extend(data)
            q_events: Queue[RawEvent] = Queue()
            q_softcodes: Queue[int] = Queue()
            thread = ReadThread(
                serial=mock_ext_serial,
                trial=0,
                confirm_fsm=confirm_fsm,
                cycle_period_us=cycle_period_us,
                queue_events=q_events,
                queue_softcodes=q_softcodes,
            )
            return thread, q_events, q_softcodes

        return _make

    def test_initial_events_enqueued(self, make_thread):
        """START_FSM and START_STATE are the first two events enqueued."""
        data = _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        events = _drain(q_events)
        assert events[0].event_index == EventID.START_FSM
        assert events[1].event_index == EventID.START_STATE

    def test_hardware_event_enqueued(self, make_thread):
        """A hardware event in an opcode-1 packet is placed on the event queue."""
        data = (
            _start()
            + _opcode1([42], n_cycles=5)
            + _opcode1([255], n_cycles=10)
            + _exit_data(10, 10)
        )
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        event_indices = [e.event_index for e in _drain(q_events)]
        assert 42 in event_indices

    def test_event_timestamp(self, make_thread):
        """Event bpod_count_us equals start_micros + n_cycles * cycle_period."""
        data = (
            _start(micros_us=100)
            + _opcode1([10], n_cycles=5)
            + _opcode1([255], n_cycles=10)
            + _exit_data(10, 110)
        )
        thread, q_events, _ = make_thread(data, cycle_period_us=2)
        thread.start()
        thread.join(timeout=2)
        event = next(e for e in _drain(q_events) if e.event_index == 10)
        assert event.bpod_count_us == 110  # 100 + 5 * 2

    def test_softcode_enqueued(self, make_thread):
        """Opcode-2 packet places zero-based softcode on the softcode queue."""
        data = _start() + _opcode2(3) + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        thread, _, q_softcodes = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        assert _drain(q_softcodes) == [3]

    def test_end_events_enqueued(self, make_thread):
        """END_FSM_CYCLES, END_FSM_MICROS, and STOP_SENTINEL are enqueued after exit."""
        data = _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        event_indices = [e.event_index for e in _drain(q_events)]
        assert EventID.END_FSM_CYCLES in event_indices
        assert EventID.END_FSM_MICROS in event_indices
        assert event_indices[-1] == EventID.STOP_SENTINEL

    def test_fsm_confirmation_success(self, make_thread):
        """confirm_fsm=True proceeds normally when Bpod responds with a truthy byte."""
        data = b'\x01' + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        thread, q_events, _ = make_thread(data, confirm_fsm=True)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        event_indices = [e.event_index for e in _drain(q_events)]
        assert EventID.START_FSM in event_indices

    @pytest.mark.filterwarnings('ignore::pytest.PytestUnhandledThreadExceptionWarning')
    def test_fsm_confirmation_failure(self, make_thread):
        """confirm_fsm=True exits without STOP_SENTINEL when Bpod sends zero."""
        data = b'\x00'  # False — no further data needed
        thread, q_events, _ = make_thread(data, confirm_fsm=True)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        event_indices = [e.event_index for e in _drain(q_events)]
        assert EventID.STOP_SENTINEL not in event_indices

    def test_timing_violation_warning(self, make_thread, caplog):
        """Logs a WARNING when cycle/micros discrepancy exceeds 1 ms."""
        # cycle-based duration: 5 * 1 = 5 µs; actual: 2000 µs; discrepancy: 1995 > 1000
        data = _start() + _opcode1([255], n_cycles=0) + _exit_data(5, 2000)
        thread, _, _ = make_thread(data, cycle_period_us=1)
        with caplog.at_level(logging.WARNING):
            thread.start()
            thread.join(timeout=2)
        assert 'timing' in caplog.text.lower()
