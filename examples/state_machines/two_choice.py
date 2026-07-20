"""Two Choice.

A state can define multiple transitions, each attached to a different event. Whichever
event occurs first determines the next state, allowing the state machine to branch —
the basis of decision tasks.

In this example, ``WaitForChoice`` waits for activity on either port 1 or port 2
(``Port1_High`` or ``Port2_High``). The LED of the chosen port is then lit at maximum
intensity for one second before the state machine exits.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.add_state(
    name='WaitForChoice',
    transitions={'Port1_High': 'LightPort1', 'Port2_High': 'LightPort2'},
)
fsm.add_state(
    name='LightPort1',
    timer=1,
    transitions={'Tup': '>exit'},
    actions={'PWM1': 255},
)
fsm.add_state(
    name='LightPort2',
    timer=1,
    transitions={'Tup': '>exit'},
    actions={'PWM2': 255},
)
