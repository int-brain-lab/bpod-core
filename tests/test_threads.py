"""Tests for bpod_core.bpod.threads."""

from __future__ import annotations

import logging
import struct
from queue import Empty, Queue
from typing import TYPE_CHECKING

import pytest

from bpod_core.bpod.threads import ReadThread, SoftcodeThread, _EventID

if TYPE_CHECKING:
    from bpod_core.bpod.structs import RawEvent


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

        def _make(data: bytes, *, cycle_period_us: int = 1):
            mock_ext_serial.response_buffer.extend(data)
            q_events: Queue[RawEvent] = Queue()
            q_softcodes: Queue[int] = Queue()
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
        data = _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(5, 2000)  # noqa: E501
        thread, _, _ = make_thread(data, cycle_period_us=1)
        with caplog.at_level(logging.WARNING):
            thread.start()
            thread.join(timeout=2)
        assert 'timing' in caplog.text.lower()


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
        thread.queue.join()
        assert received == [7]

    def test_handler_called_in_order(self, make_thread):
        """Handler is called once per softcode in the order received."""
        received = []
        thread = make_thread(handler=received.append)
        for code in [1, 5, 3]:
            thread.queue.put(code)
        thread.queue.join()
        assert received == [1, 5, 3]

    def test_no_handler_logs_warning(self, make_thread, caplog):
        """A warning is logged when no handler is defined."""
        thread = make_thread(handler=None)
        with caplog.at_level(logging.WARNING):
            thread.queue.put(4)
            thread.queue.join()
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
            thread.queue.join()
        assert received == [9]
        assert 'bad softcode' in caplog.text

    def test_stop_terminates_thread(self, make_thread):
        """stop() causes the thread to exit cleanly."""
        thread = make_thread()
        thread.stop()
        thread.join(timeout=2)
        assert not thread.is_alive()
