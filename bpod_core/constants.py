"""Constants and identifiers used throughout the package."""

from struct import Struct

import platformdirs

# pre-compiled structs for common data types
STRUCT_BOOL = Struct('?')
"""Compiled struct representing a boolean value."""
STRUCT_UINT8 = Struct('B')
"""Compiled struct representing an unsigned 8-bit integer."""
STRUCT_UINT16 = Struct('<H')
"""Compiled struct representing an unsigned 16-bit integer."""
STRUCT_UINT32 = Struct('<I')
"""Compiled struct representing an unsigned 32-bit integer."""
STRUCT_UINT64 = Struct('<Q')
"""Compiled struct representing an unsigned 64-bit integer."""
STRUCT_INT8 = Struct('b')
"""Compiled struct representing a signed 8-bit integer."""
STRUCT_INT16 = Struct('<h')
"""Compiled struct representing a signed 16-bit integer."""
STRUCT_INT32 = Struct('<i')
"""Compiled struct representing a signed 32-bit integer."""
STRUCT_INT64 = Struct('<q')
"""Compiled struct representing a signed 64-bit integer."""

IP_LOOPBACK = '127.0.0.1'
"""IPv4 loopback address."""
IP_ANY = '0.0.0.0'
"""IPv4 wildcard address for binding to all interfaces."""

VID_TEENSY: int = 0x16C0
"""Vendor ID of Teensy microcontrollers."""

PLATFORMDIRS = platformdirs.PlatformDirs(appname='bpod-core', appauthor=False)


class PIDsTeensy:
    """Product IDs of Teensy microcontrollers."""

    SERIAL: int = 0x0483
    """Product ID of Teensy microcontrollers with single USB serial port."""
    DUAL_SERIAL: int = 0x048B
    """Product ID of Teensy microcontrollers with dual USB serial ports."""
    TRIPLE_SERIAL: int = 0x048C
    """Product ID of Teensy microcontrollers with triple USB serial ports."""
