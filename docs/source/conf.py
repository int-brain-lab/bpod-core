import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath('../..'))
from bpod_core import __version__, fsm

# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = 'bpod-core'
copyright = f'{date.today().year}, International Brain Laboratory'
author = 'International Brain Laboratory'
release = '.'.join(__version__.split('.')[:3])
version = '.'.join(__version__.split('.')[:3])
rst_prolog = f"""
.. |version_code| replace:: ``{version}``
"""

# -- dump json schema --------------------------------------------------------
with open('../../schema/statemachine.json', 'w') as f:
    json.dump(fsm.StateMachine.model_json_schema(), f, indent=2)

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
