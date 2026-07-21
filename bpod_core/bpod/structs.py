"""Data structures used by the bpod module."""

from typing import Any, Literal, NamedTuple, TypeAlias

import msgspec
import numpy as np
import numpy.typing as npt

from bpod_core.misc import ByteEnum


class _ValidationData(NamedTuple):
    """Data returned by :meth:`Bpod._validate_state_machine`."""

    use_back_operator: bool
    """Whether the state machine uses the ``>back`` operator."""
    state_names: list[str]
    """A list of all state names in the state machine."""


class _InputEvents(NamedTuple):
    """Hardware-level input event names and their source channels."""

    names: list[str]
    """Event name for each hardware input event, indexed by event ID."""
    channels: list[str | None]
    """Input channel name for each event, or ``None`` for timer/condition events."""
    values: list[int | None]
    """Pre-defined value for each event, or ``None`` if not applicable."""


class _InputEventRanges(NamedTuple):
    """Index ranges for each input event category."""

    input_channels: range
    """Index range for input channel events."""
    global_timer_starts: range
    """Index range for global timer start events."""
    global_timer_ends: range
    """Index range for global timer end events."""
    global_counter_ends: range
    """Index range for global counter end events."""
    conditions: range
    """Index range for condition events."""


class StateMachineLookup(NamedTuple):
    """Lookup data to decode the raw event stream during a state machine trial."""

    fsm_hash: bytes
    """The state machine's hash value."""
    state_names: list[str]
    """Names of all states, indexed by state index."""
    state_transition_matrix: npt.NDArray[np.uint8]
    """Transition matrix of shape ``(n_states, 255)``."""
    state_actions: list[dict[str, int]]
    """Per-state mapping of action name to value."""
    use_back_op: bool
    """Whether the ``>back`` operator is used."""
    state_lookup: dict[int, str]
    """Mapping from state index to state name."""


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
    micros_us: int
    """Bpod session clock at the time of softcode firing (microseconds)."""


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


class _MessageKind(ByteEnum):
    """The types of messages exchanged between ServiceHost and ServiceClient."""

    HELLO = ord('H')
    """A message sent by :class:`~bpod_core.bpod.RemoteBpod to initiate a handshake."""
    WELCOME = ord('W')
    """A message sent by :class:`~bpod_core.bpod.Bpod` to acknowledge the handshake."""
    REQUEST = ord('Q')
    """A request sent by :class:`~bpod_core.bpod.RemoteBpod`."""
    REPLY = ord('R')
    """A reply sent by :class:`~bpod_core.bpod.Bpod`."""


class BpodMessage(msgspec.Struct, array_like=True, tag_field='kind'):
    """Base Envelope for a Bpod IPC message.

    The ``tag_field`` name is arbitrary — with ``array_like=True`` the tag is encoded
    positionally — but must not collide with any subclass field (e.g. ``type``).
    """


class BpodMessageGeneric(BpodMessage, tag='g'):
    """Envelope for a generic message."""

    data: Any = None
    """The content of the reply."""


class RequestHello(BpodMessage, tag='h'):
    """Envelope for a handshake request."""

    bpod_core_version: str
    """The version of bpod-core that the client is using."""


class RequestBye(BpodMessage, tag='b'):
    """Envelope for a disconnect notice."""


class ReplyWelcome(BpodMessage, tag='w'):
    """Envelope for a handshake reply."""

    version: 'VersionInfo'
    """Version information of the Bpod's firmware and hardware."""
    serial_number: str
    """The Bpod's unique serial number."""
    name: str | None = None
    """The Bpod's user-defined name."""
    location: str | None = None
    """The Bpod's user-defined location."""


class RequestCall(BpodMessage, tag='c'):
    """Envelope for a method call request."""

    method_name: str
    """The name of the method to be called."""
    args: tuple = ()
    """The arguments to be passed to the method."""
    kwargs: dict[str, Any] = {}
    """Keyword arguments to be passed to the method."""


class RequestData(RequestCall, tag='rd'):
    """Envelope for a data request."""

    compression: Literal['uncompressed', 'lz4', 'zstd'] = 'uncompressed'
    """The compression method to be used for the data."""


BpodEventType: TypeAlias = Literal[
    'InputEvent',
    'OutputAction',
    'TrialStart',
    'StateStart',
    'StateEnd',
    'TrialEnd',
    'TrialEndControl',
]
"""Vocabulary of the trial DataFrame's ``type`` column."""


class EventTrialStart(BpodMessage, tag='ts'):
    """
    Message marking the start of a trial.

    Trial streams are strictly sequential: all messages between an
    :class:`EventTrialStart` and the next :class:`EventTrialEnd` belong to the
    trial identified by these markers, and a clean trial's stream is terminated
    by a trailing :class:`EventTrialEndControl`.
    """

    time_us: int
    """Absolute time of the trial start (microseconds since epoch, UTC)."""

    trial: int
    """Zero-based trial index."""

    fsm_hash: str
    """Hex digest of the state machine's hash."""


class EventStateStart(BpodMessage, tag='ss'):
    """Message marking the start of a state."""

    time_us: int
    """Absolute time of the event (microseconds since epoch, UTC)."""

    state: str
    """Name of the state."""


class EventStateEnd(BpodMessage, tag='se'):
    """Message marking the end of a state."""

    time_us: int
    """Absolute time of the event (microseconds since epoch, UTC)."""

    state: str
    """Name of the state."""


class EventInput(BpodMessage, tag='i'):
    """Message for a single input event."""

    time_us: int
    """Absolute time of the event (microseconds since epoch, UTC)."""

    event: str
    """Name of the input event."""

    channel: str | None
    """Name of the input channel, or ``None`` for synthetic events."""

    value: int | None
    """The event's value, or ``None`` if not applicable."""


class EventOutput(BpodMessage, tag='o'):
    """Message for a single output action."""

    time_us: int
    """Absolute time of the action (microseconds since epoch, UTC)."""

    channel: str
    """Name of the output channel."""

    value: int
    """The value set on the output channel."""


class EventTrialEnd(BpodMessage, tag='te'):
    """
    Message marking the end of a trial.

    Only published when the hardware exit packet is received: a trial aborted
    without one publishes neither :class:`EventTrialEnd` nor
    :class:`EventTrialEndControl` - its stream simply ends. See
    :class:`EventTrialStart` for the ordering semantics.
    """

    time_us: int
    """Absolute time of the trial end, derived from the hardware cycle count
    (microseconds since epoch, UTC)."""

    trial: int
    """Zero-based trial index."""


class EventTrialEndControl(BpodMessage, tag='tec'):
    """
    Message terminating a trial's stream with timing-verification data.

    Trails :class:`EventTrialEnd` and carries the hardware's independent
    end-of-trial microsecond count - the same clock pair compared by the
    reader's timing-violation warning.
    """

    time_us: int
    """Hardware microsecond count at the end of the trial
    (microseconds since epoch, UTC)."""

    trial: int
    """Zero-based trial index."""

    n_events: int
    """Number of event messages published for the trial (completeness check)."""


BpodReplyUnion: TypeAlias = BpodMessageGeneric | ReplyWelcome
"""Tagged union of all concrete :class:`BpodMessage` subclasses used for replies."""

BpodRequestUnion: TypeAlias = RequestHello | RequestBye | RequestCall | RequestData
"""Tagged union of all concrete :class:`BpodMessage` subclasses used for requests."""

BpodEventUnion: TypeAlias = (
    EventTrialStart
    | EventStateStart
    | EventStateEnd
    | EventInput
    | EventOutput
    | EventTrialEnd
    | EventTrialEndControl
)
"""Tagged union of all concrete :class:`BpodMessage` subclasses used for events."""


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


class HardwareState(msgspec.Struct):
    """Represents the Bpod's current hardware state."""

    status_led: None | bool = None
    """The current state of the Bpod's status LED. None if unknown."""
