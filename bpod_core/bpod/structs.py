"""Data structures used by the bpod module."""

from typing import NamedTuple

import msgspec
import numpy as np
import numpy.typing as npt
import polars as pl


class _InputEvents(NamedTuple):
    """Hardware-level input event names and their source channels."""

    names: list[str]
    """Event name for each hardware input event, indexed by event ID."""
    channels: list[str | None]
    """Input channel name for each event, or ``None`` for timer/condition events."""
    values: list[int | None]
    """Pre-defined value for each event, or ``None`` if not applicable."""


class CompiledStateMachine(NamedTuple):
    """Per-trial data derived from a compiled :class:`~bpod_core.fsm.StateMachine`."""

    state_names: list[str]
    """Names of all states, indexed by state index."""
    state_transitions: npt.NDArray[np.uint8]
    """Transition matrix of shape ``(n_states, 255)``."""
    state_actions: list[dict[str, int]]
    """Per-state mapping of action name to value."""
    use_back_op: bool
    """Whether the ``>back`` operator is used."""
    state_lookup: pl.DataFrame
    """Categorical lookup DataFrame mapping state index to state name.

    Pre-built at FSM compilation time for use in :meth:`~EventThread.get_data`.
    """


class TimeReferences(NamedTuple):
    """Reference values for performance counters."""

    init_system_time_ns: int
    """System time at class initialization (nanoseconds relative to epoch)."""
    init_perf_counter_ns: int
    """Performance counter at class initialization (nanoseconds)."""
    reset_system_time_ns: int
    """System time when Bpod's session clock was last reset (nanoseconds)."""


class RawEvent(NamedTuple):
    """Raw event data from the Bpod device."""

    micros_us: int
    """Time of the event relative to the Bpod's session clock (microseconds)."""
    event_id: int
    """Index of the event."""


class RawSoftcode(NamedTuple):
    """Raw softcode data from the Bpod device."""

    softcode: int
    """Zero-based softcode value."""
    received_ns: int
    """``time.perf_counter_ns()`` captured immediately after the serial read."""


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
    cycle_period_us: int
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
