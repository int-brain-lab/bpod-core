"""Module defining classes and types for creating and managing state machines."""

import datetime
import re
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, NamedTuple, cast

import msgspec
import yaml
from cachetools import FIFOCache
from graphviz import Digraph  # type: ignore[import-untyped]
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    WrapValidator,
    validate_call,
)
from pydantic_core import PydanticCustomError
from pydantic_core.core_schema import ValidatorFunctionWrapHandler
from pydantic_extra_types.color import Color, ColorType
from xxhash import xxh3_64 as _xxh3_64

from bpod_core.constants import UINT32_MAX
from bpod_core.misc import ValidatedDict, suggest_similar


class _CheckData(NamedTuple):
    """Data returned by :meth:`StateMachine._check`."""

    all_state_names: list[str]
    """A set of all state names."""
    transition_targets: set[str]
    """A set of all transition targets."""


def enc_hook(obj: Any) -> Any:
    """Encode a :class:`~pydantic.BaseModel` instance into a dictionary."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(exclude_defaults=True)
    raise NotImplementedError(f'Objects of type {type(obj)} are not supported')


def dec_hook(obj_type: type, obj: dict) -> Any:
    """Decode a dictionary into a :class:`~pydantic.BaseModel` instance."""
    if issubclass(obj_type, BaseModel):
        return obj_type.model_validate(obj)
    raise NotImplementedError(f'Objects of type {type} are not supported')


_timedelta_adapter = TypeAdapter(datetime.timedelta)


def _validate_seconds(v: Any, h: ValidatorFunctionWrapHandler) -> float:
    try:
        return cast('float', h(v))
    except ValidationError as e1:
        try:
            td = _timedelta_adapter.validate_python(v)
            return cast('float', h(td.total_seconds()))
        except ValidationError as e2:
            raise e1 from e2


def _validate_state_timer(v: Any, h: ValidatorFunctionWrapHandler) -> float:
    try:
        return cast('float', h(v))
    except ValidationError as e:
        for error in e.errors():
            if (error_type := error.get('type')) == 'greater_than_equal':
                raise PydanticCustomError(
                    error_type,
                    'Invalid State Timer - cannot be negative',
                    error.get('ctx', {}),
                ) from e
        raise


def _validate_state_name(v: Any, h: ValidatorFunctionWrapHandler) -> 'StateName':
    try:
        return cast('StateName', h(v))
    except ValidationError as e:
        for error in e.errors():
            match error_type := error.get('type'):
                case 'string_pattern_mismatch':
                    if v.startswith('>'):
                        detail = (
                            "prefix '>' is reserved for State Machine Operators "
                            "like '>exit'"
                        )
                    elif v == 'exit':
                        detail = (
                            "can be confused with the State Machine Operator '>exit'"
                        )
                    elif v == 'back':
                        detail = (
                            "can be confused with the State Machine Operator '>back'"
                        )
                    else:
                        continue
                    detail = f"Invalid State Name '{v}' - {detail}"
                case 'string_too_short':
                    detail = 'A State Name must be at least 1 character long'
                case _:
                    continue
            raise PydanticCustomError(error_type, detail, error.get('ctx', {})) from e
        raise


def _validate_operator(v: Any, h: ValidatorFunctionWrapHandler) -> 'Operator':
    try:
        return cast('Operator', h(v))
    except ValidationError as e:
        for error in e.errors():
            match error_type := error.get('type'):
                case 'string_pattern_mismatch' if not v.startswith('>'):
                    detail = "must be a string with prefix '>'"
                case 'string_too_short':
                    detail = "must be at least 2 characters long (including '>' prefix)"
                case _:
                    continue
            detail = f"Invalid State Machine Operator '{v}' - {detail}"
            raise PydanticCustomError(error_type, detail, error.get('ctx', {})) from e
        raise


def _validate_binary_string(v: Any) -> Any:
    if isinstance(v, str) and set(v).issubset({'0', '1'}):
        return int(v, 2)
    return v


StateTimer = Annotated[
    float,
    Field(
        title='State Timer',
        description="The state's timer in seconds",
        default=0.0,
        ge=0.0,
        allow_inf_nan=False,
    ),
    WrapValidator(_validate_state_timer),
    WrapValidator(_validate_seconds),
]

StateComment = Annotated[
    str,
    Field(
        title='Comment',
        description='A comment describing the state.',
    ),
]

GlobalTimerDuration = Annotated[
    float,
    Field(
        title='Global Timer Duration',
        description='The duration of the global timer in seconds',
        ge=0.0,
        allow_inf_nan=False,
    ),
    WrapValidator(_validate_seconds),
]

GlobalTimerOnsetDelay = Annotated[
    float,
    Field(
        title='Onset Delay',
        description='The onset delay of the global timer in seconds',
        default=0.0,
        ge=0.0,
        allow_inf_nan=False,
    ),
    WrapValidator(_validate_seconds),
]

GlobalTimerChannel = Annotated[
    str,
    Field(
        title='Channel',
        description='The channel affected by the global timer',
        min_length=1,
    ),
]

GlobalTimerChannelValue = Annotated[
    int,
    Field(
        title='Channel Value',
        description='The value a channel is set to',
        default=0,
        ge=0,
        le=255,
    ),
]

GlobalTimerSendEvents = Annotated[
    bool,
    Field(
        title='Send Events',
        description='Whether the global timer is sending events',
        default=True,
    ),
]

GlobalTimerLoop = Annotated[
    int,
    Field(
        title='Loop Mode',
        description=(
            '0 = off, 1 = loop until canceled or trial end, >1 = fixed number '
            'of iterations (max 255)'
        ),
        default=0,
        ge=0,
        le=255,
    ),
]

GlobalTimerLoopInterval = Annotated[
    float,
    Field(
        title='Loop Interval',
        description=(
            'Delay in seconds between the end of a loop iteration and the start of the '
            'next'
        ),
        default=0.0,
        ge=0.0,
    ),
    WrapValidator(_validate_seconds),
]

GlobalTimerOnsetTrigger = Annotated[
    int,
    Field(
        title='Onset Trigger',
        description='An integer whose bits indicate other global timers to trigger',
        default=0,
        ge=0,
    ),
    BeforeValidator(_validate_binary_string),
]

GlobalCounterThreshold = Annotated[
    int,
    Field(
        title='Threshold',
        description='The count threshold to generate an event',
        ge=0,
        le=UINT32_MAX,
    ),
]

ConditionChannel = Annotated[
    str,
    Field(
        title='Channel',
        description='The channel or global timer attached to the condition',
        min_length=1,
    ),
]

ConditionValue = Annotated[
    bool,
    Field(
        title='Value',
        description='The value of the condition channel if the condition is met',
    ),
]

StateMachineName = Annotated[
    str,
    Field(
        title='State Machine Name',
        description='The name of the state machine',
        min_length=1,
    ),
]

OutputActionName = Annotated[
    str,
    Field(
        title='Output Action Name',
        description='The name of the output action',
        min_length=1,
    ),
]

OutputActionValue = Annotated[
    int,
    Field(
        title='Output Action Value',
        description='The integer value of the output action',
        ge=0,
        le=255,
    ),
]

StateName = Annotated[
    str,
    Field(
        title='State Name',
        description='The name of the state',
        min_length=1,
        pattern=re.compile(r'^(?!>)(?!exit$)(?!back$).+$'),
    ),
    WrapValidator(_validate_state_name),
]
"""A valid state machine state name."""

Event = Annotated[
    str,
    Field(
        title='Event',
        description='A state machine event',
        min_length=1,
    ),
]

Operator = Annotated[
    str,
    Field(
        title='State Machine Operator',
        description='A state machine operator',
        pattern=re.compile(r'^>.+$'),
        min_length=2,
        examples=['>exit', '>back'],
    ),
    WrapValidator(_validate_operator),
]


class Actions(ValidatedDict[OutputActionName, OutputActionValue], title='Actions'):
    """A collection of actions."""

    if TYPE_CHECKING:

        def __init__(
            self, root: dict[OutputActionName, OutputActionValue] | None = ...
        ) -> None: ...


class Transitions(
    ValidatedDict[Event, StateName | Operator], title='State Transitions'
):
    """A collection of state transitions."""

    if TYPE_CHECKING:

        def __init__(
            self, root: dict[Event, StateName | Operator] | None = ...
        ) -> None: ...


class State(BaseModel, title='State'):
    """A state in the state machine."""

    model_config = ConfigDict(validate_assignment=True, extra='forbid')

    timer: StateTimer = StateTimer()
    transitions: Transitions = Transitions()
    actions: Actions = Actions()
    comment: StateComment | None = None

    def __repr__(self) -> str:
        dump = self.model_dump(exclude_defaults=True)
        values = ', '.join([f'{k}={v}' for k, v in dump.items()])
        return f'{self.__class__.__name__}({values})'


class GlobalTimer(BaseModel, title='Global Timer'):
    """A global timer in the state machine."""

    model_config = ConfigDict(validate_assignment=True, extra='forbid')

    duration: GlobalTimerDuration
    onset_delay: GlobalTimerOnsetDelay = 0.0
    channel: GlobalTimerChannel | None = None
    value_on: GlobalTimerChannelValue = 0
    value_off: GlobalTimerChannelValue = 0
    send_events: GlobalTimerSendEvents = True
    loop: GlobalTimerLoop = 0
    loop_interval: GlobalTimerLoopInterval = 0.0
    onset_trigger: GlobalTimerOnsetTrigger = 0

    def __repr__(self) -> str:
        dump = self.model_dump(exclude_defaults=True)
        values = ', '.join([f'{k}={v}' for k, v in dump.items()])
        return f'{self.__class__.__name__}({values})'


class GlobalCounter(BaseModel, title='Global Counter'):
    """A global counter in the state machine."""

    model_config = ConfigDict(validate_assignment=True, extra='forbid')

    event: Event
    threshold: GlobalCounterThreshold

    def __repr__(self) -> str:
        dump = self.model_dump(exclude_defaults=True)
        values = ', '.join([f'{k}={v}' for k, v in dump.items()])
        return f'{self.__class__.__name__}({values})'


class Condition(BaseModel, title='Condition'):
    """A condition in the state machine."""

    model_config = ConfigDict(validate_assignment=True, extra='forbid')

    channel: ConditionChannel
    value: ConditionValue

    def __repr__(self) -> str:
        dump = self.model_dump(exclude_defaults=True)
        values = ', '.join([f'{k}={v}' for k, v in dump.items()])
        return f'{self.__class__.__name__}({values})'


class States(ValidatedDict[StateName, State], title='States'):
    """A collection of states."""

    model_config = ConfigDict(json_schema_extra={'additionalProperties': False})

    @property
    def transition_targets(self) -> set[str]:
        """A set of all transition targets."""
        return {t for s in self.values() for t in s.transitions.values()}


Index = Annotated[
    int,
    Field(
        title='Index',
        ge=0,
        json_schema_extra={'pattern': r'^\d+$'},
    ),
]


class GlobalTimers(ValidatedDict[Index, GlobalTimer], title='Global Timers'):
    """A collection of global timers."""

    model_config = ConfigDict(json_schema_extra={'additionalProperties': False})


class GlobalCounters(ValidatedDict[Index, GlobalCounter], title='Global Counters'):
    """A collection of global counters."""

    model_config = ConfigDict(json_schema_extra={'additionalProperties': False})


class Conditions(ValidatedDict[Index, Condition], title='Conditions'):
    """A collection of conditions."""

    model_config = ConfigDict(json_schema_extra={'additionalProperties': False})


class StateMachine(BaseModel, title='State Machine'):
    """Definition of a Bpod finite-state machine."""

    model_config = ConfigDict(
        validate_assignment=True,
        extra='forbid',
        json_schema_extra={
            '$id': 'https://raw.githubusercontent.com/int-brain-lab/bpod-core/main/.schema/statemachine.json',
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
        },
    )

    name: StateMachineName = 'State Machine'
    """The name of the state machine."""

    states: States = States()
    """A dictionary of states."""

    global_timers: GlobalTimers = GlobalTimers()
    """A dictionary of global timers."""

    global_counters: GlobalCounters = GlobalCounters()
    """A dictionary of global counters."""

    conditions: Conditions = Conditions()
    """A dictionary of conditions."""

    _validation_cache: ClassVar[FIFOCache[bytes, _CheckData]] = FIFOCache(maxsize=1024)
    """Cache holding hashes of successfully validated state machine instances."""

    def __repr__(self) -> str:
        fields = [f for f in StateMachine.model_fields if f != 'name']
        counts = [len(getattr(self, f)) for f in fields]
        string = ', '.join(f'{fields[i]}: {n}' for i, n in enumerate(counts))
        if self.name != StateMachine.model_fields['name'].default:
            return f"{self.__class__.__name__}(name='{self.name}', {string})"
        return f'{self.__class__.__name__}({string})'

    @validate_call()
    def add_state(
        self,
        name: StateName,
        timer: StateTimer = 0.0,
        transitions: dict[Event, StateName | Operator] | None = None,
        actions: dict[OutputActionName, OutputActionValue] | None = None,
        comment: StateComment | None = None,
    ) -> None:
        """
        Add a new state to the state machine.

        Parameters
        ----------
        name : str
            The name of the state to be added.
        timer : float, default: 0.0
            The duration of the state's timer.
        transitions : dict, optional
            An optional dictionary mapping conditions to target states for transitions.
        actions : dict, optional
            An optional dictionary of actions to be executed on entering the state.
        comment : str, optional
            An optional comment describing the state.

        Raises
        ------
        ValueError
            If a state with the given name already exists in the state machine.
        """
        if name in self.states:
            raise ValueError(f"A state named '{name}' is already registered")

        self.states[name] = State.model_construct(
            timer=timer,
            transitions=Transitions(transitions or {}),
            actions=Actions(actions or {}),
            comment=comment,
        )

    @validate_call()
    def set_global_timer(
        self,
        index: Index,
        duration: GlobalTimerDuration,
        *,
        onset_delay: GlobalTimerOnsetDelay = 0.0,
        channel: GlobalTimerChannel | None = None,
        value_on: GlobalTimerChannelValue = 0,
        value_off: GlobalTimerChannelValue = 0,
        send_events: GlobalTimerSendEvents = True,
        loop: GlobalTimerLoop = 0,
        loop_interval: GlobalTimerLoopInterval = 0.0,
        onset_trigger: GlobalTimerOnsetTrigger = 0,
    ) -> None:
        """
        Configure a global timer with the specified parameters.

        Parameters
        ----------
        index : int
            The index of the global timer to configure. Zero-based.
        duration : float
            The duration of the global timer.
        onset_delay : float, default: 0.0
            The onset delay of the global timer.
        channel : str, optional
            The channel affected by the global timer.
        value_on : int, default: 0
            The value to set the channel to when the timer is active.
        value_off : int, default: 0
            The value to set the channel to when the timer is inactive.
        send_events : bool, default: True
            Whether the global timer sends events.
        loop : int, default: 0
            The number of times the timer should loop
        loop_interval : float, default: 0.0
            The interval between loops.
        onset_trigger : int, default: 0
            An integer whose bits indicate other global timers to trigger.

        Returns
        -------
        None
        """
        self.global_timers[index] = GlobalTimer.model_construct(
            duration=duration,
            onset_delay=onset_delay,
            channel=channel,
            value_on=value_on,
            value_off=value_off,
            send_events=send_events,
            loop=loop,
            loop_interval=loop_interval,
            onset_trigger=onset_trigger,
        )

    @validate_call()
    def set_global_counter(
        self,
        index: Index,
        event: Event,
        threshold: GlobalCounterThreshold,
    ) -> None:
        """
        Configure a global counter with the specified parameters.

        Parameters
        ----------
        index : int
            The index of the global counter. Zero-based.
        event : str
            The name of the event to count.
        threshold : int
            The count threshold to generate an event

        Returns
        -------
        None
        """
        self.global_counters[index] = GlobalCounter.model_construct(
            event=event,
            threshold=threshold,
        )

    @validate_call()
    def set_condition(
        self,
        index: Index,
        channel: ConditionChannel,
        value: ConditionValue,
    ) -> None:
        """Configure a condition with the specified parameters.

        Parameters
        ----------
        index : int
            The index of the condition. Zero-based.
        channel : str
            The channel or global timer attached to the condition.
        value: bool
            The value of the condition channel if the condition is met

        Returns
        -------
        None
        """
        self.conditions[index] = Condition.model_construct(
            channel=channel,
            value=value,
        )

    @validate_call()
    def to_digraph(
        self,
        color_stroke: ColorType = 'black',
        color_fill: ColorType = 'white',
        color_highlight: ColorType = 'lightblue',
        color_back: ColorType = 'black',
    ) -> Digraph:
        """
        Return a graphviz Digraph instance representing the state machine.

        Parameters
        ----------
        color_stroke : ColorType, default: 'black'
            Color for fonts, node outlines, and edges.
        color_fill : ColorType, default: 'white'
            Background color of state nodes.
        color_highlight : ColorType, default: 'lightblue'
            Background color of state header and comment rows.
        color_back : ColorType, default: 'black'
            Color for edges resulting from ``>back`` transitions.

        Returns
        -------
        Digraph
            A graphviz Digraph instance representing the state machine.

        Notes
        -----
        This method depends on the `Graphviz system libraries
        <https://graphviz.readthedocs.io/en/stable/manual.html#installation>`_ to be
        installed.
        """
        # Initialize the Digraph with the name of the state machine
        dot = Digraph(self.name)

        # Return an empty Digraph if there are no states
        if len(self.states) == 0:
            return dot

        # get string representations of color parameters
        c_stroke: str = Color(color_stroke).as_hex(format='long')
        c_background = Color(color_fill).as_hex(format='long')
        c_highlight = Color(color_highlight).as_hex(format='long')
        c_back = Color(color_back).as_hex(format='long')

        # Set default graph attributes and styling
        fontname = 'Helvetica,Arial,sans-serif'
        dot.attr(overlap='false', splines='true', rankdir='LR')
        dot.attr(
            'graph',
            fontname=fontname,
            fontsize='11',
            fontcolor=c_stroke,
            bgcolor='transparent',
            outputorder='edgesfirst',
        )
        dot.attr(
            'node', fontname=fontname, fontsize='11', fontcolor=c_stroke, color=c_stroke
        )
        dot.attr(
            'edge', fontname=fontname, fontsize='10', fontcolor=c_stroke, color=c_stroke
        )

        # Add start node and edge to first state
        dot.node(
            name='',
            shape='circle',
            style='filled',
            color=c_stroke,
            width='0.25',
        )
        dot.edge('', next(iter(self.states.keys())))
        with dot.subgraph() as s:
            s.attr(rank='source')
            s.node('')

        # Add exit node if any states transition to it
        targets = [t for s in self.states.values() for t in s.transitions.values()]
        if any(t in ('exit', '>exit') for t in targets):
            dot.node(
                name='exit',
                label='',
                shape='doublecircle',
                style='filled',
                color=c_stroke,
                width='0.125',
            )
            with dot.subgraph() as s:
                s.attr(rank='sink')
                s.node('exit')

        back_ops = []  # Store back operations for later processing

        # Add nodes and edges for each state
        for state_name, state in self.states.items():
            # Create table cells for comment if present
            comment = (
                f'<TR><TD ALIGN="LEFT" COLSPAN="2" BGCOLOR="{c_highlight}">'
                f'<I>{state.comment}</I></TD></TR>'
                if state.comment is not None and len(state.comment) > 0
                else ''
            )

            # Create table rows for output actions
            actions = ''.join(
                f'<TR><TD ALIGN="LEFT">{k}  </TD><TD ALIGN="RIGHT">{v}</TD></TR>'
                for k, v in state.actions.items()
            )

            # Create HTML table label with state info
            label = (
                '<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" ALIGN="LEFT" '
                f'BGCOLOR="{c_background}"><TR><TD BGCOLOR="{c_highlight}" COLSPAN="2">'
                '<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="0" CELLPADDING="0">'
                f'<TR><TD ALIGN="LEFT"><B>{state_name} </B></TD>'
                f'<TD ALIGN="RIGHT"><B> </B>{state.timer:g}&#8239;s</TD></TR>'
                f'</TABLE></TD></TR>{comment}{actions}</TABLE>>'
            )

            # Add state node
            dot.node(state_name, label, shape='plain')

            # Add edges for state transitions
            # Use a subgraph to keep edges from the same state on the same rank
            with dot.subgraph() as s:
                s.attr(rank='same')
                for edge_label, target in state.transitions.items():
                    if target in ('exit', '>exit'):
                        dot.edge(state_name, 'exit', edge_label)
                    elif target == '>back':
                        back_ops.append((state_name, edge_label))
                    else:
                        dot.edge(state_name, target, edge_label)
                        s.node(target)

        # Add edges for back transitions
        # We label these with dashed lines to distinguish them from regular edges
        for source, label in back_ops:
            for target, state in self.states.items():
                if source in state.transitions.values():
                    dot.edge(
                        source,
                        target,
                        label,
                        color=c_back,
                        fontcolor=c_back,
                        style='dashed',
                    )

        return dot

    def to_dict(self, *, exclude_defaults: bool = True) -> dict:
        """Return the state machine as a dictionary.

        Parameters
        ----------
        exclude_defaults: bool, default: True
            Whether to exclude fields that are set to their default values.

        Returns
        -------
        dict
            A dictionary representation of the state machine.
        """
        return self.model_dump(exclude_defaults=exclude_defaults)

    def to_json(
        self, indent: int | None = None, *, exclude_defaults: bool = True
    ) -> str:
        """Return the state machine as a JSON string.

        Parameters
        ----------
        indent : int, optional
            If `indent` is a non-negative integer, then JSON array elements and object
            members will be pretty-printed with that indent level. An indent level of
            0 will only insert newlines. None is the most compact representation.
        exclude_defaults: bool, default: True
            Whether to exclude fields that are set to their default values.

        Returns
        -------
        str
            A JSON string representation of the state machine.
        """
        return self.model_dump_json(indent=indent, exclude_defaults=exclude_defaults)

    def to_yaml(self, *, exclude_defaults: bool = True) -> str:
        """Return the state machine as a YAML string.

        Parameters
        ----------
        exclude_defaults: bool, default: True
            Whether to exclude fields that are set to their default values.

        Returns
        -------
        str
            A YAML string representation of the state machine.
        """
        return msgspec.yaml.encode(
            self.to_dict(exclude_defaults=exclude_defaults)
        ).decode()

    @validate_call()
    def to_file(
        self,
        filename: PathLike | str,
        *,
        overwrite: bool = False,
        create_directory: bool = False,
    ) -> None:
        """Write the state machine to a file.

        Depending on the file extension, different outputs are produced:

        - .json: writes a JSON representation of the state machine,
        - .yaml, .yml: writes a YAML representation of the state machine,
        - .pdf, .svg, .png: renders a state diagram and stores it to the specified file.

        Parameters
        ----------
        filename : PathLike or str
            Destination path. The file extension determines the output type.
        overwrite : bool, default: False
            If False and the file already exists, a FileExistsError is raised.
            If True, existing files will be overwritten.
        create_directory : bool, default: False
            If True, the parent directory of the destination path will be created if it
            doesn't exist.

        Raises
        ------
        FileExistsError
            If the destination file already exists and overwrite is False.
        FileNotFoundError
            If the parent directory of the destination path does not exist and
            create_directory is False.
        ValueError
            If the file extension is not one of: .json, .pdf, .svg, .png.

        Notes
        -----
        Rendering diagrams depends on the `Graphviz system libraries
        <https://graphviz.readthedocs.io/en/stable/manual.html#installation>`_ to be
        installed.
        """
        # Handle file path
        filepath = Path(filename).resolve()
        if filepath.exists() and not overwrite:
            raise FileExistsError(f"File '{filepath}' already exists")
        if not filepath.parent.exists():
            if not create_directory:
                raise FileNotFoundError(f"Directory '{filepath.parent}' does not exist")
            filepath.parent.mkdir(parents=True, exist_ok=True)
        suffix = filepath.suffix.lower()

        # JSON output
        if suffix == '.json':
            filepath.write_text(self.to_json(indent=2), encoding='utf-8')

        # YAML output
        elif suffix in ('.yaml', '.yml'):
            filepath.write_text(self.to_yaml(), encoding='utf-8')

        # Rendering via Graphviz
        elif suffix in ('.pdf', '.svg', '.png'):
            common_opts = {
                'outfile': filepath,
                'cleanup': True,
                'quiet': True,
            }
            if suffix == '.svg':
                render_format = 'svg'
            elif suffix == '.png':
                render_format = 'png'
            else:
                render_format = 'pdf'
            self.to_digraph().render(**common_opts, format=render_format)

        # Handle unsupported file extension
        else:
            raise ValueError(f'Unsupported file extension: {suffix.upper().strip(".")}')

    @classmethod
    def from_dict(cls, data: dict) -> 'StateMachine':
        """Create a StateMachine instance from a dictionary.

        Parameters
        ----------
        data : dict
            A dictionary representation of a state machine.

        Returns
        -------
        StateMachine
            A StateMachine instance created from the provided dictionary.

        Notes
        -----
        This is a thin wrapper around :meth:`~pydantic.BaseModel.model_validate`
        """
        return StateMachine.model_validate(data)

    @classmethod
    def from_json(cls, json_str: str | bytes) -> 'StateMachine':
        """Create a StateMachine instance from a JSON string.

        Parameters
        ----------
        json_str : str or bytes
            A JSON string representation of a state machine.

        Returns
        -------
        StateMachine
            A StateMachine instance created from the provided JSON string.

        Raises
        ------
        ValueError
            If the JSON string is not valid or does not represent a
            :class:`StateMachine`.

        Notes
        -----
        This is a thin wrapper around :meth:`~pydantic.BaseModel.model_validate_json`
        """
        try:
            return cls.model_validate_json(json_str)
        except ValidationError as e:
            raise ValueError('Invalid JSON string') from e

    @classmethod
    def from_yaml(cls, yaml_str: str | bytes) -> 'StateMachine':
        """Create a StateMachine instance from a YAML string.

        Parameters
        ----------
        yaml_str : str or bytes
            A YAML string representation of a state machine.

        Returns
        -------
        StateMachine
            A StateMachine instance created from the provided YAML string.

        Raises
        ------
        ValueError
            If the YAML string is not valid or does not represent a
            :class:`StateMachine`.
        """
        try:
            return cls.model_validate(yaml.safe_load(yaml_str))
        except (ValidationError, yaml.YAMLError) as e:
            raise ValueError('Invalid YAML string') from e

    @classmethod
    def from_file(cls, filename: PathLike | str) -> 'StateMachine':
        """Create a StateMachine instance from a JSON or YAML file.

        Parameters
        ----------
        filename : PathLike or str
            The path to the file containing the state machine.

        Returns
        -------
        StateMachine
            A StateMachine instance created from the contents of the file.

        Raises
        ------
        FileNotFoundError
            If the file does not exist.
        NotImplementedError
            If the file extension is not .json, .yaml or .yml.
        ValueError
            If the file content is not valid JSON or YAML.
        """
        # Handle file path
        filename = Path(filename).resolve()
        if not filename.exists():
            raise FileNotFoundError(f"File '{filename}' does not exist")
        if filename.suffix.lower() not in ('.json', '.yaml', '.yml'):
            raise NotImplementedError(
                f'Unsupported file extension: {filename.suffix.upper()}'
            )

        # Load data and return StateMachine instance
        data = filename.read_bytes()
        if filename.suffix.lower() == '.json':
            return cls.from_json(data)
        return cls.from_yaml(data)

    def _hash(self) -> bytes:
        try:
            # when serializing to JSON, we first try to use Pydantic's private API,
            # which avoids an unnecessary string conversion
            return _xxh3_64(self.__pydantic_serializer__.to_json(self)).digest()
        except (AttributeError, TypeError):
            # if that fails, we fall back to the public API
            return _xxh3_64(self.model_dump_json().encode()).digest()

    @property
    def hash(self) -> bytes:
        """Hash of the state machine."""
        return self._hash()

    @property
    def valid(self) -> bool:
        """Returns True if the state machine is valid, False otherwise."""
        try:
            self.check()
        except ValueError:
            return False
        else:
            return True

    def check(self) -> None:
        """
        Check validity of the state machine.

        Raises
        ------
        ValueError
            If the state machine is invalid.

        Notes
        -----
        This method only checks the general structure of the state machine, not its
        compatibility with the hardware. For the latter, see
        :meth:`~bpod_core.bpod.Bpod.validate_state_machine`.
        """
        self._check(known_hash=self._hash())

    def _check(self, *, known_hash: bytes) -> _CheckData:
        # Shortcut if we already know that the state machine is valid
        if known_hash in self._validation_cache:
            return self._validation_cache[known_hash]

        # Check for empty state machine
        if len(self.states) == 0:
            raise ValueError('No states defined')

        # Check for unreachable states
        initial_state_name = next(iter(self.states))
        transition_targets = self.states.transition_targets
        all_state_names = list(self.states)
        unreachable_states = set(all_state_names).difference(
            transition_targets | {initial_state_name}
        )
        if unreachable_states:
            if len(unreachable_states) == 1:
                raise ValueError(f'State "{unreachable_states.pop()}" is unreachable')
            unreachable_states_list = list(unreachable_states)
            unreachable_states_string = (
                ', '.join([f'"{s}"' for s in unreachable_states_list[:-1]])
                + f' and "{unreachable_states_list[-1]}"'
            )
            raise ValueError(f'States {unreachable_states_string} are unreachable')

        # Check transitions for invalid target states
        for state_name, state in self.states.items():
            for condition_name, target in state.transitions.items():
                if target not in all_state_names and not target.startswith('>'):
                    raise ValueError(
                        f"Invalid target state '{target}' for transition condition"
                        f"'{condition_name}' in state '{state_name}'"
                        + suggest_similar(target, set(all_state_names) - {state_name})
                    )

        # add state machine's hash to cache, along with some data
        data = _CheckData(
            all_state_names=all_state_names,
            transition_targets=transition_targets,
        )
        self._validation_cache[known_hash] = data

        # returning expensive transition_targets for further use by caller
        return data
