import os
import sys
from datetime import date

sys.path.insert(0, os.path.abspath('../..'))
from bpod_core import __version__

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

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    'myst_parser',
    'sphinx.ext.intersphinx',
    'sphinx.ext.napoleon',
    'sphinx.ext.autodoc',
    'sphinx.ext.autosummary',
    'sphinx.ext.inheritance_diagram',
    'sphinx_copybutton',
    'sphinx_autodoc_typehints',
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
    'serial': ('https://pyserial.readthedocs.io/en/stable/', None),
}

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = 'sphinx_rtd_theme'

# -- Settings for automatic API generation -----------------------------------
autodoc_mock_imports = ['_typeshed']
autodoc_class_signature = 'separated'         # 'mixed', 'separated'
autodoc_member_order = 'groupwise'            # 'alphabetical', 'groupwise', 'bysource'
autodoc_inherit_docstrings = False
autodoc_typehints = 'description'             # 'description', 'signature', 'none', 'both'
autodoc_typehints_description_target = 'all'  # 'all', 'documented', 'documented_params'
autodoc_typehints_format = 'short'            # 'fully-qualified', 'short'

autosummary_generate = True
autosummary_imported_members = False

napoleon_google_docstring = False
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False
napoleon_include_special_with_doc = False
napoleon_use_admonition_for_examples = True
napoleon_use_admonition_for_notes = True
napoleon_use_admonition_for_references = True
napoleon_use_ivar = False
napoleon_use_param = False
napoleon_use_rtype = True
napoleon_use_keyword = True
napoleon_preprocess_types = True
napoleon_type_aliases = None
napoleon_attr_annotations = False
