"""Threads for FSM execution and data collection from the Bpod hardware."""

import logging
import struct
import threading
import time
import weakref
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from enum import IntEnum, auto
from queue import SimpleQueue
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
from bpod_core.misc import _HandlerSlot

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
    STOP_SENTINEL = auto()
    """Sentinel value for stopping threads."""


_EVENTS_STOP = [_RawEvents(0, (_EventID.STOP_SENTINEL,))]
"""Ends :class:`EventThread`'s queue: the one list it must not iterate."""

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
            new_buf = np.empty(n_events * 2, dtype=_EVENT_DTYPE)
            new_buf[:n_events] = buffer
            self._buffer = buffer = new_buf
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
    """What is known about one trial, which ``Bpod`` asks rather than its threads."""

    number: int
    """Zero-based trial index."""
    fsm_hash: bytes
    """The state machine's hash value."""
    recorder: _Recorder
    """The trial's event buffer."""

    # written by ReadThread
    started: threading.Event = field(default_factory=threading.Event)
    """Set once the Bpod has confirmed the trial's start, or the reader has ended."""
    ended: threading.Event = field(default_factory=threading.Event)
    """Set once the reader has ended, after everything else it writes here."""
    read_error: Exception | None = None
    """The exception that ended the reader: a lost connection (:class:`OSError`), or a
    stream that couldn't be read, e.g. an unknown opcode."""
    leftover: bytes = b''
    """Bytes read beyond the end of this trial, belonging to the next trial."""
    leftover_received_ns: int = 0
    """When :attr:`leftover` was received (:func:`time.perf_counter_ns`)."""
    end_micros_us: int | None = None
    """The trial's end, in µs of the Bpod's clock, once the reader has read it."""

    # written by EventThread
    decoded: threading.Event = field(default_factory=threading.Event)
    """Set once nothing more will come for this trial: the recorder holds the decoded
    events, or there won't be any (decoding failed, or the trial was dropped)."""
    decode_error: BpodError | None = None
    """Why the events couldn't be decoded."""

    # written by whoever sends a stop command, read by ReadThread
    stop_expected: bool = False
    """Whether a stop command is about to be sent."""


class ReadThread(threading.Thread):
    """Serial reader thread for one Bpod trial.

    Runs for the lifetime of a single trial. Reads the hardware confirmation and the
    trial's start time, then parses the two-byte opcode packets the Bpod streams over
    serial:

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

    The thread runs until the Bpod reports the end of the trial. To end a trial early,
    use :meth:`~bpod_core.bpod.Bpod.stop_state_machine`, which makes the Bpod end the
    trial and report it as usual; :meth:`cancel` ends the thread without the Bpod's
    cooperation, leaving the trial's data incomplete. If the Bpod never started the
    state machine, it answers the stop as an idle Bpod; told of the stop by
    :attr:`_Trial.stop_expected`, the thread then ends without events.
    :attr:`_Trial.started` tells when the Bpod has started the trial, or the thread has
    ended without it.

    After the loop, the thread takes the 12-byte end-of-trial packet (``uInt32`` cycle
    count + ``uInt64`` µs timestamp, unpacked via :attr:`_struct_exit`) and enqueues
    :attr:`~_EventID.END_FSM_CYCLES` and :attr:`~_EventID.END_FSM_MICROS`. Like every
    other event, :attr:`~_EventID.END_FSM_CYCLES` is timed by the exit packet's cycle
    count, and the timing check compares that same time with the µs timestamp. The
    end-of-trial packet's own cycle count is ignored. Bytes read
    beyond that packet belong to the next trial, which the Bpod starts right away if
    one is queued; they are kept in :attr:`_Trial.leftover` for the next ``ReadThread``.
    A :attr:`~_EventID.STOP_SENTINEL` is always enqueued in ``finally`` so
    :class:`EventThread` can detect termination regardless of how the trial ended. If
    the connection to the Bpod is lost or its stream can't be read, the exception is
    kept in :attr:`_Trial.read_error`.

    Parameters
    ----------
    serial : ExtendedSerial
        The serial connection to the Bpod device.
    trial : _Trial
        The trial to read, which the thread writes its results to.
    cycle_period_us : int
        The cycle period of the Bpod device in microseconds.
    queue_events : SimpleQueue[list[_RawEvents]]
        Queue taking the events parsed from one read, as a list.
    queue_softcodes : SimpleQueue[_RawSoftcode]
        Queue for storing softcodes.
    initial_bytes : bytes, default: b''
        Bytes of this trial that were already read from the serial port, i.e., the
        previous trial's :attr:`_Trial.leftover`.
    initial_received_ns : int, default: 0
        When ``initial_bytes`` were received (:func:`time.perf_counter_ns`), i.e., the
        previous trial's :attr:`_Trial.leftover_received_ns`.
    late_starts : _LateStarts
        Where a late start is recorded.
    previous_end_us : int or None, default: None
        The end of the trial before, i.e., the previous trial's
        :attr:`_Trial.end_micros_us`, if this trial should start a cycle after it: a
        start later than that is recorded in ``late_starts``.
    """

    _struct_exit = struct.Struct('<IQ')

    trial: _Trial
    """The trial being read."""

    def __init__(
        self,
        *,
        serial: ExtendedSerial,
        trial: _Trial,
        cycle_period_us: int,
        queue_events: SimpleQueue[list[_RawEvents]],
        queue_softcodes: SimpleQueue[_RawSoftcode],
        initial_bytes: bytes = b'',
        initial_received_ns: int = 0,
        late_starts: _LateStarts,
        previous_end_us: int | None = None,
    ) -> None:
        super().__init__(name='ReadThread', daemon=True)
        self._serial = serial
        self._fsm_hash = trial.fsm_hash.hex()
        self.trial = trial
        self._cycle_period_us = cycle_period_us
        self._queue_events = queue_events
        self._queue_softcodes = queue_softcodes
        self._late_starts = late_starts
        self._previous_end_us = previous_end_us
        self._buffer = bytearray(initial_bytes)
        self._received_ns = initial_received_ns

    def cancel(self) -> None:
        """End the thread without waiting for the trial; its data stays incomplete."""
        self._serial.cancel_read()
        self.join(timeout=1)
        if self.is_alive():
            logger.warning('Read thread did not terminate')
        else:  # a cancellation the thread didn't see must not end the next read
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

    def _stopped_idle(self) -> bool:
        """Whether the Bpod answered the stop as idle, never having started the trial.

        Takes the reply if so. The Bpod sends it only after the stop, so the first
        byte tells whether a stop was expected by then. A trial that started is taken
        for the reply if its start time begins with the reply's bytes: 1 in 65,536
        stops that cross the trial's start.
        """
        self._fill_to(1)
        if not self.trial.stop_expected:
            return False
        self._fill_to(len(_IDLE_STOP_REPLY_HEADER))
        if not self._buffer.startswith(_IDLE_STOP_REPLY_HEADER):
            return False
        self._take(_IDLE_STOP_REPLY_SIZE)
        return True

    def run(self) -> None:
        """Execute the ReadThread."""
        trial = self.trial
        batch: list[_RawEvents] = []  # the packets of one read, handed over together
        try:
            if self._stopped_idle():
                logger.warning(
                    'Trial #%d never started: the Bpod was idle when stopped',
                    trial.number,
                )
                return

            # confirm the state machine
            if not self._take(1)[0]:
                raise RuntimeError(  # noqa: TRY301 - kept in `error`, see below
                    f'State machine #{trial.number} not confirmed by Bpod'
                )
            trial.started.set()
            logger.info(
                'Starting state machine %s / trial #%d', self._fsm_hash, trial.number
            )

            # read the starting timestamps of the state machine
            # we do this early to get an accurate timestamp for the system clock
            (start_micros_us,) = STRUCT_UINT64_LE.unpack(self._take(8))
            if (previous_end_us := self._previous_end_us) is not None:
                # queued: due a cycle after the trial before
                late_us = start_micros_us - previous_end_us - self._cycle_period_us
                if late_us > self._cycle_period_us / 2:
                    self._late_starts.record(trial.number, late_us)

            # assign members to local variables to avoid repeated attribute lookups
            buffer = self._buffer
            cycle_period_us = self._cycle_period_us
            q_events = self._queue_events
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
                            raise RuntimeError(  # noqa: TRY301 - see above
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
            trial.end_micros_us = end_micros_us

            # anything beyond belongs to the next trial, if one has been queued
            trial.leftover = bytes(buffer)
            trial.leftover_received_ns = self._received_ns

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

        except ReadCancelled:
            logger.debug('Reading of trial #%d cancelled', trial.number)
        except Exception as error:  # noqa: BLE001 - reported by Bpod.wait()
            logger.error('Reading trial #%d failed: %s', trial.number, error)  # noqa: TRY400
            trial.read_error = error  # e.g. SerialException when unplugged
        finally:
            trial.started.set()  # also if it never started: nothing to wait for
            if batch:  # e.g. the packets parsed before an unknown opcode
                self._queue_events.put(batch)
            self._queue_events.put(_EVENTS_STOP)
            logger.debug(
                'Exiting state machine %s / trial #%d', self._fsm_hash, trial.number
            )
            trial.ended.set()


class EventThread(threading.Thread):
    """Consumer thread that processes hardware events and builds the trial DataFrame.

    Runs for the duration of one trial. :class:`ReadThread` feeds lists of raw
    :class:`~bpod_core.bpod.structs._RawEvents`, the packets of one read, into
    :attr:`queue`; ``EventThread`` drains that queue, advances the FSM state, and
    records every event — inputs, state transitions, and output actions — in the
    trial's recorder.

    All events of a cycle are evaluated in the state the cycle started in; the first
    one leading elsewhere makes the transition.

    When the trial ends (``STOP_SENTINEL`` received), its events are decoded, for
    :meth:`~bpod_core.bpod.Bpod.get_data`.

    Parameters
    ----------
    trial : _Trial
        The trial to record, which the thread writes its results to.
    fsm : _StateMachineLookup
        Compiled state machine data for this trial.
    time_reference : _TimeReferences
        Reference values for performance counters.
    publish : Callable
        Called with a list of :class:`~bpod_core.bpod.structs.BpodEventUnion`
        messages recorded since the previous call, once the thread has caught up with
        the queue. Return values are ignored. Publishing is best-effort: exceptions are
        logged but do not interrupt trial recording.
    publish_gate : Callable
        Zero-argument callable consulted once per batch of queued events; returning
        False skips resolving their messages entirely. A subscriber appearing mid-trial
        picks up the stream from the next batch.
    on_trial_start : Callable, optional
        Called once, with the trial's absolute start time (microseconds since epoch),
        when the synthetic ``TrialStart`` event is recorded. Unlike ``publish``, this
        always fires regardless of ``publish_gate`` - use it for bookkeeping that needs
        the trial-start timestamp without going through the publish/IPC path.
    """

    queue: SimpleQueue[list[_RawEvents]]
    """Per-trial event queue shared with :class:`ReadThread`."""

    trial: _Trial
    """The trial being recorded."""

    def __init__(
        self,
        *,
        trial: _Trial,
        fsm: _StateMachineLookup,
        time_reference: _TimeReferences,
        publish: Callable[[list[BpodEventUnion]], object],
        publish_gate: Callable[[], bool],
        on_trial_start: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(name='EventThread', daemon=True)
        self.queue: SimpleQueue[list[_RawEvents]] = SimpleQueue()
        self.trial = trial
        self._recorder = trial.recorder
        self._state_transition_matrix = fsm.state_transition_matrix
        self._use_back_op = fsm.use_back_op
        self._time_reference = time_reference
        self._publish = publish
        self._publish_gate = publish_gate
        self._on_trial_start = on_trial_start
        self._event_info = trial.recorder.event_tables.event_info
        self._resettable_indices = trial.recorder.event_tables.resettable_indices
        self._state_names = fsm.state_names

        self._state_action_indices = fsm.state_action_indices
        self._n_published: int = 0
        self._pending: list[BpodEventUnion] = []  # messages not published yet

    def stop(self) -> None:
        """Signal the FSM thread to stop."""
        self.queue.put(_EVENTS_STOP)

    def _append_and_publish(
        self, time_us: int, event: int, state: int, value: int = -1
    ) -> None:
        """Append an event, and queue its message for :meth:`_flush`."""
        self._recorder.append(time_us, event, state, value)
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
                        trial=self.trial.number,
                        fsm_hash=self.trial.fsm_hash.hex(),
                    )
                case 'TrialEnd':
                    message = EventTrialEnd(time_us=time_us, trial=self.trial.number)
                case 'TrialEndControl':
                    message = EventTrialEndControl(
                        time_us=time_us,
                        trial=self.trial.number,
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

    def run(self) -> None:
        """Execute the EventThread."""
        event_queue = self.queue
        state_transition_matrix = self._state_transition_matrix
        state_action_indices = self._state_action_indices
        resettable_indices = self._resettable_indices
        use_back_op = self._use_back_op
        recorder = self._recorder
        state_entered = recorder.state_entered
        record = recorder.append
        record_and_publish = self._append_and_publish
        target_exit = len(state_transition_matrix)
        target_back = 255
        current_state = 0
        previous_state = 0
        active_outputs: dict[int, int] = {}  # resettable outputs currently non-zero

        base_time_bpod_us = self._time_reference.reset_system_time_ns // 1000

        try:
            while True:
                # once the backlog is processed, publish its messages as one batch
                if self._pending and event_queue.empty():
                    self._flush()

                if (batch := event_queue.get()) is _EVENTS_STOP:
                    return
                # with nobody listening, skip building messages until the next batch
                append = record_and_publish if self._publish_gate() else record
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
                        if (
                            event_index == _EventID.START_FSM
                            and self._on_trial_start is not None
                        ):
                            self._on_trial_start(t_bpod_us)
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
            recorder.finish()  # the rows are final: no peek needs to wait for the rest
            if self._pending:
                self._flush()
            self._decode()

    def _decode(self) -> None:
        """Decode the recorded events, or keep the error doing so.

        Whoever waits for the trial's data gets an answer.
        """
        trial = self.trial
        try:
            self._recorder.decode()
        # a panic in polars isn't an Exception
        except (Exception, pl.exceptions.PanicException) as error:
            logger.exception('Failed to decode trial #%d', trial.number)
            trial.decode_error = BpodError(f'Failed to decode trial #{trial.number}')
            trial.decode_error.__cause__ = error
        trial.decoded.set()


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
        self.join(timeout=1)
        if self.is_alive():
            logger.warning('Softcode thread did not terminate')

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
