"""Constants and identifiers used throughout the package."""

IP_LOOPBACK = '127.0.0.1'
"""IPv4 loopback address."""

IP_ANY = '0.0.0.0'
"""IPv4 wildcard address for binding to all interfaces."""

VID_TEENSY: int = 0x16C0
"""Vendor ID of Teensy microcontrollers."""


class PIDsTeensy:
    """Product IDs of Teensy microcontrollers."""

    SERIAL: int = 0x0483
    """Product ID of Teensy microcontrollers with single USB serial port."""

    DUAL_SERIAL: int = 0x048B
    """Product ID of Teensy microcontrollers with dual USB serial ports."""

    TRIPLE_SERIAL: int = 0x048C
    """Product ID of Teensy microcontrollers with triple USB serial ports."""
