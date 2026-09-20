"""Classes implementing the FlexIO subsystem."""

import logging
import struct
from collections.abc import Callable, Sequence

import msgspec
from typing_extensions import override

from bpod_core.bpod.abc import AbstractFlexIO
from bpod_core.bpod.constants import CHANNEL_TYPES_INPUT, FlexIOChannelType
from bpod_core.bpod.errors import BpodError
from bpod_core.bpod.structs import _FlexIOState
from bpod_core.com import ExtendedSerial
from bpod_core.constants import UINT12_MAX

logger = logging.getLogger(__name__)

_SERIAL_TIMEOUT = 0.2


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


class FlexIO(AbstractFlexIO):
    """Local FlexIO implementation with direct hardware access."""

    __slots__ = ('_bpod_serial', '_cycle_frequency', '_is_running')

    _bpod_serial: ExtendedSerial

    def __init__(
        self,
        bpod_serial: ExtendedSerial,
        n: int,
        *,
        cycle_frequency: int,
        on_channel_types_changed: Callable[[], None],
        on_analog_sampling_rate_changed: Callable[[], None],
        is_running: Callable[[], bool],
    ) -> None:
        super().__init__(
            n,
            on_channel_types_changed=on_channel_types_changed,
            on_analog_sampling_rate_changed=on_analog_sampling_rate_changed,
        )
        self._bpod_serial = bpod_serial
        self._cycle_frequency = cycle_frequency
        self._is_running = is_running

    @override
    def _apply_settings(self, state: _FlexIOState, *, force: bool = False) -> None:
        # settings are written to and acknowledged over the same serial port a
        # running trial's read thread owns; interleaving would corrupt the trial's
        # event stream, since the ack byte (0x01) collides with an event-packet
        # opcode
        if self._is_running():
            raise BpodError(
                'Cannot change FlexIO settings while a state machine is running.'
            )
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
            polarities = (ch[k] for k in (0, 1) for ch in state.threshold_polarities)
            buffer.extend(struct.pack(f'<c{n * 2}B', b'p', *polarities))
            n_confirmations += 1
        if force or state.threshold_voltages != old.threshold_voltages:
            voltages = (
                round(ch[k] / 5 * UINT12_MAX)
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
        if channel_types_changed:
            self._on_channel_types_changed()
        if analog_sampling_rate_changed:
            self._on_analog_sampling_rate_changed()

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
        if self._is_running():
            raise BpodError(
                'Cannot re-arm an analog threshold while a state machine is running. '
                "Use the 'AnalogThreshEnable'/'AnalogThreshDisable' actions instead."
            )
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
        self._state = msgspec.structs.replace(self._state, threshold_enabled=enabled)

    @override
    def reset(self) -> None:
        """Reset the FlexIO subsystem to its default settings."""
        logger.debug('Resetting FlexIO subsystem to default state')
        default = _FlexIOState.create_default(n_channels=len(self))
        self._apply_settings(default, force=True)
