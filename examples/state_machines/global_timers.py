"""Global Timers.

Unlike a state timer, which is restarted each time its state is entered, a global
timer runs independently of state transitions. It is started using the
``GlobalTimerTrig`` action and, once its duration has elapsed, emits a
``GlobalTimer{N}_End`` event that any state can respond to with a transition. Global
timers can also be canceled (``GlobalTimerCancel``), start after an onset delay,
loop, and drive an output channel while active. Refer to the API documentation for
:meth:`~bpod_core.fsm.StateMachine.set_global_timer` for details.

In this example, ``GlobalTimer0`` is started in the first state. The states
``Port1Light`` and ``Port3Light`` then alternate in an infinite loop until the timer
elapses after 5 seconds and the ``GlobalTimer0_End`` event exits the state machine.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.set_global_timer(
    index=0,
    duration=5,
)

fsm.add_state(
    name='StartGlobalTimer',
    timer=0.25,
    transitions={'Tup': 'Port1Light'},
    actions={'GlobalTimerTrig': 0},
)
fsm.add_state(
    name='Port1Light',
    timer=0.25,
    transitions={
        'Tup': 'Port3Light',
        'GlobalTimer0_End': '>exit',
    },
    actions={'PWM1': 255},
)
fsm.add_state(
    name='Port3Light',
    timer=0.25,
    transitions={
        'Tup': 'Port1Light',
        'GlobalTimer0_End': '>exit',
    },
    actions={'PWM3': 255},
)
