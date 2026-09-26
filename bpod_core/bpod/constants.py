"""Constants used by the bpod module."""

from enum import IntEnum
from typing import Annotated
from uuid import UUID

import platformdirs
from pydantic import Field

from bpod_core.constants import VID_TEENSY, TeensyPID

BPOD_UUID_NAMESPACE = UUID('9dbb7a76-cb73-4b16-aaf7-f574f4d218f6')
"""UUID namespace for Bpod devices"""

VIDS_BPOD = [VID_TEENSY]
"""Vendor IDs of supported Bpod devices"""

PIDS_BPOD = [TeensyPID.SERIAL, TeensyPID.DUAL_SERIAL, TeensyPID.TRIPLE_SERIAL]
"""List of Product IDs of supported Bpod devices"""

MIN_BPOD_FW_VERSION = (23, 0)
"""minimum supported firmware version (major, minor)"""

MIN_BPOD_HW_VERSION = 3
"""minimum supported hardware version"""

MAX_BPOD_HW_VERSION = 4
"""maximum supported hardware version"""

_CHANNEL_BASE_NAME_GLOBAL_TIMER = 'GlobalTimer'
"""Base name of global timer channels"""
_CHANNEL_BASE_NAME_GLOBAL_COUNTER = 'GlobalCounter'
"""Base name of global counter channels"""
_CHANNEL_BASE_NAME_CONDITION = 'Condition'
"""Base name of condition channels"""

CHANNEL_TYPES_INPUT = {
    b'U': 'Serial',
    b'X': 'SoftCode',
    b'Z': 'SoftCodeApp',
    b'F': 'Flex',
    b'D': 'Digital',
    b'B': 'TTLIn',
    b'W': 'Wire',
    b'P': 'Port',
}
CHANNEL_TYPES_OUTPUT = CHANNEL_TYPES_INPUT.copy()
CHANNEL_TYPES_OUTPUT.update(
    {
        b'V': 'Valve',
        b'P': 'PWM',
        b'B': 'TTLOut',
    }
)
N_SERIAL_EVENTS_DEFAULT = 15
VALID_OPERATORS = {'>exit', '>back'}
MACHINE_TYPES = {3: 'r2.0-2.5', 4: '2+ r1.0'}
CONFIG_PATH = platformdirs.user_config_path(appname='bpod-core', appauthor=False)
DISCOVERY_TIMEOUT = 0.11


_REMOTE_CALL_METHODS = frozenset(
    {
        'reset_session_clock',
        'run',
        'set_status_led',
        'stop_state_machine',
        'update_modules',
    }
)
"""Methods that remote clients may invoke."""

_REMOTE_DATA_METHODS = frozenset({'get_data', 'peek_data'})
"""DataFrame-returning methods that remote clients may invoke."""


class FlexIOChannelType(IntEnum):
    """Represents a FlexIO channel's type."""

    DIGITAL_INPUT = 0
    """Digital input channel."""
    DIGITAL_OUTPUT = 1
    """Digital output channel."""
    ANALOG_INPUT = 2
    """Analog input channel."""
    ANALOG_OUTPUT = 3
    """Analog output channel."""
    DISABLED = 4
    """Channel disabled / high impedance."""


class FlexIOThresholdMode(IntEnum):
    """Represents a FlexIO channel's threshold mode."""

    MANUAL = 0
    """Thresholds have to be re-enabled manually."""
    LINKED = 1
    """Crossing one threshold resets the other."""


class FlexIOThresholdPolarity(IntEnum):
    """Represents a FlexIO channel's threshold polarity."""

    RISING = 0
    """Event is triggered when the signal crosses above the threshold."""
    FALLING = 1
    """Event is triggered when the signal crosses below the threshold."""


FlexIOThresholdVoltage = Annotated[float, Field(ge=0.0, le=5.0, allow_inf_nan=False)]
"""Threshold voltage of an analog FlexIO input channel in volts"""

FlexIOAnalogSamplingRate = Annotated[int, Field(ge=1, le=1000)]
"""Sampling rate for FlexIO channels configured as analog input, in Hz."""

FlexIONReadsPerSample = Annotated[int, Field(ge=1, le=4)]
"""Number of ADC reads averaged per FlexIO analog sample."""
