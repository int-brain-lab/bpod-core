"""Conditions.

Regular events fire only when a channel *changes* state. A condition, in contrast, is
evaluated when a state is entered: if the configured channel (or global timer) is
already in the specified state, the corresponding ``Condition{N}`` event fires
immediately. This allows states to be skipped based on the current state of a channel
rather than waiting for a change to occur.

In this example, `Condition2` is met while ``Port2`` is high. If that is the case when
the state `Port2Light` is entered, the ``Condition2`` event fires and the state
machine advances to `Port3Light` without waiting for the one-second state timer to
expire.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.set_condition(
    index=2,
    channel='Port2',
    value=True,  # condition is true when Port2 is high
)

fsm.add_state(
    name='Port1Light',
    timer=1,
    transitions={'Tup': 'Port2Light'},
    actions={'PWM1': 255},
)
fsm.add_state(
    name='Port2Light',
    timer=1,
    transitions={'Tup': 'Port3Light', 'Condition2': 'Port3Light'},
    actions={'PWM2': 255},
)
fsm.add_state(
    name='Port3Light',
    timer=1,
    transitions={'Tup': '>exit'},
    actions={'PWM3': 255},
)
