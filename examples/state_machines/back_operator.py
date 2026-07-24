"""Back Operator.

The ``>back`` operator returns to the state that most recently transitioned into the
current state. In state diagrams it is indicated by dashed edges.

In this example, when the ``>back`` operator is triggered by ``Port3_High`` in the
state ``WaitForExit``, the state machine returns to the state that previously
transitioned into ``WaitForExit`` - either ``FlashPort1`` or ``FlashPort2``.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.add_state(
    name='WaitForChoice',
    transitions={'Port1_High': 'FlashPort1', 'Port2_High': 'FlashPort2'},
)
fsm.add_state(
    name='FlashPort1',
    timer=0.5,
    transitions={'Tup': 'WaitForExit'},
    actions={'PWM1': 255},
)
fsm.add_state(
    name='FlashPort2',
    timer=0.5,
    transitions={'Tup': 'WaitForExit'},
    actions={'PWM2': 255},
)
fsm.add_state(
    name='WaitForExit',
    transitions={
        'Port1_High': '>exit',
        'Port2_High': '>exit',
        'Port3_High': '>back',
    },
)
