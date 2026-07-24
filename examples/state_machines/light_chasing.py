"""Light Chasing.

Each state can use actions to set output channels and transitions to react to input
events. A state without a state timer transition waits indefinitely, so the state
machine only advances when the expected input occurs.

In this example, each state lights the LED of one port (actions ``PWM1`` to ``PWM3``
at maximum intensity) and waits for that same port to go high before advancing. After
the light has been chased across ports 1 to 3 twice, the state machine exits.
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
