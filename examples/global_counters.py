"""Global Counters.

A global counter ends an infinite loop when 5 `Port1In` events occur.
`Port1In` events acquired in the first state are deliberately not counted.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.set_global_counter(
    index=1,
    event='Port1High',
    threshold=5,
)

fsm.add_state(
    name='InitialDelay',
    timer=2,
    transitions={'Tup': 'ResetGlobalCounter'},
    actions={'PWM2': 255},
)
fsm.add_state(
    name='ResetGlobalCounter',
    transitions={'Tup': 'Port1Light'},
    actions={'GlobalCounterReset': 1},
)
fsm.add_state(
    name='Port1Light',
    timer=0.25,
    transitions={
        'Tup': 'Port3Light',
        'GlobalCounter1_End': '>exit',
    },
    actions={'PWM1': 255},
)
fsm.add_state(
    name='Port3Light',
    timer=0.25,
    transitions={
        'Tup': 'Port1Light',
        'GlobalCounter1_End': '>exit',
    },
    actions={'PWM3': 255},
)
