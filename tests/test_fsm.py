import pytest
from pydantic import ValidationError

from bpod_core.fsm import State, StateMachine


def test_state_creation():
    state = State(
        timer=5.0,
        state_change_conditions={'condition1': 'exit'},
        output_actions={'action1': 255},
        comment='This is a test state',
    )
    assert state.timer == 5.0
    assert state.state_change_conditions == {'condition1': 'exit'}
    assert state.output_actions == {'action1': 255}
    assert state.comment == 'This is a test state'


def test_state_machine_creation():
    sm = StateMachine(name='Test State Machine')
    assert sm.name == 'Test State Machine'
    assert isinstance(sm.states, dict)
    assert len(sm.states) == 0


def test_add_state():
    sm = StateMachine(name='Test State Machine')
    sm.add_state(
        name='state1',
        timer=2.0,
        state_change_conditions={'condition1': 'state2'},
        output_actions={'action1': 255},
        comment='First state',
    )
    assert len(sm.states) == 1
    assert 'state1' in sm.states
    assert sm.states['state1'].timer == 2.0
    assert sm.states['state1'].state_change_conditions == {'condition1': 'state2'}
    assert sm.states['state1'].output_actions == {'action1': 255}
    assert sm.states['state1'].comment == 'First state'


def test_add_duplicate_state():
    sm = StateMachine(name='Test State Machine')
    sm.add_state(name='state1')
    with pytest.raises(ValueError, match='.*state1.* already registered'):
        sm.add_state(name='state1')


def test_digraph_empty_state_machine():
    sm = StateMachine(name='Empty State Machine')
    assert sm.digraph.name == 'Empty State Machine'
    assert len(sm.digraph.body) == 0


def test_digraph_with_states():
    sm = StateMachine(name='Test State Machine')
    sm.add_state(name='state1', state_change_conditions={'condition1': 'state2'})
    sm.add_state(name='state2', state_change_conditions={'condition2': 'exit'})
    assert len(sm.digraph.body) > 0
    assert 'state1' in sm.digraph.source
    assert 'state2' in sm.digraph.source
    assert 'exit' in sm.digraph.source


def test_invalid_state_name():
    sm = StateMachine(name='Test State Machine')
    with pytest.raises(ValidationError):
        sm.add_state(name='exit')


def test_invalid_timer():
    sm = StateMachine(name='Test State Machine')
    with pytest.raises(ValidationError):
        sm.add_state(name='state1', timer=-1.0)


if __name__ == '__main__':
    pytest.main()
