import importlib
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

from bpod_core.fsm import StateMachine


class TestExampleFSM:
    @pytest.fixture
    def fsm_examples(self):
        example_dir = Path(__file__).parent.parent / 'examples'
        assert example_dir.is_dir()

        def example_generator() -> Generator[StateMachine, None, None]:
            for py_file in example_dir.glob('*.py'):
                module_name = py_file.stem
                spec = importlib.util.spec_from_file_location(module_name, str(py_file))
                assert spec is not None, f'Failed to load example {py_file}'
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                assert spec.loader is not None, f'Failed to load example {py_file}'
                spec.loader.exec_module(module)
                module.fsm.name = module_name
                yield module.fsm

        yield example_generator()

    def test_validate_state_machines(self, mock_bpod_25, fsm_examples):
        for fsm in fsm_examples:
            try:
                mock_bpod_25.validate_state_machine(fsm)
            except ValueError as e:
                pytest.fail(f'Failed to validate state machine {fsm.name}: {e}')
