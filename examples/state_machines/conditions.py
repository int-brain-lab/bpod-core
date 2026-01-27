"""Conditions.

A condition (Port2 high) causes the state 'Port2Light' to be skipped without waiting
for the timer to expire.
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
