Developer Guide
===============


Versioning Scheme
-----------------

bpod-core uses `Semantic Versioning <https://semver.org/spec/v2.0.0.html>`_.
Its version string (currently |version_code|) is a combination of three fields, separated by dots:

.. centered:: ``MAJOR`` . ``MINOR`` . ``PATCH``

* The ``MAJOR`` field is only incremented for breaking changes, i.e., changes that are not backward compatible with previous changes.
* The ``MINOR`` field will be incremented upon adding new, backward compatible features.
* The ``PATCH`` field will be incremented with each new, backward compatible bugfix release that does not implement a new feature.

On the developer side, these 3 fields are manually controlled by, both

   1. adjusting the variable ``__version__`` in ``bpod-core/__init__.py``, and
   2. adding the corresponding version string to a commit as a `git tag <https://git-scm.com/book/en/v2/Git-Basics-Tagging>`_,
      for instance:

      .. code-block:: console

         git tag 1.2.3
         git push origin --tags


Package Management and Development Workflows with PDM
-----------------------------------------------------

We use `PDM <https://pdm-project.org/en/latest/>`_ to manage dependencies of bpod-core.
PDM can also be used to run various commands with relevance to the development process without having to activate a virtual
environment first.
Please refer to `PDM's documentation <https://pdm-project.org/en/latest/#installation>`_ for help with installing PDM.


Installing Developer Dependencies
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

To install additional dependencies needed for working on bpod-core's code-base, run:

.. code-block:: console

   pdm sync -d


Running Unit Tests
^^^^^^^^^^^^^^^^^^

To run unit tests locally, run:

.. code-block:: console

   pdm run pytest

This will also generate a HTML based coverage report which can be found in the ``htmlcov`` directory.


Linting & Formatting
^^^^^^^^^^^^^^^^^^^^

We use `Ruff <https://docs.astral.sh/ruff>`_ for linting and formatting our code-base in close accordance with `the Black code
style <https://black.readthedocs.io/en/stable/the_black_code_style/current_style.html>`_.

To lint your code, run:

.. code-block:: console

   pdm run ruff check

Appending the flag ``--fix`` to the above command will automatically fix issues that are deemed safe to handle.

To reformat your code according to the `Black code style <https://black.readthedocs.io/en/stable/the_black_code_style/current_style.html>`_ run:

.. code-block:: console

   pdm run ruff format

Appending the flag ``--check`` to the above command will check your code for formatting issues without applying any changes.
Refer to `Ruff Formater's documentation <https://docs.astral.sh/ruff/formatter/>`_ for further details.


Documentation
^^^^^^^^^^^^^

To build the documentation, run:

.. code-block:: console

   pdm run docs
