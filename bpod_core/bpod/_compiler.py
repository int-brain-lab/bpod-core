"""Translation of state machines into the Bpod's binary format."""

import logging
import struct
import threading
from collections.abc import Mapping, Sequence
from datetime import timedelta
from time import perf_counter_ns
from typing import ClassVar

import msgspec
import polars as pl
from cachetools import FIFOCache
from xxhash import xxh3_64 as _xxh3_64

from bpod_core.bpod._threads import _build_event_lookup
from bpod_core.bpod.constants import (
    _CHANNEL_BASE_NAME_CONDITION,
    _CHANNEL_BASE_NAME_GLOBAL_COUNTER,
    _CHANNEL_BASE_NAME_GLOBAL_TIMER,
    CHANNEL_TYPES_INPUT,
    CHANNEL_TYPES_OUTPUT,
    VALID_OPERATORS,
    FlexIOChannelType,
)
from bpod_core.bpod.flexio import (
    _counts_to_mv,
    _mv_to_counts,
    is_flexio_threshold_ambiguous,
)
from bpod_core.bpod.structs import (
    VersionInfo,
    _HardwareConfiguration,
    _InputEventRanges,
    _InputEvents,
    _StateMachineLookup,
    _ValidationData,
)
from bpod_core.constants import (
    FMT_UINT8,
    FMT_UINT16_LE,
    FMT_UINT32_LE,
    UINT8_MAX,
    UINT32_MAX,
)
from bpod_core.fsm import StateMachine
from bpod_core.misc import extend_packed, suggest_similar

logger = logging.getLogger(__name__)


def _name_channels(
    description: bytes, channel_types: Mapping[bytes, str]
) -> list[tuple[bytes, str]]:
    """Pair each channel of a hardware I/O description with its name, e.g. 'Port1'."""
    io_keys = [bytes([io_key]) for io_key in description]
    return [
        (io_key, f'{channel_types[io_key]}{io_keys[:idx].count(io_key) + 1}')
        for idx, io_key in enumerate(io_keys)
    ]


class StateMachineCompiler:
    """Validate and compile state machines for one hardware configuration.

    Holds the tables that name the hardware's input events and output actions, and
    translates state machines into the Bpod's binary format. Results are cached across
    instances, keyed by :attr:`hardware_hash`. A new compiler is needed whenever the
    hardware configuration changes, e.g. the modules or the FlexIO settings.

    Parameters
    ----------
    hardware : _HardwareConfiguration
        The Bpod's hardware configuration.
    version : VersionInfo
        The Bpod's version information.
    modules : Mapping[str, Sequence[str]]
        Event names of each module, keyed by module name, in the order of the module
        ports.
    flexio_channel_types : Mapping[str, FlexIOChannelType]
        Identity of each FlexIO channel, keyed by channel name, in the order of the
        channels.
    threshold_voltages : Mapping[str, Sequence[int]]
        Voltages of the analog thresholds in millivolts, keyed by channel name. Only
        needed for channels configured as analog inputs.
    """

    _validation_cache: ClassVar[FIFOCache[tuple[bytes, bytes], _ValidationData]] = (
        FIFOCache(maxsize=1024)
    )
    _compilation_cache: ClassVar[
        FIFOCache[tuple[bytes, bytes, bool], tuple[bytes, _StateMachineLookup]]
    ] = FIFOCache(maxsize=1024)
    # all compilers share the caches, possibly from several threads
    _cache_lock: ClassVar[threading.Lock] = threading.Lock()

    input_event_names: tuple[str, ...]
    """Names of all input events a state machine can transition on."""

    action_names: tuple[str, ...]
    """Names of all actions a state machine can set."""

    actions: list[str]
    """All output actions, in the order of their action indices."""

    resettable_actions: list[str]
    """Output actions that the Bpod resets at each state."""

    n_softcodes: int
    """Number of softcodes per USB channel."""

    event_lookup: pl.DataFrame
    """Event metadata for decoding a trial's events."""

    hardware_hash: bytes
    """Hash of the hardware configuration and the tables derived from it."""

    serial_event_allocation: bytes
    """Number of events per serial channel, in the order of the channels."""

    def __init__(
        self,
        hardware: _HardwareConfiguration,
        version: VersionInfo,
        modules: Mapping[str, Sequence[str]],
        *,
        flexio_channel_types: Mapping[str, FlexIOChannelType],
        threshold_voltages: Mapping[str, Sequence[int]],
    ) -> None:
        self._hardware = hardware
        self._version = version
        self._modules = {name: list(events) for name, events in modules.items()}
        self._flexio_types = dict(flexio_channel_types)
        self._threshold_voltages = dict(threshold_voltages)
        self.serial_event_allocation = self._compile_input_events()
        self._compile_output_actions()
        self.event_lookup = _build_event_lookup(self._input_events, self.actions)
        self.hardware_hash = self._compute_hardware_hash()

    def _compile_input_events(self) -> bytes:
        """Compile input events supported by the Bpod hardware.

        Returns
        -------
        bytes
            The number of events per serial channel, in the order of the channels.
        """
        hw = self._hardware
        n_serial_events = sum(len(names) for names in self._modules.values())
        n_softcodes = hw.max_serial_events - n_serial_events
        n_usb = hw.input_description.count(b'X')
        n_usb_ext = hw.input_description.count(b'Z')
        n_softcodes_per_usb = n_softcodes // (n_usb + n_usb_ext)
        n_app_softcodes = n_usb_ext * n_softcodes_per_usb
        serial_event_allocation: list[int] = []
        event_names: list[str] = []
        event_channels: list[str | None] = []
        event_values: list[int | None] = []
        non_bindable_events: list[str] = []
        flexio_types = tuple(self._flexio_types.values())

        # physical input channel events
        counters = dict.fromkeys(CHANNEL_TYPES_INPUT, 0)
        for io_key in [bytes([x]) for x in hw.input_description]:
            channel_name = CHANNEL_TYPES_INPUT[io_key]
            if io_key == b'U':  # Serial
                module_name, ev_names = list(self._modules.items())[counters[io_key]]
                ev_channels: list[str | None] = [module_name] * len(ev_names)
                ev_values: list[int | None] = [None] * len(ev_names)
            elif io_key == b'X':  # SoftCode
                ev_names = [f'{channel_name}{i}' for i in range(n_softcodes_per_usb)]
                ev_channels = [channel_name] * n_softcodes_per_usb
                ev_values = list(range(n_softcodes_per_usb))
            elif io_key == b'Z':  # SoftCodeApp
                ev_names = [f'{channel_name}{i}' for i in range(n_app_softcodes)]
                ev_channels = [channel_name] * n_app_softcodes
                ev_values = list(range(n_app_softcodes))
            elif io_key == b'F':  # Flex
                channel = f'{channel_name}{counters[io_key] + 1}'
                match self._flexio_types[channel]:
                    case FlexIOChannelType.DIGITAL_INPUT:
                        ev_names = [f'{channel}_{s}' for s in ('High', 'Low')]
                        ev_values = [1, 0]
                    case FlexIOChannelType.ANALOG_INPUT:
                        ev_names = [f'{channel}_{s}' for s in ('Trig0', 'Trig1')]
                        ev_values = list(self._threshold_voltages[channel])
                        if is_flexio_threshold_ambiguous(
                            flexio_types, counters[io_key]
                        ):
                            non_bindable_events.extend(ev_names)
                    case _:
                        # every physical Flex channel reserves 2 event slots
                        # unconditionally, matching firmware layout; channels not
                        # configured as an input can never actually trigger them
                        ev_names = [f'{channel}_{i}' for i in range(2)]
                        ev_values = [0, 1]
                        non_bindable_events.extend(ev_names)
                ev_channels = [channel] * 2
            elif io_key in b'PBW':  # Port, TTL, Wire
                channel = f'{channel_name}{counters[io_key] + 1}'
                ev_names = [f'{channel}_{s}' for s in ('High', 'Low')]
                ev_channels = [channel] * 2
                ev_values = [1, 0]
            else:
                logger.warning('Skipping unsupported input channel: %s', io_key)
                continue
            if io_key in b'UXZ':
                serial_event_allocation.append(len(ev_names))
            event_names.extend(ev_names)
            event_channels.extend(ev_channels)
            event_values.extend(ev_values)
            counters[io_key] += 1
        _n_events = len(event_names)
        range_input = range(_n_events)

        # global timer start events
        range_global_timer_starts = range(_n_events, _n_events + hw.n_global_timers)
        for i in range(hw.n_global_timers):
            event_names.append(f'{_CHANNEL_BASE_NAME_GLOBAL_TIMER}{i}_Start')
            event_channels.append(f'{_CHANNEL_BASE_NAME_GLOBAL_TIMER}{i}')
            event_values.append(1)
        _n_events += hw.n_global_timers

        # global timer end events
        range_global_timer_ends = range(_n_events, _n_events + hw.n_global_timers)
        for i in range(hw.n_global_timers):
            event_names.append(f'{_CHANNEL_BASE_NAME_GLOBAL_TIMER}{i}_End')
            event_channels.append(f'{_CHANNEL_BASE_NAME_GLOBAL_TIMER}{i}')
            event_values.append(0)
        _n_events += hw.n_global_timers

        # global counter end events
        range_global_counter_ends = range(_n_events, _n_events + hw.n_global_counters)
        for i in range(hw.n_global_counters):
            event_names.append(f'{_CHANNEL_BASE_NAME_GLOBAL_COUNTER}{i}_End')
            event_channels.append(None)
            event_values.append(None)
        _n_events += hw.n_global_counters

        # condition events
        range_conditions = range(_n_events, _n_events + hw.n_conditions)
        for i in range(hw.n_conditions):
            event_names.append(f'{_CHANNEL_BASE_NAME_CONDITION}{i}')
            event_channels.append(None)
            event_values.append(None)

        # state timer end event
        event_names.append('Tup')
        event_channels.append(None)
        event_values.append(None)

        self.n_softcodes = n_softcodes_per_usb
        self._input_events = _InputEvents(
            names=event_names, channels=event_channels, values=event_values
        )
        self._event_indices = {k: v for v, k in enumerate(event_names)}
        self.input_event_names = tuple(
            n for n in event_names if n not in non_bindable_events
        )
        self._input_event_ranges = _InputEventRanges(
            input_channels=range_input,
            global_timer_starts=range_global_timer_starts,
            global_timer_ends=range_global_timer_ends,
            global_counter_ends=range_global_counter_ends,
            conditions=range_conditions,
        )
        # for each event but Tup: its range's position in _input_event_ranges, and
        # its index relative to the range's first event
        self._event_groups = {
            event_idx: (group_idx, event_idx - index_range.start)
            for group_idx, index_range in enumerate(self._input_event_ranges)
            for event_idx in index_range
        }

        modules = list(self._modules)
        input_channels = _name_channels(hw.input_description, CHANNEL_TYPES_INPUT)
        physical_input_channels = [
            name for io_key, name in input_channels if io_key != b'U'
        ]
        global_timers = [
            f'{_CHANNEL_BASE_NAME_GLOBAL_TIMER}{i}' for i in range(hw.n_global_timers)
        ]
        self._condition_channel_indices = {
            k: v
            for v, k in enumerate(modules + physical_input_channels + global_timers)
        }
        return bytes(serial_event_allocation)

    def _compile_output_actions(self) -> None:
        """Compile the list of output actions supported by the Bpod hardware."""
        self.actions = []
        non_bindable_actions: list[str] = []
        self.resettable_actions = []  # reset by the Bpod at each state

        # compile actions for output channels
        counters = dict.fromkeys(CHANNEL_TYPES_OUTPUT, 0)
        for io_key in [bytes([x]) for x in self._hardware.output_description]:
            if io_key == b'U':  # Serial
                name = list(self._modules)[counters[io_key]]
            elif io_key in b'XZ':  # SoftCode, SoftCodeApp
                name = CHANNEL_TYPES_OUTPUT[io_key]
            elif io_key in b'FVPBW':  # Flex, Valve, PWM, TTL, Wire
                name = f'{CHANNEL_TYPES_OUTPUT[io_key]}{counters[io_key] + 1}'
                self.resettable_actions.append(name)
                if io_key == b'F' and self._flexio_types[name] not in (
                    FlexIOChannelType.DIGITAL_OUTPUT,
                    FlexIOChannelType.ANALOG_OUTPUT,
                ):
                    non_bindable_actions.append(name)
            else:
                continue
            self.actions.append(name)
            counters[io_key] += 1

        # add output actions for global timers, global counters and analog thresholds
        self._global_timer_actions = ['GlobalTimerTrig', 'GlobalTimerCancel']
        self._global_counter_actions = ['GlobalCounterReset']
        self.actions.extend(self._global_timer_actions)
        self.actions.extend(self._global_counter_actions)
        if self._version.machine == 4:
            self.actions.extend(['AnalogThreshEnable', 'AnalogThreshDisable'])

        self.action_names = tuple(
            n for n in self.actions if n not in non_bindable_actions
        )
        self._action_indices = {k: v for v, k in enumerate(self.actions)}
        self._physical_output_channels = list(self._modules) + [
            name
            for io_key, name in _name_channels(
                self._hardware.output_description, CHANNEL_TYPES_OUTPUT
            )
            if io_key != b'U'
        ]
        self._timer_channel_indices: dict[str | None, int] = {
            k: v for v, k in enumerate(self._physical_output_channels)
        }
        self._timer_channel_indices[None] = 254

    def _compute_hardware_hash(self) -> bytes:
        """Compute a hash for the current hardware configuration."""
        flexio_channel_types = tuple(self._flexio_types.values())
        return _xxh3_64(
            msgspec.msgpack.encode(
                (
                    self._hardware,
                    self._version.firmware,  # compile() depends on these two
                    self._version.machine,
                    self._input_events.names,
                    self.actions,
                    flexio_channel_types,
                ),
                order='deterministic',
            )
        ).digest()

    def validate(
        self, state_machine: StateMachine, *, known_hash: bytes | None = None
    ) -> _ValidationData:
        """
        Validate the provided state machine for compatibility with the hardware.

        Parameters
        ----------
        state_machine : StateMachine
            The state machine to validate.
        known_hash : bytes | None, optional
            Known hash of the state machine. Hash will be computed if not provided.

        Returns
        -------
        _ValidationData
            A named tuple with data to be reused during compilation

        Raises
        ------
        ValueError
            If the state machine is invalid or not compatible with the hardware.
        """
        debugging = logger.isEnabledFor(logging.DEBUG)

        # get nanosecond count for benchmarking
        if debugging:
            t0 = perf_counter_ns()

        # skip validation if the state machine has been validated before
        fsm_hash = known_hash or state_machine.hash
        cache_key = (self.hardware_hash, fsm_hash)
        with self._cache_lock:
            cached = self._validation_cache.get(cache_key)
        if cached is not None:
            if debugging:
                logger.debug(
                    'Skipped validation of known valid state machine (%s μs)',
                    (perf_counter_ns() - t0) // 1000,
                )
            return cached

        # run hardware independent checks
        check_data = state_machine._check(known_hash=fsm_hash)  # noqa: SLF001

        # check if the '>back' operator is being used
        use_back_op = '>back' in check_data.transition_targets

        # check the number of states
        state_names = check_data.all_state_names
        n_states = len(state_names)
        max_n_states = self._hardware.max_states - 1 - use_back_op
        if n_states > max_n_states:
            raise ValueError(
                f'{n_states} states in state machine - Bpod Finite State Machine '
                f'{self._version.machine_str} only supports up to {max_n_states} states'
                + (" when using the '>back' operator" if use_back_op else '')
            )

        # check id's of global timers, global counters and conditions
        global_timer_ids = set(state_machine.global_timers)
        global_counter_ids = set(state_machine.global_counters)
        condition_ids = set(state_machine.conditions)
        for name, ids, maximum_value in (
            ('Global Timer', global_timer_ids, self._hardware.n_global_timers),
            ('Global Counter', global_counter_ids, self._hardware.n_global_counters),
            ('Condition', condition_ids, self._hardware.n_conditions),
        ):
            if ids and (largest_requested := max(ids)) >= maximum_value:
                raise ValueError(
                    f'Requested invalid {name} with index {largest_requested} - '
                    f'Bpod Finite State Machine {self._version.machine_str} supports '
                    f'up to {maximum_value} {name}s with indices 0-{maximum_value - 1}'
                )

        # define valid input events and actions
        valid_input_events = set(self.input_event_names)
        valid_actions = set(self.action_names)
        if not global_timer_ids:
            # TODO: remove global timer events from valid_input_events
            valid_actions -= set(self._global_timer_actions)
        if not global_counter_ids:
            # TODO: remove global counter events from valid_input_events
            valid_actions -= set(self._global_counter_actions)
        if not condition_ids:
            pass  # TODO: remove condition events from valid_input_events

        # validate states
        max_time_uint32 = UINT32_MAX / self._hardware.cycle_frequency
        for state_name, state in state_machine.states.items():
            if state.timer > max_time_uint32:
                max_timedelta = timedelta(seconds=max_time_uint32)
                raise ValueError(
                    f"Invalid state timer for state '{state_name}' - must not exceed "
                    f'{max_timedelta}',
                )
            for condition_name, target in state.transitions.items():
                if target.startswith('>') and target not in VALID_OPERATORS:
                    raise ValueError(
                        f"Invalid operator '{target}' for transition condition "
                        f"'{condition_name}' in state '{state_name}'"
                        + suggest_similar(target, VALID_OPERATORS),
                    )
                if condition_name not in valid_input_events:
                    # TODO: add more specific error messages if condition_name refers
                    #       to a global timer, global counter or condition event
                    raise ValueError(
                        f"Invalid transition condition '{condition_name}' in state "
                        f"'{state_name}'"
                        + suggest_similar(condition_name, self.input_event_names),
                    )

            # validate actions
            if bad_actions := set(state.actions).difference(valid_actions):
                bad_action = bad_actions.pop()
                if bad_action in self._global_timer_actions:
                    detail = ' - no global timer has been set'
                elif bad_action in self._global_counter_actions:
                    detail = ' - no global counter has been set'
                else:
                    detail = suggest_similar(bad_action, self.actions)
                raise ValueError(
                    f"Invalid action '{bad_action}' in state '{state_name}' {detail}"
                )

            # validate action values
            for action_name, action_value in state.actions.items():
                is_flexio_analog_output = (
                    self._flexio_types.get(action_name)
                    == FlexIOChannelType.ANALOG_OUTPUT
                )
                if action_value != int(action_value):
                    raise ValueError(
                        f"Invalid value {action_value!r} for action '{action_name}' "
                        f"in state '{state_name}' - must be an integer"
                    )
                # the Bpod uses one byte of each action value, except for FlexIO
                # analog outputs (millivolts, checked below)
                if not is_flexio_analog_output and action_value > UINT8_MAX:
                    raise ValueError(
                        f"Invalid value {action_value!r} for action '{action_name}' "
                        f"in state '{state_name}' - must not exceed {UINT8_MAX}"
                    )
                # the Bpod sends softcodes as one byte, and bpod-core sends them +1
                if 'SoftCode' in action_name and action_value >= UINT8_MAX:
                    raise ValueError(
                        f"Invalid value {action_value!r} for action '{action_name}' "
                        f"in state '{state_name}' - must not exceed {UINT8_MAX - 1}"
                    )
                if is_flexio_analog_output and not 0 <= action_value <= 5000:
                    raise ValueError(
                        f'Invalid value {action_value!r} for action '
                        f"'{action_name}' in state '{state_name}' - FlexIO analog "
                        f'output actions must be between 0 and 5000 (mV)'
                    )
                if (
                    action_name in ('AnalogThreshEnable', 'AnalogThreshDisable')
                    and self._flexio_types
                ):
                    channel_types = list(self._flexio_types.values())
                    if int(action_value) >> len(channel_types):
                        raise ValueError(
                            f'Invalid value {action_value!r} for action '
                            f"'{action_name}' in state '{state_name}' - this Bpod "
                            f'has only {len(channel_types)} FlexIO channels'
                        )
                    for bit, channel_type in enumerate(channel_types):
                        if not int(action_value) & (1 << bit):
                            continue
                        if channel_type != FlexIOChannelType.ANALOG_INPUT:
                            raise ValueError(
                                f'Invalid value {action_value!r} for action '
                                f"'{action_name}' in state '{state_name}' - "
                                f'channel {bit + 1} is not configured as '
                                'ANALOG_INPUT'
                            )
                        if (
                            action_name == 'AnalogThreshEnable'
                            and is_flexio_threshold_ambiguous(channel_types, bit)
                        ):
                            raise ValueError(
                                f'Invalid value {action_value!r} for action '
                                f"'{action_name}' in state '{state_name}' - arms "
                                f"channel {bit + 1}'s threshold, whose "
                                'analog-threshold events cannot be reliably '
                                'attributed (upstream firmware quirk)'
                            )
            for key in self._global_timer_actions:
                value = state.actions.get(key)
                if value is not None and value not in global_timer_ids:
                    raise ValueError(
                        f"Invalid value {value} for action '{key}' in state "
                        f"'{state_name}' - Global Timer {value} has not been set"
                    )
            for key in self._global_counter_actions:
                value = state.actions.get(key)
                if value is not None and value not in global_counter_ids:
                    raise ValueError(
                        f"Invalid value {value} for action '{key}' in state "
                        f"'{state_name}' - Global Counter {value} has not been set"
                    )

        # validate global timers
        if global_timer_ids:
            for timer_id, timer in state_machine.global_timers.items():
                if timer.channel not in (*self._physical_output_channels, None):
                    raise ValueError(
                        f"Invalid channel '{timer.channel}' for Global Timer {timer_id}"
                        + suggest_similar(
                            timer.channel or '', self._physical_output_channels
                        ),
                    )
                flex_type = self._flexio_types.get(timer.channel or '')
                if flex_type is not None and flex_type not in (
                    FlexIOChannelType.DIGITAL_OUTPUT,
                    FlexIOChannelType.ANALOG_OUTPUT,
                ):
                    raise ValueError(
                        f"Invalid channel '{timer.channel}' for Global Timer "
                        f'{timer_id} - it is not configured as an output'
                    )
                # FlexIO analog outputs take millivolts; every other channel uses at
                # most 8 bits of a timer's value (PWM level, message index, softcode)
                is_flexio_analog_output = flex_type == FlexIOChannelType.ANALOG_OUTPUT
                max_value = 5000 if is_flexio_analog_output else UINT8_MAX
                for key in ('value_on', 'value_off'):
                    if (value := getattr(timer, key)) > max_value:
                        unit = ' mV' if is_flexio_analog_output else ''
                        raise ValueError(
                            f'Invalid {key} {value} for Global Timer {timer_id} - '
                            f"must not exceed {max_value}{unit} on '{timer.channel}'"
                        )
                for index in range(timer.onset_trigger.bit_length()):
                    if (
                        timer.onset_trigger >> index & 1
                        and index not in global_timer_ids
                    ):
                        raise ValueError(
                            f'Invalid onset trigger for Global Timer {timer_id} - '
                            f'Global Timer {index} has not been set'
                        )
                for key in ('duration', 'onset_delay', 'loop_interval'):
                    if getattr(timer, key) > max_time_uint32:
                        max_timedelta = timedelta(seconds=max_time_uint32)
                        name = key.replace('_', ' ')
                        raise ValueError(
                            f'Invalid {name} {getattr(timer, key)} s for Global Timer '
                            f'{timer_id} - {name} must not exceed {max_timedelta}'
                        )

        # validate global counters
        for counter_id, counter in state_machine.global_counters.items():
            if counter.event not in valid_input_events:
                raise ValueError(
                    f"Invalid event '{counter.event}' for Global Counter {counter_id}"
                    + suggest_similar(counter.event, self.input_event_names),
                )

        # validate conditions
        for condition_id, condition in state_machine.conditions.items():
            if condition.channel not in self._condition_channel_indices:
                raise ValueError(
                    f"Invalid channel '{condition.channel}' for Condition "
                    f'{condition_id}'
                    + suggest_similar(
                        condition.channel, list(self._condition_channel_indices)
                    ),
                )

        # TODO: Check that sync channel is not used as state output

        # add validated state machine to cache
        validation_data = _ValidationData(
            use_back_operator=use_back_op,
            state_names=state_names,
        )
        with self._cache_lock:
            self._validation_cache[cache_key] = validation_data

        # report benchmarking results
        if debugging:
            logger.debug(
                'Validated state machine %s (%d μs)',
                fsm_hash.hex(),
                (perf_counter_ns() - t0) // 1000,
            )

        # return expensive transition_targets for further use by caller
        return validation_data

    def compile(
        self,
        *,
        state_machine: StateMachine,
        known_hash: bytes | None = None,
        validate: bool = True,
    ) -> tuple[bytes, _StateMachineLookup]:
        """Compile a state machine into its binary wire format and annotation data.

        Builds the state transition matrix, encodes states, transitions, actions,
        global timers, global counters, and conditions into bytes compatible with
        the Bpod firmware, and returns annotation data for post-trial decoding by
        :class:`~bpod_core.bpod._threads.EventThread`.

        Parameters
        ----------
        state_machine : StateMachine
            The state machine to compile.
        known_hash : bytes | None
            Known hash of the state machine. Hash will be computed if not provided.
        validate : bool, default: True
            Whether to validate the state machine.

        Returns
        -------
        bytes
            Binary payload ready to be sent to the Bpod device.
        _StateMachineLookup
            Annotation data for post-trial event stream decoding.
        """
        debugging = logger.isEnabledFor(logging.DEBUG)

        # get nanosecond count for benchmarking
        if debugging:
            t0 = perf_counter_ns()

        # compute the state machine hash if it was not provided
        state_machine_hash = known_hash or state_machine.hash

        # use cached results if they are available
        cache_key = (self.hardware_hash, state_machine_hash, validate)
        with self._cache_lock:
            cached = self._compilation_cache.get(cache_key)
        if cached is not None:
            if debugging:
                logger.debug(
                    'Retrieved compiled, %s state machine from cache (%d μs)',
                    'validated' if validate else 'unvalidated',
                    (perf_counter_ns() - t0) // 1000,
                )
            return cached

        # validate the state machine,
        if validate:
            validation_data = self.validate(
                state_machine, known_hash=state_machine_hash
            )
            use_back_op = validation_data.use_back_operator
            state_names = validation_data.state_names
        else:
            use_back_op = any(
                t == '>back'
                for s in state_machine.states.values()
                for t in s.transitions.values()
            )
            state_names = list(state_machine.states.keys())

        # state machine
        states = list(state_machine.states.values())
        n_states = len(states)

        # hardware
        version = self._version
        cycle_frequency = self._hardware.cycle_frequency

        # dense lists for global timers, counters, conditions
        n_global_timers = max(state_machine.global_timers.keys(), default=-1) + 1
        global_timers_list = [
            state_machine.global_timers.get(i) for i in range(n_global_timers)
        ]
        n_global_counters = max(state_machine.global_counters.keys(), default=-1) + 1
        global_counters_list = [
            state_machine.global_counters.get(i) for i in range(n_global_counters)
        ]
        n_conditions = max(state_machine.conditions.keys(), default=-1) + 1
        conditions_list = [state_machine.conditions.get(i) for i in range(n_conditions)]

        # index lookups
        target_indices = {k: v for v, k in enumerate([*state_names, '>exit'])}
        if use_back_op:
            target_indices['>back'] = 255
        event_indices = self._event_indices
        action_indices = self._action_indices
        condition_channel_indices = self._condition_channel_indices
        timer_channel_indices = self._timer_channel_indices

        # indexed transitions per state
        state_transition_indices = [
            [(event_indices[e], target_indices[t]) for e, t in s.transitions.items()]
            for s in states
        ]

        # build the state transition matrix (n_states x 255 events)
        state_transition_matrix = [[state_idx] * 255 for state_idx in range(n_states)]
        for targets, transitions in zip(
            state_transition_matrix, state_transition_indices, strict=True
        ):
            for event_idx, target_idx in transitions:
                targets[event_idx] = target_idx

        # FlexIO analog outputs take millivolts (0-5000), sent as DAC counts. Every
        # other value must already be an integer (enforced during validation) and
        # needs no further scaling.
        analog_outputs = {
            name
            for name, channel_type in self._flexio_types.items()
            if channel_type == FlexIOChannelType.ANALOG_OUTPUT
        }

        # scale action values once, shared by the wire-encoding section further down
        # and (for non-FlexIO actions) the annotation below
        state_actions = [
            {
                k: _mv_to_counts(v) if k in analog_outputs else int(v)
                for k, v in s.actions.items()
            }
            for s in states
        ]

        # the annotation EventThread uses to record get_data()'s reported value
        # reports FlexIO ANALOG_OUTPUT in millivolts (what was actually applied to
        # the pin), not the wire's raw DAC count
        reported_state_actions = [
            {k: _counts_to_mv(v) if k in analog_outputs else v for k, v in a.items()}
            for a in state_actions
        ]

        # build annotation data structure for event decoding in EventThread
        annotations = _StateMachineLookup(
            fsm_hash=state_machine_hash,
            state_names=state_names,
            state_transition_matrix=state_transition_matrix,
            state_actions=reported_state_actions,
            use_back_op=use_back_op,
            state_lookup=dict(enumerate(state_names)),
        )

        # Initialize bytearray for the compiled state machine.
        # This will be appended to in the following sections.
        #
        # The first 3 bytes are reserved for the state machine header and will be set
        # at the very end of the compilation
        fsm_bytes = bytearray(3)

        # transitions per event range and state, as flat lists of relative event
        # indices and target indices - for use in append_events closure
        grouped_transitions: list[list[list[int]]] = [
            [[] for _ in states] for _ in self._input_event_ranges
        ]
        for state_idx, transitions in enumerate(state_transition_indices):
            for event_idx, target_idx in transitions:
                if (group := self._event_groups.get(event_idx)) is not None:
                    group_idx, relative_idx = group
                    grouped_transitions[group_idx][state_idx].extend(
                        (relative_idx, target_idx)
                    )

        def append_events(transitions_per_state: list[list[int]]) -> None:
            """Append each state's transitions for one range of events to fsm_bytes.

            For each state: [count] [event_idx, target_idx] ..., where count is the
            number of transitions, event_idx is relative to the range's first event,
            and target_idx is the target state index.
            """
            for transitions in transitions_per_state:
                fsm_bytes.append(len(transitions) // 2)
                fsm_bytes.extend(transitions)

        # COUNTERS (4 bytes):
        fsm_bytes.extend((n_states, n_global_timers, n_global_counters, n_conditions))

        # STATE TIMER TARGET INDICES (n_states bytes):
        # Target state index for each state's 'Tup' event (defaults to self)
        tup_index = event_indices['Tup']
        fsm_bytes.extend(targets[tup_index] for targets in state_transition_matrix)

        # INPUT EVENTS (variable length, per state):
        #   [count] [event_idx, target_idx] ...  for events on physical input channels
        input_channel_transitions, *remaining_transitions = grouped_transitions
        append_events(input_channel_transitions)

        # ACTIONS (variable length, per state):
        #   [count] [action_idx, value] ...  (8-bit on Bpod 0.5-1, 16-bit on Bpod 2+)
        i1 = action_indices['GlobalTimerTrig']
        tmp_list: list[int] = []
        for actions in state_actions:
            counter_pos = len(tmp_list)
            tmp_list.append(0)
            for action_name, action_value in actions.items():
                if (key_idx := action_indices[action_name]) < i1:
                    tmp_list[counter_pos] += 1
                    tmp_list.extend(
                        (key_idx, action_value + ('SoftCode' in action_name))
                    )
        extend_packed(
            fsm_bytes, tmp_list, FMT_UINT16_LE if version.machine == 4 else FMT_UINT8
        )

        # REMAINING EVENTS (global timer starts/ends, global counter ends, conditions)
        #   [count] [event_idx, target_idx] ...  for each event
        for transitions_per_state in remaining_transitions:
            append_events(transitions_per_state)

        if n_global_timers:
            # GLOBAL TIMER CHANNELS
            fsm_bytes.extend(
                timer_channel_indices[gt.channel if gt else None]
                for gt in global_timers_list
            )

            # GLOBAL TIMER ON & OFF VALUES
            # Bpod 2+ uses 16-bit values for value_on and value_off; values for FlexIO
            # analog outputs are millivolts, sent as DAC counts like action values
            format_string = FMT_UINT16_LE if version.machine == 4 else FMT_UINT8
            for field_name in ('value_on', 'value_off'):
                extend_packed(
                    fsm_bytes,
                    [
                        _mv_to_counts(getattr(gt, field_name))
                        if gt is not None and gt.channel in analog_outputs
                        else getattr(gt, field_name, 0)
                        for gt in global_timers_list
                    ],
                    format_string,
                )

            # GLOBAL TIMER LOOP & SEND_EVENTS
            for field_name, default in (('loop', 0), ('send_events', 1)):
                fsm_bytes.extend(
                    getattr(gt, field_name, default) for gt in global_timers_list
                )

        # GLOBAL COUNTER EVENTS
        if n_global_counters:
            fsm_bytes.extend(
                event_indices[gc.event] if gc else 254 for gc in global_counters_list
            )

        # CONDITION CHANNELS & VALUES
        if n_conditions:
            fsm_bytes.extend(
                condition_channel_indices[c.channel] if c else 0
                for c in conditions_list
            )
            fsm_bytes.extend(c.value if c else 0 for c in conditions_list)

        if version.firmware < (23, 0):
            fsm_bytes.extend(
                actions.get('GlobalCounterReset', -1) + 1 for actions in state_actions
            )
        else:
            counter_idx = len(fsm_bytes)
            fsm_bytes.append(0)
            for state_idx, actions in enumerate(state_actions):
                if (value := actions.get('GlobalCounterReset', -1)) >= 0:
                    fsm_bytes[counter_idx] += 1
                    fsm_bytes.extend([state_idx, value + 1])

        # ANALOG THRESHOLDS (variable length, per action):
        #   [count] [state_idx, bitmask] ...  for states that arm/disarm thresholds
        if version.machine == 4:
            for key in ('AnalogThreshEnable', 'AnalogThreshDisable'):
                count_pos = len(fsm_bytes)
                fsm_bytes.append(0)
                for state_idx, actions in enumerate(state_actions):
                    if bitmask := actions.get(key, 0):
                        fsm_bytes[count_pos] += 1
                        fsm_bytes.extend([state_idx, bitmask])

        # Timer trigger/cancel bitmasks need enough bits to address all timers.
        # Use the smallest integer type that fits n_global_timers bits.
        if self._hardware.n_global_timers > 16:
            format_string = FMT_UINT32_LE
        elif self._hardware.n_global_timers > 8:
            format_string = FMT_UINT16_LE
        else:
            format_string = FMT_UINT8

        # GLOBAL TIMER TRIGGERS AND CANCELS (bitmasks, one bit per timer)
        for key in ('GlobalTimerTrig', 'GlobalTimerCancel'):
            bitmasks = [1 << s.actions[key] if key in s.actions else 0 for s in states]
            extend_packed(fsm_bytes, bitmasks, format_string)

        # GLOBAL TIMER ONSET TRIGGERS
        extend_packed(
            fsm_bytes,
            [getattr(gt, 'onset_trigger', 0) for gt in global_timers_list],
            format_string,
        )

        # STATE TIMERS (uInt32)
        state_timers = [round(s.timer * cycle_frequency) for s in states]
        extend_packed(fsm_bytes, state_timers, FMT_UINT32_LE)

        # GLOBAL TIMER DURATION, ONSET DELAY, LOOP INTERVAL (uInt32)
        for key in ('duration', 'onset_delay', 'loop_interval'):
            extend_packed(
                fsm_bytes,
                [
                    round(getattr(gt, key, 0.0) * cycle_frequency)
                    for gt in global_timers_list
                ],
                FMT_UINT32_LE,
            )

        # GLOBAL COUNTER THRESHOLDS (uInt32)
        extend_packed(
            fsm_bytes,
            [getattr(gc, 'threshold', 0) for gc in global_counters_list],
            FMT_UINT32_LE,
        )

        # FOOTER (firmware 23+, 1 byte):
        #   Reserved for additional opcodes
        if version.firmware > (22, 0):
            fsm_bytes.append(0)

        # HEADER
        struct.pack_into('<?H', fsm_bytes, 0, use_back_op, len(fsm_bytes) - 3)

        # store the compiled state machine for future use, as immutable bytes
        compiled = (bytes(fsm_bytes), annotations)
        with self._cache_lock:
            self._compilation_cache[cache_key] = compiled

        # report benchmarking results
        if debugging:
            logger.debug(
                'Compiled state machine %s (%d μs)',
                state_machine_hash.hex(),
                (perf_counter_ns() - t0) // 1000,
            )

        # Return the compiled state machine and annotations
        return compiled
