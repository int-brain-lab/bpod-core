"""Hello World.

A simple state machine with two states, `Hello` and `World`. During the first state,
the output channel ``PWM1`` is set to 255. The `Hello` state transitions into the
`World` state after its 1-second state timer expires, emitting a ``Tup`` event. After
another two second in the `World` state, the state machine exits using the ``>exit``
operator.
"""

from bpod_core.fsm import StateMachine

fsm = StateMachine()

fsm.add_state(
    name='Hello',  # the name of the state
    timer=1.0,  # the state timer (in seconds)
    state_change_conditions={'Tup': 'World'},  # definition of state transitions
    output_actions={'PWM1': 255},  # an LED connected to PWM1 will light up
)
fsm.add_state(
    name='World',
    timer=2.0,
    state_change_conditions={'Tup': '>exit'},  # transition to exit
)
