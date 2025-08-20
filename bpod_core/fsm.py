"""Module defining classes and types for creating and managing state machines."""

import ctypes
from typing import Annotated

import msgspec
from graphviz import Digraph  # type: ignore[import-untyped]
from pydantic import validate_call

StateName = Annotated[
    str,
    msgspec.Meta(
        min_length=1,
        title='State Name',
        description='The name of the state',
        pattern=r'^(?!exit$).*$',
    ),
]
StateTimer = Annotated[
    float,
    msgspec.Meta(
        ge=0.0,
        title='State Timer',
        description="The state's timer in seconds",
    ),
]
TargetState = Annotated[
    str,
    msgspec.Meta(
        min_length=1,
        title='Target State',
        description='The name of the target state',
    ),
]
StateChangeConditions = Annotated[
    dict[str, TargetState],
    msgspec.Meta(
        title='State Change Conditions',
        description='The conditions for switching from the current state to others',
    ),
]
OutputActionValue = Annotated[
    int,
    msgspec.Meta(
        ge=0,
        le=255,
        title='Output Action Value',
        description='The integer value of the output action',
    ),
]
OutputActions = Annotated[
    dict[str, OutputActionValue],
    msgspec.Meta(
        title='Output Actions',
        description='The actions to be executed during the state',
    ),
]
Comment = Annotated[
    str,
    msgspec.Meta(
        title='Comment',
        description='An optional comment describing the state.',
    ),
]
GlobalTimerIndex = Annotated[
    int,
    msgspec.Meta(
        ge=0,
        title='Global Timer ID',
        description='The ID of the global timer',
    ),
]
GlobalTimerDuration = Annotated[
    float,
    msgspec.Meta(
        ge=0.0,
        title='Global Timer Duration',
        description='The duration of the global timer in seconds',
    ),
]
GlobalTimerOnsetDelay = Annotated[
    float,
    msgspec.Meta(
        ge=0.0,
        title='Onset Delay',
        description='The onset delay of the global timer in seconds',
    ),
]
GlobalTimerChannel = Annotated[
    str,
    msgspec.Meta(
        title='Channel',
        description='The channel affected by the global timer',
    ),
]
GlobalTimerChannelValue = Annotated[
    int,
    msgspec.Meta(
        ge=0,
        le=255,
        title='Channel Value',
        description='The value a channel is set to',
    ),
]
GlobalTimerSendEvents = Annotated[
    bool,
    msgspec.Meta(
        title='Send Events',
        description='Whether the global timer is sending events',
    ),
]
GlobalTimerLoop = Annotated[
    int,
    msgspec.Meta(
        ge=0,
        le=255,
        title='Loop Mode',
        description='Whether the global timer is looping or not',
    ),
]
GlobalTimerLoopInterval = Annotated[
    float,
    msgspec.Meta(
        ge=0.0,
        title='Loop Interval',
        description='The interval in seconds that the global timer is looping',
    ),
]
GlobalTimerOnsetTrigger = Annotated[
    int,
    msgspec.Meta(
        ge=0,
        title='Onset Trigger',
        description='An integer whose bits indicate other global timers to trigger',
    ),
]
GlobalCounterID = Annotated[
    int,
    msgspec.Meta(
        ge=0,
        title='ID',
        description='The ID of the global counter',
    ),
]
GlobalCounterEvent = Annotated[
    str,
    msgspec.Meta(
        title='Event',
        description='The name of the event to count',
    ),
]
GlobalCounterThreshold = Annotated[
    int,
    msgspec.Meta(
        ge=0,
        le=ctypes.c_uint32(-1).value,
        title='Threshold',
        description='The count threshold to generate an event',
    ),
]
ConditionID = Annotated[
    int,
    msgspec.Meta(
        ge=0,
        title='ID',
        description='The ID of the condition',
    ),
]
ConditionChannel = Annotated[
    str,
    msgspec.Meta(
        title='Channel',
        description='The channel or global timer attached to the condition',
    ),
]
ConditionValue = Annotated[
    bool,
    msgspec.Meta(
        title='Value',
        description='The value of the condition channel if the condition is met',
    ),
]


def validate_struct(data: msgspec.Struct):
    msgspec.msgpack.decode(msgspec.msgpack.encode(data), type=type(data))


class State(msgspec.Struct, forbid_unknown_fields=True):
    """Represents a state in the state machine."""

    timer: StateTimer = 0.0
    """The state's timer in seconds."""

    state_change_conditions: StateChangeConditions = {}
    """A dictionary mapping conditions to target states for transitions."""

    output_actions: OutputActions = {}
    """A dictionary of actions to be executed during the state."""

    comment: Comment | None = None
    """An optional comment describing the state."""


class GlobalTimer(msgspec.Struct, forbid_unknown_fields=True):
    timer_id: GlobalTimerIndex
    duration: GlobalTimerDuration
    onset_delay: GlobalTimerOnsetDelay = 0.0
    channel: GlobalTimerChannel | None = None
    value_on: GlobalTimerChannelValue = 0
    value_off: GlobalTimerChannelValue = 0
    send_events: GlobalTimerSendEvents = True
    loop: GlobalTimerLoop = 0
    loop_interval: GlobalTimerLoopInterval = 0.0
    onset_trigger: GlobalTimerOnsetTrigger = 0


class GlobalCounter(msgspec.Struct, forbid_unknown_fields=True):
    id: GlobalCounterID
    event: GlobalCounterEvent
    threshold: GlobalCounterThreshold


class Condition(msgspec.Struct, forbid_unknown_fields=True):
    id: ConditionID
    channel: ConditionChannel
    value: ConditionValue


StateMachineName = Annotated[
    str,
    msgspec.Meta(
        min_length=1,
        title='State Machine Name',
        description='The name of the state machine',
    ),
]
StateMachineStates = Annotated[
    dict[StateName, State],
    msgspec.Meta(
        title='States',
        description='A collection of states',
        min_length=1,
    ),
]
StateMachineGlobalTimers = Annotated[
    dict[GlobalTimerIndex, GlobalTimer],
    msgspec.Meta(title='Global Timers', description='A collection of global timers'),
]
StateMachineGlobalCounters = Annotated[
    dict[GlobalCounterID, GlobalCounter],
    msgspec.Meta(
        title='Global Counters', description='A collection of global counters'
    ),
]
StateMachineConditions = Annotated[
    dict[ConditionID, Condition],
    msgspec.Meta(title='Conditions', description='A collection of conditions'),
]


class StateMachine(msgspec.Struct):
    """Represents a state machine with a collection of states."""

    name: StateMachineName = 'State Machine'
    """The name of the state machine."""

    states: StateMachineStates = dict()
    """An ordered dictionary of states in the state machine."""

    global_timers: StateMachineGlobalTimers = dict()
    """An ordered dictionary of global timers in the state machine."""

    global_counters: StateMachineGlobalCounters = dict()
    """An ordered dictionary of global counters in the state machine."""

    conditions: StateMachineConditions = dict()
    """An ordered dictionary of conditions in the state machine."""

    model_config = {
        'validate_assignment': True,
        'json_schema_extra': {'additionalProperties': False},
    }
    """Configuration for the `StateMachine` model."""

    @validate_call
    def add_state(
        self,
        name: StateName,
        timer: StateTimer = 0.0,
        state_change_conditions: StateChangeConditions | None = None,
        output_actions: OutputActions | None = None,
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
        self.states[name] = State(
            timer=timer,
            state_change_conditions=state_change_conditions or {},
            output_actions=output_actions or {},
            comment=comment,
        )

    def set_global_timer(  # noqa: PLR0913
        self,
        timer_id: GlobalTimerIndex,
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
        This method depends on the Graphviz system libraries to be installed.
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
