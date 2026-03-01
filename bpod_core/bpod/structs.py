"""Data structures used by the bpod module."""

import msgspec


class BpodSettings(msgspec.Struct):
    """Settings for a specific Bpod device."""

    serial_number: str
    """Serial number of the device."""
    name: str = ''
    """User-defined name of the device."""
    location: str = ''
    """User-defined location of the device."""
    zmq_port_pub: int | None = None
    """Port number for the ZeroMQ PUB service."""
    zmq_port_rep: int | None = None
    """Port number for the ZeroMQ REP service."""


class BpodInfo(msgspec.Struct):
    """Information about a specific Bpod device."""

    serial_number: str
    """Serial number of the device."""
    port: str | None = None
    """Port on which the device is connected."""
    name: str = ''
    """User-defined name of the device."""
    location: str = ''
    """User-defined location of the device."""
    zmq_pub: str | None = None
    """ZeroMQ PUB service address."""
    zmq_rep: str | None = None
    """ZeroMQ REP service address."""


class VersionInfo(msgspec.Struct, frozen=True):
    """Data structure representing various version information."""

    firmware: tuple[int, int]
    """Firmware version (major, minor)"""
    machine: int
    """Machine type (numerical)"""
    machine_str: str
    """Machine type (string)"""
    pcb: int | None
    """PCB revision, if applicable"""
    bpod_core: str
    """bpod-core version"""


class HardwareConfiguration(msgspec.Struct, frozen=True):
    """Represents the Bpod's on-board hardware configuration."""

    max_states: int
    """Maximum number of supported states in a single state machine description."""
    cycle_period: int
    """Period of the state machine's refresh cycle during a trial in microseconds."""
    max_serial_events: int
    """Maximum number of behavior events allocatable among connected modules."""
    max_bytes_per_serial_message: int
    """Maximum number of bytes allowed per serial message."""
    n_global_timers: int
    """Number of global timers supported."""
    n_global_counters: int
    """Number of global counters supported."""
    n_conditions: int
    """Number of condition-events supported."""
    n_inputs: int
    """Number of input channels."""
    input_description: bytes
    """Array indicating the state machine's onboard input channel types."""
    n_outputs: int
    """Number of channels in the state machine's output channel description array."""
    output_description: bytes
    """Array indicating the state machine's onboard output channel types."""
    cycle_frequency: int
    """Frequency of the state machine's refresh cycle during a trial in Hertz."""
    n_modules: int
    """Number of modules supported by the state machine."""
