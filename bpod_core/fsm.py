"""Module defining classes and types for creating and managing state machines."""

import re
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import msgspec
import numpy as np
import yaml
from graphviz import Digraph  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, validate_call

from bpod_core.misc import ValidatedDict


def enc_hook(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(exclude_defaults=True)
    else:
        raise NotImplementedError(f'Objects of type {type(obj)} are not supported')


def dec_hook(obj_type: type, obj: dict) -> Any:
    if issubclass(obj_type, BaseModel):
        return obj_type.model_validate(obj)
    else:
        raise NotImplementedError(f'Objects of type {type} are not supported')


StateTimer = Annotated[
    float,
    Field(
        title='State Timer',
        description="The state's timer in seconds",
        default=0.0,
        allow_inf_nan=False,
        ge=0.0,
    ),
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
    ),
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
]

GlobalTimerChannel = Annotated[
    str,
    msgspec.Meta(
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
        description='Whether the global timer is looping or not',
        default=0,
        ge=0,
        le=255,
    ),
]

GlobalTimerLoopInterval = Annotated[
    float,
    Field(
        title='Loop Interval',
        description='The interval in seconds that the global timer is looping',
        default=0.0,
        ge=0.0,
        allow_inf_nan=False,
    ),
]

GlobalTimerOnsetTrigger = Annotated[
    int,
    Field(
        title='Onset Trigger',
        description='An integer whose bits indicate other global timers to trigger',
        default=0,
        ge=0,
    ),
]

GlobalCounterThreshold = Annotated[
    int,
    Field(
        title='Threshold',
        description='The count threshold to generate an event',
        ge=0,
        le=np.iinfo(np.uint32).max,
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
        pattern=re.compile(r'^(?!>)(?!exit$).+$'),
    ),
]

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
        title='Operator',
        description='A state machine operator',
        pattern=re.compile(r'^(exit)|(>.+)$'),
    ),
]


class Actions(ValidatedDict[OutputActionName, OutputActionValue], title='Actions'):
    """A collection of actions."""

    if TYPE_CHECKING:

        def __init__(
            self, root: Mapping[OutputActionName, OutputActionValue] | None = ...
        ) -> None: ...


class Transitions(
    ValidatedDict[Event, StateName | Operator], title='State Transitions'
):
    """A collection of state transitions."""

    if TYPE_CHECKING:

        def __init__(
            self, root: Mapping[Event, StateName | Operator] | None = ...
        ) -> None: ...


class State(BaseModel, validate_assignment=True, title='State'):
    """A state in the state machine."""

    timer: StateTimer = StateTimer()
    transitions: Transitions = Transitions()
    actions: Actions = Actions()
    comment: StateComment | None = None

    def __repr__(self) -> str:
        dump = self.model_dump(exclude_defaults=True)
        values = ', '.join([f'{k}={v}' for k, v in dump.items()])
        return f'{self.__class__.__name__}({values})'


class GlobalTimer(BaseModel, validate_assignment=True, title='Global Timer'):
    """A global timer in the state machine."""

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


class GlobalCounter(BaseModel, validate_assignment=True, title='Global Counter'):
    """A global counter in the state machine."""

    event: Event
    threshold: GlobalCounterThreshold

    def __repr__(self) -> str:
        dump = self.model_dump(exclude_defaults=True)
        values = ', '.join([f'{k}={v}' for k, v in dump.items()])
        return f'{self.__class__.__name__}({values})'


class Condition(BaseModel, validate_assignment=True, title='Condition'):
    """A condition in the state machine."""

    channel: ConditionChannel
    value: ConditionValue

    def __repr__(self) -> str:
        dump = self.model_dump(exclude_defaults=True)
        values = ', '.join([f'{k}={v}' for k, v in dump.items()])
        return f'{self.__class__.__name__}({values})'


class States(ValidatedDict[StateName, State], title='States'):
    """A collection of states."""


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


class GlobalCounters(ValidatedDict[Index, GlobalCounter], title='Global Counters'):
    """A collection of global counters."""


class Conditions(ValidatedDict[Index, Condition], title='Conditions'):
    """A collection of conditions."""


class StateMachine(BaseModel, validate_assignment=True, title='State Machine'):
    """Represents a state machine with a collection of states."""

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

    def __repr__(self) -> str:
        fields = [f for f in StateMachine.model_fields if f != 'name']
        counts = [len(getattr(self, f)) for f in fields]
        string = ', '.join(f'{fields[i]}: {n}' for i, n in enumerate(counts))
        if self.name != StateMachine.model_fields['name'].default:
            return f"{self.__class__.__name__}(name='{self.name}', {string})"
        else:
            return f'{self.__class__.__name__}({string})'

    @validate_call
    def add_state(
        self,
        name: StateName,
        timer: StateTimer = 0.0,
        transitions: Mapping[Event, StateName | Operator] | None = None,
        actions: Mapping[OutputActionName, OutputActionValue] | None = None,
        comment: StateComment | None = None,
    ) -> None:
        """
        Adds a new state to the state machine.

        Parameters
        ----------
        name : str
            The name of the state to be added.
        timer : float, optional
            The duration of the state's timer in seconds. Default to 0.
        transitions : Mapping, optional
            A dictionary mapping conditions to target states for transitions.
            Defaults to an empty dictionary.
        actions : Mapping, optional
            A dictionary of actions to be executed on entering the state.
            Defaults to an empty dictionary.
        comment : str, optional
            An optional comment describing the state.

        Raises
        ------
        ValueError
            If a state with the given name already exists in the state machine.
        """
        if name in self.states:
            raise ValueError(f"A state named '{name}' is already registered")

        self.states[name] = State(
            timer=timer,
            transitions=Transitions(transitions or {}),
            actions=Actions(actions or {}),
            comment=comment,
        )

    @validate_call
    def set_global_timer(  # noqa: PLR0913
        self,
        index: Index,
        duration: GlobalTimerDuration,
        onset_delay: GlobalTimerOnsetDelay = 0.0,
        channel: GlobalTimerChannel | None = None,
        value_on: GlobalTimerChannelValue = 0,
        value_off: GlobalTimerChannelValue = 0,
        send_events: GlobalTimerSendEvents = True,
        loop: GlobalTimerLoop = 0,
        loop_interval: GlobalTimerLoopInterval = 0,
        onset_trigger: GlobalTimerOnsetTrigger = 0,
    ) -> None:
        """
        Configure a global timer with the specified parameters.

        Parameters
        ----------
        index : int
            The index of the global timer to configure.
        duration : float
            The duration of the global timer in seconds.
        onset_delay : float, optional
            The onset delay of the global timer in seconds. Default is 0.0.
        channel : str, optional
            The channel affected by the global timer. Default is None.
        value_on : int, optional
            The value to set the channel to when the timer is active. Default is 0.
        value_off : int, optional
            The value to set the channel to when the timer is inactive. Default is 0.
        send_events : bool, optional
            Whether the global timer sends events. Default is True.
        loop : int, optional
            The number of times the timer should loop. Default is 0.
        loop_interval : float, optional
            The interval in seconds between loops. Default is 0.
        onset_trigger : int, optional
            An integer whose bits indicate other global timers to trigger.

        Returns
        -------
        None
        """
        self.global_timers[index] = GlobalTimer(
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

    @validate_call
    def set_global_counter(
        self,
        index: Index,
        event: Event,
        threshold: GlobalCounterThreshold,
    ) -> None:
        """
        Configure a global timer with the specified parameters.

        Parameters
        ----------
        index : int
            The index of the global counter.
        event : str
            The name of the event to count.
        threshold : int
            The count threshold to generate an event

        Returns
        -------
        None
        """
        self.global_counters[index] = GlobalCounter(
            event=event,
            threshold=threshold,
        )

    @validate_call
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
            The index of the condition.
        channel : str
            The channel or global timer attached to the condition.
        value: bool
            The value of the condition channel if the condition is met

        Returns
        -------
        None
        """
        self.conditions[index] = Condition(
            channel=channel,
            value=value,
        )

    def to_digraph(self) -> Digraph:
        """
        Returns a graphviz Digraph instance representing the state machine.

        The Digraph includes:

        - A point-shaped node representing the start of the state machine,
        - An optional 'exit' node if any state transitions to 'exit',
        - Record-like nodes for each state displaying state name, timer, comment and
          output actions, and
        - Edges representing state transitions based on conditions.

        Returns
        -------
        Digraph
            A graphviz Digraph instance representing the state machine.

        Notes
        -----
        This method depends on the Graphviz system libraries to be installed.
        See https://graphviz.readthedocs.io/en/stable/manual.html#installation
        """
        # Initialize the Digraph with the name of the state machine
        dot = Digraph(self.name)

        # Return an empty Digraph if there are no states
        if len(self.states) == 0:
            return dot

        # Set default graph attributes and styling
        fontname = 'Helvetica,Arial,sans-serif'
        dot.attr(overlap='false', splines='true', rankdir='LR')
        dot.attr('graph', fontname=fontname, fontsize='11', bgcolor='transparent')
        dot.attr('node', fontname=fontname, fontsize='11', bgcolor='white')
        dot.attr('edge', fontname=fontname, fontsize='10')

        # Add start node and edge to first state
        dot.node(
            name='', shape='circle', style='filled', fillcolor='black', width='0.25'
        )
        dot.edge('', next(iter(self.states.keys())))
        with dot.subgraph() as s:
            s.attr(rank='source')
            s.node('')

        # Add exit node if any states transition to it
        targets = [t for s in self.states.values() for t in s.transitions.values()]
        if 'exit' in targets or '>exit' in targets:
            dot.node(
                name='exit',
                label='',
                shape='doublecircle',
                style='filled',
                fillcolor='black',
                width='0.125',
                rank='sink',
            )
            with dot.subgraph() as s:
                s.attr(rank='sink')
                s.node('exit')

        back_ops = []  # Store back operations for later processing

        # Add nodes and edges for each state
        for state_name, state in self.states.items():
            # Create table cells for comment if present
            comment = (
                f'<TR><TD ALIGN="LEFT" COLSPAN="2" BGCOLOR="LIGHTBLUE">'
                f'<I>{state.comment}</I></TD></TR>'
                if state.comment is not None and len(state.comment) > 0
                else ''
            )

            # Create table rows for output actions
            actions = ''.join(
                f'<TR><TD ALIGN="LEFT">{k}</TD><TD ALIGN="RIGHT">{v}</TD></TR>'
                for k, v in state.actions.items()
            )

            # Create HTML table label with state info
            label = (
                '<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" ALIGN="LEFT" '
                'BGCOLOR="WHITE"><TR><TD BGCOLOR="LIGHTBLUE" ALIGN="LEFT">'
                f'<B>{state_name}  </B></TD><TD BGCOLOR="LIGHTBLUE" ALIGN="RIGHT">'
                f'{state.timer:g} s</TD></TR>{comment}{actions}</TABLE>>'
            )

            # Add state node
            dot.node(state_name, label, shape='plain')

            # Add edges for state transitions
            # Use a subgraph to keep edges from the same state on the same rank
            with dot.subgraph() as s:
                s.attr(rank='same')
                for label, target in state.transitions.items():
                    if 'exit' in target:
                        dot.edge(state_name, 'exit', label)
                    elif target == '>back':
                        back_ops.append((state_name, label))
                    else:
                        dot.edge(state_name, target, label)
                        s.node(target)

        # Add edges for back transitions
        # We label these in red to distinguish them from regular edges
        for source, label in back_ops:
            for target, state in self.states.items():
                if source in state.transitions.values():
                    dot.edge(source, target, label, color='red', fontcolor='red')

        return dot

    def to_dict(self, exclude_defaults: bool = True) -> dict:
        """Returns the state machine as a dictionary.

        Parameters
        ----------
        exclude_defaults: bool, optional
            Whether to exclude fields that are set to their default values.
            Defaults to True.

        Returns
        -------
        dict
            A dictionary representation of the state machine.
        """
        return self.model_dump(exclude_defaults=exclude_defaults)

    def to_json(self, indent: None | int = None, exclude_defaults: bool = True) -> str:
        """Returns the state machine as a JSON string.

        Parameters
        ----------
        indent : int or None, optional
            If `indent` is a non-negative integer, then JSON array elements and object
            members will be pretty-printed with that indent level. An indent level of
            0 will only insert newlines. None is the most compact representation.
        exclude_defaults: bool, optional
            Whether to exclude fields that are set to their default values.
            Defaults to True.

        Returns
        -------
        str
            A dictionary representation of the state machine.
        """
        return self.model_dump_json(indent=indent, exclude_defaults=exclude_defaults)

    def to_yaml(self, indent: None | int = None, exclude_defaults: bool = True) -> str:
        """Returns the state machine as a YAML string.

        Parameters
        ----------
        exclude_defaults: bool, optional
            Whether to exclude fields that are set to their default values.
            Defaults to True.

        Returns
        -------
        str
            A dictionary representation of the state machine.
        """
        dictionary = self.model_dump(exclude_defaults=exclude_defaults)
        return yaml.dump(dictionary, sort_keys=False)

    @validate_call
    def to_file(
        self,
        filename: PathLike | str,
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
        filename : os.PathLike or str
            Destination path. The file extension determines the output type.
        overwrite : bool, optional
            If False (default) and the file already exists, a FileExistsError is
            raised. If True, existing files will be overwritten.
        create_directory : bool, optional
            If True, the parent directory of the destination path will be created if it
            doesn't exist. Default is False.

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
        Rendering diagrams depends on the Graphviz system libraries to be installed.
        See https://graphviz.readthedocs.io/en/stable/manual.html#installation
        """
        # Handle file path
        filename = Path(filename).resolve()
        if filename.exists() and not overwrite:
            raise FileExistsError(f"File '{filename}' already exists")
        if not filename.parent.exists():
            if not create_directory:
                raise FileNotFoundError(f"Directory '{filename.parent}' does not exist")
            else:
                filename.parent.mkdir(parents=True, exist_ok=True)
        suffix = filename.suffix.lower()

        # JSON output
        if suffix == '.json':
            filename.write_text(self.to_json(indent=2), encoding='utf-8')

        # YAML output
        elif suffix in ('.yaml', '.yml'):
            filename.write_text(self.to_yaml(indent=2), encoding='utf-8')

        # Rendering via Graphviz
        elif suffix in ('.pdf', '.svg', '.png'):
            common_opts = {
                'outfile': filename,
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
        """Creates a StateMachine instance from a dictionary.

        Parameters
        ----------
        data : dict
            A dictionary representation of a state machine.

        Returns
        -------
        StateMachine
            A StateMachine instance created from the provided dictionary.
        """
        return msgspec.convert(data, type=StateMachine, dec_hook=dec_hook)

    @classmethod
    def from_json(cls, json_str: str | bytes) -> 'StateMachine':
        """Creates a StateMachine instance from a JSON string.

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
        ValidationError
            If the JSON string is not valid.

        Notes
        -----
        This is a thin wrapper around :meth:`~BaseModel.model_validate_json`
        """
        return StateMachine.model_validate_json(json_str)

    @classmethod
    def from_yaml(cls, yaml_str: str | bytes) -> 'StateMachine':
        """Creates a StateMachine instance from a YAML string.

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
        ValidationError
            If the YAML string is not valid.
        """
        dictionary = yaml.safe_load(yaml_str)
        return StateMachine.model_validate(dictionary)

    @classmethod
    def from_file(cls, filename: PathLike | str) -> 'StateMachine':
        """Creates a StateMachine instance from a JSON or YAML file.

        Parameters
        ----------
        filename : os.PathLike or str
            The path to the file containing the state machine.

        Returns
        -------
        StateMachine
            A StateMachine instance created from the contens of the file.

        Raises
        ------
        FileNotFoundError
            If the file does not exist.
        ValueError
            If the file extension is not .json, .yaml or .yml.
        """
        # Handle file path
        filename = Path(filename).resolve()
        if not filename.exists():
            raise FileNotFoundError(f"File '{filename}' does not exist")
        if filename.suffix.lower() not in ('.json', '.yaml', '.yml'):
            raise ValueError(f'Unsupported file extension: {filename.suffix.upper()}')

        # Load data and return StateMachine instance
        data = filename.read_text(encoding='utf-8')
        if filename.suffix.lower() == '.json':
            return cls.from_json(data)
        else:
            return cls.from_yaml(data)
