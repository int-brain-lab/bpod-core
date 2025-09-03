import importlib.util
import inspect
import json
import sys
from datetime import date
from pathlib import Path

project_root = Path(__file__).parents[2].resolve()
docs_source_path = Path(__file__).parent.resolve()
sys.path.insert(0, project_root)
sys.path.insert(0, str(docs_source_path / '_ext'))

from bpod_core import __version__  # noqa: E402
from bpod_core.fsm import StateMachine  # noqa: E402


def generate_fsm_examples(app):
    if app.builder.name == 'doctest':
        return

    # Create docs/source/state_machines/examples/ with one page per example
    examples_source_path = project_root / 'examples'
    examples_target_path = docs_source_path / 'state_machines' / 'examples'
    examples_target_path.mkdir(parents=True, exist_ok=True)
    example_files = sorted(
        [f for f in examples_source_path.glob('*.py')], key=lambda f: f.name
    )

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
        state_machine.to_file(image_file, overwrite=True)

        # Generate JSON
        json = state_machine.to_json(indent=2).splitlines()
        json = [' ' * 7 + line for line in json]

        # Generate YAML
        yaml = state_machine.to_yaml(indent=2).splitlines()
        yaml = [' ' * 7 + line for line in yaml]

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
            f'    .. literalinclude:: ../../../../examples/{fn.name}',
            '       :language: python',
            '       :start-at: from bpod_core.',
            '',
            '   .. tab-item:: JSON',
            '',
            '    .. code-block:: json',
            '',
            *json,
            '',
            '   .. tab-item:: YAML',
            '',
            '    .. code-block:: yaml',
            '',
            *yaml,
            '',
        ]
        with page_path.open('w', encoding='utf-8') as pf:
            pf.write('\n'.join(page_lines) + '\n')


def setup(app):
    app.connect('builder-inited', generate_fsm_examples)


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

html_context = {
    'display_github': False,
    'github_user': 'int-brain-lab',
    'github_repo': 'bpod-core',
    'github_version': 'master',
    'conf_py_path': '/docs/source/',
}

# -- dump json schema --------------------------------------------------------
schema_root = project_root / 'schema'
schema_root.mkdir(exist_ok=True)
with schema_root.joinpath('statemachine.json').open('w') as f:
    schema = StateMachine.model_json_schema()
    json.dump(schema, f, indent=2)
    f.write('\n')  # add final newline

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    'myst_parser',
    'sphinx.ext.intersphinx',
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx_autodoc_typehints',
    'sphinx.ext.autosummary',
    'sphinx.ext.graphviz',
    'sphinx.ext.doctest',
    'sphinx_github_style',
    'sphinx_copybutton',
    'sphinx_design',
    'sphinx-jsonschema',
    'sphinx_toolbox.wikipedia',
    'doctest_codeblock',
    'fsm_codeblock',
]
source_suffix = ['.rst', '.md']

templates_path = ['_templates']
exclude_patterns = []

intersphinx_timeout = 30
intersphinx_mapping = {
    'python': ('https://docs.python.org/3.10', None),
    'numpy': ('https://numpy.org/doc/stable', None),
    'pandas': ('https://pandas.pydata.org/docs', None),
    'serial': ('https://pyserial.readthedocs.io/en/stable', None),
    'graphviz': ('https://graphviz.readthedocs.io/en/stable', None),
    'pydantic': ('https://docs.pydantic.dev/latest', None),
}

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_logo = '_static/bpod-core.svg'
html_theme = 'sphinx_rtd_theme'
html_theme_options = {
    'logo_only': True,
    'collapse_navigation': True,
    'sticky_navigation': True,
    'navigation_depth': 4,
    'includehidden': True,
    'titles_only': False,
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

typehints_defaults = None
typehints_use_rtype = True
typehints_use_signature = False
typehints_use_signature_return = True

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
napoleon_type_aliases = {
    'ndarray': 'numpy.ndarray',
    'DataFrame': 'pandas.DataFrame',
    'Series': 'pandas.Series',
    'Mapping': 'collections.abc.Mapping',
    'ValidationError': 'pydantic.ValidationError',
}
napoleon_attr_annotations = True

graphviz_output_format = 'svg'
graphviz_inline = False

numfig = True
html_static_path = ['_static']
html_css_files = ['custom.css']

linkcode_link_text = ' '
pygments_style = 'default'
highlight_language = 'python3'

# -- Graphviz settings -----------------------------------
graphviz_dot = 'dot'
graphviz_output_format = 'svg'
graphviz_dot_args = [
    '-Grankdir=LR',  # Graph layout direction (left-to-right)
    '-Gfontsize=11',  # Graph-level font size
    '-Gtooltip= ',  # no graph tooltips
    '-Gbgcolor=transparent',  # transparent background
    '-Nshape=box',  # Node shape
    '-Nfontname=Helvetica, sans-serif',  # Node font
    '-Nfontsize=11',  # Node font size
    '-Ntooltip= ',  # no node tooltips
    '-Efontname=Helvetica, sans-serif',  # Edge font
    '-Efontsize=10',  # Edge font size
    '-Etooltip= ',  # no edge tooltips
]
