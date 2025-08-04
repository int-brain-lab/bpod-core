"""Module defining classes and types for creating and managing state machines."""

import ctypes
import re
from collections import OrderedDict
from typing import Annotated

from graphviz import Digraph  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, NonNegativeFloat, NonNegativeInt, validate_call

StateName = Annotated[
    str,
    Field(
        min_length=1,
        title='State Name',
        description='The name of the state',
        pattern=re.compile(r'^(?!exit$).*$'),
    ),
]
StateTimer = Annotated[
    float,
    Field(
        ge=0.0,
        allow_inf_nan=False,
        default=0.0,
        title='State Timer',
        description="The state's timer in seconds",
    ),
]
TargetState = Annotated[
    str,
    Field(
        min_length=1,
        title='Target State',
        description='The name of the target state',
    ),
]
StateChangeConditions = Annotated[
    dict[str, TargetState],
    Field(
        default_factory=dict,
        title='State Change Conditions',
        description='The conditions for switching from the current state to others',
    ),
]
OutputActionValue = Annotated[
    int,
    Field(
        ge=0,
        le=255,
        title='Output Action Value',
        description='The integer value of the output action',
    ),
]
OutputActions = Annotated[
    dict[str, OutputActionValue],
    Field(
        default_factory=dict,
        title='Output Actions',
        description='The actions to be executed during the state',
    ),
]
Comment = Annotated[
    str,
    Field(
        title='Comment',
        description='An optional comment describing the state.',
    ),
]
GlobalTimerIndex = Annotated[
    NonNegativeInt,
    Field(
        title='Global Timer ID',
        description='The ID of the global timer',
    ),
]
GlobalTimerDuration = Annotated[
    NonNegativeFloat,
    Field(
        allow_inf_nan=False,
        title='Global Timer Duration',
        description='The duration of the global timer in seconds',
    ),
]
GlobalTimerOnsetDelay = Annotated[
    NonNegativeFloat,
    Field(
        allow_inf_nan=False,
        default=0.0,
        title='Onset Delay',
        description='The onset delay of the global timer in seconds',
    ),
]
GlobalTimerChannel = Annotated[
    str | None,
    Field(
        title='Channel',
        default=None,
        description='The channel affected by the global timer',
    ),
]
GlobalTimerChannelValue = Annotated[
    int,
    Field(
        ge=0,
        le=255,
        default=0,
        title='Channel Value',
        description='The value a channel is set to',
    ),
]
GlobalTimerSendEvents = Annotated[
    bool,
    Field(
        default=True,
        title='Send Events',
        description='Whether the global timer is sending events',
    ),
]
GlobalTimerLoop = Annotated[
    int,
    Field(
        ge=0,
        le=255,
        default=0,
        title='Loop Mode',
        description='Whether the global timer is looping or not',
    ),
]
GlobalTimerLoopInterval = Annotated[
    NonNegativeFloat,
    Field(
        default=0.0,
        title='Loop Interval',
        description='The interval in seconds that the global timer is looping',
    ),
]
GlobalTimerOnsetTrigger = Annotated[
    NonNegativeInt | None,
    Field(
        default=0,
        title='Onset Trigger',
        description='An integer whose bits indicate other global timers to trigger',
    ),
]
GlobalCounterID = Annotated[
    NonNegativeInt,
    Field(
        title='ID',
        description='The ID of the global counter',
    ),
]
GlobalCounterEvent = Annotated[
    str,
    Field(
        title='Event',
        description='The name of the event to count',
    ),
]
GlobalCounterThreshold = Annotated[
    NonNegativeInt,
    Field(
        le=ctypes.c_uint32(-1).value,
        title='Threshold',
        description='The count threshold to generate an event',
    ),
]
ConditionID = Annotated[
    NonNegativeInt,
    Field(
        title='ID',
        description='The ID of the condition',
    ),
]
ConditionChannel = Annotated[
    str,
    Field(
        title='Channel',
        description='The channel or global timer attached to the condition',
    ),
]
ConditionValue = Annotated[
    bool,
    Field(
        title='Value',
        description='The value of the condition channel if the condition is met',
    ),
]


class State(BaseModel):
    """Represents a state in the state machine."""

    timer: StateTimer = StateTimer()
    """The state's timer in seconds."""

    state_change_conditions: StateChangeConditions = StateChangeConditions()
    """A dictionary mapping conditions to target states for transitions."""

    output_actions: OutputActions = OutputActions()
    """A dictionary of actions to be executed during the state."""

    comment: Comment = Comment()
    """An optional comment describing the state."""

    model_config = {
        'validate_assignment': True,
        'json_schema_extra': {'additionalProperties': False},
    }
    """Configuration for the `State` model."""


class GlobalTimer(BaseModel, validate_assignment=True):
    timer_id: GlobalTimerIndex
    duration: GlobalTimerDuration
    onset_delay: GlobalTimerOnsetDelay = GlobalTimerOnsetDelay()
    channel: GlobalTimerChannel = None
    value_on: GlobalTimerChannelValue = GlobalTimerChannelValue()
    value_off: GlobalTimerChannelValue = GlobalTimerChannelValue()
    send_events: GlobalTimerSendEvents = GlobalTimerSendEvents()
    loop: GlobalTimerLoop = GlobalTimerLoop()
    loop_interval: GlobalTimerLoopInterval = GlobalTimerLoopInterval()
    onset_trigger: GlobalTimerOnsetTrigger = None


class GlobalCounter(BaseModel, validate_assignment=True):
    id: GlobalCounterID
    event: GlobalCounterEvent
    threshold: GlobalCounterThreshold


class Condition(BaseModel, validate_assignment=True):
    id: ConditionID
    channel: ConditionChannel
    value: ConditionValue


class StateMachine(BaseModel):
    """Represents a state machine with a collection of states."""

    name: str = Field(
        min_length=1,
        default='State Machine',
        title='State Machine Name',
        description='The name of the state machine',
    )
    """The name of the state machine."""

    states: OrderedDict[StateName, State] = Field(
        description='A collection of states',
        title='States',
        default_factory=OrderedDict,
        json_schema_extra={
            'propertyNames': {
                'minLength': 1,
                'type': 'string',
                'not': {'const': 'exit'},
            },
        },
    )
    """An ordered dictionary of states in the state machine."""

    global_timers: OrderedDict[GlobalTimerIndex, GlobalTimer] = Field(
        description='A collection of global timers',
        title='Global Timers',
        default_factory=OrderedDict,
        json_schema_extra={'propertyNames': {'type': 'int'}},
    )
    """An ordered dictionary of global timers in the state machine."""

    global_counters: OrderedDict[GlobalCounterID, GlobalCounter] = Field(
        description='A collection of global counters',
        title='Global Counters',
        default_factory=OrderedDict,
        json_schema_extra={'propertyNames': {'type': 'int'}},
    )
    """An ordered dictionary of global counters in the state machine."""

    conditions: OrderedDict[ConditionID, Condition] = Field(
        description='A collection of conditions',
        title='Conditions',
        default_factory=OrderedDict,
        json_schema_extra={'propertyNames': {'type': 'int'}},
    )

    model_config = {
        'validate_assignment': True,
        'json_schema_extra': {'additionalProperties': False},
    }
    """Configuration for the `StateMachine` model."""

    @validate_call
    def add_state(
        self,
        name: StateName,
        timer: StateTimer,
        state_change_conditions: StateChangeConditions,
        output_actions: OutputActions,
        comment: Comment | None = None,
    ) -> None:
        """
        Adds a new state to the state machine.

        Parameters
        ----------
        name : str
            The name of the state to be added.
        timer : float, optional
            The duration of the state's timer in seconds. Default to 0.
        state_change_conditions : dict, optional
            A dictionary mapping conditions to target states for transitions.
            Defaults to an empty dictionary.
        output_actions : dict, optional
            A dictionary of actions to be executed on entering the state.
            Defaults to an empty dictionary.
        comment : Comment, optional
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
            state_change_conditions=state_change_conditions,
            output_actions=output_actions,
            comment=comment,
        )

    def set_global_timer(  # noqa: PLR0913
        self,
        timer_id: GlobalTimerIndex,
        duration: GlobalTimerDuration,
        onset_delay: GlobalTimerOnsetDelay = 0.0,
        channel: GlobalTimerChannel = None,
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
        timer_id : int
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
        self.global_timers[timer_id] = GlobalTimer(
            timer_id=timer_id,
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

    def set_global_counter(
        self,
        counter_id: GlobalCounterID,
        event: GlobalCounterEvent,
        threshold: GlobalCounterThreshold,
    ) -> None:
        """
        Configure a global timer with the specified parameters.

        Parameters
        ----------
        counter_id : int
            The ID of the global counter.
        event : str
            The name of the event to count.
        threshold : int
            The count threshold to generate an event

        Returns
        -------
        None
        """
        self.global_counters[counter_id] = GlobalCounter(
            id=counter_id,
            event=event,
            threshold=threshold,
        )

    def set_condition(
        self,
        condition_id: ConditionID,
        channel: ConditionChannel,
        value: ConditionValue,
    ) -> None:
        """Configure a condition with the specified parameters.

        Parameters
        ----------
        condition_id : int
            The ID of the condition.
        channel : str
            The channel or global timer attached to the condition.
        value: bool
            The value of the condition channel if the condition is met

        Returns
        -------
        None
        """
        self.conditions[condition_id] = Condition(
            id=condition_id,
            channel=channel,
            value=value,
        )

    @property
    def digraph(self) -> Digraph:
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
        This method depends on theGraphviz system libraries to be installed.
        See https://graphviz.readthedocs.io/en/stable/manual.html#installation
        """
        # Initialize the Digraph with the name of the state machine
        digraph = Digraph(self.name)

        # Return an empty Digraph if there are no states
        if len(self.states) == 0:
            return digraph

        # Add the start node represented by a point-shaped node
        digraph.node(name='', shape='point')
        digraph.edge('', next(iter(self.states.keys())))

        # Add an 'exit' node if any state transitions to 'exit'
        if 'exit' in [
            target
            for state in self.states.values()
            for target in state.state_change_conditions.values()
        ]:
            digraph.node(name='exit', label='<<b>exit</b>>', shape='plain')

        # Add nodes for each state
        for state_name, state in self.states.items():
            # Create table rows for the state's comment and output actions
            comment = (
                f'<TR><TD ALIGN="LEFT" COLSPAN="2" BGCOLOR="LIGHTBLUE">'
                f'<I>{state.comment}</I></TD></TR>'
                if state.comment is not None and len(state.comment) > 0
                else ''
            )
            actions = ''.join(
                f'<TR><TD ALIGN="LEFT">{k}</TD><TD ALIGN="RIGHT">{v}</TD></TR>'
                for k, v in state.output_actions.items()
            )

            # Create label for the state node with its name, timer, comment, and actions
            label = (
                f'<<TABLE BORDER="1" CELLBORDER="0" CELLSPACING="0" ALIGN="LEFT">'
                f'<TR><TD BGCOLOR="LIGHTBLUE" ALIGN="LEFT"><B>{state_name}  </B></TD>'
                f'<TD BGCOLOR="LIGHTBLUE" ALIGN="RIGHT">{state.timer:g} s</TD></TR>'
                f'{comment}{actions}</TABLE>>'
            )

            # Add the state node to the Digraph
            digraph.node(name=state_name, label=label, shape='none')

            # Add edges for state transitions based on conditions
            for condition, target_state in state.state_change_conditions.items():
                digraph.edge(state_name, target_state, label=condition)

        return digraph
