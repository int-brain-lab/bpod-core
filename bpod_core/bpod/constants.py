from bpod_core.constants import PLATFORMDIRS, VID_TEENSY, PIDsTeensy

VIDS_BPOD = [VID_TEENSY]
"""Vendor IDs of supported Bpod devices"""

PIDS_BPOD = [PIDsTeensy.SERIAL, PIDsTeensy.DUAL_SERIAL, PIDsTeensy.TRIPLE_SERIAL]
"""List of Product IDs of supported Bpod devices"""

MIN_BPOD_FW_VERSION = (23, 0)
"""minimum supported firmware version (major, minor)"""

MIN_BPOD_HW_VERSION = 3
"""minimum supported hardware version"""

MAX_BPOD_HW_VERSION = 4
"""maximum supported hardware version"""

CHANNEL_TYPES_INPUT = {
    b'U': 'Serial',
    b'X': 'SoftCode',
    b'Z': 'SoftCodeApp',
    b'F': 'Flex',
    b'D': 'Digital',
    b'B': 'BNC',
    b'W': 'Wire',
    b'P': 'Port',
}
CHANNEL_TYPES_OUTPUT = CHANNEL_TYPES_INPUT.copy()
CHANNEL_TYPES_OUTPUT.update({b'V': 'Valve', b'P': 'PWM'})
N_SERIAL_EVENTS_DEFAULT = 15
VALID_OPERATORS = {'exit', '>exit', '>back'}
MACHINE_TYPES = {3: 'r2.0-2.5', 4: '2+ r1.0'}
CONFIG_PATH = PLATFORMDIRS.user_config_path
DISCOVERY_TIMEOUT = 0.11
