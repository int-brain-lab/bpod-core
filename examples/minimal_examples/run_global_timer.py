"""Global Timer Example.

This example demonstrates how to define a state machine with a global timer and run it
on a Bpod with info logging enabled. The state machine alternates LEDs of Port 1 and
Port 3 every 250 ms until a 5-second global timer expires, then exits the state machine.
The trial data is finally returned to the user as a dataframe.
"""

import logging

from bpod_core.bpod import Bpod
from bpod_core.fsm import StateMachine

# configure debug logging
logging.basicConfig(level=logging.INFO)

# create a new StateMachine instance and configure a 5-second global timer
fsm = StateMachine()
fsm.set_global_timer(index=0, duration=5)

# define the state machine's states
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

# connect to the Bpod and run the state machine
with Bpod() as bpod:
    bpod.run(fsm)

# collect the data and display it
trial_data = bpod.get_data()
trial_data.show(limit=None, tbl_width_chars=200)
