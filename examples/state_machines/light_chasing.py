"""Light Chasing.

Follow the LED to proceed to the next state.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.add_state(
    name='Port1Active1',
    transitions={'Port1_High': 'Port2Active1'},
    actions={'PWM1': 255},
)
fsm.add_state(
    name='Port2Active1',
    transitions={'Port2_High': 'Port3Active1'},
    actions={'PWM2': 255},
)
fsm.add_state(
    name='Port3Active1',
    transitions={'Port3_High': 'Port1Active2'},
    actions={'PWM3': 255},
)
fsm.add_state(
    name='Port1Active2',
    transitions={'Port1_High': 'Port2Active2'},
    actions={'PWM1': 255},
)
fsm.add_state(
    name='Port2Active2',
    transitions={'Port2_High': 'Port3Active2'},
    actions={'PWM2': 255},
)
fsm.add_state(
    name='Port3Active2',
    transitions={'Port3_High': '>exit'},
    actions={'PWM3': 255},
)
