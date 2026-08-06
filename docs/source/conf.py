import json
import os
import sys
from datetime import datetime
from pathlib import Path

from docutils import nodes

project_root = Path(__file__).parents[2].resolve()
docs_source_path = Path(__file__).parent.resolve()
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(docs_source_path / '_ext'))

from bpod_core import __version__  # noqa: E402
from bpod_core.fsm import StateMachine  # noqa: E402
from bpod_core.misc import ValidatedDict  # noqa: E402

# -- Project information -------------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = 'bpod-core'
copyright = f'{datetime.now().year}, International Brain Laboratory'  # noqa: A001, DTZ005
author = 'International Brain Laboratory'
language = 'en'
release = '.'.join(__version__.split('.')[:3])
version = '.'.join(__version__.split('.')[:3])
rst_prolog = f"""
.. |version_code| replace:: ``{version}``
"""

# -- Schema generation ---------------------------------------------------------

schema_root = project_root / '.schema'
schema_root.mkdir(exist_ok=True)
with schema_root.joinpath('statemachine.json').open('w') as f:
    schema = StateMachine.model_json_schema()
    json.dump(schema, f, indent=2)
    f.write('\n')  # add final newline

# -- General configuration -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'sphinx_autodoc_typehints',  # must be listed after napoleon
    'myst_parser',
    'sphinx.ext.intersphinx',
    'sphinx.ext.autosummary',
    'sphinx.ext.graphviz',
    'sphinx.ext.doctest',
    'sphinx_github_style',
    'sphinx_copybutton',
    'sphinx_design',
    'sphinx-jsonschema',
    'dark_light_figure',
    'doctest_codeblock',
    'fsm_codeblock',
    'fsm_examples',
    'missing_references',
    'matplotlib.sphinxext.plot_directive',
    'sphinx_llm.txt',
    'sphinx_sitemap',
    'sphinxext.opengraph',
]

source_suffix = {'.rst': 'restructuredtext', '.md': 'myst'}
templates_path = ['_templates']
exclude_patterns = []

numfig = True
nitpicky = True

# -- MyST ----------------------------------------------------------------------

myst_heading_anchors = 2
myst_enable_extensions = ['alert']

# -- Code blocks ---------------------------------------------------------------

doctest_global_setup = f"""
_DOCS_STATIC = __import__('pathlib').Path({str(docs_source_path / '_static')!r})
"""
plot_pre_code = f"""
_DOCS_STATIC = __import__('pathlib').Path({str(docs_source_path / '_static')!r})
"""

copybutton_prompt_text = r'>>> |\.\.\. |\$ |In \[\d*\]: | {2,5}\.\.\.: | {5,8}: '
copybutton_prompt_is_regexp = True

# -- Intersphinx ---------------------------------------------------------------

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
    'msgspec': ('https://msgspec.dev/', None),
    'zmq': ('https://pyzmq.readthedocs.io/en/latest', None),
    'zeroconf': ('https://python-zeroconf.readthedocs.io/en/latest/', None),
    'typing_extensions': ('https://typing-extensions.readthedocs.io/en/latest', None),
    'filelock': ('https://py-filelock.readthedocs.io/en/latest/', None),
    'PySide6': ('https://doc.qt.io/qtforpython-6', None),
}

# -- HTML output ---------------------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output


html_theme = 'shibuya'
html_title = 'bpod-core documentation'
html_favicon = '_static/favicon.svg'
html_css_files = ['custom.css']
html_static_path = ['_static']
html_theme_options = {
    'color_mode': 'auto',
    'light_logo': '_static/bpod-core.svg',
    'dark_logo': '_static/bpod-core__dark.svg',
    'show_ai_links': False,
    'accent_color': 'cyan',
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
html_baseurl = 'https://int-brain-lab.github.io/bpod-core/'
html_copy_source = False
html_extra_path = ['robots.txt']

# -- Analytics -----------------------------------------------------------------

# Umami is loaded only when UMAMI_SCRIPT_URL and UMAMI_SITE_ID are set, so local builds
# stay untracked.
umami_script_url = os.getenv('UMAMI_SCRIPT_URL', '')
umami_site_id = os.getenv('UMAMI_SITE_ID', '')
if umami_script_url and umami_site_id:
    html_js_files = [
        (
            umami_script_url,
            {'defer': 'defer', 'data-website-id': umami_site_id},
        ),
        'umami-search.js',  # reports search queries; only useful alongside Umami
    ]

# -- Open Graph ----------------------------------------------------------------

# with this extension enabled, shibuya delegates the description, Open Graph and
# Twitter tags to it, so the theme's own 'og_image_url' option no longer applies
ogp_site_url = html_baseurl
ogp_image = f'{html_baseurl}_static/open_graph_card.png'
ogp_social_cards = {'enable': False}  # use the static card above instead
ogp_custom_meta_tags = ['<meta name="twitter:card" content="summary"/>']

# -- Sitemap -------------------------------------------------------------------

sitemap_url_scheme = '{link}'
sitemap_show_lastmod = True
sitemap_indent = 2
sitemap_locales = ['en']

# -- Autodoc -------------------------------------------------------------------

autodoc_mock_imports = ['_typeshed']
autodoc_class_signature = 'separated'  # 'mixed', 'separated'
autodoc_member_order = 'groupwise'  # 'alphabetical', 'groupwise', 'bysource'
autodoc_inherit_docstrings = True
autodoc_typehints = 'description'  # 'description', 'signature', 'none', 'both'
autodoc_typehints_description_target = (
    'documented_params'  # 'all', 'documented', 'documented_params'
)
autodoc_typehints_format = 'short'  # 'fully-qualified', 'short'
autodoc_use_type_comments = False
autodoc_default_options = {
    'member-order': 'groupwise',
    'show-inheritance': True,
    'exclude-members': '__new__, __init__, model_config',
    'class-doc-from': 'class',
}
autodoc_type_aliases = {
    'polars.dataframe.frame.DataFrame': 'polars.DataFrame',
    'polars.lazyframe.frame.LazyFrame': 'polars.LazyFrame',
}

# -- Autosummary ---------------------------------------------------------------
autosummary_generate = True
autosummary_imported_members = False

# -- Autodoc Typehints ---------------------------------------------------------
typehints_defaults = 'comma'
typehints_document_rtype_none = False
typehints_document_overloads = False
typehints_use_rtype = True
always_use_bars_union = True
always_document_param_types = False

# -- Napoleon ------------------------------------------------------------------

napoleon_attr_annotations = True
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
napoleon_use_keyword = False
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
    'ColorType': '~pydantic_extra_types.color.ColorType',
}

# -- FSM diagrams --------------------------------------------------------------

fsm_light_colors = {
    'color_stroke': 'black',
    'color_fill': 'white',
    'color_highlight': 'lightblue',
    'color_back': 'black',
}
fsm_dark_colors = {
    'color_stroke': 'white',
    'color_fill': 'black',
    'color_highlight': 'darkred',
    'color_back': 'white',
}

# -- Graphviz ------------------------------------------------------------------

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

# -- Plot directive ------------------------------------------------------------

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

# -- Miscellaneous -------------------------------------------------------------

linkcode_link_text = ' '
pygments_style = 'default'
highlight_language = 'python3'
numpydoc_show_class_members = False
llms_txt_description = 'A modern Python interface for Bpod Finite State Machines.'

# -- Hooks ---------------------------------------------------------------------


def _og_description_from_meta(_app, _pagename, _templatename, context, doctree):
    """Reuse a page's meta description as its Open Graph description.

    sphinxext-opengraph derives ``og:description`` from the page's body text, but
    picks up per-page overrides from the ``meta`` context. Copying the description
    of the ``meta`` directive there keeps both tags in sync.
    """
    if doctree is None:
        return
    for node in doctree.findall(nodes.meta):
        if node.get('name') == 'description' and node.get('content'):
            # replace rather than mutate: context['meta'] is env.metadata[docname]
            context['meta'] = {
                **(context['meta'] or {}),
                'og:description': node['content'],
            }
            return


def _skip_pydantic_parameterized(_app, _what, name, obj, skip, _options):
    """Skip pydantic-generated parameterized subclasses of ValidatedDict."""
    if skip:
        return True
    try:
        if '[' in name and issubclass(obj, ValidatedDict):
            return True
    except TypeError:
        pass
    return None


def setup(app):
    app.connect('autodoc-skip-member', _skip_pydantic_parameterized)

    # priority < 500 so this runs before sphinxext-opengraph builds its tags
    app.connect('html-page-context', _og_description_from_meta, priority=400)
