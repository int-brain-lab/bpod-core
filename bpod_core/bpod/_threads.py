"""Threads for FSM execution and data collection from the Bpod hardware."""

import logging
import struct
import threading
import time
import weakref
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum, IntEnum, auto
from queue import Empty, SimpleQueue
from typing import NamedTuple, get_args

import numpy as np

# polars (2.1 and later) imports numpy.ma on its first array conversion: about
# 10 ms that would otherwise delay the end of a process's first trial
import numpy.ma
import numpy.typing as npt
import polars as pl

from bpod_core.bpod.errors import BpodError
from bpod_core.bpod.structs import (
    BpodEventType,
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
from bpod_core.com import ExtendedSerial, ReadCancelled
from bpod_core.constants import STRUCT_UINT32_LE, STRUCT_UINT64_LE
from bpod_core.misc import _grown, _HandlerSlot

logger = logging.getLogger(__name__)

_TIMING_VIOLATION_THRESHOLD_US = 1_000
"""Threshold for logging timing violations in microseconds."""

_INITIAL_BUFFER_SIZE = 2048
"""Initial size of the event buffer in :class:`_Recorder`."""

_OUTPUT_ID_OFFSET = 1000
"""Event ID offset for output action events."""

# while idle, the Bpod answers 'X' with a trial-end report: opcode 1, one event, event
# code 255 (end of trial), then 2 x uInt32 cycle counts and a uInt64 timestamp
_IDLE_STOP_REPLY_HEADER = b'\x01\x01\xff'
_IDLE_STOP_REPLY_SIZE = len(_IDLE_STOP_REPLY_HEADER) + 16

_DRAIN_GRACE_S = 0.1
"""Wait for a drain marker past the deadline: an idle thread reaches it at once.

Generous, so that a trial that finished in time never fails on its softcodes.
"""

_SOFTCODE_STOP = _RawSoftcode(-1, 0, 0)
"""Sentinel that ends :class:`SoftcodeThread` once the softcodes before it are done."""


class _DrainMarker(_RawSoftcode):
    """Queued by :meth:`SoftcodeThread.drain`; ``done`` is set once it is reached."""

    done: threading.Event


_PEEK_RECHECK_S = 0.1
"""Longest wait of a blocking ``peek()``: another caller can clear its wake-up."""

_WAIT_SLICE_S = 0.5
"""Longest single wait on an event: between slices, Ctrl+C reaches the caller.

polars installs a SIGINT handler with ``SA_RESTART`` (pola-rs/polars#21739), so an
unbounded ``Thread.join`` would swallow Ctrl+C until the thread ends.
"""


def wait_until(event: threading.Event, deadline: float | None) -> bool:
    """Wait for `event`, until `deadline` (:func:`time.monotonic`) if given.

    Returns True once the event is set, False if the deadline passed first.
    """
    while not event.is_set():
        remaining = _WAIT_SLICE_S if deadline is None else deadline - time.monotonic()
        if remaining <= 0:
            return False
        event.wait(min(remaining, _WAIT_SLICE_S))
    return True


class _LateStarts:
    """A connection's trials that started over 1.5 cycles after the trial before ended.

    A start later than all before it is logged as a warning, the others at debug level;
    :meth:`summarize` counts them all.
    """

    def __init__(self) -> None:
        self.count = 0
        self.longest_us = 0

    def record(self, trial: int, late_us: int) -> None:
        """Log that `trial` started `late_us` µs after the cycle it should have."""
        logger.log(
            logging.WARNING if late_us > self.longest_us else logging.DEBUG,
            "Trial #%d started %.1f ms late: its state machine wasn't ready in time",
            trial,
            late_us / 1000,
        )
        self.count += 1
        self.longest_us = max(self.longest_us, late_us)

    def summarize(self) -> None:
        """Log how many trials started late, if more than the first, and start over."""
        if self.count > 1:
            logger.warning(
                '%d trials started late, by up to %.1f ms',
                self.count,
                self.longest_us / 1000,
            )
        self.count = self.longest_us = 0


_EVENT_DTYPE = np.dtype(
    [
        ('Time', np.int64),  # microseconds since the epoch
        ('event_id', np.int16),
        ('state_id', np.int16),
        ('value', np.int16),
    ]
)
"""Structured NumPy dtype for recorded events."""


class _EventID(IntEnum):
    """Enum for event codes."""

    EXIT = 255
    """Exit code for the Bpod."""
    START_FSM = auto()
    START_STATE = auto()
    END_STATE = auto()
    END_FSM_CYCLES = auto()
    END_FSM_MICROS = auto()


class _Mark(Enum):
    """Marks on :class:`EventThread`'s queue, between the trials and their events."""

    TRIAL_END = auto()
    """The trial's events are complete."""
    STOP = auto()
    """The thread is to end."""


def _join_or_warn(thread: threading.Thread) -> None:
    """Wait a second for `thread` to end, and warn if it hasn't."""
    thread.join(timeout=1)
    if thread.is_alive():
        logger.warning('%s did not terminate', thread.name)


_SYNTHETIC_EVENT_TYPES: dict[_EventID, str] = {
    _EventID.START_FSM: 'TrialStart',
    _EventID.START_STATE: 'StateStart',
    _EventID.END_STATE: 'StateEnd',
    _EventID.END_FSM_CYCLES: 'TrialEnd',
    _EventID.END_FSM_MICROS: 'TrialEndControl',
}
"""Names for synthetic events generated by :class:`ReadThread`."""

_EVENT_TYPE_ENUM = pl.Enum(get_args(BpodEventType))
"""Polars Enum dtype for the ``type`` column in trial DataFrames.

Built from :data:`~bpod_core.bpod.structs.BpodEventType`.
"""

_TRIAL_DATA_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    'Time': pl.Datetime('us', time_zone='UTC'),
    'Trial': pl.UInt32,
    'State Machine': pl.Categorical,
    'State': pl.Categorical,
    'Type': _EVENT_TYPE_ENUM,
    'Event': pl.Categorical,
    'Channel': pl.Categorical,
    'Value': pl.UInt16,
}
"""Column schema for trial DataFrames returned by :meth:`~bpod_core.bpod.Bpod.get_data`
and :meth:`~bpod_core.bpod.Bpod.peek_data`."""

_SYNTHETIC_EVENT_LOOKUP = pl.DataFrame(
    {
        'event_id': pl.Series(list(_SYNTHETIC_EVENT_TYPES), dtype=pl.Int16),
        'Type': pl.Series(
            list(_SYNTHETIC_EVENT_TYPES.values()), dtype=_EVENT_TYPE_ENUM
        ),
        'Event': pl.Series([None] * len(_SYNTHETIC_EVENT_TYPES), dtype=pl.Null),
        'Channel': pl.Series([None] * len(_SYNTHETIC_EVENT_TYPES), dtype=pl.Null),
        'default_value': pl.Series(
            [None] * len(_SYNTHETIC_EVENT_TYPES), dtype=pl.UInt16
        ),
    }
)
"""Pre-built lookup rows for synthetic FSM events (stable across all sessions)."""


def _build_event_lookup(
    input_events: _InputEvents, action_names: list[str]
) -> pl.DataFrame:
    """Build the event metadata lookup DataFrame.

    Parameters
    ----------
    input_events : _InputEvents
        Hardware-level input event names and their source channels.
    action_names : list of str
        Names of all output channels, indexed by action ID.

    Returns
    -------
    pl.DataFrame
        DataFrame with columns ``event_id`` (Int16), ``Type`` (Enum),
        ``Event`` (Categorical), ``Channel`` (Categorical), and
        ``default_value`` (UInt16, null if no pre-defined value),
        covering input events, synthetic events, and output actions.
    """
    output_ids = [_OUTPUT_ID_OFFSET + i for i in range(len(action_names))]
    input_lookup = pl.DataFrame(
        {
            'event_id': pl.Series(range(len(input_events.names)), dtype=pl.Int16),
            'Type': pl.Series(
                ['InputEvent'] * len(input_events.names), dtype=_EVENT_TYPE_ENUM
            ),
            'Event': pl.Series(input_events.names, dtype=pl.Categorical),
            'Channel': pl.Series(input_events.channels, dtype=pl.Categorical),
            'default_value': pl.Series(input_events.values, dtype=pl.UInt16),
        }
    )
    output_lookup = pl.DataFrame(
        {
            'event_id': pl.Series(output_ids, dtype=pl.Int16),
            'Type': pl.Series(
                ['OutputAction'] * len(action_names), dtype=_EVENT_TYPE_ENUM
            ),
            'Event': pl.Series([None] * len(action_names), dtype=pl.Categorical),
            'Channel': pl.Series(action_names, dtype=pl.Categorical),
            'default_value': pl.Series([None] * len(action_names), dtype=pl.UInt16),
        }
    )
    return pl.concat([input_lookup, _SYNTHETIC_EVENT_LOOKUP, output_lookup])


class _EventTables(NamedTuple):
    """Lookup tables for decoding a trial's events, fixed per hardware configuration.

    Built once by :func:`_build_event_tables` so that :class:`EventThread` does not
    rebuild them for every trial.
    """

    event_info: dict[int, tuple[BpodEventType, str | None, str | None, int | None]]
    """Type, event name, channel and default value of each event ID."""
    resettable_indices: frozenset[int]
    """Indices of the outputs that the Bpod resets to 0 at each state transition."""
    decode_table: pl.DataFrame
    """Metadata by event ID, see :func:`_build_event_lookup`: nulls for unknown IDs."""
    default_values: npt.NDArray[np.int32]
    """Default value by event ID: -1 for none."""


def _build_event_tables(
    input_events: _InputEvents,
    action_names: list[str],
    resettable_actions: Collection[str],
) -> _EventTables:
    """Build the lookup tables for decoding a trial's events.

    Parameters
    ----------
    input_events : _InputEvents
        Hardware-level input event names and their source channels.
    action_names : list of str
        Names of all output channels, indexed by action ID.
    resettable_actions : Collection of str
        Names of the actions driving outputs that the Bpod resets to 0 when a state
        begins that doesn't set them.

    Returns
    -------
    _EventTables
        The lookup tables.
    """
    lookup = _build_event_lookup(input_events, action_names)
    action_indices = {name: i for i, name in enumerate(action_names)}
    # room for every ID: the Bpod's event codes and action indices are single bytes
    decode_table = pl.DataFrame(
        {'event_id': pl.int_range(_OUTPUT_ID_OFFSET + 256, dtype=pl.Int16, eager=True)}
    ).join(lookup, on='event_id', how='left', maintain_order='left')
    return _EventTables(
        event_info={
            row['event_id']: (
                row['Type'],
                row['Event'],
                row['Channel'],
                row['default_value'],
            )
            for row in lookup.iter_rows(named=True)
        },
        resettable_indices=frozenset(
            action_indices[name] for name in resettable_actions
        ),
        decode_table=decode_table,
        default_values=decode_table['default_value']
        .cast(pl.Int32)
        .fill_null(-1)
        .to_numpy(),
    )


class _Recorder:
    """A trial's event buffer: appended by the event thread, read by anyone.

    **Buffer design**: events are appended to a pre-allocated NumPy structured array
    (``_EVENT_DTYPE``) that doubles in size when full. The buffer is only written at
    index ``_n_events`` before that counter is incremented, so indices
    ``0.._n_events-1`` are immutable once written.

    **Thread safety**: :meth:`peek` and :meth:`to_frame` may be called from another
    thread during the trial. They only read rows that are written already, which never
    change, ensuring a consistent snapshot.

    Parameters
    ----------
    trial : int
        Zero-based trial index, used to populate the ``trial`` column.
    fsm : _StateMachineLookup
        Compiled state machine data for this trial.
    event_tables : _EventTables
        Pre-built lookup tables for decoding events, see :func:`_build_event_tables`.
    """

    def __init__(
        self, *, trial: int, fsm: _StateMachineLookup, event_tables: _EventTables
    ) -> None:
        self._trial = trial
        self._state_names = fsm.state_names
        self._fsm_hash_str = fsm.fsm_hash.hex()
        self.event_tables = event_tables
        self._buffer: npt.NDArray = np.empty(_INITIAL_BUFFER_SIZE, dtype=_EVENT_DTYPE)
        self._n_events: int = 0
        self._check_trigger = threading.Event()
        self._finished = False  # set before the final wake-up of peek()
        self._frame: pl.DataFrame | None = None  # replaces the buffer once decoded

    def append(self, time_us: int, event: int, state: int, value: int = -1) -> None:
        """Append an event to the buffer, growing it if necessary."""
        n_events = self._n_events
        buffer = self._buffer
        if n_events == len(buffer):
            self._buffer = buffer = _grown(buffer)
        buffer[n_events] = (time_us, event, state, value)
        self._n_events = n_events + 1

    def state_entered(self) -> None:
        """Wake a waiting :meth:`peek`: call after appending the state's start."""
        # set() takes a lock even if the flag is set. peek() can't miss the event: it's
        # in the buffer before this check, and peek() clears the flag before reading it
        if not self._check_trigger.is_set():
            self._check_trigger.set()

    def finish(self) -> None:
        """Mark the recording as complete, which ends any waiting :meth:`peek`."""
        self._finished = True
        self._check_trigger.set()

    def decode(self) -> None:
        """Decode the events for good, and free the buffer."""
        self._frame = self.to_frame()
        self._buffer = np.empty(0, dtype=_EVENT_DTYPE)

    def peek(self, trigger_states: Collection[str] | None = None) -> pl.DataFrame:
        """Return a snapshot of events recorded so far as a :class:`polars.DataFrame`.

        The returned DataFrame reflects events up to the moment ``_n_events`` was read;
        one in-flight event may be missed.

        Parameters
        ----------
        trigger_states : Collection of str, optional
            Block until at least one of the given states has been entered. If the trial
            ends before any of the states have been entered, returns whatever was
            recorded.

        Returns
        -------
        DataFrame
            A snapshot of the recorded events.

        Raises
        ------
        ValueError
            If one or several of the trigger states are not part of the state machine.
        """
        if trigger_states is None:
            return self.to_frame()
        if unknown_states := (set(trigger_states) - set(self._state_names)):
            pt1 = f'Unknown trigger state{"s" if len(unknown_states) > 1 else ""}: '
            pt2 = ', '.join([f"'{u}'" for u in sorted(unknown_states)])
            raise ValueError(pt1 + pt2)
        target_ids = [i for i, s in enumerate(self._state_names) if s in trigger_states]
        while True:
            self._check_trigger.clear()
            states = self._buffer_view['state_id']
            if np.any(np.isin(states, target_ids)) or self._finished:
                return self.to_frame()
            self._check_trigger.wait(_PEEK_RECHECK_S)

    def to_frame(self) -> pl.DataFrame:
        """Decode the recorded events into a DataFrame: :data:`_TRIAL_DATA_SCHEMA`."""
        data = self._buffer_view  # before the frame, which decode() sets first
        if (frame := self._frame) is not None:
            return frame.clone()
        tables = self.event_tables
        n_events = len(data)
        event_ids = data['event_id']
        decoded = tables.decode_table
        n_states = len(self._state_names)
        # as unsigned, the -1 of no state is out of range too: both get the row of nulls
        state_rows = np.minimum(data['state_id'].astype(np.uint16), n_states)
        values = np.where(
            data['value'] == -1, tables.default_values[event_ids], data['value']
        )
        return pl.DataFrame(
            [
                # a copy: polars would share a single row's memory with the buffer
                pl.Series('Time', data['Time'].copy()).cast(_TRIAL_DATA_SCHEMA['Time']),
                pl.Series('Trial', [self._trial], dtype=pl.UInt32).new_from_index(
                    0, n_events
                ),
                pl.Series(
                    'State Machine', [self._fsm_hash_str], dtype=pl.Categorical
                ).new_from_index(0, n_events),
                pl.Series('State', [*self._state_names, None], dtype=pl.Categorical)[
                    state_rows
                ],
                decoded['Type'][event_ids],
                decoded['Event'][event_ids],
                decoded['Channel'][event_ids],
                pl.Series('Value', values).cast(pl.UInt16, strict=False),
            ]
        )

    @property
    def _buffer_view(self) -> npt.NDArray:
        """Return a view of the buffer."""
        n = self._n_events
        return self._buffer[:n]


@dataclass(eq=False)
class _Trial:
    """What is known about one trial, which ``Bpod`` asks rather than its threads.

    Created by :meth:`~bpod_core.bpod.Bpod.run` with what the threads need to read and
    record it, then handed to :class:`ReadThread`, which hands it on to
    :class:`EventThread`.
    """

    number: int
    """Zero-based trial index."""
    lookup: _StateMachineLookup
    """Compiled state machine data for this trial."""
    recorder: _Recorder
    """The trial's event buffer."""
    time_reference: _TimeReferences
    """The session clock's reference when the trial was sent."""
    cycle_period_us: int
    """The cycle period of the Bpod device in microseconds."""
    queued: bool = False
    """Whether the trial was sent while another ran: it is then due a cycle after it."""

    # written by ReadThread
    started: threading.Event = field(default_factory=threading.Event)
    """Set once the Bpod has reported the trial's start time, i.e. run its first
    cycle, or the reader is done with the trial without it."""
    ended: threading.Event = field(default_factory=threading.Event)
    """Set once the reader is done with the trial, after everything else it writes
    here."""
    read_error: Exception | None = None
    """The exception that ended the reader during this trial: a lost connection
    (:class:`OSError`), or a stream that couldn't be read, e.g. an unknown opcode."""
    end_micros_us: int | None = None
    """The trial's end, in µs of the Bpod's clock, once the reader has read it."""

    # written by EventThread
    decoded: threading.Event = field(default_factory=threading.Event)
    """Set once nothing more will come for this trial: the recorder holds the decoded
    events, or there won't be any (decoding failed, or the trial was dropped)."""
    decode_error: BpodError | None = None
    """Why the events couldn't be decoded."""

    def finish_unread(self, error: BpodError | None = None) -> None:
        """Settle a trial that no reader will read: nothing was or will be recorded.

        With `error`, :meth:`~bpod_core.bpod.Bpod.get_data` raises it for the trial
        instead of returning no data.
        """
        self.recorder.finish()
        self.decode_error = error
        self.started.set()
        self.ended.set()
        self.decoded.set()


class ReadThread(threading.Thread):
    """Serial reader thread of one connection to a Bpod.

    Reads the trials that :meth:`~bpod_core.bpod.Bpod.run` queues in :attr:`trials`,
    one after the other, and sleeps on the queue in between: the port's replies are
    then free for commands. For each trial, the thread reads the hardware confirmation
    and the trial's start time, then parses the two-byte opcode packets the Bpod
    streams over serial:

    - **Opcode 1** – hardware events: ``param`` event bytes followed by a 4-byte
      ``uInt32`` cycle count. Timestamps are derived as
      ``start_micros_us + n_cycles * cycle_period_us``, and the packet's events (one
      cycle) become one :class:`~bpod_core.bpod.structs._RawEvents`. The packets parsed
      from one read are pushed to ``queue_events`` together, as one list. An ``EXIT``
      byte (255) marks the end of the trial.
    - **Opcode 2** – softcodes: the ``param`` byte (the softcode) is forwarded to
      ``queue_softcodes``.

    **Chunked reads:** rather than reading packet by packet, the thread reads all bytes
    waiting on the serial port at once - blocking only until there is at least one -
    and parses every complete packet from a local buffer. An incomplete packet stays
    in the buffer until the rest of it has arrived. This keeps the number of system
    calls per packet low at high event and softcode rates, without adding latency.

    A trial is read until the Bpod reports its end. To end a trial early, use
    :meth:`~bpod_core.bpod.Bpod.stop_state_machine`, which makes the Bpod end the
    trial and report it as usual. :attr:`_Trial.started` tells when the Bpod has
    started the trial, or the thread is done without it.

    After the loop, the thread takes the 12-byte end-of-trial packet (``uInt32`` cycle
    count + ``uInt64`` µs timestamp, unpacked via :attr:`_struct_exit`) and enqueues
    :attr:`~_EventID.END_FSM_CYCLES` and :attr:`~_EventID.END_FSM_MICROS`. Like every
    other event, :attr:`~_EventID.END_FSM_CYCLES` is timed by the exit packet's cycle
    count, and the timing check compares that same time with the µs timestamp. The
    end-of-trial packet's own cycle count is ignored. Bytes read beyond that packet
    belong to the next trial, which the Bpod starts right away if one is queued; they
    stay in the buffer for it.

    Each trial is handed to :class:`EventThread` as the trial itself, then its event
    lists, then :attr:`_Mark.TRIAL_END` - always, however the trial ended. If the
    connection to the Bpod is lost or its stream can't be read, the exception is kept
    in :attr:`_Trial.read_error` and the thread ends: the trial being read is done
    with what it has, the trials still queued are settled unread, and the port's
    replies are released. :meth:`cancel` ends the thread the same way without the
    Bpod's cooperation.

    Parameters
    ----------
    serial : ExtendedSerial
        The serial connection to the Bpod device.
    queue_events : SimpleQueue[list[_RawEvents] | _Trial | _Mark]
        Queue taking each trial, then the events parsed from one read, as a list, then
        :attr:`_Mark.TRIAL_END`.
    queue_softcodes : SimpleQueue[_RawSoftcode]
        Queue for storing softcodes.
    late_starts : _LateStarts
        Where a late start is recorded.
    """

    _struct_exit = struct.Struct('<IQ')

    trials: SimpleQueue[_Trial | None]
    """The trials to read, in the order their state machines were sent. ``None`` ends
    the thread once it is done with the trials before."""

    trial: _Trial | None
    """The trial being read, or the last one read."""

    def __init__(
        self,
        *,
        serial: ExtendedSerial,
        queue_events: SimpleQueue[list[_RawEvents] | _Trial | _Mark],
        queue_softcodes: SimpleQueue[_RawSoftcode],
        late_starts: _LateStarts,
    ) -> None:
        super().__init__(name='ReadThread', daemon=True)
        self._serial = serial
        self._queue_events = queue_events
        self._queue_softcodes = queue_softcodes
        self._late_starts = late_starts
        self.trials = SimpleQueue()
        self.trial = None
        self._buffer = bytearray()
        self._received_ns = 0
        self._previous_end_us: int | None = None  # the end of the last trial read

    def stop(self) -> None:
        """End the thread, once it is done with the trials queued.

        Right away instead if the Bpod won't end the trial being read, e.g. one that
        was given up: its data stays incomplete.
        """
        self.trials.put(None)
        if (trial := self.trial) is not None and not trial.ended.is_set():
            self.cancel()
        else:
            _join_or_warn(self)

    def cancel(self) -> None:
        """End the thread without waiting for the trial; its data stays incomplete."""
        self._serial.cancel_read()
        _join_or_warn(self)
        # a cancellation the thread didn't see must not end a later read: it has ended,
        # or it was done with the trial already and waits for the next
        trial = self.trial
        if not self.is_alive() or (trial is not None and trial.ended.is_set()):
            self._serial.discard_cancel_read()

    def _fill(self) -> int:
        """Read all bytes pending on the serial port, blocking until there is one.

        Returns how many there were: the host's read backlog at that moment.
        """
        chunk = self._serial.read_waiting()
        if not chunk:  # the port's timeout elapsed (off POSIX only): nothing yet
            return 0
        self._buffer += chunk
        self._received_ns = time.perf_counter_ns()
        return len(chunk)

    def _fill_to(self, n_bytes: int) -> None:
        """Read until the buffer holds at least ``n_bytes`` bytes."""
        while len(self._buffer) < n_bytes:
            self._fill()

    def _take(self, n_bytes: int) -> bytes:
        """Remove and return the next ``n_bytes`` bytes, reading more as needed."""
        self._fill_to(n_bytes)
        data = bytes(self._buffer[:n_bytes])
        del self._buffer[:n_bytes]
        return data

    def run(self) -> None:
        """Execute the ReadThread."""
        trial: _Trial | None = None
        holding = False  # the port's replies
        try:
            while (trial := self.trials.get()) is not None:
                self.trial = trial
                # run() took the port's replies for the trial, or left them with us
                holding = True
                self._read_trial(trial)
                # the replies go back to the commands, unless the next trial's machine
                # was sent: run() queues it under the write lock, so this can't miss it
                with self._serial.write_lock:
                    if self.trials.empty():
                        self._serial.read_lock.release()
                        holding = False
                trial.ended.set()
        except ReadCancelled:
            logger.debug('Reading of trial #%d cancelled', self._number(trial))
        except Exception as error:  # noqa: BLE001 - reported by Bpod.wait()
            logger.error(  # noqa: TRY400
                'Reading trial #%d failed: %s', self._number(trial), error
            )
            if trial is not None:
                trial.read_error = error  # e.g. SerialException when unplugged
        finally:
            with self._serial.write_lock:
                if holding:
                    self._serial.read_lock.release()
            if trial is not None:  # the trial being read, done with what it has
                trial.started.set()
                trial.ended.set()
            while True:  # the trials still queued: nobody will read them
                try:
                    pending = self.trials.get_nowait()
                except Empty:
                    break
                if pending is not None:
                    pending.finish_unread(
                        BpodError(
                            f'Trial #{pending.number} was not read: the connection '
                            f'failed during trial #{self._number(trial)}'
                        )
                    )

    @staticmethod
    def _number(trial: _Trial | None) -> int:
        """The trial's number for a log line, -1 without a trial."""
        return -1 if trial is None else trial.number

    def _read_trial(self, trial: _Trial) -> None:
        """Read one trial, handing its events to the event thread.

        Returns once the Bpod has reported the trial's end. Raises if the stream can't
        be read; :attr:`_Mark.TRIAL_END` is queued either way.
        """
        fsm_hash = trial.lookup.fsm_hash.hex()
        q_events = self._queue_events
        batch: list[_RawEvents] = []  # the packets of one read, handed over together
        q_events.put(trial)
        try:
            # confirm the state machine
            if not self._take(1)[0]:
                raise RuntimeError(
                    f'State machine #{trial.number} not confirmed by Bpod'
                )
            logger.info('Starting state machine %s / trial #%d', fsm_hash, trial.number)

            # read the starting timestamps of the state machine
            # we do this early to get an accurate timestamp for the system clock
            (start_micros_us,) = STRUCT_UINT64_LE.unpack(self._take(8))
            # only now has the trial run its first cycle: a stop sent before the Bpod
            # reports the start time is answered without it
            trial.started.set()
            cycle_period_us = trial.cycle_period_us
            if trial.queued and (previous_end_us := self._previous_end_us) is not None:
                # queued: due a cycle after the trial before
                late_us = start_micros_us - previous_end_us - cycle_period_us
                if late_us > cycle_period_us / 2:
                    self._late_starts.record(trial.number, late_us)

            # assign members to local variables to avoid repeated attribute lookups
            buffer = self._buffer
            q_softcodes = self._queue_softcodes
            unpack_n_cycles = STRUCT_UINT32_LE.unpack_from
            exit_event = int(_EventID.EXIT)

            # handle events: start of state machine, start of state
            batch = [
                _RawEvents(start_micros_us, (_EventID.START_FSM,)),
                _RawEvents(start_micros_us, (_EventID.START_STATE,)),
            ]

            # tracks the most recent Bpod cycle timestamp for softcode attribution
            last_micros_us = start_micros_us

            # position of the next unparsed byte in the buffer, and the time at which
            # the bytes in the buffer were received
            # the most bytes a read of this loop found pending: how far the host fell
            # behind the Bpod. Once the OS buffer is full, the Bpod stalls in its state
            # machine cycle until the host reads again. A read returns at most what the
            # OS hands out at once, on Linux about 4 KB, so the figure saturates there.
            backlog = 0
            pos = 0
            received_ns = self._received_ns

            # enter the reading loop
            trial_ended = False
            while not trial_ended:
                # parse the next packet - if it has been received completely:
                # opcode 1 carries `param` event bytes + 4 bytes for n_cycles
                n_available = len(buffer) - pos
                if n_available >= 2:
                    opcode = buffer[pos]
                    param = buffer[pos + 1]
                    size = param + 6 if opcode == 1 else 2
                    if n_available >= size:
                        # HANDLE EVENTS
                        if opcode == 1:
                            # derive the event timestamp from the cycle count
                            (n_cycles,) = unpack_n_cycles(buffer, pos + 2 + param)
                            n_cycles_us = n_cycles * cycle_period_us
                            derived_micros_us = start_micros_us + n_cycles_us
                            last_micros_us = derived_micros_us

                            # hand the cycle's events over to the EventThread
                            events = buffer[pos + 2 : pos + 2 + param]
                            if (i_exit := events.find(exit_event)) >= 0:
                                trial_ended = True
                                events = events[:i_exit]
                            if events:
                                batch.append(_RawEvents(derived_micros_us, events))

                        # HANDLE SOFTCODES
                        # note: while incoming softcodes do not carry a timestamp,
                        #       they are always preceded by a state change in the same
                        #       cycle. We can thus use the last available timestamp.
                        elif opcode == 2:
                            if batch:  # the state change of this cycle comes first
                                q_events.put(batch)
                                batch = []
                            q_softcodes.put(
                                _RawSoftcode(param, received_ns, last_micros_us)
                            )

                        # HANDLE UNKNOWN OPCODES
                        else:
                            raise RuntimeError(
                                f'Received unknown opcode from Bpod: {opcode}'
                            )
                        pos += size
                        continue

                # the next packet is incomplete: drop the parsed bytes and read all
                # bytes waiting on the serial port, blocking until there is at least one
                del buffer[:pos]
                pos = 0
                if batch:
                    q_events.put(batch)
                    batch = []
                backlog = max(backlog, self._fill())
                received_ns = self._received_ns

            # the Bpod sends two values after exiting the state machine trial:
            # 1) the number of hardware timer callbacks executed (uInt32) - ignored: for
            #    a queued trial, it also counts the cycle in which the previous trial
            #    ended (firmware quirk). n_cycles keeps the exit packet's cycle count,
            #    the time base of all events.
            # 2) bpod_micros - the microsecond count at the end of the trial (uInt64)
            del buffer[:pos]
            if batch:
                q_events.put(batch)
                batch = []
            _, end_micros_us = self._struct_exit.unpack(self._take(12))
            trial.end_micros_us = self._previous_end_us = end_micros_us
            # anything beyond belongs to the next trial, if one has been queued: it
            # stays in the buffer

            # verify hardware timer reliability: warn if cycle-based and micros-based
            # trial durations diverge beyond the threshold.
            duration_micros_us = end_micros_us - start_micros_us
            duration_cycles_us = n_cycles * cycle_period_us
            duration_discrepancy_us = duration_cycles_us - duration_micros_us
            if abs(duration_discrepancy_us) > _TIMING_VIOLATION_THRESHOLD_US:
                average_cycle_period_us = (
                    (duration_micros_us / n_cycles) if n_cycles else float('nan')
                )
                logger.warning(
                    'Violation of hardware timing guarantees. Trial was %d µs %s than '
                    'expected. Average cycle period: %0.3f µs (expected: %0.3f µs). '
                    'Peak read backlog on the host: %d bytes (a backlog near the OS '
                    "buffer's size means the host stalled the Bpod).",
                    abs(duration_discrepancy_us),
                    'longer' if duration_discrepancy_us < 0 else 'shorter',
                    average_cycle_period_us,
                    cycle_period_us,
                    backlog,
                )

            # enqueue end-of-trial event
            derived_micros = start_micros_us + duration_cycles_us
            q_events.put(
                [
                    _RawEvents(derived_micros, (_EventID.END_FSM_CYCLES,)),
                    _RawEvents(end_micros_us, (_EventID.END_FSM_MICROS,)),
                ]
            )
        finally:
            if batch:  # e.g. the packets parsed before an unknown opcode
                q_events.put(batch)
            q_events.put(_Mark.TRIAL_END)
            logger.debug('Exiting state machine %s / trial #%d', fsm_hash, trial.number)


class _EventCallbacks(NamedTuple):
    """What :class:`EventThread` calls on its owner's behalf.

    Held in a :class:`~bpod_core.misc._HandlerSlot`, so that the thread doesn't pin
    the owner.
    """

    publish: Callable[[list[BpodEventUnion]], object]
    """Called with a list of :class:`~bpod_core.bpod.structs.BpodEventUnion` messages
    recorded since the previous call, once the thread has caught up with the queue.
    Return values are ignored. Publishing is best-effort: exceptions are logged but do
    not interrupt trial recording."""
    publish_gate: Callable[[], bool]
    """Consulted once per batch of queued events; returning False skips resolving
    their messages entirely. A subscriber appearing mid-trial picks up the stream from
    the next batch."""
    on_trial_start: Callable[[int], None]
    """Called once per trial, with the trial's absolute start time (microseconds since
    epoch), when the synthetic ``TrialStart`` event is recorded. Unlike ``publish``,
    this always fires regardless of ``publish_gate`` - for bookkeeping that needs the
    trial-start timestamp without going through the publish/IPC path."""


_NO_CALLBACKS = _EventCallbacks(
    publish=lambda _: None, publish_gate=lambda: False, on_trial_start=lambda _: None
)
"""What :class:`EventThread` calls once its owner is gone, or before it is set up."""


def _no_recorder(*_: int) -> None:
    """Stands in for the recorder's ``append`` between trials."""
    raise RuntimeError('No trial is being recorded')


class EventThread(threading.Thread):
    """Consumer thread that processes hardware events and builds the trial DataFrames.

    Runs for the lifetime of one connection. :class:`ReadThread` feeds each trial into
    :attr:`queue` as the :class:`_Trial` itself, then lists of raw
    :class:`~bpod_core.bpod.structs._RawEvents`, the packets of one read, then
    :attr:`_Mark.TRIAL_END`; ``EventThread`` drains that queue, advances the FSM
    state, and records every event — inputs, state transitions, and output actions —
    in the trial's recorder. Trials are recorded one after the other, in the order
    they were read, so their published event streams never interleave.

    All events of a cycle are evaluated in the state the cycle started in; the first
    one leading elsewhere makes the transition.

    When the trial ends (:attr:`_Mark.TRIAL_END` received), its events are decoded, for
    :meth:`~bpod_core.bpod.Bpod.get_data`. A trial that couldn't be recorded is
    reported through :attr:`_Trial.decode_error`; the thread goes on with the next.

    Parameters
    ----------
    callbacks : _HandlerSlot[_EventCallbacks | None]
        What the thread calls on its owner's behalf, see :class:`_EventCallbacks`. The
        thread references the slot only weakly, and its callbacks only while recording
        a trial: once the owner and its slot are gone, or until the slot is filled,
        trials are recorded without them.
    """

    queue: SimpleQueue[list[_RawEvents] | _Trial | _Mark]
    """Event queue shared with :class:`ReadThread`."""

    trial: _Trial | None
    """The trial being recorded, or the last one recorded."""

    def __init__(self, *, callbacks: _HandlerSlot[_EventCallbacks | None]) -> None:
        super().__init__(name='EventThread', daemon=True)
        self.queue = SimpleQueue()
        self.trial = None
        self._callbacks = weakref.ref(callbacks)
        # the trial being recorded, flat for _append_and_publish: see _record()
        self._append: Callable[[int, int, int, int], None] = _no_recorder
        self._publish = _NO_CALLBACKS.publish
        self._event_info: Mapping[
            int, tuple[BpodEventType, str | None, str | None, int | None]
        ] = {}
        self._state_names: Sequence[str] = ()
        self._trial_number = 0
        self._fsm_hash = ''
        self._n_published: int = 0
        self._pending: list[BpodEventUnion] = []  # messages not published yet

    def stop(self) -> None:
        """End the thread once it has handled what is queued before the call.

        A trial being recorded is finished with what it has.
        """
        self.queue.put(_Mark.STOP)
        _join_or_warn(self)

    def run(self) -> None:
        """Execute the EventThread."""
        queue = self.queue
        while (item := queue.get()) is not _Mark.STOP:
            if not isinstance(item, _Trial):
                continue  # what is left of a trial that couldn't be recorded
            self.trial = item
            try:
                if self._record(item):
                    return
            except Exception as error:  # the thread must go on
                logger.exception('Failed to record trial #%d', item.number)
                item.decode_error = BpodError(f'Failed to record trial #{item.number}')
                item.decode_error.__cause__ = error
            finally:
                item.decoded.set()  # whoever waits for the trial moves on

    def _append_and_publish(
        self, time_us: int, event: int, state: int, value: int = -1
    ) -> None:
        """Append an event, and queue its message for :meth:`_flush`."""
        self._append(time_us, event, state, value)
        try:
            kind, name, channel, default_value = self._event_info[event]
            message: BpodEventUnion
            match kind:
                case 'InputEvent' if name is not None:
                    message = EventInput(
                        time_us=time_us,
                        event=name,
                        channel=channel,
                        value=default_value if value == -1 else value,
                    )
                case 'OutputAction' if channel is not None:
                    message = EventOutput(time_us=time_us, channel=channel, value=value)
                case 'StateStart':
                    message = EventStateStart(
                        time_us=time_us, state=self._state_names[state]
                    )
                case 'StateEnd':
                    message = EventStateEnd(
                        time_us=time_us, state=self._state_names[state]
                    )
                case 'TrialStart':
                    message = EventTrialStart(
                        time_us=time_us,
                        trial=self._trial_number,
                        fsm_hash=self._fsm_hash,
                    )
                case 'TrialEnd':
                    message = EventTrialEnd(time_us=time_us, trial=self._trial_number)
                case 'TrialEndControl':
                    message = EventTrialEndControl(
                        time_us=time_us,
                        trial=self._trial_number,
                        n_events=self._n_published + 1,
                    )
                case _:
                    logger.warning('Not publishing unknown event type: %s', kind)
                    return
            self._pending.append(message)
            self._n_published += 1
        except Exception:
            logger.exception('Failed to build trial event message')

    def _flush(self) -> None:
        """Publish the messages collected since the last call, as one batch."""
        batch, self._pending = self._pending, []
        try:
            self._publish(batch)
        except Exception:
            logger.exception('Failed to publish trial events')

    def _record(self, trial: _Trial) -> bool:
        """Record `trial`'s events from the queue until its end mark, then decode.

        The recorder is finished and the decoding done, or its error kept, before
        returning - however the recording ended. Returns whether :attr:`_Mark.STOP`
        ended the trial instead: the thread is to stop.
        """
        # the callbacks are held only for the trial: the owner may be dropped meanwhile
        slot = self._callbacks()
        callbacks = (slot.handler if slot is not None else None) or _NO_CALLBACKS
        publish_gate = callbacks.publish_gate
        on_trial_start = callbacks.on_trial_start
        self._publish = callbacks.publish
        self._n_published = 0
        self._pending = []
        recorder = trial.recorder
        fsm = trial.lookup
        self._append = recorder.append
        self._event_info = recorder.event_tables.event_info
        self._state_names = fsm.state_names
        self._trial_number = trial.number
        self._fsm_hash = fsm.fsm_hash.hex()
        event_queue = self.queue
        state_transition_matrix = fsm.state_transition_matrix
        state_action_indices = fsm.state_action_indices
        resettable_indices = recorder.event_tables.resettable_indices
        use_back_op = fsm.use_back_op
        state_entered = recorder.state_entered
        record = recorder.append
        record_and_publish = self._append_and_publish
        target_exit = len(state_transition_matrix)
        target_back = 255
        current_state = 0
        previous_state = 0
        active_outputs: dict[int, int] = {}  # resettable outputs currently non-zero

        base_time_bpod_us = trial.time_reference.reset_system_time_ns // 1000

        try:
            while True:
                # once the backlog is processed, publish its messages as one batch
                if self._pending and event_queue.empty():
                    self._flush()

                batch = event_queue.get()
                if isinstance(batch, _Mark):  # a trial's lists end with a mark
                    return batch is _Mark.STOP
                if isinstance(batch, _Trial):  # can't happen: trials end with a mark
                    batch.decode_error = BpodError(
                        f'Trial #{batch.number} was queued before the end of trial '
                        f'#{trial.number}'
                    )
                    batch.decoded.set()  # nobody waits for a trial that won't record
                    raise TypeError(str(batch.decode_error))
                # with nobody listening, skip building messages until the next batch
                append = record_and_publish if publish_gate() else record
                # the packets of one read: the events of each cycle, or synthetic events
                for bpod_count_us, event_ids in batch:
                    event_index = event_ids[0]

                    # convert relative timestamps to absolute timestamps
                    t_bpod_us = base_time_bpod_us + bpod_count_us

                    # hardware events of one cycle: the first leading elsewhere makes
                    # the transition
                    if event_index < 255:
                        for event_id in event_ids:
                            append(t_bpod_us, event_id, current_state)
                        if current_state < 0:  # FSM already exited - record events only
                            continue
                        transitions = state_transition_matrix[current_state]
                        for event_id in event_ids:
                            if (target_state := transitions[event_id]) != current_state:
                                break
                        else:
                            continue  # no transition
                        # state exited without a successor
                        if target_state == target_exit:
                            append(t_bpod_us, _EventID.END_STATE, current_state)
                            current_state = -1
                            continue
                        if target_state == target_back and use_back_op:  # >back
                            target_state = previous_state

                        # record end of current state, advance, record start of next
                        append(t_bpod_us, _EventID.END_STATE, current_state)
                        previous_state = current_state
                        current_state = target_state
                        append(t_bpod_us, _EventID.START_STATE, current_state)
                        state_entered()

                    # start of the initial state
                    elif event_index == _EventID.START_STATE:
                        append(t_bpod_us, event_index, current_state)
                        state_entered()

                    # trial start and end, which belong to no state
                    else:
                        # implicitly end the current state and reset active outputs
                        # before the TrialEnd row
                        if event_index == _EventID.END_FSM_CYCLES:
                            if current_state >= 0:
                                append(t_bpod_us, _EventID.END_STATE, current_state)
                            for idx in sorted(active_outputs):
                                output_id = _OUTPUT_ID_OFFSET + idx
                                append(t_bpod_us, output_id, -1, 0)
                            active_outputs.clear()
                        append(t_bpod_us, event_index, -1)
                        if event_index == _EventID.START_FSM:
                            on_trial_start(t_bpod_us)
                        continue

                    # record output actions (initial state & after state transitions):
                    # absent channels are reset (0); others get their new value.
                    new_actions = state_action_indices[current_state]
                    for idx in sorted(active_outputs.keys() | new_actions.keys()):
                        val = new_actions.get(idx, 0)
                        output_id = _OUTPUT_ID_OFFSET + idx
                        append(t_bpod_us, output_id, current_state, val)
                    active_outputs.clear()
                    for idx, val in new_actions.items():
                        if idx in resettable_indices and val > 0:
                            active_outputs[idx] = val  # noqa: PERF403
        finally:
            try:
                recorder.finish()  # the rows are final: no peek waits for the rest
                if self._pending:
                    self._flush()
                self._decode(trial)
            finally:
                # between trials, neither the recorder nor anything of the owner's
                self._append = _no_recorder
                self._publish = _NO_CALLBACKS.publish

    @staticmethod
    def _decode(trial: _Trial) -> None:
        """Decode the recorded events, or keep the error doing so."""
        try:
            trial.recorder.decode()
        # a panic in polars isn't an Exception
        except (Exception, pl.exceptions.PanicException) as error:
            logger.exception('Failed to decode trial #%d', trial.number)
            trial.decode_error = BpodError(f'Failed to decode trial #{trial.number}')
            trial.decode_error.__cause__ = error


class SoftcodeThread(threading.Thread):
    """Consumer thread that executes softcode handlers sent from the Bpod.

    :class:`ReadThread` feeds :class:`~bpod_core.bpod.structs._RawSoftcode` items into
    :attr:`queue`; ``SoftcodeThread`` drains that queue and calls the registered handler
    for each softcode.

    The thread looks the handler up in a ``_HandlerSlot`` held by its owner, so the
    handler can be swapped at any time without restarting the thread - the new handler
    takes effect on the next softcode. Once the owner and its slot are gone, remaining
    softcodes are dropped.

    Handler exceptions are caught, logged, and execution continues.
    """

    queue: SimpleQueue[_RawSoftcode]
    """Softcode queue shared with :class:`ReadThread`."""

    def __init__(
        self,
        *,
        handler_slot: _HandlerSlot[Callable[[int], None] | None],
    ) -> None:
        super().__init__(name='SoftcodeThread', daemon=True)
        self.queue: SimpleQueue[_RawSoftcode] = SimpleQueue()
        self._handler_slot = weakref.ref(handler_slot)
        self._pc_base_ns: int = 0  # perf_counter_ns equivalent of session clock t=0

    def drain(self, deadline: float | None = None) -> bool:
        """Block until the softcodes queued so far have been handled.

        Returns False if they weren't by `deadline` (a :func:`time.monotonic` value),
        after one short grace period. Returns True right away from a handler, which
        would wait for itself, and once the thread has ended and handles nothing more.
        """
        if self is threading.current_thread() or not self.is_alive():
            return True
        marker = _DrainMarker(-2, 0, 0)
        marker.done = threading.Event()
        self.queue.put(marker)
        warned = False
        while True:
            remaining = 0.1 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                return marker.done.wait(_DRAIN_GRACE_S) or not self.is_alive()
            if marker.done.wait(min(remaining, 0.1)) or not self.is_alive():
                return True  # handled, or the thread stopped before the marker
            if deadline is None and not warned:
                logger.warning('Waiting for softcodes to be processed ...')
                warned = True

    def stop(self) -> None:
        """Process the softcodes still queued, then end the thread."""
        self.queue.put(_SOFTCODE_STOP)
        if self is threading.current_thread():
            return  # called by a handler or the garbage collector on this thread
        _join_or_warn(self)

    def set_time_reference(self, time_ref: _TimeReferences) -> None:
        """Update the session clock reference used for Bpod-relative latency logging."""
        self._pc_base_ns = (
            time_ref.reset_system_time_ns
            - time_ref.init_system_time_ns
            + time_ref.init_perf_counter_ns
        )

    def run(self) -> None:
        """Execute the SoftcodeThread."""
        queue = self.queue
        handler_slot = self._handler_slot

        # enter the reading loop
        while (item := queue.get()) is not _SOFTCODE_STOP:
            if isinstance(item, _DrainMarker):  # the softcodes before it are done
                item.done.set()
                continue
            softcode, received_ns, micros_us = item
            handler = None if (slot := handler_slot()) is None else slot.handler
            if handler is not None:
                try:
                    start_ns = time.perf_counter_ns()
                    handler(softcode)
                    if logger.isEnabledFor(logging.DEBUG):
                        done_ns = time.perf_counter_ns()
                        fire_ns = self._pc_base_ns + micros_us * 1000
                        logger.debug(
                            "'%s(%d)': "
                            'latency=%.3f ms, dispatch=%.3f ms, duration=%.3f ms',
                            getattr(handler, '__name__', 'unknown'),
                            softcode,
                            (start_ns - fire_ns) / 1e6,
                            (start_ns - received_ns) / 1e6,
                            (done_ns - start_ns) / 1e6,
                        )
                except Exception as e:
                    logger.exception(
                        "Error calling '%s(%d)'",
                        getattr(handler, '__name__', 'unknown'),
                        softcode,
                        exc_info=e,
                    )
            elif slot is not None:  # without a slot, the owner is being collected
                logger.warning(
                    'Received softcode %d from Bpod but no handler is defined', softcode
                )
            del slot, handler  # waiting for the next softcode mustn't pin the owner
