import importlib.util
import inspect
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from docutils import nodes
from sphinx.ext.intersphinx import InventoryAdapter

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
        image_file = examples_target_path / fn.with_suffix('.svg').name
        state_machine.to_file(image_file, overwrite=True)

        # Generate JSON
        json = state_machine.to_json(indent=2).splitlines()
        json = [' ' * 7 + line for line in json]

        # Generate YAML
        yaml = state_machine.to_yaml().splitlines()
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
            f'    .. literalinclude:: ../../../../examples/state_machines/{fn.name}',
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


_REFERENCE_URL_MAP = {
    'polars.DataFrame': 'https://docs.pola.rs/py-polars/html/reference/dataframe',
    'polars.LazyFrame': 'https://docs.pola.rs/py-polars/html/reference/lazyframe',
    'typing.Annotated': 'https://docs.python.org/3/library/typing.html#typing.Annotated',
    'numpy.uint8': 'https://numpy.org/doc/stable/reference/arrays.scalars.html#numpy.uint8',
}


def resolve_missing_references(_, env, node, contnode):
    """Resolve missing cross-references not covered by intersphinx inventories."""

    target = node.get('reftarget', '')

    # Remap platform-specific serial.Serial subclasses to the public API entry
    if re.match(r'^serial\.serial\w+\.Serial$', target):
        node['reftarget'] = 'serial.Serial'
        return None

    # # Remap fully-parameterized ValidatedDict generics to typing.Annotated
    # if re.match(r'^bpod_core\.misc\.ValidatedDict\[', target):
    #     target = node['reftarget'] = 'typing.Annotated'

    # Strip generic subscripts that intersphinx can't resolve
    if '[' in target:
        node['reftarget'] = target[: target.index('[')]
        text = contnode.astext()
        if '[' in text:
            contnode[0] = nodes.Text(text[: text.index('[')])
        return None

    # Direct URL fallbacks for targets absent from all inventories
    if target in _REFERENCE_URL_MAP:
        ref = nodes.reference('', '', internal=False, refuri=_REFERENCE_URL_MAP[target])
        ref += contnode
        return ref

    # For failed py:class lookups, retry as py:data or py:attribute
    if node.get('refdomain') == 'py' and node.get('reftype') == 'class':
        for inv in InventoryAdapter(env).named_inventory.values():
            for alt in ('py:data', 'py:attribute'):
                entry = inv.get(alt, {}).get(target)
                if entry:
                    _proj, _ver, location, _display = entry
                    ref = nodes.reference('', '', internal=False, refuri=location)
                    ref += contnode
                    return ref

    return None


def setup(app):
    app.connect('builder-inited', generate_fsm_examples)
    app.connect('missing-reference', resolve_missing_references, priority=400)


# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = 'bpod-core'
copyright = f'{datetime.now().year}, International Brain Laboratory'  # noqa: A001
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
    'sphinx.ext.inheritance_diagram',
    'sphinx_github_style',
    'sphinx_copybutton',
    'sphinx_design',
    'sphinx-jsonschema',
    'sphinx_toolbox.wikipedia',
    # 'sphinx_toolbox.more_autodoc.autonamedtuple',
    # 'sphinx_toolbox.more_autodoc.generic_bases',
    'sphinx_toolbox.more_autodoc.typevars',
    'sphinx_toolbox.more_autodoc.genericalias',
    # 'sphinx_toolbox.more_autodoc.overloads',
    'doctest_codeblock',
    'fsm_codeblock',
    'matplotlib.sphinxext.plot_directive',
]
doctest_global_setup = f"""
_DOCS_STATIC = __import__('pathlib').Path({str(docs_source_path / '_static')!r})
"""
plot_pre_code = f"""
_DOCS_STATIC = __import__('pathlib').Path({str(docs_source_path / '_static')!r})
"""
source_suffix = ['.rst', '.md']

copybutton_prompt_text = r'>>> |\.\.\. |\$ |In \[\d*\]: | {2,5}\.\.\.: | {5,8}: '
copybutton_prompt_is_regexp = True

templates_path = ['_templates']
exclude_patterns = []

intersphinx_timeout = 30
intersphinx_mapping = {
    'python': ('https://docs.python.org/3', None),
    'numpy': ('https://numpy.org/doc/stable', None),
    'pandas': ('https://pandas.pydata.org/docs', None),
    'polars': ('https://docs.pola.rs/api/python/stable', None),
    'pyarrow': ('https://arrow.apache.org/docs/', None),
    'serial': ('https://pyserial.readthedocs.io/en/stable', None),
    'graphviz': ('https://graphviz.readthedocs.io/en/stable', None),
    'pydantic': ('https://pydantic.dev/docs/validation/latest', None),
    'msgspec': ('https://jcristharif.com/msgspec/', None),
    'zmq': ('https://pyzmq.readthedocs.io/en/latest', None),
    'zeroconf': ('https://python-zeroconf.readthedocs.io/en/latest/', None),
    'typing_extensions': ('https://typing-extensions.readthedocs.io/en/latest', None),
}

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_title = 'bpod-core documentation'
html_logo = '_static/bpod-core.svg'
html_static_path = ['_static']
html_css_files = ['custom.css']
html_theme = 'shibuya'
html_theme_options = {
    'color_mode': 'light',
}
html_context = {
    'display_github': False,
    'github_user': 'int-brain-lab',
    'github_repo': 'bpod-core',
    'github_version': 'master',
    'conf_py_path': '/docs/source/',
    # 'source_type': 'github',
    # 'source_user': 'int-brain-lab',
    # 'source_repo': 'bpod-core',
    # 'source_version': 'develop',
    # 'source_docs_path': '/docs/source/',
}
html_favicon = '_static/favicon.svg'

# -- Settings for automatic API generation -----------------------------------
autodoc_mock_imports = ['_typeshed', 'serial']
autodoc_class_signature = 'separated'  # 'mixed', 'separated'
autodoc_member_order = 'groupwise'  # 'alphabetical', 'groupwise', 'bysource'
autodoc_inherit_docstrings = True
autodoc_typehints = 'signature'  # 'description', 'signature', 'none', 'both'
autodoc_typehints_description_target = 'all'  # 'all', 'documented', 'documented_params'
autodoc_typehints_format = 'short'  # 'fully-qualified', 'short'
autodoc_use_type_comments = False
autodoc_default_options = {
    'member-order': 'groupwise',
    'show-inheritance': True,
    'undoc-members': True,
    'exclude-members': '__new__, __init__, model_config',
    'class-doc-from': 'class',
}
autodoc_type_aliases = {}

autosummary_generate = True
autosummary_imported_members = False

always_use_bars_union = True
typehints_defaults = 'comma'
typehints_use_rtype = True
typehints_use_signature = False
typehints_use_signature_return = False
typehints_document_overloads = True

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
    'ndarray': '~numpy.ndarray',
    'Mapping': '~collections.abc.Mapping',
    'MutableMapping': '~collections.abc.MutableMapping',
    'Collection': '~collections.abc.Collection',
    'Sequence': '~collections.abc.Sequence',
    'Iterable': '~collections.abc.Iterable',
    'ValidationError': '~pydantic_core.ValidationError',
    'Buffer': '~collections.abc.Buffer',
    'Callable': '~collections.abc.Callable',
    'PathLike': '~os.PathLike',
    'Any': '~typing.Any',
    'UUID': '~uuid.UUID',
    'SerialException': '~serial.SerialException',
    'SerialTimeoutException': '~serial.SerialTimeoutException',
    'StateMachine': '~bpod_core.fsm.StateMachine',
    'StateMachineLookup': '~bpod_core.bpod.structs.StateMachineLookup',
    'SimpleQueue': '~queue.SimpleQueue',
    'DataFrame': 'polars.DataFrame',
    'LazyFrame': 'polars.LazyFrame',
    'TimeReferences': '~bpod_core.bpod.structs.TimeReferences',
    'BpodInfo': '~bpod_core.bpod.structs.BpodInfo',
    'Digraph': '~graphviz.Digraph',
    'ListPortInfo': '~serial.tools.list_ports.ListPortInfo',
    'TypeVar': '~typing.TypeVar',
    'ValidatedDict': '~bpod_core.misc.ValidatedDict',
}
napoleon_attr_annotations = True

graphviz_output_format = 'svg'
# graphviz_inline = False

numfig = True

linkcode_link_text = ' '
pygments_style = 'default'
highlight_language = 'python3'

nitpicky = True

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

# -- Plot settings ---------------------------------------
plot_include_source = True
plot_formats = [('svg', 90)]
plot_html_show_source_link = False
plot_html_show_formats = False
plot_apply_rcparams = True
plot_rcparams = {
    'font.size': 8,
    'figure.constrained_layout.use': True,
    'figure.facecolor': 'none',
}
