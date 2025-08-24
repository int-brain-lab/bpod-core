"""Conditions.

A condition (Port2 high) causes the second state to be skipped.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()
fsm.set_condition(
    condition_id=2,
    channel='Port2',
    value=True,
)
fsm.add_state(
    name='Port1Light',
    timer=1,
    state_change_conditions={'Tup': 'Port2Light', 'Condition2': 'Port3Light'},
    output_actions={'PWM1': 255},
)
fsm.add_state(
    name='Port2Light',
    timer=1,
    state_change_conditions={'Tup': 'Port3Light'},
    output_actions={'PWM2': 255},
)
fsm.add_state(
    name='Port3Light',
    timer=1,
    state_change_conditions={'Tup': '>exit'},
    output_actions={'PWM3': 255},
)
