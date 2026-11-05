"""Classes implementing the FlexIO subsystem."""

import logging
import struct
import threading
from collections.abc import Callable, Sequence
from queue import SimpleQueue
from time import perf_counter_ns
from typing import TypeVar

import msgspec
import numpy as np
import numpy.typing as npt
import polars as pl
from typing_extensions import override

from bpod_core.bpod.abc import AbstractFlexIO
from bpod_core.bpod.constants import CHANNEL_TYPES_INPUT, FlexIOChannelType
from bpod_core.bpod.errors import BpodError, owning_replies
from bpod_core.bpod.structs import _FlexIOState
from bpod_core.com import ExtendedSerial
from bpod_core.constants import UINT12_MAX
from bpod_core.misc import _grown

logger = logging.getLogger(__name__)

_SERIAL_TIMEOUT = 0.2

_ADC_DAC_FULL_SCALE_MV = 5000  # FlexIO ADC/DAC full-scale range (0-5 V), in mV

_ANALOG_FLUSH_INTERVAL_NS = 100_000_000  # 100 ms
"""How often the analog recorder turns its buffered samples into a DataFrame."""

_INITIAL_ANALOG_BUFFER_SIZE = 1024
"""Initial size of the analog recorder's sample buffer, in samples."""

_CountsT = TypeVar('_CountsT', int, pl.Expr)


def _counts_to_mv(counts: _CountsT) -> _CountsT:
    """Convert 12-bit ADC/DAC counts (0-4095) to integer millivolts, losslessly."""
    return (counts * _ADC_DAC_FULL_SCALE_MV + UINT12_MAX // 2) // UINT12_MAX


def _mv_to_counts(mv: _CountsT) -> _CountsT:
    """Convert integer millivolts (0-5000) to 12-bit ADC/DAC counts, losslessly."""
    return (mv * UINT12_MAX + _ADC_DAC_FULL_SCALE_MV // 2) // _ADC_DAC_FULL_SCALE_MV


def is_flexio_threshold_ambiguous(
    channel_types: Sequence[FlexIOChannelType], index: int
) -> bool:
    """Whether channel `index`'s analog-threshold event codes are ambiguous.

    Firmware numbers analog-threshold events by rank among ``ANALOG_INPUT``
    channels only, not by physical channel index, while every other FlexIO event
    (digital transitions, etc.) is numbered by fixed physical position. The two
    schemes only agree when channel `index` is preceded exclusively by other
    ``ANALOG_INPUT`` channels; otherwise its Trig0/Trig1 codes collide with
    another channel's fixed-position event codes, an upstream firmware quirk
    also present in MATLAB.
    """
    return any(t != FlexIOChannelType.ANALOG_INPUT for t in channel_types[:index])


class _AnalogRecorder:
    """The FlexIO analog samples of one connection, buffered for ``get_analog_data()``.

    The analog reader thread feeds :meth:`on_chunk` with one sample block at a time
    and holds the recorder, not the Bpod: a dropped Bpod is collected while it still
    streams. Samples go into a pre-allocated NumPy structured array that doubles when
    full; :meth:`flush` turns them into a DataFrame on `frames` every 100 ms of
    samples, once the first trial's start time is known (the samples are timed by
    counting from it, so they are counted before it is known, too).

    Parameters
    ----------
    channel_names : tuple of str
        The channels recorded, in the firmware's order.
    sample_period_us : int
        The time between samples, in µs.
    cycle_period_us : int
        The Bpod's cycle period, in µs: the first sample of a session fires one cycle
        after the trial start.
    frames : SimpleQueue[pl.DataFrame]
        Where the flushed DataFrames go: ``get_analog_data()``'s queue.
    """

    def __init__(
        self,
        *,
        channel_names: tuple[str, ...],
        sample_period_us: int,
        cycle_period_us: int,
        frames: SimpleQueue[pl.DataFrame],
    ) -> None:
        self.channel_names = channel_names
        self.sample_period_us = sample_period_us
        self._cycle_period_us = cycle_period_us
        self._frames = frames
        # a block: the firmware's trial counter, then a 12-bit count per channel
        self._struct = struct.Struct(f'<{1 + len(channel_names)}H')
        self.chunk_size = self._struct.size
        """The size of one sample block on the serial port."""
        self._buffer: npt.NDArray = np.empty(
            _INITIAL_ANALOG_BUFFER_SIZE,
            dtype=[
                ('k', np.int64),  # the sample's ordinal since the session clock reset
                ('Trial', np.uint32),
                *((name, np.uint16) for name in channel_names),
            ],
        )
        self._n = 0  # samples in the buffer
        self._lock = threading.Lock()  # the reader thread writes, any thread flushes
        self._next_index = 0  # of the next sample since the session clock reset
        self._last_flush_ns = perf_counter_ns()
        self.anchor_us: int | None = None
        """The first trial's start since the session clock reset, in µs since the
        epoch: the samples are timed from it. ``None`` until that trial starts."""
        self.trial_numbers: list[int] = []
        """The trial numbers by the firmware's trial count since the clock reset: the
        samples only carry that count."""

    def reset(self) -> None:
        """Start over after a session clock reset: the firmware restarts its counts.

        The samples buffered so far are flushed, timed by the old anchor - or dropped
        if no trial anchored them: nothing will.
        """
        with self._lock:
            if self.anchor_us is not None:
                self._flush()
            self._n = 0
            self.anchor_us = None
            self.trial_numbers = []
            self._next_index = 0

    def on_chunk(self, chunk: bytearray) -> None:
        """Buffer one sample block from the serial port."""
        trial_counter, *counts = self._struct.unpack(chunk)
        if trial_counter < 1:
            return  # the firmware hasn't started sampling yet: pre-trial idle state
        # the firmware's trial counter increments before its first use: the first
        # trial after a reset reports 1
        trial_numbers = self.trial_numbers
        sequence_index = trial_counter - 1
        trial = (
            trial_numbers[sequence_index]
            if sequence_index < len(trial_numbers)
            else trial_counter
        )
        with self._lock:
            n, buffer = self._n, self._buffer
            if n == len(buffer):
                self._buffer = buffer = _grown(buffer)
            buffer[n] = (self._next_index, trial, *counts)
            self._n = n + 1
            self._next_index += 1
        if perf_counter_ns() - self._last_flush_ns >= _ANALOG_FLUSH_INTERVAL_NS:
            self.flush()

    def flush(self) -> None:
        """Turn the buffered samples into a DataFrame, if their timing is known."""
        with self._lock:
            self._flush()

    def _flush(self) -> None:
        """:meth:`flush` under the lock: the frames are queued in sample order."""
        self._last_flush_ns = perf_counter_ns()
        if (anchor_us := self.anchor_us) is None:
            return  # keep buffering until the first trial's start is known
        rows = self._buffer[: self._n].copy()
        self._n = 0
        if not len(rows):
            return
        # the firmware preloads its sample clock, so the first sample fires one cycle
        # after the trial start, not a full sample period
        first_us = anchor_us + self._cycle_period_us
        self._frames.put(
            pl.from_numpy(rows).select(
                pl.from_epoch(first_us + pl.col('k') * self.sample_period_us, 'us')
                .dt.replace_time_zone('UTC')
                .alias('Time'),
                'Trial',
                _counts_to_mv(pl.col(self.channel_names).cast(pl.UInt32)).cast(
                    pl.UInt16
                ),
            )
        )


class FlexIO(AbstractFlexIO):
    """Local FlexIO implementation with direct hardware access."""

    __slots__ = (
        '_bpod_serial',
        '_configuration_locked',
        '_cycle_frequency',
    )

    _bpod_serial: ExtendedSerial

    def __init__(
        self,
        bpod_serial: ExtendedSerial,
        n: int,
        *,
        cycle_frequency: int,
        on_input_events_changed: Callable[[], object],
        configuration_locked: Callable[[], bool],
    ) -> None:
        super().__init__(n, on_input_events_changed=on_input_events_changed)
        self._bpod_serial = bpod_serial
        self._cycle_frequency = cycle_frequency
        self._configuration_locked = configuration_locked

    @override
    def _apply_settings(self, state: _FlexIOState, *, force: bool = False) -> None:
        # settings are written to and acknowledged over the port a running trial's
        # reader owns; interleaving would corrupt the trial's event stream, since the
        # acknowledgement byte (0x01) collides with an event-packet opcode
        with owning_replies(self._bpod_serial, 'change FlexIO settings'):
            old = self._state
            n = len(state.channel_types)
            buffer = bytearray()
            n_confirmations = 0

            channel_types_changed = force or state.channel_types != old.channel_types
            if channel_types_changed:
                buffer.extend(struct.pack(f'<c{n}B', b'Q', *state.channel_types))
                n_confirmations += 1
            analog_sampling_rate_changed = (
                force or state.analog_sampling_rate != old.analog_sampling_rate
            )
            # channel types and the sampling rate set the analog stream's frame layout
            # and timing. Once a state machine has started the stream, the firmware
            # keeps it running (also across session-clock resets), so changing them
            # would corrupt it - they stay fixed until the Bpod is reconnected
            if (
                channel_types_changed or analog_sampling_rate_changed
            ) and self._configuration_locked():
                raise BpodError(
                    'Cannot change FlexIO channel types or the analog sampling rate '
                    'after a state machine has run. Close and reopen the Bpod to '
                    'change them.'
                )
            if analog_sampling_rate_changed:
                n_cycles = round(self._cycle_frequency / state.analog_sampling_rate)
                buffer.extend(struct.pack('<cI', b'^', n_cycles))
                n_confirmations += 1
            if force or state.n_reads_per_sample != old.n_reads_per_sample:
                buffer.extend(struct.pack('<cB', b'o', state.n_reads_per_sample))
                n_confirmations += 1
            if force or state.threshold_modes != old.threshold_modes:
                buffer.extend(struct.pack(f'<c{n}B', b'm', *state.threshold_modes))
                n_confirmations += 1
            if force or state.threshold_polarities != old.threshold_polarities:
                polarities = (
                    ch[k] for k in (0, 1) for ch in state.threshold_polarities
                )
                buffer.extend(struct.pack(f'<c{n * 2}B', b'p', *polarities))
                n_confirmations += 1
            threshold_voltages_changed = (
                force or state.threshold_voltages != old.threshold_voltages
            )
            if threshold_voltages_changed:
                voltages = (
                    _mv_to_counts(ch[k])
                    for k in (0, 1)
                    for ch in state.threshold_voltages
                )
                buffer.extend(struct.pack(f'<c{n * 2}H', b't', *voltages))
                n_confirmations += 1

            # threshold_enabled is excluded from the diff below: firmware disarms a
            # threshold autonomously when it fires, so a diff against cached state
            # would miss re-arming it (see _set_threshold_enabled). Only a full reset
            # (force=True) resends it here, to put every threshold in a known state.
            if force:
                for channel_index in range(n):
                    for threshold_index in (0, 1):
                        value = state.threshold_enabled[channel_index][threshold_index]
                        buffer.extend(
                            struct.pack(
                                '<cBB?', b'e', channel_index, threshold_index, value
                            )
                        )
                        n_confirmations += 1

            if len(buffer) > 0 and not self._bpod_serial.verify(
                query=bytes(buffer),
                expected_response=n_confirmations * b'\x01',
                timeout=_SERIAL_TIMEOUT,
            ):
                raise RuntimeError('Failed to apply FlexIO settings')

            self._state = state
            # channel types name the input events, threshold voltages set their values
            if channel_types_changed or threshold_voltages_changed:
                self._on_input_events_changed()

    @override
    def _set_threshold_enabled(
        self,
        channel_index: int,
        threshold_index: int,
        value: bool,
    ) -> None:
        # deliberately bypasses _apply_settings's diffing: firmware changes this
        # flag on its own (disarms on trigger, flips it in LINKED mode, and via
        # the AnalogThreshEnable/AnalogThreshDisable actions), so the cached state
        # can't be trusted to detect a real change - always write it
        with owning_replies(
            self._bpod_serial,
            're-arm an analog threshold',
            " Use the 'AnalogThreshEnable'/'AnalogThreshDisable' actions instead.",
        ):
            channel_types = self._state.channel_types
            if (
                value
                and channel_types[channel_index] == FlexIOChannelType.ANALOG_INPUT
                and is_flexio_threshold_ambiguous(channel_types, channel_index)
            ):
                name = f'{CHANNEL_TYPES_INPUT[b"F"]}{channel_index + 1}'
                raise BpodError(
                    f"Cannot arm '{name}' threshold: an earlier FlexIO channel isn't "
                    'configured as ANALOG_INPUT, so firmware cannot reliably attribute '
                    f"'{name}'s threshold-crossing events (upstream firmware quirk)."
                )
            if not self._bpod_serial.verify(
                query=struct.pack('<cBB?', b'e', channel_index, threshold_index, value),
                expected_response=b'\x01',
                timeout=_SERIAL_TIMEOUT,
            ):
                raise RuntimeError('Failed to apply FlexIO settings')
            enabled = [list(e) for e in self._state.threshold_enabled]
            enabled[channel_index][threshold_index] = value
            self._state = msgspec.structs.replace(
                self._state, threshold_enabled=enabled
            )

    @override
    def reset(self) -> None:
        """Reset the FlexIO subsystem to its default settings."""
        logger.debug('Resetting FlexIO subsystem to default state')
        default = _FlexIOState.create_default(n_channels=len(self))
        self._apply_settings(default, force=True)
