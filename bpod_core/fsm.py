"""Module defining classes and types for creating and managing state machines."""

import re
from collections import OrderedDict
from typing import Annotated

from graphviz import Digraph  # type: ignore
from pydantic import BaseModel, Field, validate_call

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


class State(BaseModel):
    """Represents a state in the state machine."""

    timer: float = Field(
        ge=0.0,
        allow_inf_nan=False,
        default=0.0,
        title='State Timer',
        description="The state's timer in seconds",
    )
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
            }
        },
    )
    """An ordered dictionary of states in the state machine."""

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

    def validate_target_states(self):
        """
        Validates the target states in state_change_conditions.

        Raises
        ------
        ValueError
            If any target state in state_change_conditions does not exist in the
            defined states or is not 'exit'.
        """
        valid_targets = list(self.states.keys()) + ['exit']
        for state_name, state in self.states.items():
            for target in state.state_change_conditions.values():
                if target not in valid_targets:
                    raise ValueError(
                        f'Invalid target `{target}` in state `{state_name}`'
                    )
