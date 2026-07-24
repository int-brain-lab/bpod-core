"""TTL Triggered State Change.

Digital input channels generate events whenever their logic level changes — for
example, ``TTLIn1_High`` when a TTL pulse arrives on TTL input 1, and ``TTLIn1_Low``
when the line returns to low. These events can drive state transitions just like any
other event, allowing external hardware to control the state machine.

In this example, ``Port1Light`` waits for a TTL pulse on TTL input 1. The
``TTLIn1_High`` event then advances the state machine to ``Port2Light``, which exits
after one second.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.add_state(
    name='Port1Light',
    transitions={'TTLIn1_High': 'Port2Light'},
    actions={'PWM1': 255},
)
fsm.add_state(
    name='Port2Light',
    timer=1,
    transitions={'Tup': '>exit'},
    actions={'PWM2': 255},
)
