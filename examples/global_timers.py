"""Global Timers.

A global timer ends an infinite loop when 5 seconds have passed.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.set_global_timer(
    index=0, # this is 0 indexed
    duration=5
)


fsm.add_state(
    name='StartGlobalTimer',
    timer=0.25,
    transitions={'Tup': 'Port1Light'},
    actions={'GlobalTimerTrig': 1}, # this is 1 indexed
)
fsm.add_state(
    name='Port1Light',
    timer=0.25,
    transitions={
        'Tup': 'Port3Light',
        'GlobalTimer1_End': '>exit', # this is 1 indexed
    },
    actions={'PWM1': 255},
)
fsm.add_state(
    name='Port3Light',
    timer=0.25,
    transitions={
        'Tup': 'Port1Light',
        'GlobalTimer1_End': '>exit', # this is 1 indexed
    },
    actions={'PWM3': 255},
)