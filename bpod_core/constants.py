"""Constants and identifiers used throughout the package."""

from enum import IntEnum
from struct import Struct

# maximum integer values
INT8_MIN: int = -128
"""Minimum value for a signed 8-bit integer."""
INT16_MIN: int = -32768
"""Minimum value for a signed 16-bit integer."""
INT32_MIN: int = -2147483648
"""Minimum value for a signed 32-bit integer."""
INT64_MIN: int = -9223372036854775808
"""Minimum value for a signed 64-bit integer."""
INT8_MAX: int = 127
"""Maximum value for a signed 8-bit integer."""
INT16_MAX: int = 32767
"""Maximum value for a signed 16-bit integer."""
INT32_MAX: int = 2147483647
"""Maximum value for a signed 32-bit integer."""
INT64_MAX: int = 9223372036854775807
"""Maximum value for a signed 64-bit integer."""
UINT8_MAX: int = 255
"""Maximum value for an unsigned 8-bit integer."""
UINT16_MAX: int = 65535
"""Maximum value for an unsigned 16-bit integer."""
UINT32_MAX: int = 4294967295
"""Maximum value for an unsigned 32-bit integer."""
UINT64_MAX: int = 18446744073709551615

# struct format strings
FMT_UINT8: str = 'B'
"""Format string for an unsigned 8-bit integer."""
FMT_UINT16_LE: str = '<H'
"""Format string for an unsigned 16-bit integer (little-endian)."""
FMT_UINT32_LE: str = '<I'
"""Format string for an unsigned 32-bit integer (little-endian)."""
FMT_UINT64_LE: str = '<Q'
"""Format string for an unsigned 64-bit integer (little-endian)."""
FMT_INT8: str = 'b'
"""Format string for a signed 8-bit integer."""
FMT_INT16_LE: str = '<h'
"""Format string for a signed 16-bit integer (little-endian)."""
FMT_INT32_LE: str = '<i'
"""Format string for a signed 32-bit integer (little-endian)."""
FMT_INT64_LE: str = '<q'
"""Format string for a signed 64-bit integer (little-endian)."""

# pre-compiled structs for common data types
STRUCT_UINT8 = Struct(FMT_UINT8)
"""Compiled struct representing an unsigned 8-bit integer."""
STRUCT_UINT16_LE: Struct = Struct(FMT_UINT16_LE)
"""Compiled struct representing an unsigned 16-bit integer (little-endian)."""
STRUCT_UINT32_LE: Struct = Struct(FMT_UINT32_LE)
"""Compiled struct representing an unsigned 32-bit integer (little-endian)."""
STRUCT_UINT64_LE: Struct = Struct(FMT_UINT64_LE)
"""Compiled struct representing an unsigned 64-bit integer (little-endian)."""
STRUCT_INT8: Struct = Struct(FMT_INT8)
"""Compiled struct representing a signed 8-bit integer."""
STRUCT_INT16_LE: Struct = Struct(FMT_INT16_LE)
"""Compiled struct representing a signed 16-bit integer (little-endian)."""
STRUCT_INT32_LE: Struct = Struct(FMT_INT32_LE)
"""Compiled struct representing a signed 32-bit integer (little-endian)."""
STRUCT_INT64_LE: Struct = Struct(FMT_INT64_LE)
"""Compiled struct representing a signed 64-bit integer (little-endian)."""

IPV4_LOOPBACK: str = '127.0.0.1'
"""IPv4 loopback address."""
IPV4_WILDCARD: str = '0.0.0.0'  # noqa: S104
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
