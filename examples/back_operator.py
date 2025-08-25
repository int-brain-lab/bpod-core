"""Back Operator.

In this example, when the ``>back`` operator is triggered by ``Port3In`` in the
state `WaitForExit`, the state machine returns to the state that previously
transitioned into `WaitForExit` - either `FlashPort1` or `FlashPort2`.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.add_state(
    name='WaitForChoice',
    state_change_conditions={'Port1_High': 'FlashPort1', 'Port2_High': 'FlashPort2'},
)
fsm.add_state(
    name='FlashPort1',
    timer=0.5,
    state_change_conditions={'Tup': 'WaitForExit'},
    output_actions={'PWM1': 255},
)
fsm.add_state(
    name='FlashPort2',
    timer=0.5,
    state_change_conditions={'Tup': 'WaitForExit'},
    output_actions={'PWM2': 255},
)
fsm.add_state(
    name='WaitForExit',
    state_change_conditions={
        'Port1_High': '>exit',
        'Port2_High': '>exit',
        'Port3_High': '>back',
    },
)
