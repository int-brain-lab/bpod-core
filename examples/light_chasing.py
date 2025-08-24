"""Light Chasing.

Follow the LED to proceed to the next state.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.add_state(
    name='Port1Active1',
    state_change_conditions={'Port1_High': 'Port2Active1'},
    output_actions={'PWM1': 255},
)
fsm.add_state(
    name='Port2Active1',
    state_change_conditions={'Port2_High': 'Port3Active1'},
    output_actions={'PWM2': 255},
)
fsm.add_state(
    name='Port3Active1',
    state_change_conditions={'Port3_High': 'Port1Active2'},
    output_actions={'PWM3': 255},
)
fsm.add_state(
    name='Port1Active2',
    state_change_conditions={'Port1_High': 'Port2Active2'},
    output_actions={'PWM1': 255},
)
fsm.add_state(
    name='Port2Active2',
    state_change_conditions={'Port2_High': 'Port3Active2'},
    output_actions={'PWM2': 255},
)
fsm.add_state(
    name='Port3Active2',
    state_change_conditions={'Port3_High': '>exit'},
    output_actions={'PWM3': 255},
)
