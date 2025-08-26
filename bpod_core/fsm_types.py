import re
from typing import Annotated

import msgspec
import numpy as np
from pydantic import Field

StateName = Annotated[
    str,
    Field(
        title='State Name',
        description='The name of the state',
        min_length=1,
        pattern=re.compile(r'^(?!>)(?!exit$).+$'),
    ),
]
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
StateChangeConditions = Annotated[
    dict[Event, StateName | Operator],
    Field(
        title='State Change Conditions',
        description='A collection of events and their assigned target states',
        default_factory=dict,
    ),
]
StateActionName = Annotated[
    str,
    Field(
        title='Output Action Name',
        description='The name of the output action',
        min_length=1,
    ),
]
StateActionValue = Annotated[
    int,
    Field(
        title='Output Action Value',
        description='The integer value of the output action',
        ge=0,
        le=255,
    ),
]
StateActions = Annotated[
    dict[StateActionName, StateActionValue],
    Field(
        title='Output Actions',
        description='A collection of output actions and their respective values',
        default_factory=dict,
    ),
]
StateComment = Annotated[
    str,
    Field(
        title='Comment',
        description='A comment describing the state.',
    ),
]
GlobalTimerIndex = Annotated[
    int,
    Field(
        title='Global Timer ID',
        description='The ID of the global timer',
        ge=0,
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
GlobalCounterID = Annotated[
    int,
    Field(
        title='ID',
        description='The ID of the global counter',
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
ConditionID = Annotated[
    int,
    Field(
        title='ID',
        description='The ID of the condition',
        ge=0,
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
