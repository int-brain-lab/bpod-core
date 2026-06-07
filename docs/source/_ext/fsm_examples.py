"""
Sphinx extension that auto-generates example pages for state machines.

On ``builder-inited``, each ``examples/state_machines/*.py`` file is imported, executed,
and turned into an RST page under ``docs/source/state_machines/examples/``. Each page
contains the rendered SVG diagram and tabbed Python / JSON / YAML representations of the
state machine.
"""

import importlib.util
import inspect
import sys
from pathlib import Path


def _generate(app):
    if app.builder.name == 'doctest':
        return

    docs_source_path = Path(app.srcdir)
    project_root = docs_source_path.parents[1]

    # Create docs/source/state_machines/examples/ with one page per example
    examples_source_path = project_root / 'examples' / 'state_machines'
    examples_target_path = docs_source_path / 'state_machines' / 'examples'
    examples_target_path.mkdir(parents=True, exist_ok=True)
    example_files = sorted(examples_source_path.glob('*.py'), key=lambda f: f.name)

    for fn in example_files:
        # Import the example file as a module
        spec = importlib.util.spec_from_file_location(fn.stem, str(fn))
        module = importlib.util.module_from_spec(spec)
        sys.modules[fn.stem] = module
        spec.loader.exec_module(module)

        # Extract title and description from docstring
        doc = inspect.getdoc(module)
        page_title = doc.splitlines()[0].strip('."')
        description = '\n'.join(doc.splitlines()[1:]).strip('"')

        # Generate state machine diagram and save as SVG
        state_machine = module.fsm
        fn_light = examples_target_path / (fn.stem + '__light.svg')
        fn_dark = examples_target_path / (fn.stem + '__dark.svg')
        digraph_light = state_machine.to_digraph(**app.config.fsm_light_colors)
        digraph_dark = state_machine.to_digraph(**app.config.fsm_dark_colors)
        digraph_light.render(outfile=fn_light, cleanup=True, quiet=True)
        digraph_dark.render(outfile=fn_dark, cleanup=True, quiet=True)

        # Generate JSON
        json_lines = state_machine.to_json(indent=2).splitlines()
        json_lines = [' ' * 7 + line for line in json_lines]

        # Generate YAML
        yaml_lines = state_machine.to_yaml().splitlines()
        yaml_lines = [' ' * 7 + line for line in yaml_lines]

        page_path = examples_target_path.joinpath(f'{fn.stem}.rst')
        page_lines = [
            page_title,
            '-' * len(page_title),
            '',
            description,
            '',
            f'.. image:: {fn_light.name}',
            '   :align: center',
            '   :class: light-only',
            '',
            f'.. image:: {fn_dark.name}',
            '   :align: center',
            '   :class: dark-only',
            '',
            '.. tab-set::',
            '',
            '   .. tab-item:: Python',
            '',
            f'    .. literalinclude:: ../../../../examples/state_machines/{fn.name}',
            '       :language: python',
            '       :start-at: from bpod_core.',
            '       :linenos:',
            '',
            '   .. tab-item:: JSON',
            '',
            '    .. code-block:: json',
            '       :linenos:',
            '',
            *json_lines,
            '',
            '   .. tab-item:: YAML',
            '',
            '    .. code-block:: yaml',
            '       :linenos:',
            '',
            *yaml_lines,
            '',
        ]
        with page_path.open('w', encoding='utf-8') as pf:
            pf.write('\n'.join(page_lines) + '\n')


def setup(app):
    app.add_config_value(
        'fsm_light_colors',
        {
            'color_stroke': 'black',
            'color_fill': 'white',
            'color_highlight': 'lightblue',
            'color_back': 'red',
        },
        'env',
    )
    app.add_config_value(
        'fsm_dark_colors',
        {
            'color_stroke': 'white',
            'color_fill': 'black',
            'color_highlight': 'darkred',
            'color_back': 'red',
        },
        'env',
    )
    app.connect('builder-inited', _generate)
    return {
        'version': '0.1',
        'parallel_read_safe': False,
        'parallel_write_safe': True,
    }
