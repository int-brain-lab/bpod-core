"""Constants and identifiers used throughout the package."""

from enum import IntEnum
from struct import Struct

# struct format strings
FMT_UINT8 = 'B'
"""Format string for an unsigned 8-bit integer."""
FMT_UINT16_LE = '<H'
"""Format string for an unsigned 16-bit integer (little-endian)."""
FMT_UINT32_LE = '<I'
"""Format string for an unsigned 32-bit integer (little-endian)."""
FMT_UINT64_LE = '<Q'
"""Format string for an unsigned 64-bit integer (little-endian)."""
FMT_INT8 = 'b'
"""Format string for a signed 8-bit integer."""
FMT_INT16_LE = '<h'
"""Format string for a signed 16-bit integer (little-endian)."""
FMT_INT32_LE = '<i'
"""Format string for a signed 32-bit integer (little-endian)."""
FMT_INT64_LE = '<q'
"""Format string for a signed 64-bit integer (little-endian)."""

# pre-compiled structs for common data types
STRUCT_UINT8 = Struct(FMT_UINT8)
"""Compiled struct representing an unsigned 8-bit integer."""
STRUCT_UINT16_LE = Struct(FMT_UINT16_LE)
"""Compiled struct representing an unsigned 16-bit integer (little-endian)."""
STRUCT_UINT32_LE = Struct(FMT_UINT32_LE)
"""Compiled struct representing an unsigned 32-bit integer (little-endian)."""
STRUCT_UINT64_LE = Struct(FMT_UINT64_LE)
"""Compiled struct representing an unsigned 64-bit integer (little-endian)."""
STRUCT_INT8 = Struct(FMT_INT8)
"""Compiled struct representing a signed 8-bit integer."""
STRUCT_INT16_LE = Struct(FMT_INT16_LE)
"""Compiled struct representing a signed 16-bit integer (little-endian)."""
STRUCT_INT32_LE = Struct(FMT_INT32_LE)
"""Compiled struct representing a signed 32-bit integer (little-endian)."""
STRUCT_INT64_LE = Struct(FMT_INT64_LE)
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
