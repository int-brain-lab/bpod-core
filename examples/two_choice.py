"""Two Choice.

Wait for a choice between ports 1 and 2. Indicate the choice for 1 second with the
chosen port LED at max intensity.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.add_state(
    name='WaitForChoice',
    state_change_conditions={'Port1_High': 'LightPort1', 'Port2_High': 'LightPort2'},
)
fsm.add_state(
    name='LightPort1',
    timer=1,
    state_change_conditions={'Tup': '>exit'},
    output_actions={'PWM1': 255},
)
fsm.add_state(
    name='LightPort2',
    timer=1,
    state_change_conditions={'Tup': '>exit'},
    output_actions={'PWM2': 255},
)
