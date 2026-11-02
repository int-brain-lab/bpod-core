"""Tests for the state machine compiler, built without a (mocked) Bpod."""

import struct
from collections.abc import Mapping, Sequence

import msgspec
import polars as pl
import pytest

from bpod_core.bpod._compiler import StateMachineCompiler
from bpod_core.bpod.constants import FlexIOChannelType
from bpod_core.bpod.structs import VersionInfo, _HardwareConfiguration
from bpod_core.fsm import StateMachine

HARDWARE = _HardwareConfiguration(  # a Bpod Finite State Machine 2+ r1.0
    max_states=256,
    cycle_period_us=100,
    max_serial_events=75,
    max_bytes_per_serial_message=5,
    n_global_timers=16,
    n_global_counters=8,
    n_conditions=16,
    n_inputs=16,
    input_description=b'UUUXZFFFFBBPPPPP',
    n_outputs=21,
    output_description=b'UUUXZFFFFBBPPPPPVVVVV',
    cycle_frequency=10000,
    n_modules=3,
    n_flexio=4,
)
VERSION = VersionInfo(
    firmware=(23, 0), machine=4, machine_str='2+ r1.0', pcb=1, bpod_core='test'
)
MODULES = {f'Serial{i}': [f'Serial{i}_{j}' for j in range(15)] for i in (1, 2, 3)}

AI, DI, DO, AO, OFF = (
    FlexIOChannelType.ANALOG_INPUT,
    FlexIOChannelType.DIGITAL_INPUT,
    FlexIOChannelType.DIGITAL_OUTPUT,
    FlexIOChannelType.ANALOG_OUTPUT,
    FlexIOChannelType.DISABLED,
)


def make_compiler(
    flexio_channel_types: Sequence[FlexIOChannelType] = (OFF, OFF, OFF, OFF),
    threshold_voltages: Mapping[str, Sequence[int]] | None = None,
    version: VersionInfo = VERSION,
) -> StateMachineCompiler:
    names = [f'Flex{i + 1}' for i in range(len(flexio_channel_types))]
    return StateMachineCompiler(
        HARDWARE,
        version,
        MODULES,
        flexio_channel_types=dict(zip(names, flexio_channel_types, strict=True)),
        threshold_voltages=threshold_voltages or {},
    )


class TestTables:
    def test_events_and_actions_follow_the_hardware(self):
        """Module events, softcodes and channel events are named after the hardware."""
        compiler = make_compiler()
        assert compiler.input_event_names[:2] == ('Serial1_0', 'Serial1_1')
        assert {'Port1_High', 'TTLIn1_Low', 'Tup'} <= set(compiler.input_event_names)
        assert {'PWM1', 'Valve1', 'GlobalTimerTrig'} <= set(compiler.action_names)
        # 75 serial events, 45 of them taken by modules, shared by 2 USB channels
        assert compiler.n_softcodes == 15
        assert compiler.serial_event_allocation == bytes([15] * 5)

    def test_flexio_events_and_actions_follow_channel_identity(self):
        """Each FlexIO channel adds the events or the action of its identity."""
        compiler = make_compiler((AI, DI, DO, AO), {'Flex1': [2500, 1000]})
        events, actions = set(compiler.input_event_names), set(compiler.action_names)
        assert {'Flex1_Trig0', 'Flex1_Trig1', 'Flex2_High', 'Flex2_Low'} <= events
        assert not {'Flex3_0', 'Flex4_0'} & events
        assert {'Flex3', 'Flex4'} <= actions
        assert not {'Flex1', 'Flex2'} & actions
        lookup = compiler.event_tables.lookup.filter(
            pl.col('Event').cast(pl.String).str.starts_with('Flex1_Trig')
        )
        assert lookup['default_value'].to_list() == [2500, 1000]

    def test_misnumbered_threshold_events_are_refused(self):
        """An analog input after a channel of another identity has no usable events."""
        compiler = make_compiler((DI, AI, OFF, OFF), {'Flex2': [0, 0]})
        assert 'Flex2_Trig0' not in compiler.input_event_names

    def test_hardware_hash_follows_identities_not_voltages(self):
        """Compiled state machines stay valid when only threshold voltages change."""
        base = make_compiler((AI, OFF, OFF, OFF), {'Flex1': [2500, 1000]})
        other_voltages = make_compiler((AI, OFF, OFF, OFF), {'Flex1': [1, 2]})
        other_types = make_compiler((DI, OFF, OFF, OFF))
        assert other_voltages.hardware_hash == base.hardware_hash
        assert other_types.hardware_hash != base.hardware_hash

    def test_hardware_hash_follows_firmware(self):
        """Compiled state machines aren't shared between firmware versions."""
        newer = make_compiler(
            version=msgspec.structs.replace(VERSION, firmware=(24, 0))
        )
        assert newer.hardware_hash != make_compiler().hardware_hash


class TestValidateAndCompile:
    @pytest.mark.parametrize(
        ('n_states', 'last_target', 'expected'),
        [(255, '>exit', 255), (254, '>exit', 254), (254, '>back', 255)],
    )
    def test_largest_targets_fit_the_transition_matrix(
        self, n_states, last_target, expected
    ):
        """The exit and back targets of the largest machines fit a row's bytes."""
        fsm = StateMachine()
        for i in range(n_states - 1):
            fsm.add_state(f's{i}', 1, {'Tup': f's{i + 1}'})
        fsm.add_state(f's{n_states - 1}', 1, {'Tup': last_target})
        _, lookup = make_compiler().compile(state_machine=fsm)
        assert expected in lookup.state_transition_matrix[-1]

    def test_events_without_a_transition_keep_the_state(self):
        """A matrix row defaults to its own state, which EventThread relies on."""
        fsm = StateMachine()
        fsm.add_state('a', 1, {'Tup': 'b'})
        fsm.add_state('b', 1, {'Tup': '>exit'})
        _, lookup = make_compiler().compile(state_machine=fsm)
        rows = lookup.state_transition_matrix
        assert [row.count(i) for i, row in enumerate(rows)] == [254, 254]

    @pytest.mark.parametrize(
        ('timer', 'counter', 'action', 'message'),
        [
            (False, False, 'GlobalTimerTrig', 'no global timer has been set'),
            (False, True, 'GlobalTimerCancel', 'no global timer has been set'),
            (False, False, 'GlobalCounterReset', 'no global counter has been set'),
            (True, False, 'GlobalCounterReset', 'no global counter has been set'),
        ],
    )
    def test_action_needs_its_timer_or_counter(self, timer, counter, action, message):
        """Global timer and counter actions need one to be set, whichever others are."""
        fsm = StateMachine()
        if timer:
            fsm.set_global_timer(0, duration=1)
        if counter:
            fsm.set_global_counter(0, 'Port1_High', 1)
        fsm.add_state('a', 1, actions={action: 0})
        with pytest.raises(ValueError, match=message):
            make_compiler().validate(fsm)

    def test_unknown_timer_channel_is_rejected_with_a_suggestion(self):
        """A misspelled global timer channel is rejected, suggesting the closest."""
        fsm = StateMachine()
        fsm.set_global_timer(0, duration=1, channel='Valv1')
        fsm.add_state('a', 1)
        with pytest.raises(ValueError, match=r"'Valv1'.*did you mean 'Valve1'"):
            make_compiler().validate(fsm)

    def test_unknown_event_is_rejected_with_a_suggestion(self):
        """A misspelled event name is rejected, suggesting the closest valid one."""
        fsm = StateMachine()
        fsm.add_state('a', 1, {'Port1_Hgh': '>exit'})
        with pytest.raises(ValueError, match="did you mean 'Port1_High'"):
            make_compiler().validate(fsm)

    def test_unknown_operator_is_rejected_with_a_suggestion(self):
        """A misspelled operator is rejected, suggesting the closest valid one."""
        fsm = StateMachine()
        fsm.add_state('a', 1, {'Tup': '>exitt'})
        with pytest.raises(ValueError, match="did you mean '>exit'"):
            make_compiler().validate(fsm)

    def test_unknown_action_is_rejected_with_a_suggestion(self):
        """A misspelled action is rejected, suggesting the closest valid one."""
        fsm = StateMachine()
        fsm.add_state('a', 1, actions={'PWM_1': 255})
        with pytest.raises(ValueError, match="in state 'a' - did you mean 'PWM1'"):
            make_compiler().validate(fsm)

    def test_too_many_global_timers_are_rejected(self):
        """A global timer index beyond the hardware's limit is rejected."""
        fsm = StateMachine()
        fsm.add_state('a', 1, {'Tup': '>exit'})
        fsm.set_global_timer(index=16, duration=1)
        with pytest.raises(
            ValueError, match=r'Machine 2\+ r1.0 supports up to 16 Global'
        ):
            make_compiler().validate(fsm)

    def test_state_machine_too_large_for_the_bpod_is_rejected(self):
        """A state machine compiling to more bytes than the Bpod takes is rejected."""
        events = [f'Serial{i}_{j}' for i in (1, 2, 3) for j in range(15)]
        events += [f'GlobalTimer{i}_{e}' for i in range(16) for e in ('Start', 'End')]
        events += [f'Port{i}_{level}' for i in range(1, 6) for level in ('High', 'Low')]
        events += [f'GlobalCounter{i}_End' for i in range(8)]
        fsm = StateMachine()
        for i in range(255):
            fsm.add_state(f's{i}', 1, dict.fromkeys(events, f's{(i + 1) % 255}'))
        with pytest.raises(ValueError, match='compiles to 52,538 bytes'):
            make_compiler().compile(state_machine=fsm)

    def test_compiled_state_machines_are_shared_across_instances(self):
        """Compilers for the same hardware reuse each other's immutable results."""
        fsm = StateMachine()
        fsm.add_state('a', 0.5, {'Port1_High': '>exit'}, {'PWM1': 255})
        first, _ = make_compiler().compile(state_machine=fsm)
        second, _ = make_compiler().compile(state_machine=fsm)
        assert second is first
        assert isinstance(first, bytes)

    @pytest.mark.parametrize(
        ('setup', 'match'),
        [
            pytest.param(
                lambda fsm: fsm.add_state('a', 1, actions={'PWM1': 256}),
                'must not exceed 255',
                id='action value beyond one byte',
            ),
            pytest.param(
                lambda fsm: fsm.add_state('a', 1, actions={'SoftCode': 0}),
                r'must be 1 to 255 \(0 sends no softcode\)',
                id='softcode of none',
            ),
            pytest.param(
                lambda fsm: fsm.add_state('a', 1, actions={'AnalogThreshEnable': 17}),
                'has only 4 FlexIO channels',
                id='threshold bit for a fifth FlexIO channel',
            ),
            pytest.param(
                lambda fsm: (
                    fsm.set_global_counter(index=0, event='Port1_High', threshold=5),
                    fsm.add_state('a', 1, actions={'GlobalCounterReset': 1}),
                ),
                'Global Counter 1 has not been set',
                id='reset of an unset global counter',
            ),
            pytest.param(
                lambda fsm: (
                    fsm.set_global_timer(index=0, duration=1, onset_trigger=0b10),
                    fsm.add_state('a', 1),
                ),
                'Global Timer 1 has not been set',
                id='onset trigger of an unset global timer',
            ),
            pytest.param(
                lambda fsm: (
                    fsm.set_global_counter(index=0, event='Port1_Hgh', threshold=5),
                    fsm.add_state('a', 1),
                ),
                "did you mean 'Port1_High'",
                id='unknown global counter event',
            ),
            pytest.param(
                lambda fsm: (
                    fsm.set_condition(index=0, channel='Prot1', value=True),
                    fsm.add_state('a', 1),
                ),
                "did you mean 'Port1'",
                id='unknown condition channel',
            ),
        ],
    )
    def test_values_compilation_cannot_encode_are_rejected(self, setup, match):
        """Values the Bpod can't receive are rejected during validation."""
        fsm = StateMachine()
        setup(fsm)
        compiler = make_compiler((AI, OFF, OFF, OFF), {'Flex1': [2500, 2500]})
        with pytest.raises(ValueError, match=match):
            compiler.validate(fsm)

    @pytest.mark.parametrize('action', ['SoftCode', 'SoftCodeApp'])
    def test_softcodes_are_sent_as_is(self, action):
        """A softcode action's value is the byte the Bpod sends, like MATLAB's."""
        fsm = StateMachine()
        fsm.add_state('a', 1, actions={action: 7})
        compiler = make_compiler()
        fsm_bytes, _ = compiler.compile(state_machine=fsm)
        assert struct.pack('<HHH', 1, compiler.actions.index(action), 7) in fsm_bytes

    def test_global_timer_drives_a_softcode_channel_by_its_action_name(self):
        """A global timer's softcode channel is named like the action, as in MATLAB."""
        fsm = StateMachine()
        fsm.set_global_timer(0, duration=1, channel='SoftCode', value_on=5)
        fsm.add_state('a', 1, actions={'GlobalTimerTrig': 0})
        make_compiler().validate(fsm)

    def test_values_at_the_limits_are_compiled(self):
        """The valid counterparts of the rejected values compile."""
        fsm = StateMachine()
        fsm.set_global_timer(index=0, duration=1, onset_trigger=0b10)
        fsm.set_global_timer(index=1, duration=1)
        fsm.set_global_counter(index=0, event='Port1_High', threshold=5)
        fsm.set_condition(index=0, channel='Port1', value=True)
        actions = {
            'SoftCode': 255,
            'SoftCodeApp': 255,
            'AnalogThreshEnable': 1,
            'GlobalCounterReset': 0,
            'GlobalTimerTrig': 0,
        }
        fsm.add_state('a', 1, actions=actions)
        compiler = make_compiler((AI, OFF, OFF, OFF), {'Flex1': [2500, 2500]})
        compiler.compile(state_machine=fsm)


class TestCaching:
    def test_recurring_machine_outlives_one_offs(self):
        """A machine compiled again stays cached while one-off machines drop out."""

        def machine(timer: float) -> StateMachine:
            fsm = StateMachine()
            fsm.add_state('a', timer, {'Tup': '>exit'})
            return fsm

        compiler = make_compiler()
        recurring = machine(1)
        first = compiler.compile(state_machine=recurring)
        for i in range(compiler._compilation_cache.maxsize + 1):
            compiler.compile(state_machine=machine(2 + i))
            assert compiler.compile(state_machine=recurring) is first


class TestActionSections:
    @pytest.mark.parametrize('action', ['AnalogThreshEnable', 'AnalogThreshDisable'])
    def test_zero_threshold_bitmask_sends_nothing(self, action):
        """A zero bitmask arms or disarms nothing, like leaving the action out."""
        with_action, without = StateMachine(), StateMachine()
        with_action.add_state('a', 1, actions={action: 0})
        without.add_state('a', 1)
        compiler = make_compiler()
        compiled, _ = compiler.compile(state_machine=with_action)
        assert compiled == compiler.compile(state_machine=without)[0]

    @pytest.mark.parametrize(
        ('action', 'bitmasks'),
        [('GlobalTimerTrig', (0b100, 0)), ('GlobalTimerCancel', (0, 0b100))],
    )
    def test_timer_bitmasks(self, action, bitmasks):
        """Triggers come before cancels, with one bit per global timer."""
        fsm = StateMachine()
        fsm.set_global_timer(2, duration=1)
        fsm.add_state('a', 1, actions={action: 2})
        compiled, _ = make_compiler().compile(state_machine=fsm)
        # from the end: footer, 3 uint32 per timer, state timer, onset trigger per timer
        end = -(1 + 3 * 3 * 4 + 4 + 3 * 2)
        assert compiled[end - 4 : end] == struct.pack('<2H', *bitmasks)

    @pytest.mark.parametrize(
        ('firmware', 'n_footer_bytes', 'resets'),
        [((22, 0), 0, bytes((0, 2, 0))), ((23, 0), 1, bytes((1, 1, 2)))],
    )
    def test_counter_resets(self, firmware, n_footer_bytes, resets):
        """Firmware 23 lists the states that reset a counter; older gets every state."""
        fsm = StateMachine()
        fsm.set_global_counter(1, 'Port1_High', 5)
        fsm.add_state('a', 1, {'Tup': 'b'})
        fsm.add_state('b', 1, {'Tup': 'c'}, {'GlobalCounterReset': 1})
        fsm.add_state('c', 1, {'Tup': '>exit'})
        version = msgspec.structs.replace(VERSION, firmware=firmware)
        compiled, _ = make_compiler(version=version).compile(state_machine=fsm)
        # from the end: footer, counter thresholds, state timers, triggers and cancels,
        # the 2 counts of the threshold sections
        end = -(n_footer_bytes + 2 * 4 + 3 * 4 + 2 * 3 * 2 + 2)
        assert compiled[end - 3 : end] == resets
