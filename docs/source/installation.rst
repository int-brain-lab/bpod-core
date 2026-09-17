.. meta::
   :description: How to install, verify and update bpod-core.

Installation
============

Requirements
------------

bpod-core requires **Python 3.10 or later** and is supported on Linux, macOS, and
Windows. It is compatible with Bpod Finite State Machines r2 and newer. Older hardware
revisions are not supported as of now.

Virtual Environment
-------------------

While optional, we recommend installing bpod-core into a dedicated virtual environment,
isolating bpod-core and its dependencies from other Python projects, preventing version
conflicts and keeping your system Python clean. We recommend using
`uv <https://docs.astral.sh/uv/>`__ to manage Python environments. See
`uv's documentation <https://docs.astral.sh/uv/getting-started/installation/>`_ for
installation instructions.

Creation
^^^^^^^^

To create a virtual environment:

.. code-block:: console

   $ uv venv bpod-core --python 3.14

Activation
^^^^^^^^^^

To activate the virtual environment:

.. tab-set::

   .. tab-item:: Linux and macOS

      .. code-block:: console

         $ source bpod-core/bin/activate

   .. tab-item:: Windows

      .. code-block:: pwsh-session

         PS> bpod-core\Scripts\activate

Installing bpod-core
--------------------

To install bpod-core and its dependencies into the active virtual environment:

.. code-block:: console

   $ uv pip install bpod-core

.. note::

   **Linux users:** You may encounter permission errors when connecting to a Bpod
   device. See :doc:`faq` for instructions on installing the required udev rules.

Verifying the installation
--------------------------

After installation, run the ``bpod`` command-line tool:

.. code-block:: console

   $ bpod --version

This should print the installed version of bpod-core.

Updating
--------

To update bpod-core to the latest release:

.. code-block:: console

   $ uv pip install --upgrade bpod-core
