"""BNC Triggered State Change.

Switches states when a TTL pulse arrives on BNC trigger channel 1.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.add_state(
    name='Port1Light',
    timer=1,
    transitions={'BNC1_High': 'Port2Light'},
    actions={'PWM1': 255},
)
fsm.add_state(
    name='Port2Light',
    timer=1,
    transitions={'Tup': '>exit'},
    actions={'PWM2': 255},
)
