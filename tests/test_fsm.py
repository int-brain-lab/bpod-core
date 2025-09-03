from collections.abc import MutableMapping
from pathlib import Path

import msgspec
import pytest
from pydantic import ValidationError, create_model

from bpod_core.fsm import State, StateMachine, dec_hook, enc_hook


class TestEncDecHooks:
    @pytest.fixture
    def model(self):
        model = create_model('Model', x=(int, ...), y=(str, 'default'), z=(bool, True))
        return model(x=1)

    def test_enc_hook_with_pydantic(self, model):
        """Encode a Pydantic model to dict."""
        model_instance = model.model_construct(x=1, y='test')
        result = enc_hook(model_instance)
        assert result == {'x': 1, 'y': 'test'}

    def test_enc_hook_with_non_pydantic(self):
        """Encode a non-Pydantic object to dict."""
        with pytest.raises(NotImplementedError):
            enc_hook({'id': 1})

    def test_dec_hook_with_pydantic(self, model):
        """Decode a dict to a Pydantic model."""
        data = {'x': 2, 'y': 'Bob'}
        model_instance = dec_hook(type(model), data)
        assert isinstance(model_instance, type(model))
        assert model_instance.x == 2
        assert model_instance.y == 'Bob'
        assert model_instance.z is True  # default applied

    def test_dec_hook_with_non_pydantic(self):
        """Decode a non-Pydantic object to dict."""
        with pytest.raises(NotImplementedError):
            dec_hook(dict, {'x': 1})

    def test_round_trip(self, model):
        """Encode a Pydantic model to dict and decode back."""
        original = model.model_construct(x=3, y='Carol', z=False)
        encoded = enc_hook(original)
        decoded = dec_hook(type(model), encoded)
        assert decoded == original


class TestState:
    def test_state_creation(self):
        """Create a State and verify all fields are set correctly."""
        state = State(
            timer=5.0,
            transitions={'condition1': 'exit'},
            actions={'action1': 255},
            comment='This is a test state',
        )
        assert state.timer == 5.0
        assert state.transitions == {'condition1': 'exit'}
        assert state.actions == {'action1': 255}
        assert state.comment == 'This is a test state'


class TestStateMachineBasic:
    def test_state_machine_creation(self):
        """Construct an empty StateMachine and check initial state."""
        sm = StateMachine(name='Test State Machine')
        assert sm.name == 'Test State Machine'
        assert isinstance(sm.states, MutableMapping)
        assert len(sm.states) == 0

    def test_add_state(self):
        """Add a state and verify it appears with expected values."""
        sm = StateMachine(name='Test State Machine')
        sm.add_state(
            name='state1',
            timer=2.0,
            transitions={'condition1': 'state2'},
            actions={'action1': 255},
            comment='First state',
        )
        assert len(sm.states) == 1
        assert 'state1' in sm.states
        assert sm.states['state1'].timer == 2.0
        assert sm.states['state1'].transitions == {'condition1': 'state2'}
        assert sm.states['state1'].actions == {'action1': 255}
        assert sm.states['state1'].comment == 'First state'

    def test_add_duplicate_state(self):
        """Adding a duplicate state name should raise ValueError."""
        sm = StateMachine(name='Test State Machine')
        sm.add_state(name='state1')
        with pytest.raises(ValueError, match='.*state1.* already registered'):
            sm.add_state(name='state1')

    def test_invalid_state_name(self):
        """Using reserved state name 'exit' should fail validation."""
        sm = StateMachine(name='Test State Machine')
        with pytest.raises(ValidationError):
            sm.add_state(name='exit')
        with pytest.raises(ValidationError):
            sm.add_state(name='>exit')

    def test_invalid_timer(self):
        """Negative timer values should raise validation errors."""
        sm = StateMachine(name='Test State Machine')
        with pytest.raises(ValidationError):
            sm.add_state(name='state1', timer=-1.0)

    def test_empty_repr_default_name(self):
        """Default name should be omitted from __repr__."""
        fsm = StateMachine()
        expected = (
            'StateMachine(states: 0, global_timers: 0, '
            'global_counters: 0, conditions: 0)'
        )
        assert repr(fsm) == expected

    def test_empty_repr_custom_name(self):
        """Custom name should be included in __repr__."""
        fsm = StateMachine(name='My FSM')
        expected = (
            "StateMachine(name='My FSM', states: 0, global_timers: 0, "
            'global_counters: 0, conditions: 0)'
        )
        assert repr(fsm) == expected

    def test_repr_counts_update(self):
        fsm = StateMachine()
        fsm.add_state('a', 1, {'Tup': 'b'}, {'PWM1': 255})
        fsm.add_state('b', 1, {'Tup': 'a'})
        fsm.set_global_timer(1, 0.5)
        fsm.set_global_counter(0, 'Port1_High', 3)
        fsm.set_condition(2, 'Port2', 1)
        expected = (
            'StateMachine(states: 2, global_timers: 1, '
            'global_counters: 1, conditions: 1)'
        )
        assert repr(fsm) == expected


class TestToDigraph:
    def test_to_digraph_empty_state_machine(self):
        """to_digraph on an empty machine yields an empty body graph."""
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
        transitions={'tup': 'state2'},
        actions={'action1': 255},
        comment='First state',
    )
    fsm.add_state(
        name='state2',
        transitions={'tup': 'exit', 'condition': '>back'},
        actions={'action2': 128},
        comment='Second state',
    )
    return fsm


class TestToDigraphWithStates:
    def test_to_digraph_with_states(self, state_machine):
        """Graph contains nodes and edges for defined states and transitions."""
        digraph = state_machine.to_digraph()
        assert len(digraph.body) > 0
        assert 'state1' in digraph.source
        assert 'state2' in digraph.source
        assert 'exit' in digraph.source


class TestSerialization:
    def test_to_dict(self, state_machine):
        """Convert to dict and verify structure and omitted defaults."""
        sm = state_machine.to_dict()
        assert sm['name'] == 'Test State Machine'
        assert 'state1' in sm['states']
        assert 'state2' in sm['states']
        assert sm['states']['state1']['timer'] == 2.0
        assert sm['states']['state1']['transitions'] == {'tup': 'state2'}
        assert sm['states']['state1']['actions'] == {'action1': 255}
        assert sm['states']['state1']['comment'] == 'First state'
        assert sm['states']['state2']['actions'] == {'action2': 128}
        assert sm['states']['state2']['comment'] == 'Second state'
        assert 'timer' not in sm['states']['state2']  # Default value should be omitted

    def test_to_json(self, state_machine):
        """Serialize to compact JSON by default (no newlines)."""
        json_str = state_machine.to_json()
        assert '"name":"Test State Machine"' in json_str
        assert '"state1"' in json_str
        assert '"timer":2.0' in json_str
        assert '"transitions":{' in json_str
        assert '"tup":"state2"' in json_str
        assert '"actions":{' in json_str
        assert '"action1":255' in json_str
        assert '"comment":"First state"' in json_str
        assert '"state2"' in json_str
        assert '"transitions":{' in json_str
        assert '"tup":"exit"' in json_str
        assert '"actions":{' in json_str
        assert '"action2":128' in json_str
        assert '"comment":"Second state"' in json_str
        assert '\n' not in json_str  # No newlines when `indent` is None
        assert ': ' not in json_str  # No spaces when `indent` is None
        assert ', ' not in json_str  # No spaces when `indent` is None

    def test_to_json_indent(self, state_machine):
        """Serialize to pretty-printed JSON when indent is provided."""
        json_str = state_machine.to_json(indent=2)
        assert '\n' in json_str


class TestFromConstructors:
    def test_from_dict(self):
        """Construct from dict and compare round-trip via to_dict."""
        dictionary = {}
        fsm = StateMachine.from_dict(dictionary)
        assert isinstance(fsm, StateMachine)
        assert dictionary == fsm.to_dict()  # roundtrip

    def test_from_json(self):
        """Construct from JSON string and compare round-trip via to_json."""
        json_str = '{}'
        fsm = StateMachine.from_json(json_str)
        assert isinstance(fsm, StateMachine)
        assert json_str == fsm.to_json()  # roundtrip

    def test_from_invalid_json_raises(self, tmp_path):
        """Invalid JSON should raise ValidationError in from_json."""
        json_str = 'not valid json'
        with pytest.raises(ValidationError):
            StateMachine.from_json(json_str)


class TestSchema:
    def test_schema(self):
        """Test that the schema file exists and is up to date."""
        schema_path = Path(__file__).parents[1].joinpath('schema/statemachine.json')
        assert schema_path.exists(), 'schema file does not exist'
        with schema_path.open('r') as f:
            data = f.read()
        schema_from_file = msgspec.json.decode(data)
        schema_from_struct = StateMachine.model_json_schema()
        assert schema_from_file == schema_from_struct, 'schema file is out of date'


class TestFromFile:
    def test_from_file_roundtrip_json(self, tmp_path, state_machine):
        """from_file loads a JSON file and matches original machine."""
        # Write JSON to file
        path = tmp_path / 'machine.json'
        path.write_text(state_machine.to_json(indent=2), encoding='utf-8')

        # Load via Path
        fsm = StateMachine.from_file(path)
        assert isinstance(fsm, StateMachine)
        assert fsm.to_dict() == state_machine.to_dict()

        # Load via string path
        fsm2 = StateMachine.from_file(str(path))
        assert isinstance(fsm2, StateMachine)
        assert fsm2.to_dict() == state_machine.to_dict()

    def test_from_file_roundtrip_yaml(self, tmp_path, state_machine):
        """from_file loads a YAML file and matches original machine."""
        # Write JSON to file
        path = tmp_path / 'machine.yaml'
        path.write_text(state_machine.to_yaml(indent=2), encoding='utf-8')

        # Load via Path
        fsm = StateMachine.from_file(path)
        assert isinstance(fsm, StateMachine)
        assert fsm.to_dict() == state_machine.to_dict()

        # Load via string path
        fsm2 = StateMachine.from_file(str(path))
        assert isinstance(fsm2, StateMachine)
        assert fsm2.to_dict() == state_machine.to_dict()

    def test_from_file_missing_raises(self, tmp_path):
        """from_file should raise FileNotFoundError for missing files."""
        missing = tmp_path / 'missing.json'
        assert not missing.exists()
        with pytest.raises(FileNotFoundError):
            StateMachine.from_file(missing)

    def test_from_file_wrong_extension(self, tmp_path):
        """Non-.json extension should raise ValueError in from_file."""
        # Create a non-json file that exists but has wrong extension
        path = tmp_path / 'machine.txt'
        path.write_text('{}', encoding='utf-8')
        with pytest.raises(ValueError, match='Unsupported file extension'):
            StateMachine.from_file(path)

    def test_from_file_invalid_json_raises(self, tmp_path):
        """Invalid JSON content on disk should raise msgspec.DecodeError."""
        bad = tmp_path / 'bad.json'
        bad.write_text('not valid json', encoding='utf-8')
        with pytest.raises(ValidationError):
            StateMachine.from_file(bad)


class TestToFile:
    def test_to_file_json_write_and_overwrite(self, tmp_path, state_machine):
        """Write JSON file, prevent overwrite, allow overwrite=True."""
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

    def test_to_file_yaml_write_and_overwrite(self, tmp_path, state_machine):
        """Write JSON file, prevent overwrite, allow overwrite=True."""
        # Write JSON file
        path = tmp_path / 'machine.yaml'
        state_machine.to_file(path)
        assert path.exists()
        content = path.read_text()
        assert content == state_machine.to_yaml()
        with pytest.raises(FileExistsError):
            state_machine.to_file(path)
        state_machine.to_file(path, overwrite=True)
        assert path.read_text() == state_machine.to_yaml()

    def test_to_file_unsupported_extension(self, tmp_path, state_machine):
        """Unsupported extension should raise ValueError in to_file."""
        path = tmp_path / 'machine.txt'
        with pytest.raises(ValueError, match='Unsupported file extension'):
            state_machine.to_file(path)

    def test_to_file_missing_directory_raises(self, tmp_path, state_machine):
        """Writing into a non-existent directory should raise FileNotFoundError."""
        path = tmp_path / 'missing_dir' / 'machine.json'
        assert not path.parent.exists()
        with pytest.raises(FileNotFoundError):
            state_machine.to_file(path)

    def test_to_file_create_directory_json(self, tmp_path, state_machine):
        """create_directory=True should create parent dir and write JSON."""
        path = tmp_path / 'new_dir' / 'machine.json'
        assert not path.parent.exists()
        state_machine.to_file(path, create_directory=True)
        assert path.exists()
        assert path.read_text() == state_machine.to_json(indent=2)

    def test_to_file_graph_formats_call_render(self, tmp_path, state_machine, mocker):
        """Graph formats (.pdf/.svg/.png) should call render with expected args."""
        # Prepare a dummy object with a render method to capture calls
        render_mock = mocker.Mock()

        class DummyGraph:
            def render(self, **kwargs):
                return render_mock(**kwargs)

        # Monkeypatch to_digraph to return our dummy graph
        mocker.patch.object(StateMachine, 'to_digraph', return_value=DummyGraph())

        # Parametrize manually
        cases = [
            ('diagram.pdf', 'pdf'),
            ('diagram.svg', 'svg'),
            ('diagram.png', 'png'),
            ('diagram.PDF', 'pdf'),
            ('diagram.SVG', 'svg'),
            ('diagram.PNG', 'png'),
        ]
        for filename, expected_format in cases:
            render_mock.reset_mock()
            out = tmp_path / 'nested' / filename
            # ensure parent does not exist
            if out.parent.exists():
                # unlikely, but clean up to assert creation behavior
                pass
            state_machine.to_file(out, create_directory=True)
            assert out.parent.exists()
            assert render_mock.call_count == 1
            kwargs = render_mock.call_args.kwargs
            assert kwargs['outfile'] == out
            assert kwargs['cleanup'] is True
            assert kwargs['quiet'] is True
            assert kwargs['format'] == expected_format


class TestValidation:
    def test_validate_assignment(self, state_machine):
        """Validate assignment of state machine attributes."""
        with pytest.raises(ValidationError):
            state_machine.states['state1'].timer = -1

    def test_validate_call(self, state_machine):
        """Validate call of state machine methods."""
        with pytest.raises(ValidationError):
            state_machine.add_state('state3', timer=-1)
