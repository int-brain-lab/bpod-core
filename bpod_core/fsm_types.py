import re
from typing import Annotated

import msgspec
import numpy as np
from pydantic import Field

StateName = Annotated[
    str,
    msgspec.Meta(
        min_length=1,
        title='State Name',
        description='The name of the state',
        pattern=r'^(?!>)(?!exit$).*$',
    ),
    Field(min_length=1, pattern=re.compile(r'^(?!>)(?!exit$).*$')),
]
StateTimer = Annotated[
    float,
    msgspec.Meta(
        ge=0.0,
        title='State Timer',
        description="The state's timer in seconds",
    ),
    Field(ge=0.0),
]
StateTarget = Annotated[
    str,
    msgspec.Meta(
        min_length=1,
        title='Target State',
        description='The name of the target state',
    ),
    Field(min_length=1),
]
StateConditions = Annotated[
    dict[str, StateTarget],
    msgspec.Meta(
        title='State Change Conditions',
        description='The conditions for switching from the current state to others',
    ),
]
StateActionValue = Annotated[
    int,
    msgspec.Meta(
        ge=0,
        le=255,
        title='Output Action Value',
        description='The integer value of the output action',
    ),
]
StateActions = Annotated[
    dict[str, StateActionValue],
    msgspec.Meta(
        title='Output Actions',
        description='The actions to be executed during the state',
    ),
]
StateComment = Annotated[
    str,
    msgspec.Meta(
        title='Comment',
        description='A comment describing the state.',
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
        le=np.iinfo(np.uint32).max,
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
StateMachineName = Annotated[
    str,
    msgspec.Meta(
        min_length=1,
        title='State Machine Name',
        description='The name of the state machine',
    ),
]
