"""Tests for the state machine compiler, built without a (mocked) Bpod."""

from collections.abc import Mapping, Sequence

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
) -> StateMachineCompiler:
    names = [f'Flex{i + 1}' for i in range(len(flexio_channel_types))]
    return StateMachineCompiler(
        HARDWARE,
        VERSION,
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
        lookup = compiler.event_lookup.filter(
            pl.col('event').cast(pl.String).str.starts_with('Flex1_Trig')
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


class TestValidateAndCompile:
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

    def test_too_many_global_timers_are_rejected(self):
        """A global timer index beyond the hardware's limit is rejected."""
        fsm = StateMachine()
        fsm.add_state('a', 1, {'Tup': '>exit'})
        fsm.set_global_timer(index=16, duration=1)
        with pytest.raises(
            ValueError, match=r'Machine 2\+ r1.0 supports up to 16 Global'
        ):
            make_compiler().validate(fsm)

    def test_compiled_state_machines_are_shared_across_instances(self):
        """Compilers for the same hardware reuse each other's results."""
        fsm = StateMachine()
        fsm.add_state('a', 0.5, {'Port1_High': '>exit'}, {'PWM1': 255})
        first, _ = make_compiler().compile(state_machine=fsm)
        second, _ = make_compiler().compile(state_machine=fsm)
        assert second is first

    @pytest.mark.parametrize(
        ('setup', 'match'),
        [
            pytest.param(
                lambda fsm: fsm.add_state('a', 1, actions={'PWM1': 256}),
                'must not exceed 255',
                id='action value beyond one byte',
            ),
            pytest.param(
                lambda fsm: fsm.add_state('a', 1, actions={'SoftCode': 255}),
                'must not exceed 254',
                id='softcode beyond one byte',
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

    def test_values_at_the_limits_are_compiled(self):
        """The valid counterparts of the rejected values compile."""
        fsm = StateMachine()
        fsm.set_global_timer(index=0, duration=1, onset_trigger=0b10)
        fsm.set_global_timer(index=1, duration=1)
        fsm.set_global_counter(index=0, event='Port1_High', threshold=5)
        fsm.set_condition(index=0, channel='Port1', value=True)
        actions = {
            'SoftCode': 254,
            'AnalogThreshEnable': 1,
            'GlobalCounterReset': 0,
            'GlobalTimerTrig': 0,
        }
        fsm.add_state('a', 1, actions=actions)
        compiler = make_compiler((AI, OFF, OFF, OFF), {'Flex1': [2500, 2500]})
        compiler.compile(state_machine=fsm)
