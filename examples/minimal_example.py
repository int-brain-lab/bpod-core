from bpod_core.fsm import StateMachine
from bpod_core.bpod import Bpod
import logging
import sys


LOG_FILE = "bpod_debug.log"

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)  # This also keeps logs appearing in the terminal
    ]
)

# 3. Specifically ensure the bpod_core library is set to DEBUG
logging.getLogger('bpod_core').setLevel(logging.DEBUG)

print(f"Logging initialized. All Bpod traffic will be saved to: {LOG_FILE}")

fsm = StateMachine()

fsm.set_global_timer(
    index=1,
    duration=5
)


fsm.add_state(
    name='StartGlobalTimer',
    timer=0.25,
    transitions={'Tup': 'Port1Light'},
    actions={'GlobalTimerTrig': 1},
)
fsm.add_state(
    name='Port1Light',
    timer=0.25,
    transitions={
        'Tup': 'Port3Light',
        'GlobalTimer1_End': '>exit',
    },
    actions={'PWM1': 255},
)
fsm.add_state(
    name='Port3Light',
    timer=0.25,
    transitions={
        'Tup': '>exit',
        'GlobalTimer1_End': '>exit',
    },
    actions={'PWM3': 255},
)



with Bpod() as bpod:
    print(f"Connected to Bpod!")
    print(f"Found Bpod on port {bpod.serial0.port}")
    print(f"Firmware Version: {bpod.version.firmware}")
    print(f"Hardware Version: {bpod.version.machine}")
    print("Send State machine.")
    bpod.send_state_machine(fsm)
    print("Run State machine.")
    bpod.run_state_machine()
    print("State machine finished.")