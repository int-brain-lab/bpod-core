import importlib.util
import inspect
import json
import sys
from datetime import date
from pathlib import Path

import msgspec

project_root = Path(__file__).parents[2].resolve()
docs_source_path = Path(__file__).parent.resolve()
sys.path.insert(0, project_root)

from bpod_core import __version__, fsm  # noqa: E402

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = 'bpod-core'
copyright = f'{date.today().year}, International Brain Laboratory'  # noqa: A001
author = 'International Brain Laboratory'
release = '.'.join(__version__.split('.')[:3])
version = '.'.join(__version__.split('.')[:3])
rst_prolog = f"""
.. |version_code| replace:: ``{version}``
"""

# -- dump json schema --------------------------------------------------------
schema_root = project_root / 'schema'
schema_root.mkdir(exist_ok=True)
with schema_root.joinpath('statemachine.json').open('w') as f:
    schema = msgspec.json.schema(fsm.StateMachine)
    json.dump(schema, f, indent=2)
    f.write('\n')  # add final newline

# -- Generate Examples pages --------------------------------------------------
# Create docs/source/examples/ with one page per example and an index.rst
examples_source_path = project_root / 'examples'
examples_target_path = docs_source_path / 'examples'
examples_target_path.mkdir(exist_ok=True)
example_files = [f for f in examples_source_path.glob('*.py')]

examples_target_path.mkdir(exist_ok=True)

# Write an index.rst with a toctree listing all example pages
index_lines = [
    'Example State Machines',
    '======================',
    '',
    'The following examples illustrate usage and features of the '
    ':class:`~bpod_core.fsm.StateMachine` class.',
    '',
    '.. toctree::',
    '   :maxdepth: 1',
    '',
]

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
    image_file = examples_target_path / fn.with_suffix('.svg').name
    state_machine.to_file(image_file)

    # Generate JSON
    json = state_machine.to_json(indent=2).splitlines()
    json = [' ' * 7 + line for line in json]

    page_path = examples_target_path.joinpath(f'{fn.stem}.rst')
    page_lines = [
        page_title,
        '-' * len(page_title),
        '',
        description,
        '',
        f'.. image:: {image_file.name}',
        '   :align: center',
        '',
        '.. tab-set::',
        '',
        '   .. tab-item:: Python',
        '',
        f'    .. literalinclude:: ../../../examples/{fn.name}',
        '       :language: python',
        '       :start-at: from bpod_core.',
        '',
        '   .. tab-item:: JSON',
        '',
        '    .. code-block:: json',
        '',
        *json,
        '',
    ]
    with page_path.open('w', encoding='utf-8') as pf:
        pf.write('\n'.join(page_lines) + '\n')

    # Add page to toctree
    index_lines.append(f'   {fn.stem}')

index_out = examples_target_path / 'index.rst'
with index_out.open('w', encoding='utf-8') as f:
    f.write('\n'.join(index_lines) + '\n')

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    'myst_parser',
    'sphinx.ext.intersphinx',
    'sphinx.ext.napoleon',
    'sphinx.ext.autodoc',
    'sphinx.ext.autosummary',
    'sphinx_copybutton',
    'sphinx_design',
    'sphinx_autodoc_typehints',
    'sphinx-jsonschema',
]
source_suffix = ['.rst', '.md']

templates_path = ['_templates']
exclude_patterns = []

typehints_defaults = None
typehints_use_rtype = False
typehints_use_signature = False
typehints_use_signature_return = False

intersphinx_mapping = {
    'python': ('https://docs.python.org/3.10/', None),
    'numpy': ('http://docs.scipy.org/doc/numpy/', None),
    'pandas': ('https://pandas.pydata.org/docs/', None),
    'serial': ('https://pyserial.readthedocs.io/en/stable/', None),
    'graphviz': ('https://graphviz.readthedocs.io/en/stable/', None),
}

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = 'sphinx_rtd_theme'
html_theme_options = {
    'collapse_navigation': True,
    'sticky_navigation': True,
    'navigation_depth': 4,
    'includehidden': True,
    'titles_only': False,
    'display_version': True,
}

# -- Settings for automatic API generation -----------------------------------
autodoc_mock_imports = ['_typeshed']
autodoc_class_signature = 'separated'  # 'mixed', 'separated'
autodoc_member_order = 'groupwise'  # 'alphabetical', 'groupwise', 'bysource'
autodoc_inherit_docstrings = False
autodoc_typehints = 'description'  # 'description', 'signature', 'none', 'both'
autodoc_typehints_description_target = 'all'  # 'all', 'documented', 'documented_params'
autodoc_typehints_format = 'short'  # 'fully-qualified', 'short'

autosummary_generate = True
autosummary_imported_members = False

napoleon_google_docstring = False
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = False
napoleon_include_private_with_doc = False
napoleon_include_special_with_doc = False
napoleon_use_admonition_for_examples = True
napoleon_use_admonition_for_notes = True
napoleon_use_admonition_for_references = True
napoleon_use_ivar = True
napoleon_use_param = True
napoleon_use_rtype = True
napoleon_use_keyword = True
napoleon_preprocess_types = True
napoleon_type_aliases = None
napoleon_attr_annotations = True
