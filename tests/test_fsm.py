from pathlib import Path

import msgspec
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


def test_invalid_state_name():
    sm = StateMachine(name='Test State Machine')
    with pytest.raises(ValidationError):
        sm.add_state(name='exit')


def test_invalid_timer():
    sm = StateMachine(name='Test State Machine')
    with pytest.raises(ValidationError):
        sm.add_state(name='state1', timer=-1.0)


def test_to_digraph_empty_state_machine():
    sm = StateMachine(name='Empty State Machine')
    digraph = sm.to_digraph()
    assert digraph.name == 'Empty State Machine'
    assert len(digraph.body) == 0


@pytest.fixture
def state_machine():
    fsm = StateMachine(name='Test State Machine')
    fsm.add_state(
        name='state1',
        timer=2.0,
        state_change_conditions={'tup': 'state2'},
        output_actions={'action1': 255},
        comment='First state',
    )
    fsm.add_state(
        name='state2',
        state_change_conditions={'tup': 'exit', 'condition': '>back'},
        output_actions={'action2': 128},
        comment='Second state',
    )
    return fsm


def test_to_digraph_with_states(state_machine):
    digraph = state_machine.to_digraph()
    assert len(digraph.body) > 0
    assert 'state1' in digraph.source
    assert 'state2' in digraph.source
    assert 'exit' in digraph.source


def test_to_dict(state_machine):
    sm_dict = state_machine.to_dict()
    assert sm_dict['name'] == 'Test State Machine'
    assert 'state1' in sm_dict['states']
    assert 'state2' in sm_dict['states']
    assert sm_dict['states']['state1']['timer'] == 2.0
    assert sm_dict['states']['state1']['state_change_conditions'] == {'tup': 'state2'}
    assert sm_dict['states']['state1']['output_actions'] == {'action1': 255}
    assert sm_dict['states']['state1']['comment'] == 'First state'
    assert sm_dict['states']['state2']['output_actions'] == {'action2': 128}
    assert sm_dict['states']['state2']['comment'] == 'Second state'
    assert 'timer' not in sm_dict['states']['state2']  # Default value should be omitted


def test_to_json(state_machine):
    json_str = state_machine.to_json()
    assert '"name": "Test State Machine"' in json_str
    assert '"state1"' in json_str
    assert '"timer": 2.0' in json_str
    assert '"state_change_conditions": {' in json_str
    assert '"tup": "state2"' in json_str
    assert '"output_actions": {' in json_str
    assert '"action1": 255' in json_str
    assert '"comment": "First state"' in json_str
    assert '"state2"' in json_str
    assert '"state_change_conditions": {' in json_str
    assert '"tup": "exit"' in json_str
    assert '"output_actions": {' in json_str
    assert '"action2": 128' in json_str
    assert '"comment": "Second state"' in json_str
    assert '\n' not in json_str  # No newlines when `indent` is None


def test_to_json_indent(state_machine):
    json_str = state_machine.to_json(indent=2)
    assert '\n' in json_str


def test_to_json_compact(state_machine):
    json_str = state_machine.to_json(compact=True)
    assert '\n' not in json_str  # No newlines in compact mode
    assert ': ' not in json_str  # No spaces after colons in compact mode
    assert ', ' not in json_str  # No spaces after commas in compact mode


def test_from_dict():
    dictionary = {}
    fsm = StateMachine.from_dict(dictionary)
    assert isinstance(fsm, StateMachine)
    assert dictionary == fsm.to_dict()  # roundtrip


def test_from_json():
    json_str = '{}'
    fsm = StateMachine.from_json(json_str)
    assert isinstance(fsm, StateMachine)
    assert json_str == fsm.to_json()  # roundtrip


def test_schema():
    """Test that the schema file exists and is up to date."""
    schema_path = Path(__file__).parents[1].joinpath('schema/statemachine.json')
    assert schema_path.exists(), 'schema file does not exist'
    with schema_path.open('r') as f:
        data = f.read()
    schema_from_file = msgspec.json.decode(data)
    schema_from_struct = msgspec.json.schema(StateMachine)
    assert schema_from_file == schema_from_struct, 'schema file is out of date'


def test_to_file_json_write_and_overwrite(tmp_path, state_machine):
    # Write JSON file
    path = tmp_path / 'machine.json'
    state_machine.to_file(path)
    assert path.exists()
    content = path.read_text()
    assert content == state_machine.to_json(indent=2)
    with pytest.raises(FileExistsError):
        state_machine.to_file(path)
    state_machine.to_file(path, overwrite=True)
    assert path.read_text() == state_machine.to_json(indent=2)


def test_to_file_unsupported_extension(tmp_path, state_machine):
    path = tmp_path / 'machine.txt'
    with pytest.raises(ValueError, match='Unsupported file extension'):
        state_machine.to_file(path)


def test_to_file_missing_directory_raises(tmp_path, state_machine):
    path = tmp_path / 'missing_dir' / 'machine.json'
    assert not path.parent.exists()
    with pytest.raises(FileNotFoundError):
        state_machine.to_file(path)


def test_to_file_graph_formats_call_render(tmp_path, state_machine, mocker):
    # Prepare a dummy object with a render method to capture calls
    render_mock = mocker.Mock()

    class DummyGraph:
        def render(self, **kwargs):
            return render_mock(**kwargs)

    # Monkeypatch to_digraph to return our dummy graph
    mocker.patch.object(StateMachine, 'to_digraph', return_value=DummyGraph())

    # Parametrize manually for three formats
    cases = [
        ('diagram.pdf', 'pdf'),
        ('diagram.svg', 'svg'),
        ('diagram.png', 'png'),
    ]
    for filename, expected_format in cases:
        render_mock.reset_mock()
        out = tmp_path / filename
        state_machine.to_file(out)
        assert render_mock.call_count == 1
        kwargs = render_mock.call_args.kwargs
        assert kwargs['filename'] == out.stem
        assert kwargs['directory'] == out.parent
        assert kwargs['cleanup'] is True
        assert kwargs['quiet'] is True
        assert kwargs['format'] == expected_format
