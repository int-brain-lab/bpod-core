import os
import sys
from importlib.metadata import version

# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = "bpod_core"
copyright = "2025, International Brain Laboratory"
author = "International Brain Laboratory"
release = version(project)
version = ".".join(release.split(".")[:3])
sys.path.insert(0, os.path.abspath("../../src/"))

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "myst_parser",
    "sphinx.ext.intersphinx",
    "sphinx.ext.napoleon",
    "sphinx.ext.autodoc",
    "sphinx_copybutton",
    "sphinx_autodoc_typehints",
]
source_suffix = [".rst", ".md"]

templates_path = ["_templates"]
exclude_patterns = []

napoleon_preprocess_types = True
napoleon_use_param = True
napoleon_use_rtype = False

typehints_defaults = None
typehints_use_rtype = False
typehints_use_signature = False
typehints_use_signature_return = False

intersphinx_mapping = {
    "python": ("https://docs.python.org/3.10/", None),
    "numpy": ("http://docs.scipy.org/doc/numpy/", None),
    "serial": ("https://pyserial.readthedocs.io/en/stable/", None),
}

# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "sphinx_rtd_theme"
