Frequently Asked Questions
==========================

How do I cite bpod-core?
------------------------
bpod-core is archived on `Zenodo <https://zenodo.org>`_. Please cite it using the DOI
`10.5281/zenodo.21497456 <https://doi.org/10.5281/zenodo.21497456>`_, which links to
citation details and export formats (BibTeX, APA, etc.) for the latest release of
bpod-core. To cite a specific version, use the version-specific DOI listed on that
release's Zenodo page.

Can I use an AI coding agent with bpod-core?
--------------------------------------------
You can, and giving the agent access to bpod-core's documentation makes a noticeable
difference. Without it, assistants tend to fall back on what they have seen most of —
the original MATLAB Bpod or older Python wrappers — and produce code that looks
plausible but uses methods bpod-core does not have.

To that end, bpod-core's documentation is served through a
`Model Context Protocol <https://modelcontextprotocol.io>`__ (MCP) server, hosted by
`GitMCP <https://gitmcp.io>`__:

.. code-block:: text

   https://int-brain-lab.gitmcp.io/bpod-core

The MCP server is public, read-only and requires no account or API key. Refer to your
coding agent's documentation for details on how to add the MCP server. Once connected,
the agent can look things up on demand through four tools:

.. list-table::
   :header-rows: 1
   :widths: 45 55

   * - Tool
     - Purpose
   * - ``fetch_bpod-core_documentation``
     - Retrieve bpod-core's documentation as a whole.
   * - ``search_bpod-core_documentation``
     - Search the documentation for a specific topic.
   * - ``search_bpod-core_code``
     - Search the repository's source code, tests and examples.
   * - ``fetch_url_content``
     - Follow links encountered in the above.

.. note::

   The MCP server mirrors the documentation published at
   `int-brain-lab.github.io/bpod-core <https://int-brain-lab.github.io/bpod-core>`__,
   which is built from the latest release. If you work against an unreleased version,
   expect it to lag behind your working copy — in that case, point the agent at the
   ``docs`` directory of your checkout as well.

Either way, treat generated code as a draft: verify it against the documentation, and
never let an agent drive hardware unattended.

I can't connect to a Bpod on Linux
----------------------------------
If you're experiencing "Permission denied" errors, slow or unreliable connections,
missing data, or timeout errors when trying to connect to your Bpod on Linux, you likely
need to install additional udev rules.

By default, Linux restricts access to USB serial devices to root and members of the
``dialout`` group for security reasons. Additionally, system services like
`ModemManager <https://modemmanager.org/>`__  automatically probe serial devices to
detect modems, which can interfere with normal communication. Bpod devices use
`Teensy microcontrollers <https://www.pjrc.com/teensy/>`__, which appear as USB serial
devices that are subject to these restrictions.

Installing the `Teensy udev rules <https://www.pjrc.com/teensy/00-teensy.rules>`__
should solve these issues by:

- Granting all users read/write access to Teensy devices without requiring ``dialout``
  group membership
- Preventing `ModemManager <https://modemmanager.org/>`__  from probing and interfering
  with the connection
- Configuring serial ports with appropriate low-level settings (raw mode, no echo)
