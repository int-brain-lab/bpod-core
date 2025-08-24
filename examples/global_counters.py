"""Global Counters.

A global counter ends an infinite loop when 5 `Port1In` events occur.
`Port1In` events acquired in the first state are deliberately not counted.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.set_global_counter(
    counter_id=1,
    event='Port1High',
    threshold=5,
)

fsm.add_state(
    name='InitialDelay',
    timer=2,
    state_change_conditions={'Tup': 'ResetGlobalCounter'},
    output_actions={'PWM2': 255},
)
fsm.add_state(
    name='ResetGlobalCounter',
    state_change_conditions={'Tup': 'Port1Light'},
    output_actions={'GlobalCounterReset': 1},
)
fsm.add_state(
    name='Port1Light',
    timer=0.25,
    state_change_conditions={
        'Tup': 'Port3Light',
        'GlobalCounter1_End': '>exit',
    },
    output_actions={'PWM1': 255},
)
fsm.add_state(
    name='Port3Light',
    timer=0.25,
    state_change_conditions={
        'Tup': 'Port1Light',
        'GlobalCounter1_End': '>exit',
    },
    output_actions={'PWM3': 255},
)
