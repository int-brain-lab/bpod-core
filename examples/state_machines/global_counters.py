"""Global Counters.

A global counter counts occurrences of a specified event across all states. Once the
count reaches the configured threshold, the counter emits a ``GlobalCounter{N}_End``
event that any state can respond to with a transition. The count can be reset to zero
using the ``GlobalCounterReset`` action.

In this example, ``GlobalCounter0`` counts ``Port1High`` events with a threshold of 5.
The states ``Port1Light`` and ``Port3Light`` alternate in an infinite loop until the
``GlobalCounter0_End`` event exits the state machine. Events occurring during
``InitialDelay`` are deliberately discarded by the ``GlobalCounterReset`` action in
``ResetGlobalCounter``.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.set_global_counter(
    index=0,
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
    actions={'GlobalCounterReset': 0},
)
fsm.add_state(
    name='Port1Light',
    timer=0.25,
    transitions={
        'Tup': 'Port3Light',
        'GlobalCounter0_End': '>exit',
    },
    actions={'PWM1': 255},
)
fsm.add_state(
    name='Port3Light',
    timer=0.25,
    transitions={
        'Tup': 'Port1Light',
        'GlobalCounter0_End': '>exit',
    },
    actions={'PWM3': 255},
)
