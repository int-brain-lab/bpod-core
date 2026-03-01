"""Constants and identifiers used throughout the package."""

from enum import IntEnum
from struct import Struct

# pre-compiled structs for common data types
STRUCT_UINT8 = Struct('B')
"""Compiled struct representing an unsigned 8-bit integer."""
STRUCT_UINT16_LE = Struct('<H')
"""Compiled struct representing an unsigned 16-bit integer (little-endian)."""
STRUCT_UINT32_LE = Struct('<I')
"""Compiled struct representing an unsigned 32-bit integer (little-endian)."""
STRUCT_UINT64_LE = Struct('<Q')
"""Compiled struct representing an unsigned 64-bit integer (little-endian)."""
STRUCT_INT8 = Struct('b')
"""Compiled struct representing a signed 8-bit integer."""
STRUCT_INT16_LE = Struct('<h')
"""Compiled struct representing a signed 16-bit integer (little-endian)."""
STRUCT_INT32_LE = Struct('<i')
"""Compiled struct representing a signed 32-bit integer (little-endian)."""
STRUCT_INT64_LE = Struct('<q')
"""Compiled struct representing a signed 64-bit integer (little-endian)."""

IPV4_LOOPBACK = '127.0.0.1'
"""IPv4 loopback address."""
IPV4_WILDCARD = '0.0.0.0'  # noqa: S104
"""IPv4 wildcard address for binding to all interfaces."""

VID_TEENSY: int = 0x16C0
"""Vendor ID of Teensy microcontrollers."""


class TeensyPID(IntEnum):
    """Product IDs of Teensy microcontrollers."""

    SERIAL = 0x0483
    """Product ID of Teensy microcontrollers with single USB serial port."""
    DUAL_SERIAL = 0x048B
    """Product ID of Teensy microcontrollers with dual USB serial ports."""
    TRIPLE_SERIAL = 0x048C
    """Product ID of Teensy microcontrollers with triple USB serial ports."""
