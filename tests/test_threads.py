"""Tests for bpod_core.bpod.threads."""

from __future__ import annotations

import logging
import os
import struct
import subprocess
import sys
import threading
import time
from queue import SimpleQueue
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import numpy as np
import polars as pl
import pytest
from polars.testing import assert_frame_equal
from serial import SerialException

from bpod_core.bpod._threads import (
    _EVENT_DTYPE,
    _EVENTS_STOP,
    _OUTPUT_ID_OFFSET,
    _SOFTCODE_STOP,
    _TRIAL_DATA_SCHEMA,
    EventThread,
    ReadThread,
    SoftcodeThread,
    _build_event_tables,
    _EventID,
    _EventTables,
    _LateStarts,
    _Recorder,
    _Trial,
    join_until,
)
from bpod_core.bpod.errors import BpodError
from bpod_core.bpod.structs import (
    BpodEventUnion,
    EventInput,
    EventOutput,
    EventStateEnd,
    EventStateStart,
    EventTrialEnd,
    EventTrialEndControl,
    EventTrialStart,
    _InputEvents,
    _RawEvents,
    _RawSoftcode,
    _StateMachineLookup,
    _TimeReferences,
)
from bpod_core.constants import STRUCT_UINT32_LE, STRUCT_UINT64_LE
from bpod_core.misc import _HandlerSlot

if TYPE_CHECKING:
    from collections.abc import Callable

_CONFIRM_OK = b'\x01'
_CONFIRM_FAIL = b'\x00'


def _start(micros_us: int = 0) -> bytes:
    """Encode the start time the Bpod sends at the beginning of a trial."""
    return STRUCT_UINT64_LE.pack(micros_us)


def _opcode1(events: list[int], n_cycles: int) -> bytes:
    """Encode an opcode-1 (hardware events) packet."""
    return bytes([1, len(events), *events]) + STRUCT_UINT32_LE.pack(n_cycles)


def _opcode2(softcode: int) -> bytes:
    """Encode an opcode-2 (softcode) packet."""
    return bytes([2, softcode])


def _make_event_tables(
    names: list[str] | None = None,
    values: list[int | None] | None = None,
    actions: list[str] | None = None,
    resettable: list[str] | None = None,
) -> _EventTables:
    """Build the lookup tables for a hardware with the given events and actions."""
    names = names or ['Ev0', 'Tup']
    actions = actions or []
    return _build_event_tables(
        _InputEvents(
            names=names,
            channels=[None] * len(names),
            values=values or [None] * len(names),
        ),
        actions,
        actions if resettable is None else resettable,
    )


def _make_trial(
    number: int = 0,
    fsm: _StateMachineLookup | None = None,
    event_tables: _EventTables | None = None,
) -> _Trial:
    """Build a trial with a recorder, from a minimal FSM by default."""
    fsm = fsm or _make_fsm()
    recorder = _Recorder(
        trial=number, fsm=fsm, event_tables=event_tables or _make_event_tables()
    )
    return _Trial(number=number, fsm_hash=fsm.fsm_hash, recorder=recorder)


def _reader(serial) -> tuple[ReadThread, SimpleQueue]:
    """A ReadThread for trial 0 on `serial`, and its event queue."""
    q_events: SimpleQueue[list[_RawEvents]] = SimpleQueue()
    thread = ReadThread(
        serial=serial,
        trial=_make_trial(),
        cycle_period_us=1,
        queue_events=q_events,
        queue_softcodes=SimpleQueue(),
        late_starts=_LateStarts(),
    )
    return thread, q_events


def _exit_data(n_cycles: int, end_micros_us: int) -> bytes:
    """Encode the 12-byte post-loop exit payload."""
    return struct.pack('<IQ', n_cycles, end_micros_us)


class _TrickleSerial:
    """Fake serial port that hands out its data one byte per read."""

    def __init__(self, data: bytes) -> None:
        self._data = bytearray(data)

    def read_waiting(self) -> bytes:
        chunk = bytes(self._data[:1])
        del self._data[:1]
        return chunk


class _UnpluggedSerial(_TrickleSerial):
    """Fake serial port that fails once its data is used up, like an unplugged one."""

    def read_waiting(self) -> bytes:
        if not self._data:
            raise SerialException('device disconnected')
        return super().read_waiting()


def _drain(q: SimpleQueue) -> list:
    items = []
    while not q.empty():
        items.append(q.get())
    return items


def _timed_events(q: SimpleQueue[list[_RawEvents]]) -> list[tuple[int, int]]:
    """Drain an event queue into one (time, event index) pair per event."""
    return [
        (item.micros_us, i)
        for batch in _drain(q)
        for item in batch
        for i in item.event_ids
    ]


class TestReadThread:
    @pytest.fixture
    def make_thread(self, mock_ext_serial):
        """Factory: loads raw bytes into the mock serial and returns a ReadThread."""

        def _make(
            data: bytes,
            *,
            cycle_period_us: int = 1,
            serial=None,
            initial_bytes: bytes = b'',
            initial_received_ns: int = 0,
            previous_end_us: int | None = None,
        ):
            mock_ext_serial.response_buffer.extend(data)
            q_events: SimpleQueue[list[_RawEvents]] = SimpleQueue()
            q_softcodes: SimpleQueue[_RawSoftcode] = SimpleQueue()
            thread = ReadThread(
                serial=serial or mock_ext_serial,
                trial=_make_trial(),
                cycle_period_us=cycle_period_us,
                queue_events=q_events,
                queue_softcodes=q_softcodes,
                initial_bytes=initial_bytes,
                initial_received_ns=initial_received_ns,
                late_starts=_LateStarts(),
                previous_end_us=previous_end_us,
            )
            return thread, q_events, q_softcodes

        return _make

    def test_cancel_ends_the_thread_without_the_bpod(self, pty_port):
        """cancel() ends a reader waiting on a silent port; no error is recorded."""
        _, port = pty_port  # a real port: the mock serial never blocks
        thread, q_events = _reader(port)
        thread.start()
        thread.join(timeout=0.05)
        assert thread.is_alive()  # waiting for the confirmation byte
        thread.cancel()  # joins the thread
        assert not thread.is_alive()
        assert thread.trial.read_error is None
        assert q_events.get(timeout=1) is _EVENTS_STOP

    def test_cancel_after_the_trial_ended_withdraws_the_abort(self, pty_port):
        """A cancel the reader never saw must not end the next read on the port."""
        master, port = pty_port
        thread, _ = _reader(port)
        data = _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        os.write(master, data)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        thread.cancel()  # too late to end anything
        os.write(master, b'a')
        assert port.read_waiting() == b'a'  # not b'': the abort byte was withdrawn

    def test_empty_read_without_cancel_is_retried(self):
        """Off POSIX an elapsed port timeout yields nothing: the reader reads again."""
        data = _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        serial = MagicMock()
        serial.read_waiting.side_effect = [b'', data]
        thread, q_events = _reader(serial)
        thread.run()
        assert thread.trial.read_error is None
        assert len(_drain(q_events)) > 1  # events, not just the stop sentinel

    def test_initial_events_enqueued(self, make_thread):
        """START_FSM and START_STATE are the first two events enqueued."""
        data = _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        events = [i for _, i in _timed_events(q_events)]
        assert events[:2] == [_EventID.START_FSM, _EventID.START_STATE]

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
        event_indices = [i for _, i in _timed_events(q_events)]
        assert 42 in event_indices

    def test_events_of_one_cycle_enqueued_together(self, make_thread):
        """The events of one packet - one cycle - form one item of a read's list."""
        data = (
            _CONFIRM_OK
            + _start()
            + _opcode1([42, 43, 44], n_cycles=5)
            + _opcode1([45], n_cycles=6)
            + _opcode1([46, 255], n_cycles=10)  # EXIT isn't enqueued
            + _exit_data(10, 10)
        )
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        cycles = [
            list(e.event_ids)
            for batch in _drain(q_events)
            for e in batch
            if 0 <= e.event_ids[0] < 255
        ]
        assert cycles == [[42, 43, 44], [45], [46]]

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
        events = {i: t for t, i in _timed_events(q_events)}
        assert events[10] == 110  # 100 + 5 * 2

    def test_packets_of_one_read_are_handed_over_together(self, make_thread):
        """One list per read: the start and the cycles, then the end, then the stop."""
        data = (
            _CONFIRM_OK
            + _start()
            + _opcode1([42], n_cycles=1)
            + _opcode1([43, 44], n_cycles=2)
            + _opcode1([255], n_cycles=3)
            + _exit_data(3, 3)
        )
        thread, q_events, _ = make_thread(data)  # the mock hands out all data at once
        thread.start()
        thread.join(timeout=2)
        batches = [[tuple(e.event_ids) for e in batch] for batch in _drain(q_events)]
        assert batches == [
            [(_EventID.START_FSM,), (_EventID.START_STATE,), (42,), (43, 44)],
            [(_EventID.END_FSM_CYCLES,), (_EventID.END_FSM_MICROS,)],
            [(_EventID.STOP_SENTINEL,)],
        ]

    def test_softcode_follows_the_state_change_of_its_cycle(self, mock_ext_serial):
        """A softcode is handed over only after the events parsed before it."""
        data = (
            _CONFIRM_OK
            + _start()
            + _opcode1([42], n_cycles=1)
            + _opcode2(3)
            + _opcode1([255], n_cycles=2)
            + _exit_data(2, 2)
        )
        mock_ext_serial.response_buffer.extend(data)
        calls = MagicMock()  # one parent records the order across both queues
        thread = ReadThread(
            serial=mock_ext_serial,
            trial=_make_trial(),
            cycle_period_us=1,
            queue_events=calls.events,
            queue_softcodes=calls.softcodes,
            late_starts=_LateStarts(),
        )
        thread.start()
        thread.join(timeout=2)
        order = [
            f'events{len(args[0])}' if name == 'events.put' else 'softcode'
            for name, args, _ in calls.mock_calls
        ]
        assert order == ['events3', 'softcode', 'events2', 'events1']

    def test_parsed_events_survive_an_unknown_opcode(self, make_thread):
        """The cycles parsed before a bad packet reach the event thread."""
        data = _CONFIRM_OK + _start() + _opcode1([42], n_cycles=1) + bytes([7, 0])
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        event_indices = [i for _, i in _timed_events(q_events)]
        assert event_indices[:3] == [_EventID.START_FSM, _EventID.START_STATE, 42]
        assert event_indices[-1] == _EventID.STOP_SENTINEL
        assert 'unknown opcode' in str(thread.trial.read_error)  # kept for Bpod.wait()

    @pytest.mark.parametrize('softcode', [0, 3])  # 0: a global timer's value_off
    def test_softcode_enqueued(self, make_thread, softcode):
        """Opcode-2 packet places the softcode, as sent, on the softcode queue."""
        data = (
            _CONFIRM_OK
            + _start()
            + _opcode2(softcode)
            + _opcode1([255], n_cycles=0)
            + _exit_data(0, 0)
        )
        thread, _, q_softcodes = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        assert [item.softcode for item in _drain(q_softcodes)] == [softcode]

    def test_error_is_kept(self, make_thread):
        """A lost connection ends the trial, keeping the error and the events so far."""
        data = _CONFIRM_OK + _start(100) + _opcode1([42], n_cycles=5)
        thread, q_events, _ = make_thread(b'', serial=_UnpluggedSerial(data))
        thread.start()
        thread.join(timeout=2)
        assert isinstance(thread.trial.read_error, SerialException)
        events = [i for _, i in _timed_events(q_events)]
        assert 42 in events
        assert events[-1] == _EventID.STOP_SENTINEL

    def test_packets_split_across_reads(self, make_thread):
        """Packets that arrive one byte at a time are reassembled correctly."""
        data = (
            _CONFIRM_OK
            + _start(100)
            + _opcode1([42, 43], n_cycles=5)
            + _opcode2(3)
            + _opcode1([255], n_cycles=10)
            + _exit_data(10, 110)
        )
        thread, q_events, q_softcodes = make_thread(b'', serial=_TrickleSerial(data))
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        events = _timed_events(q_events)
        assert (105, 42) in events
        assert (105, 43) in events
        assert [item.softcode for item in _drain(q_softcodes)] == [3]
        assert thread.trial.leftover == b''

    def test_leftover_is_handed_to_next_trial(self, make_thread):
        """Bytes read beyond a trial's end start the next reader's trial."""
        next_trial = _CONFIRM_OK + _start(200) + _opcode2(4)
        data = (
            _CONFIRM_OK
            + _start()
            + _opcode1([255], n_cycles=1)
            + _exit_data(1, 1)
            + next_trial
        )
        thread, _, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        assert thread.trial.leftover == next_trial
        assert thread.trial.leftover_received_ns > 0

        # the next reader starts from the leftover and reads the rest from the port;
        # the softcode in the leftover keeps the time it was received at
        next_thread, q_events, q_softcodes = make_thread(
            _opcode1([255], n_cycles=1) + _exit_data(1, 201),
            initial_bytes=thread.trial.leftover,
            initial_received_ns=thread.trial.leftover_received_ns,
        )
        next_thread.start()
        next_thread.join(timeout=2)
        assert not next_thread.is_alive()
        assert _timed_events(q_events)[0] == (200, _EventID.START_FSM)
        softcode = _drain(q_softcodes)[0]
        assert softcode.softcode == 4
        assert softcode.received_ns == thread.trial.leftover_received_ns

    def test_end_events_enqueued(self, make_thread):
        """END_FSM_CYCLES, END_FSM_MICROS, and STOP_SENTINEL are enqueued after exit."""
        data = _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        event_indices = [i for _, i in _timed_events(q_events)]
        assert _EventID.END_FSM_CYCLES in event_indices
        assert _EventID.END_FSM_MICROS in event_indices
        assert event_indices[-1] == _EventID.STOP_SENTINEL

    def test_trial_end_timed_by_exit_packet(self, make_thread, caplog):
        """TrialEnd uses the exit packet's cycle count, like every other event.

        For a queued trial, the firmware's end-of-trial cycle count is one higher
        than the exit packet's, since it also counts the cycle in which the previous
        trial ended.
        """
        data = (
            _CONFIRM_OK
            + _start(micros_us=1000)
            + _opcode1([255], n_cycles=10)
            + _exit_data(11, 2000)
        )
        thread, q_events, _ = make_thread(data, cycle_period_us=100)
        with caplog.at_level(logging.WARNING):
            thread.start()
            thread.join(timeout=2)
        events = {i: t for t, i in _timed_events(q_events)}
        assert events[_EventID.END_FSM_CYCLES] == 2000  # 1000 + 10 * 100
        assert events[_EventID.END_FSM_MICROS] == 2000
        assert 'timing' not in caplog.text.lower()

    def test_timing_warning_names_the_read_backlog(self, make_thread, caplog):
        """The warning tells how far the host fell behind, which can stall the Bpod."""
        head = _CONFIRM_OK + _start(micros_us=1000)
        body = _opcode1([255], n_cycles=10) + _exit_data(10, 5000)  # 4000 µs, 10 cycles
        serial = MagicMock(read_waiting=MagicMock(return_value=body))  # all at once
        thread, _, _ = make_thread(
            b'', cycle_period_us=100, serial=serial, initial_bytes=head
        )
        with caplog.at_level(logging.WARNING):
            thread.start()
            thread.join(timeout=2)
        assert f'Peak read backlog on the host: {len(body)} bytes' in caplog.text

    def test_timing_check_uses_exit_packet(self, make_thread, caplog):
        """The timing check compares the reported TrialEnd, not the end-of-trial count.

        The exit packet's 10 cycles match the 1000 µs the trial took; the end-of-trial
        count (30 cycles) would suggest a 2 ms discrepancy.
        """
        data = (
            _CONFIRM_OK
            + _start(micros_us=1000)
            + _opcode1([255], n_cycles=10)
            + _exit_data(30, 2000)
        )
        thread, _, _ = make_thread(data, cycle_period_us=100)
        with caplog.at_level(logging.WARNING):
            thread.start()
            thread.join(timeout=2)
        assert 'timing' not in caplog.text.lower()

    def test_fsm_confirmation_success(self, make_thread):
        """Proceeds normally when Bpod responds with a truthy confirmation byte."""
        data = _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        event_indices = [i for _, i in _timed_events(q_events)]
        assert _EventID.START_FSM in event_indices

    def test_fsm_confirmation_failure(self, make_thread):
        """Sends STOP_SENTINEL and exits when Bpod sends a zero confirmation byte."""
        data = _CONFIRM_FAIL  # no further data needed
        thread, q_events, _ = make_thread(data)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        event_indices = [i for _, i in _timed_events(q_events)]
        assert event_indices == [_EventID.STOP_SENTINEL]
        assert 'not confirmed' in str(thread.trial.read_error)

    def test_trial_started_once_confirmed(self, pty_port):
        """`trial_started` is set by the confirmation, while the trial goes on."""
        master, port = pty_port  # a real port: the mock serial never blocks
        thread, _ = _reader(port)
        thread.start()
        try:
            assert not thread.trial.started.wait(0.05)  # no confirmation yet
            os.write(master, _CONFIRM_OK)
            assert thread.trial.started.wait(1)
            assert thread.is_alive()
        finally:
            thread.cancel()

    def test_stop_found_the_bpod_idle(self, make_thread, caplog):
        """A stop the Bpod answers as idle, never having started the trial, ends it.

        The thread takes the reply, rather than the trial's start, and enqueues no
        events.
        """
        idle_reply = _opcode1([255], n_cycles=7) + _exit_data(7, 1234)
        thread, q_events, _ = make_thread(idle_reply + b'\x99')
        thread.expect_stop()
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert [i for _, i in _timed_events(q_events)] == [_EventID.STOP_SENTINEL]
        assert thread._buffer == b'\x99'  # exactly the reply was taken
        assert 'Trial #0 never started' in caplog.text

    def test_stop_during_the_trial_reads_the_trial(self, make_thread):
        """A stop sent while the trial runs doesn't change how the trial is read."""
        data = _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0)
        thread, q_events, _ = make_thread(data)
        thread.expect_stop()
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert _EventID.START_FSM in [i for _, i in _timed_events(q_events)]

    def test_timing_violation_warning(self, make_thread, caplog):
        """Logs a WARNING when cycle/micros discrepancy exceeds 1 ms."""
        # cycle-based duration: 5 * 1 = 5 µs; actual: 2000 µs; discrepancy: 1995 > 1000
        data = (
            _CONFIRM_OK + _start() + _opcode1([255], n_cycles=5) + _exit_data(5, 2000)
        )
        thread, _, _ = make_thread(data, cycle_period_us=1)
        with caplog.at_level(logging.WARNING):
            thread.start()
            thread.join(timeout=2)
        assert 'timing' in caplog.text.lower()

    @pytest.mark.parametrize(
        ('previous_end_us', 'start_us', 'late'),
        [
            (1000, 1100, False),
            (1000, 1150, False),
            (1000, 1151, True),
            (None, 99_999, False),
        ],
    )
    def test_late_start(self, make_thread, previous_end_us, start_us, late):
        """A queued trial starting over half a cycle after its due cycle is late."""
        end_us = start_us + 100
        data = _CONFIRM_OK + _start(start_us) + _opcode1([255], n_cycles=1)
        thread, _, _ = make_thread(
            data + _exit_data(1, end_us),
            cycle_period_us=100,
            previous_end_us=previous_end_us,
        )
        thread.start()
        thread.join(timeout=2)
        assert thread._late_starts.count == late
        assert thread.trial.end_micros_us == end_us

    def test_ended_is_set_last(self, make_thread, mocker):
        """`ended` tells that everything the reader writes to the trial is there."""
        data = _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 7)
        thread, q_events, _ = make_thread(data)
        trial = thread.trial
        seen = []
        mocker.patch.object(
            trial.ended,
            'set',
            side_effect=lambda: seen.append(
                (trial.started.is_set(), trial.end_micros_us, q_events.empty())
            ),
        )
        thread.start()
        thread.join(timeout=2)
        assert seen == [(True, 7, False)]

    @pytest.mark.parametrize(
        ('data', 'expect_stop'),
        [
            (
                _CONFIRM_OK + _start() + _opcode1([255], n_cycles=0) + _exit_data(0, 0),
                False,
            ),
            (_CONFIRM_FAIL, False),  # the Bpod refused
            (_opcode1([255], n_cycles=7) + _exit_data(7, 1234), True),  # stopped idle
        ],
    )
    def test_started_and_ended_on_every_way_out(self, make_thread, data, expect_stop):
        """Nobody is left waiting on the trial, however the reader ends."""
        thread, _, _ = make_thread(data)
        if expect_stop:
            thread.expect_stop()
        thread.start()
        thread.join(timeout=2)
        assert thread.trial.started.is_set()
        assert thread.trial.ended.is_set()


class TestLateStarts:
    def test_longest_yet_is_a_warning_and_all_are_counted(self, caplog):
        """A start later than all before it warns; the others are debug messages."""
        late_starts = _LateStarts()
        with caplog.at_level(logging.DEBUG, logger='bpod_core.bpod._threads'):
            late_starts.record(3, 2300)
            late_starts.record(7, 900)
            late_starts.record(9, 3000)
            late_starts.summarize()
            late_starts.summarize()  # the connection closed twice: counted once
        text = "Trial #%d started %.1f ms late: its state machine wasn't ready in time"
        assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
            ('WARNING', text % (3, 2.3)),
            ('DEBUG', text % (7, 0.9)),
            ('WARNING', text % (9, 3.0)),
            ('WARNING', '3 trials started late, by up to 3.0 ms'),
        ]

    def test_no_count_after_a_single_late_start(self, caplog):
        """A single late start, warned about already, isn't counted again."""
        late_starts = _LateStarts()
        late_starts.record(3, 2300)
        caplog.clear()
        late_starts.summarize()
        assert caplog.records == []


def _make_fsm(
    n_states: int = 2,
    transitions: dict[tuple[int, int], int] | None = None,
    state_action_indices: list[dict[int, int]] | None = None,
) -> _StateMachineLookup:
    """Build a minimal CompiledStateMachine for testing."""
    mat = [bytearray((state,)) * 255 for state in range(n_states)]
    for (state, event), target in (transitions or {}).items():
        mat[state][event] = target
    state_names = [f'S{i}' for i in range(n_states)]
    return _StateMachineLookup(
        fsm_hash=b'',
        state_names=state_names,
        state_transition_matrix=mat,
        state_action_indices=state_action_indices or [{} for _ in range(n_states)],
        use_back_op=False,
    )


def _peek_in_background(
    peek: Callable[[list[str]], pl.DataFrame], states: list[str]
) -> tuple[threading.Thread, list[pl.DataFrame]]:
    """Start a peek on another thread, which puts its snapshot in the returned list."""
    snapshots: list[pl.DataFrame] = []
    peeker = threading.Thread(
        target=lambda: snapshots.append(peek(states)), daemon=True
    )
    peeker.start()
    time.sleep(0.02)  # let it block first
    return peeker, snapshots


class TestRecorder:
    @staticmethod
    def _make_recorder(trial: int = 7) -> _Recorder:
        return _Recorder(
            trial=trial,
            fsm=_make_fsm()._replace(fsm_hash=b'\xab\xcd'),
            event_tables=_make_event_tables(
                ['Ev0', 'Tup', 'Low'], [None, 5, 0], ['Valve1']
            ),
        )

    def test_to_frame_matches_a_reference(self):
        """Each kind of event decodes to its row, values and nulls included."""
        recorder = self._make_recorder()
        recorder.append(1000, _EventID.START_FSM, -1)
        recorder.append(1000, _EventID.START_STATE, 0)
        recorder.append(1000, _OUTPUT_ID_OFFSET, 0, 1)
        recorder.append(1100, 0, 0)  # an input without a default value
        recorder.append(1200, 1, 1)  # one with a default value
        recorder.append(1250, 2, 1)  # one with a default value of 0
        recorder.append(1300, 0, 1, 3)  # one with its own value
        recorder.append(1400, 200, 1)  # an event code the hardware doesn't name
        recorder.append(1500, _EventID.END_FSM_CYCLES, -1)
        expected = pl.DataFrame(
            {
                'Time': [1000, 1000, 1000, 1100, 1200, 1250, 1300, 1400, 1500],
                'Trial': [7] * 9,
                'State Machine': ['abcd'] * 9,
                'State': [None, 'S0', 'S0', 'S0', 'S1', 'S1', 'S1', 'S1', None],
                'Type': [
                    'TrialStart',
                    'StateStart',
                    'OutputAction',
                    'InputEvent',
                    'InputEvent',
                    'InputEvent',
                    'InputEvent',
                    None,
                    'TrialEnd',
                ],
                'Event': [None, None, None, 'Ev0', 'Tup', 'Low', 'Ev0', None, None],
                'Channel': [None, None, 'Valve1', None, None, None, None, None, None],
                'Value': [None, None, 1, None, 5, 0, 3, None, None],
            },
            schema=_TRIAL_DATA_SCHEMA,
        )
        assert_frame_equal(recorder.to_frame(), expected, categorical_as_str=True)

    def test_to_frame_without_events(self):
        """No events give an empty frame of the right schema."""
        frame = self._make_recorder().to_frame()
        assert frame.is_empty()
        assert list(frame.schema.items()) == list(_TRIAL_DATA_SCHEMA.items())

    def test_trial_number_beyond_uint16(self):
        """Trial numbers above 65535 are kept."""
        recorder = self._make_recorder(trial=70_000)
        recorder.append(0, _EventID.START_STATE, 0)
        assert recorder.to_frame()['Trial'].to_list() == [70_000]

    def test_buffer_doubles_and_keeps_its_rows(self, mocker):
        """Growing keeps every row, in order."""
        mocker.patch('bpod_core.bpod._threads._INITIAL_BUFFER_SIZE', 2)
        recorder = self._make_recorder()
        for time_us in range(5):
            recorder.append(time_us, 0, 0)
        assert len(recorder._buffer) == 8
        assert recorder.to_frame()['Time'].dt.epoch('us').to_list() == list(range(5))

    def test_one_row_frame_is_a_copy_of_the_buffer(self):
        """A single element isn't strided, which would keep polars from copying it."""
        recorder = self._make_recorder()
        recorder.append(10, 0, 0)
        frame = recorder.to_frame()
        recorder._buffer['Time'][0] = 0
        assert frame['Time'].dt.epoch('us').to_list() == [10]

    def test_peek_without_states_returns_the_events_so_far(self):
        """Without trigger states, peek returns at once."""
        recorder = self._make_recorder()
        recorder.append(10, 0, 0)
        assert recorder.peek().height == 1

    @pytest.mark.parametrize(
        ('states', 'message'),
        [
            (['nope'], "Unknown trigger state: 'nope'"),
            (['S0', 'x', 'y'], "Unknown trigger states: 'x', 'y'"),
        ],
    )
    def test_peek_rejects_unknown_states(self, states, message):
        """Trigger states must be part of the state machine."""
        recorder = self._make_recorder()
        recorder.finish()  # so that a missing check fails rather than waits
        with pytest.raises(ValueError, match=message):
            recorder.peek(states)

    def test_peek_returns_at_once_if_the_state_was_entered(self):
        """A state entered before the call needs no wake-up."""
        recorder = self._make_recorder()
        recorder.append(10, _EventID.START_STATE, 1)
        peeker, snapshots = _peek_in_background(recorder.peek, ['S1'])
        peeker.join(timeout=2)
        assert snapshots[0].height == 1

    def test_peek_waits_for_its_state(self, mocker):
        """Another state's wake-up doesn't end the wait; its own state's does."""
        # long enough that only the wake-up can end its wait within the test
        mocker.patch('bpod_core.bpod._threads._PEEK_RECHECK_S', 60)
        recorder = self._make_recorder()
        peeker, snapshots = _peek_in_background(recorder.peek, ['S1'])
        recorder.append(10, _EventID.START_STATE, 0)
        recorder.state_entered()
        time.sleep(0.02)
        assert not snapshots
        recorder.append(20, _EventID.START_STATE, 1)
        recorder.state_entered()
        peeker.join(timeout=2)
        assert snapshots[0]['State'].to_list() == ['S0', 'S1']

    def test_finish_ends_the_wait(self, mocker):
        """A state that's never entered doesn't leave peek waiting after the trial."""
        mocker.patch('bpod_core.bpod._threads._PEEK_RECHECK_S', 60)
        recorder = self._make_recorder()
        peeker, snapshots = _peek_in_background(recorder.peek, ['S1'])
        recorder.append(10, 0, 0)
        recorder.finish()
        peeker.join(timeout=2)
        assert snapshots[0].height == 1

    def test_peek_recovers_from_a_lost_wake_up(self):
        """Another caller's clear() can't leave a blocking peek waiting."""
        recorder = self._make_recorder()
        peeker, snapshots = _peek_in_background(recorder.peek, ['S1'])
        recorder._finished = True  # its final set() went to the other caller's clear()
        peeker.join(timeout=2)
        assert snapshots[0].is_empty()

    def test_decode_frees_the_buffer_and_answers_with_the_frame(self):
        """Once decoded, reads return the decoded frame, whatever is asked."""
        recorder = self._make_recorder()
        recorder.append(10, 0, 1)
        frame = recorder.to_frame()
        recorder.finish()
        recorder.decode()
        assert recorder._buffer.size == 0
        assert recorder._buffer.base is None
        for snapshot in (recorder.to_frame(), recorder.peek(), recorder.peek(['S0'])):
            assert_frame_equal(snapshot, frame, categorical_as_str=True)

    def test_decoded_frame_is_independent_of_its_copies(self):
        """Changing a frame it returned in place changes nothing in the recorder."""
        recorder = self._make_recorder()
        recorder.append(10, 0, 1)
        recorder.decode()
        recorder.to_frame().drop_in_place('Time')
        recorder.peek().drop_in_place('Time')
        assert 'Time' in recorder.peek().columns

    def test_read_overtaken_by_decode_returns_the_frame(self, mocker):
        """A read that finds the buffer already emptied still gets the trial's rows."""
        recorder = self._make_recorder()
        recorder.append(10, 0, 1)
        frame = recorder.to_frame()

        def decoded_first():
            recorder._frame = frame
            recorder._buffer = np.empty(0, dtype=_EVENT_DTYPE)
            return recorder._buffer

        mocker.patch.object(
            _Recorder,
            '_buffer_view',
            new_callable=mocker.PropertyMock,
            side_effect=decoded_first,
        )
        assert recorder.to_frame().height == 1

    def test_snapshots_only_see_rows_written_in_full(self, mocker):
        """A snapshot at any moment holds each row once, also while the buffer grows."""
        mocker.patch('bpod_core.bpod._threads._INITIAL_BUFFER_SIZE', 2)
        real_empty = np.empty

        class Buffer(np.ndarray):
            """Takes a snapshot just before each write."""

            def __setitem__(self, key, value):
                times = recorder.peek()['Time'].dt.epoch('us').to_list()
                assert times == list(range(len(times)))
                super().__setitem__(key, value)

        def empty(shape, dtype=float, **kwargs):
            if dtype is _EVENT_DTYPE:
                return np.zeros(shape, dtype=dtype).view(Buffer)
            return real_empty(shape, dtype=dtype, **kwargs)

        mocker.patch('bpod_core.bpod._threads.np.empty', empty)
        recorder = self._make_recorder()
        for time_us in range(9):
            recorder.append(time_us, 0, 0)
        assert len(recorder._buffer) == 16
        assert recorder.peek().height == 9


class TestEventThread:
    @pytest.fixture
    def make_thread(self):
        """Factory: builds an EventThread from a minimal FSM and starts it."""
        threads = []

        def _make(
            *,
            fsm: _StateMachineLookup | None = None,
            action_names: list[str] | None = None,
            resettable_actions: list[str] | None = None,
            event_names: list[str] | None = None,
            time_reference: _TimeReferences | None = None,
            publish: Callable[[list[BpodEventUnion]], object] | None = None,
            should_publish: Callable[[], bool] | None = None,
            on_trial_start: Callable[[int], None] | None = None,
            trial: int = 0,
        ) -> tuple[EventThread, SimpleQueue[pl.DataFrame | BpodError]]:
            fsm = fsm or _make_fsm()
            event_tables = _make_event_tables(
                event_names, None, action_names, resettable_actions
            )
            data_queue: SimpleQueue[pl.DataFrame | BpodError] = SimpleQueue()
            thread = EventThread(
                trial=_make_trial(trial, fsm, event_tables),
                fsm=fsm,
                data_queue=data_queue,
                time_reference=time_reference or _TimeReferences(0, 0, 0),
                publish=publish if publish is not None else lambda _: None,
                publish_gate=should_publish
                if should_publish is not None
                else (lambda: True),
                on_trial_start=on_trial_start,
            )
            thread.start()
            threads.append(thread)
            return thread, data_queue

        yield _make

        for t in threads:
            t.stop()
            t.join(timeout=2)

    def _next_trial(
        self, data_queue: SimpleQueue[pl.DataFrame | BpodError]
    ) -> pl.DataFrame | BpodError:
        return data_queue.get(timeout=2)

    def test_stop_exits_thread(self, make_thread):
        """stop() causes the thread to exit cleanly."""
        thread, _ = make_thread()
        thread.stop()
        thread.join(timeout=2)
        assert not thread.is_alive()

    def test_data_enqueued_on_stop(self, make_thread):
        """A trial is pushed to data_queue when the thread exits."""
        thread, data_queue = make_thread()
        thread.stop()
        thread.join(timeout=2)
        assert not data_queue.empty()

    def test_on_trial_start_fires_regardless_of_publish_gate(self, make_thread):
        """on_trial_start fires with the absolute time even when publish_gate=False."""
        captured = []
        thread, _data_queue = make_thread(
            time_reference=_TimeReferences(0, 0, 5_000_000),  # +5000 us offset
            should_publish=lambda: False,
            on_trial_start=captured.append,
        )
        thread.queue.put([_RawEvents(micros_us=100, event_ids=(_EventID.START_FSM,))])
        thread.queue.put([_RawEvents(micros_us=100, event_ids=(_EventID.START_STATE,))])
        thread.stop()
        thread.join(timeout=2)
        assert captured == [5100]

    @staticmethod
    def _put_full_trial(thread: EventThread) -> None:
        """Enqueue a complete trial as one list: start, a transition, the exit."""
        thread.queue.put(
            [
                _RawEvents(micros_us=0, event_ids=(_EventID.START_FSM,)),
                _RawEvents(micros_us=0, event_ids=(_EventID.START_STATE,)),
                _RawEvents(micros_us=10, event_ids=(0,)),
                _RawEvents(micros_us=20, event_ids=(_EventID.END_FSM_CYCLES,)),
                _RawEvents(micros_us=25, event_ids=(_EventID.END_FSM_MICROS,)),
            ]
        )

    def test_published_events_match_dataframe(self, make_thread):
        """Published messages mirror the trial DataFrame's event rows in order."""
        published = []
        fsm = _make_fsm(transitions={(0, 0): 1})
        thread, data_queue = make_thread(fsm=fsm, publish=published.extend)
        self._put_full_trial(thread)
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)

        # the stream is framed by TrialStart / TrialEnd, terminated by the
        # trailing TrialEndControl record
        start, *events, end, end_control = published
        assert isinstance(start, EventTrialStart)
        assert isinstance(end, EventTrialEnd)
        assert isinstance(end_control, EventTrialEndControl)
        assert start.trial == end.trial == end_control.trial == 0
        assert start.fsm_hash == ''
        assert end.time_us == 20
        assert end_control.time_us == 25
        # n_events counts TrialStart + content events + TrialEnd + itself
        assert end_control.n_events == len(events) + 3

        # published event messages correspond 1:1 to the DataFrame's event rows
        struct_by_type = {
            'StateStart': EventStateStart,
            'StateEnd': EventStateEnd,
            'InputEvent': EventInput,
            'OutputAction': EventOutput,
        }
        rows = df.filter(pl.col('Type').cast(pl.String).is_in(list(struct_by_type)))
        types = rows['Type'].cast(pl.String).to_list()
        assert [type(m) for m in events] == [struct_by_type[t] for t in types]
        times = rows['Time'].dt.epoch(time_unit='us').to_list()
        assert [m.time_us for m in events] == times

    def test_should_publish_gates_all_messages(self, make_thread):
        """should_publish=False skips publishing entirely; trial data remains."""
        published = []
        thread, data_queue = make_thread(
            publish=published.extend, should_publish=lambda: False
        )
        self._put_full_trial(thread)
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        assert df.height > 0, 'trial data must be unaffected by the gate'
        assert published == []

    def test_unknown_event_id_decodes_to_nulls(self, make_thread):
        """An event code the hardware doesn't name is kept, with nothing to report."""
        thread, data_queue = make_thread(should_publish=lambda: False)
        thread.queue.put([_RawEvents(micros_us=10, event_ids=(200,))])
        thread.stop()
        thread.join(timeout=2)
        row = self._next_trial(data_queue).row(0, named=True)
        assert row['Time'].timestamp() == pytest.approx(10e-6)
        assert row['Trial'] == 0
        assert row['State'] == 'S0'
        assert [row[c] for c in ('Type', 'Event', 'Channel', 'Value')] == [None] * 4

    @pytest.mark.parametrize('error', [RuntimeError(), pl.exceptions.PanicException()])
    def test_failed_decode_queues_the_error(self, mocker, make_thread, caplog, error):
        """A trial that can't be decoded is logged; get_data() isn't left waiting."""
        thread, data_queue = make_thread()
        mocker.patch.object(thread.trial.recorder, 'to_frame', side_effect=error)
        thread.stop()
        thread.join(timeout=2)
        item = self._next_trial(data_queue)
        assert isinstance(item, BpodError)
        assert 'Failed to decode trial #0' in str(item)
        assert item.__cause__ is error
        assert 'Failed to decode trial #0' in caplog.text
        assert thread.trial.recorder._finished  # a blocking peek isn't left waiting
        assert thread.trial.decoded.is_set()
        assert thread.trial.decode_error is item
        assert thread.trial.recorder._buffer.size > 0  # a later peek raises, too

    def test_recorder_keeps_the_decoded_frame(self, make_thread):
        """The queued frame is what the recorder holds, and the buffer is freed."""
        thread, data_queue = make_thread()
        thread.queue.put([_RawEvents(micros_us=10, event_ids=(0,))])
        thread.stop()
        thread.join(timeout=2)
        recorder = thread.trial.recorder
        assert thread.trial.decoded.is_set()
        assert thread.trial.decode_error is None
        assert recorder._buffer.size == 0
        assert_frame_equal(
            recorder.to_frame(), self._next_trial(data_queue), categorical_as_str=True
        )

    def test_recording_is_finished_before_decoding(self, mocker, make_thread):
        """A slow or stuck decode can't hold up a blocking peek."""
        thread, _ = make_thread()
        recorder = thread.trial.recorder
        finished = []
        mocker.patch.object(
            recorder,
            'decode',
            side_effect=lambda: finished.append(recorder._finished),
        )
        thread.stop()
        thread.join(timeout=2)
        assert finished == [True]

    def test_trial_start_message_names_the_trial_and_its_machine(self, make_thread):
        """The published trial start carries the trial's number and machine's hash."""
        published = []
        thread, _ = make_thread(
            fsm=_make_fsm()._replace(fsm_hash=b'\xab\xcd'),
            publish=published.extend,
            trial=3,
        )
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(_EventID.START_FSM,))])
        thread.stop()
        thread.join(timeout=2)
        start = next(m for m in published if isinstance(m, EventTrialStart))
        assert (start.trial, start.fsm_hash) == (3, 'abcd')

    def test_publish_gate_is_checked_once_per_batch(self, mocker, make_thread):
        """The gate's answer holds for one batch: subscribers can come and go."""
        published = []
        gate = mocker.Mock(side_effect=[False, True, False])
        thread, _ = make_thread(publish=published.extend, should_publish=gate)
        thread.queue.put(
            [
                _RawEvents(micros_us=10, event_ids=(0,)),
                _RawEvents(micros_us=20, event_ids=(0,)),
            ]
        )
        thread.queue.put([_RawEvents(micros_us=30, event_ids=(0,))])
        thread.queue.put([_RawEvents(micros_us=40, event_ids=(0,))])
        thread.stop()
        thread.join(timeout=2)
        assert [m.time_us for m in published if isinstance(m, EventInput)] == [30]

    @pytest.mark.parametrize(
        ('state', 'batches'),
        [
            ('S0', [(_EventID.START_STATE,)]),  # the initial state
            ('S1', [(_EventID.START_STATE,), (0,)]),  # a transition, flag already set
        ],
    )
    def test_peek_data_wakes_when_its_state_starts(
        self, mocker, make_thread, state, batches
    ):
        """A waiting peek_data returns once the state starts, before the trial ends."""
        # long enough that only the wake-up can end its wait within the test
        mocker.patch('bpod_core.bpod._threads._PEEK_RECHECK_S', 60)
        thread, _ = make_thread(fsm=_make_fsm(transitions={(0, 0): 1}))
        peeker, snapshots = _peek_in_background(thread.peek_data, [state])
        for event_ids in batches:
            thread.queue.put([_RawEvents(micros_us=0, event_ids=event_ids)])
            time.sleep(0.02)  # let it block again, so that it depends on the wake-up
        peeker.join(timeout=2)
        assert snapshots
        assert state in snapshots[0]['State'].to_list()

    def test_events_after_fsm_exit_trigger_no_transitions(self, make_thread, caplog):
        """Events arriving after an exit without successor are recorded but inert."""
        published = []
        # event 0 in S0 exits without a successor (target == n_states)
        fsm = _make_fsm(transitions={(0, 0): 2})
        with caplog.at_level(logging.ERROR, logger='bpod_core.bpod._threads'):
            thread, data_queue = make_thread(fsm=fsm, publish=published.extend)
            thread.queue.put(
                [_RawEvents(micros_us=0, event_ids=(_EventID.START_STATE,))]
            )
            thread.queue.put([_RawEvents(micros_us=10, event_ids=(0,))])  # S0 -> exit
            thread.queue.put([_RawEvents(micros_us=20, event_ids=(0,))])  # after exit
            thread.stop()
            thread.join(timeout=2)
        df = self._next_trial(data_queue)

        # the post-exit event is published but triggers no phantom transition
        state_messages = [
            m for m in published if isinstance(m, (EventStateStart, EventStateEnd))
        ]
        assert [type(m) for m in state_messages] == [EventStateStart, EventStateEnd]
        assert [m.time_us for m in published if isinstance(m, EventInput)] == [10, 20]
        assert not any('publish' in record.message for record in caplog.records)

        # the DataFrame records the post-exit event without an active state
        assert df.filter(pl.col('Type') == 'StateStart').height == 1
        assert df.filter(pl.col('Type') == 'StateEnd').height == 1
        post_exit = df.filter(pl.col('Time').dt.epoch(time_unit='us') == 20)
        assert post_exit['State'].to_list() == [None]

    def test_aborted_trial_publishes_no_trial_end(self, make_thread):
        """Without a hardware exit packet, no end messages are published."""
        published = []
        thread, data_queue = make_thread(publish=published.extend)
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(_EventID.START_FSM,))])
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(0,))])
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        assert df.height > 0
        assert isinstance(published[0], EventTrialStart)
        assert not any(
            isinstance(m, (EventTrialEnd, EventTrialEndControl)) for m in published
        )

    def test_publish_failure_does_not_interrupt_recording(self, make_thread, caplog):
        """A raising publish callable is logged but does not affect the trial data."""

        def broken_publish(_: list[BpodEventUnion]) -> None:
            raise RuntimeError('boom')

        with caplog.at_level(logging.ERROR, logger='bpod_core.bpod._threads'):
            thread, data_queue = make_thread(publish=broken_publish)
            thread.queue.put([_RawEvents(micros_us=0, event_ids=(0,))])
            thread.stop()
            thread.join(timeout=2)
        df = self._next_trial(data_queue)
        assert df.height > 0
        assert any('publish' in record.message for record in caplog.records)

    def test_hardware_event_recorded(self, make_thread):
        """Hardware event appears in output with correct event name."""
        thread, data_queue = make_thread(event_names=['Ev0', 'Tup'])
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(0,))])
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        assert 'Ev0' in df.filter(pl.col('Type') == 'InputEvent')['Event'].to_list()

    def test_event_timestamp(self, make_thread):
        """Event timestamp equals reset_system_time_ns // 1000 + micros_us."""
        time_ref = _TimeReferences(0, 0, reset_system_time_ns=5_000_000)
        thread, data_queue = make_thread(
            event_names=['Ev0', 'Tup'], time_reference=time_ref
        )
        thread.queue.put([_RawEvents(micros_us=100, event_ids=(0,))])
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        ev = df.filter(pl.col('Event') == 'Ev0')
        assert (
            ev['Time'][0]
            == pl.Series([5100], dtype=pl.Datetime('us', time_zone='UTC'))[0]
        )  # 5000 + 100 µs

    def test_state_transition_generates_state_events(self, make_thread):
        """State transition generates StateEnd for old state and StateStart for new."""
        # event 0 triggers S0 → S1
        fsm = _make_fsm(transitions={(0, 0): 1})
        thread, data_queue = make_thread(fsm=fsm)
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(0,))])
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        types = df['Type'].cast(pl.String).to_list()
        assert 'StateEnd' in types
        assert 'StateStart' in types

    def test_state_names_in_output(self, make_thread):
        """State column contains the correct state names after a transition."""
        fsm = _make_fsm(transitions={(0, 0): 1})
        thread, data_queue = make_thread(fsm=fsm)
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(0,))])
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        states = df['State'].drop_nulls().cast(pl.String).to_list()
        assert 'S0' in states
        assert 'S1' in states

    @pytest.mark.parametrize(
        ('transitions', 'target'),
        [
            # in S0, Ev0 leads to S1 and Ev1 to S2; in S1, Ev1 would lead to S2 too
            ({(0, 0): 1, (0, 1): 2, (1, 1): 2}, 'S1'),
            # in S0, only Ev1 leads elsewhere
            ({(0, 1): 2}, 'S2'),
        ],
        ids=['first event', 'second event'],
    )
    def test_one_transition_per_cycle(self, make_thread, transitions, target):
        """Only the first of a cycle's events leading elsewhere makes a transition."""
        fsm = _make_fsm(n_states=3, transitions=transitions)
        thread, data_queue = make_thread(fsm=fsm, event_names=['Ev0', 'Ev1', 'Tup'])
        thread.queue.put([_RawEvents(micros_us=10, event_ids=(0, 1))])
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        rows = df.select(
            pl.col('Type').cast(pl.String),
            pl.col('Event').cast(pl.String),
            pl.col('State').cast(pl.String),
        ).rows()
        assert rows == [
            ('InputEvent', 'Ev0', 'S0'),
            ('InputEvent', 'Ev1', 'S0'),
            ('StateEnd', None, 'S0'),
            ('StateStart', None, target),
        ]

    def test_same_cycle_event_does_not_act_on_next_state(self, make_thread):
        """An event in the cycle of a transition can't end the state it leads to."""
        # in S0, Ev0 leads to S1; in S1, Tup leads to >exit
        fsm = _make_fsm(transitions={(0, 0): 1, (1, 1): 2})
        thread, data_queue = make_thread(fsm=fsm)
        thread.queue.put([_RawEvents(micros_us=10, event_ids=(0, 1))])  # Tup ignored
        thread.queue.put([_RawEvents(micros_us=20, event_ids=(1,))])  # S1's Tup, exits
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        rows = df.select(
            pl.col('Time').dt.epoch(time_unit='us'),
            pl.col('Type').cast(pl.String),
            pl.col('State').cast(pl.String),
        ).filter(pl.col('Type') != 'StateStart')
        assert rows.rows() == [
            (10, 'InputEvent', 'S0'),
            (10, 'InputEvent', 'S0'),
            (10, 'StateEnd', 'S0'),
            (20, 'InputEvent', 'S1'),
            (20, 'StateEnd', 'S1'),
        ]

    def test_exit_waits_for_end_of_cycle(self, make_thread):
        """Events in the cycle of an exit are recorded in the state that exits."""
        fsm = _make_fsm(transitions={(0, 0): 2})  # in S0, Ev0 leads to >exit
        thread, data_queue = make_thread(fsm=fsm)
        thread.queue.put([_RawEvents(micros_us=10, event_ids=(0, 1))])  # Ev0, then Tup
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        rows = df.select(
            pl.col('Type').cast(pl.String), pl.col('State').cast(pl.String)
        ).rows()
        assert rows == [
            ('InputEvent', 'S0'),
            ('InputEvent', 'S0'),
            ('StateEnd', 'S0'),
        ]

    def test_output_action_recorded(self, make_thread):
        """Output actions for the current state are recorded on START_STATE."""
        fsm = _make_fsm(state_action_indices=[{0: 128}, {}])
        thread, data_queue = make_thread(fsm=fsm, action_names=['PWM1'])
        # START_STATE triggers initial output recording for S0
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(_EventID.START_STATE,))])
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        output = df.filter(pl.col('Type') == 'OutputAction')
        assert 'PWM1' in output['Channel'].cast(pl.String).to_list()

    def test_output_action_value_above_uint8_range(self, make_thread):
        """Output values above 255 (e.g. scaled FlexIO analog-output counts) survive."""
        fsm = _make_fsm(state_action_indices=[{0: 2048}, {}])
        thread, data_queue = make_thread(fsm=fsm, action_names=['Flex4'])
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(_EventID.START_STATE,))])
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        output = df.filter(pl.col('Channel') == 'Flex4')
        assert output['Value'].to_list() == [2048]

    def test_ttl_reset_on_state_transition(self, make_thread):
        """TTL output is reset to 0."""
        # S0 sets TTL1=1; S1 has no actions; event 0 triggers S0→S1
        fsm = _make_fsm(
            transitions={(0, 0): 1},
            state_action_indices=[{0: 1}, {}],
        )
        thread, data_queue = make_thread(fsm=fsm, action_names=['TTL1'])
        # START_STATE seeds active_outputs with TTL1=1 for S0
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(_EventID.START_STATE,))])
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(0,))])  # S0 → S1
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        ttl = df.filter(
            (pl.col('Type') == 'OutputAction') & (pl.col('Channel') == 'TTL1')
        )
        assert 0 in ttl['Value'].to_list()

    def test_non_resettable_action_not_reset(self, make_thread):
        """An action the Bpod doesn't reset, e.g. a module message, isn't reset."""
        fsm = _make_fsm(transitions={(0, 0): 1}, state_action_indices=[{0: 3}, {}])
        thread, data_queue = make_thread(
            fsm=fsm, action_names=['Module1'], resettable_actions=[]
        )
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(_EventID.START_STATE,))])
        thread.queue.put([_RawEvents(micros_us=0, event_ids=(0,))])  # S0 → S1
        thread.stop()
        thread.join(timeout=2)
        df = self._next_trial(data_queue)
        module = df.filter(pl.col('Channel') == 'Module1')
        assert module['Value'].to_list() == [3]

    def test_peek_data_after_the_thread_ended(self, make_thread):
        """A blocking peek_data() returns once the thread is done."""
        thread, _ = make_thread()
        thread.stop()
        thread.join(timeout=2)
        peeker, snapshots = _peek_in_background(thread.peek_data, ['S1'])
        peeker.join(timeout=1)
        assert snapshots


class TestSoftcodeThread:
    @pytest.fixture
    def make_thread(self):
        """Factory: returns a started SoftcodeThread (daemon)."""
        slots = []  # the thread references its handler slot only weakly

        def _make(handler=None, *, slot=None):
            if slot is None:
                slot = _HandlerSlot(handler)
                slots.append(slot)
            thread = SoftcodeThread(handler_slot=slot)
            thread.start()
            return thread

        return _make

    def test_handler_called(self, make_thread):
        """Handler is called with the correct softcode value."""
        received = []
        done = threading.Event()

        def handler(code):
            received.append(code)
            done.set()

        thread = make_thread(handler=handler)
        thread.queue.put(_RawSoftcode(7, 0, 0))
        assert done.wait(timeout=2)
        assert received == [7]

    def test_stop(self, make_thread):
        """stop() handles the queued softcodes first, then ends the thread."""
        received = []
        thread = make_thread(handler=received.append)
        thread.queue.put(_RawSoftcode(3, 0, 0))
        thread.stop()
        assert received == [3]
        assert not thread.is_alive()

    def test_stop_from_handler(self, make_thread):
        """stop() called from within a handler ends the thread without raising."""
        done = threading.Event()

        def handler(_):
            thread.stop()
            done.set()

        thread = make_thread(handler=handler)
        thread.queue.put(_RawSoftcode(1, 0, 0))
        assert done.wait(timeout=2)
        thread.join(timeout=2)
        assert not thread.is_alive()

    def test_handler_called_in_order(self, make_thread):
        """Handler is called once per softcode in the order received."""
        received = []
        done = threading.Event()

        def handler(code):
            received.append(code)
            if len(received) == 3:
                done.set()

        thread = make_thread(handler=handler)
        for code in [1, 5, 3]:
            thread.queue.put(_RawSoftcode(code, 0, 0))
        assert done.wait(timeout=2)
        assert received == [1, 5, 3]

    def test_no_handler_logs_warning(self, make_thread, caplog):
        """A warning is logged when no handler is defined."""
        thread = make_thread(handler=None)
        with caplog.at_level(logging.WARNING):
            thread.queue.put(_RawSoftcode(4, 0, 0))
            # give the thread time to process and log
            deadline = time.time() + 1
            while (time.time() < deadline) and len(caplog.text) == 0:
                time.sleep(0.0005)
        assert '4' in caplog.text

    def test_handler_exception_logged_and_continues(self, make_thread, caplog):
        """An exception in the handler is logged and processing continues."""
        received = []
        done = threading.Event()

        def handler(code):
            if code == 0:
                raise ValueError('bad softcode')
            received.append(code)
            done.set()

        thread = make_thread(handler=handler)
        with caplog.at_level(logging.ERROR):
            thread.queue.put(_RawSoftcode(0, 0, 0))
            thread.queue.put(_RawSoftcode(9, 0, 0))
            assert done.wait(timeout=2)
        assert received == [9]
        assert 'bad softcode' in caplog.text

    def test_swap_handler(self, make_thread):
        """A new handler in the slot is used for subsequent softcodes."""
        received = []
        done = threading.Event()

        def handler_b(code):
            received.append(('b', code))
            done.set()

        slot = _HandlerSlot(lambda code: received.append(('a', code)))
        thread = make_thread(slot=slot)
        slot.handler = handler_b
        thread.queue.put(_RawSoftcode(3, 0, 0))
        assert done.wait(timeout=2)
        assert received == [('b', 3)]

    def test_softcodes_dropped_without_slot(self, make_thread, caplog):
        """Once the handler slot is gone, softcodes are dropped without warnings."""
        slot = _HandlerSlot(None)
        thread = make_thread(slot=slot)
        del slot
        thread.queue.put(_RawSoftcode(1, 0, 0))
        thread.stop()
        assert 'no handler' not in caplog.text

    @pytest.fixture
    def gated_thread(self):
        """A started SoftcodeThread slow to pick up its next softcode, and its gate."""
        received = []
        gate = threading.Event()

        class GatedQueue(SimpleQueue):
            def get(self, *args, **kwargs):
                gate.wait()
                return super().get(*args, **kwargs)

        slot = _HandlerSlot(received.append)
        thread = SoftcodeThread(handler_slot=slot)
        thread.queue = GatedQueue()
        thread.start()
        yield thread, gate, received
        gate.set()
        thread.stop()

    def test_drain_waits_for_queued_softcodes(self, gated_thread):
        """drain() returns only once the softcodes queued before it are handled."""
        thread, gate, received = gated_thread
        thread.queue.put(_RawSoftcode(5, 0, 0))
        drainer = threading.Thread(target=thread.drain, daemon=True)
        drainer.start()
        drainer.join(timeout=0.05)
        assert drainer.is_alive()  # still waiting for softcode 5
        gate.set()
        drainer.join(timeout=2)
        assert not drainer.is_alive()
        assert received == [5]

    def test_drain_deadline(self, gated_thread, caplog):
        """drain(deadline) reports whether the softcodes were handled in time."""
        thread, gate, _ = gated_thread
        assert not thread.drain(time.monotonic() + 0.05)
        assert 'Waiting for softcodes' not in caplog.text  # unbounded waits warn
        gate.set()
        assert thread.drain(time.monotonic() + 2)

    def test_drain_after_stop(self, make_thread):
        """drain() returns once the thread stops, even before reaching its marker."""
        release = threading.Event()
        thread = make_thread(handler=lambda _: release.wait())
        thread.queue.put(_RawSoftcode(1, 0, 0))
        thread.queue.put(_SOFTCODE_STOP)  # as stop() does, without waiting
        drainer = threading.Thread(target=thread.drain, daemon=True)
        drainer.start()
        drainer.join(timeout=0.05)
        assert drainer.is_alive()  # its marker is queued behind the stop sentinel
        release.set()
        drainer.join(timeout=2)
        assert not drainer.is_alive()

    def test_drain_from_handler(self, make_thread):
        """drain() called from within a handler returns instead of waiting forever."""
        done = threading.Event()

        def handler(_):
            thread.drain()
            done.set()

        thread = make_thread(handler=handler)
        thread.queue.put(_RawSoftcode(1, 0, 0))
        assert done.wait(timeout=2)


class TestJoinUntil:
    def test_true_once_the_thread_ends(self, blocked_thread):
        threading.Timer(0.02, blocked_thread.release.set).start()
        assert join_until(blocked_thread, time.monotonic() + 2)
        assert not blocked_thread.is_alive()

    def test_false_at_the_deadline(self, blocked_thread):
        t0 = time.monotonic()
        assert not join_until(blocked_thread, t0 + 0.05)
        assert 0.05 <= time.monotonic() - t0 < 0.5
        assert blocked_thread.is_alive()

    def test_joins_in_slices(self, blocked_thread, mocker):
        """Even an unbounded wait returns to Python regularly: Ctrl+C gets through."""
        mocker.patch('bpod_core.bpod._threads._JOIN_SLICE_S', 0.01)
        timeouts = []
        join = blocked_thread.join

        def counted_join(timeout=None):
            timeouts.append(timeout)
            if len(timeouts) == 3:  # the thread ends only after a few slices
                blocked_thread.release.set()
            join(timeout)

        mocker.patch.object(blocked_thread, 'join', side_effect=counted_join)
        assert join_until(blocked_thread, None)
        assert len(timeouts) >= 3
        assert all(timeout <= 0.01 for timeout in timeouts)


def test_polars_imports_numpy_ma_lazily():
    """The reason _threads imports numpy.ma up front; drop both if this fails."""
    pytest.importorskip('polars', minversion='2.1')  # 2.0 never imports numpy.ma
    script = (
        'import sys, numpy, polars\n'
        "assert 'numpy.ma' not in sys.modules\n"
        'polars.from_numpy(numpy.zeros(1))\n'
        "assert 'numpy.ma' in sys.modules\n"
    )
    subprocess.run([sys.executable, '-c', script], check=True)  # noqa: S603
