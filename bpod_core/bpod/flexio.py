"""Classes implementing the FlexIO subsystem."""

import logging
import struct

from typing_extensions import override

from bpod_core.bpod.abc import AbstractFlexIO
from bpod_core.bpod.structs import _FlexIOState
from bpod_core.com import ExtendedSerial
from bpod_core.constants import UINT12_MAX

logger = logging.getLogger(__name__)

_SERIAL_TIMEOUT = 0.2


class FlexIO(AbstractFlexIO):
    """Local FlexIO implementation with direct hardware access."""

    __slots__ = ('_serial',)

    _serial: ExtendedSerial

    def __init__(self, serial: ExtendedSerial, n: int) -> None:
        super().__init__(n)
        self._serial = serial

    @override
    def _apply_settings(self, state: _FlexIOState, *, force: bool = False) -> None:
        old = self._state
        n = len(state.channel_types)
        buffer = bytearray()
        n_confirmations = 0

        if force or state.channel_types != old.channel_types:
            buffer.extend(struct.pack(f'<c{n}B', b'Q', *state.channel_types))
            n_confirmations += 1
        if force or state.threshold_modes != old.threshold_modes:
            buffer.extend(struct.pack(f'<c{n}B', b'm', *state.threshold_modes))
            n_confirmations += 1
        if force or state.threshold_polarities != old.threshold_polarities:
            polarities = (x for ch in state.threshold_polarities for x in ch)
            buffer.extend(struct.pack(f'<c{n * 2}B', b'p', *polarities))
            n_confirmations += 1
        if force or state.threshold_voltages != old.threshold_voltages:
            voltages = (
                round(v / 5 * UINT12_MAX) for ch in state.threshold_voltages for v in ch
            )
            buffer.extend(struct.pack(f'<c{n * 2}H', b't', *voltages))
            n_confirmations += 1

        for channel_index in range(n):
            for threshold_index in (0, 1):
                new_value = state.threshold_enabled[channel_index][threshold_index]
                old_value = old.threshold_enabled[channel_index][threshold_index]
                if force or new_value != old_value:
                    buffer.extend(
                        struct.pack(
                            '<cBB?', b'e', channel_index, threshold_index, new_value
                        )
                    )
                    n_confirmations += 1

        if len(buffer) > 0 and not self._serial.verify(
            query=bytes(buffer),
            expected_response=n_confirmations * b'\x01',
            timeout=_SERIAL_TIMEOUT,
        ):
            raise RuntimeError('Failed to apply FlexIO settings')

        self._state = state

    @override
    def reset(self) -> None:
        """Reset the FlexIO subsystem to its default settings."""
        logger.debug('Resetting FlexIO subsystem')
        default = _FlexIOState.create_default(n_channels=len(self._view))
        self._apply_settings(default, force=True)
