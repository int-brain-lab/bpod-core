.. meta::
   :description: Sharing a single Bpod device between processes over ZeroMQ.

Inter-Process Communication
===========================

Only one process at a time can hold the serial connection to a Bpod device. To let
several programs work with the same device — say, a GUI, an online analysis script, and
the process that owns the hardware — every :class:`~bpod_core.bpod.Bpod` instance also
acts as a small IPC service. Other processes attach to that service through
:class:`~bpod_core.bpod.RemoteBpod`, a proxy that implements the same interface
(:class:`~bpod_core.bpod.abc.AbstractBpod`) and forwards each call over `ZeroMQ`_.

.. _ZeroMQ: https://zeromq.org/

.. note::

   :class:`~bpod_core.bpod.RemoteBpod` is still incomplete: state machine execution,
   data retrieval and the live event stream work, but individual channels, modules and
   softcode handlers are not yet exposed remotely.


Architecture
------------

A Bpod service consists of two independent ZeroMQ channels, both of which encode their
payloads with `MessagePack <https://msgpack.org/>`_ via `msgspec`_:

.. _msgspec: https://msgspec.dev/

.. list-table::
   :header-rows: 1
   :widths: 20 25 55

   * - Channel
     - Socket pattern
     - Purpose
   * - control
     - ``REQ``/``REP``
     - Synchronous remote procedure calls: run a state machine, fetch data, toggle
       the status LED. One request at a time, each answered by exactly one reply.
   * - events
     - ``PUB``/``SUB``
     - Fire-and-forget broadcast of live trial events to any number of subscribers.

The addresses of both channels are exposed as
:attr:`~bpod_core.bpod.Bpod.address_control` and
:attr:`~bpod_core.bpod.Bpod.address_events` on either end of the connection.

Clients do not normally need to know these addresses: the host advertises itself, and
:class:`~bpod_core.bpod.RemoteBpod` looks it up. Two discovery mechanisms are used:

* **Locally**, the host writes a small JSON advertisement to the user's runtime
  directory (see :class:`~bpod_core.ipc.LocalServiceAdvertisement`). Advertisements
  left behind by dead processes are pruned during discovery. This mechanism is always
  active.
* **On the network**, the host registers a Zeroconf/mDNS service of type
  ``_bpod._tcp.local.``. This requires the host to opt in with ``remote=True``.

Which transport a connection ends up using is negotiated during the handshake: a client
on the same machine as the host is upgraded from TCP to a Unix domain socket
(an abstract socket on Linux) where available, which avoids the network stack
altogether. Clients on another machine communicate over TCP.


Hosting a Bpod service
----------------------

There is nothing to enable — the service is started by the
:class:`~bpod_core.bpod.Bpod` constructor and shut down when the instance is closed. By
default it is only advertised and reachable on the local machine (the TCP sockets bind
to loopback). Pass ``remote=True`` to bind on all interfaces and advertise via
Zeroconf:

.. code-block:: python
   :caption: Hosting a Bpod that can be reached from other machines.

   from bpod_core.bpod import Bpod

   with Bpod(serial_number='14260000', remote=True) as bpod:
       input('Press Enter to shut down the service ...')

The hosting process must stay alive for as long as clients need the device — closing
the :class:`~bpod_core.bpod.Bpod` instance also removes the advertisement and closes
both channels.

For the common case of a process that does nothing but own the hardware, bpod-core
ships a command line entry point that does exactly the above:

.. code-block:: console

   $ bpod --serial-number 14260000 --remote

The ``--remote`` flag corresponds to the ``remote`` argument of
:class:`~bpod_core.bpod.Bpod`; omit it to keep the device private to the local machine.
Port, serial number and the remote flag can alternatively be set through the
environment variables ``BPOD_OVERRIDE_PORT``, ``BPOD_OVERRIDE_SERIAL_NUMBER`` and
``BPOD_OVERRIDE_REMOTE``, which is convenient when a supervising process launches the
host itself.

.. tip::

   A service is identified by the device's serial number, so the TCP ports of a given
   Bpod are stable across restarts of the hosting process.


Connecting with RemoteBpod
--------------------------

:class:`~bpod_core.bpod.RemoteBpod` discovers a matching service, connects to both
channels, and performs a handshake. Like :class:`~bpod_core.bpod.Bpod`, it is best used
as a context manager:

.. code-block:: python
   :caption: Connecting to a running Bpod instance.

   from bpod_core.bpod import RemoteBpod

   with RemoteBpod() as bpod:
       print(bpod.serial_number, bpod.version.machine_str)

Without arguments, the first Bpod service found — local or remote — is used. To
disambiguate between several devices, filter by ``serial_number``, ``name`` or
``location``; these are matched against the properties the host advertises:

.. code-block:: python
   :caption: Selecting a specific device.

   with RemoteBpod(serial_number='14260000') as bpod:
       pass  # do things

   with RemoteBpod(name='left_rig', timeout=30.0) as bpod:
       pass  # do things

Discovery waits up to ``timeout`` seconds (10 by default) and raises
:exc:`TimeoutError` if no matching service shows up. If you already know where the
service lives, pass its control address directly and skip discovery entirely:

.. code-block:: python

   with RemoteBpod('tcp://192.168.1.10:5555') as bpod:
       pass  # do things

.. note::

   Host and client must run the same version of bpod-core. A mismatch is rejected
   during the handshake and surfaces as a :exc:`~bpod_core.ipc.RemoteError`.


Discovering available devices
-----------------------------

:func:`~bpod_core.bpod.discover_remote_bpod` monitors the same two discovery
mechanisms and yields :class:`~bpod_core.ipc.ServiceEvent` tuples as services appear
and disappear. Use it to present a choice of devices, or to wait for one to come up:

.. code-block:: python
   :caption: Listing the Bpod services currently available.

   from bpod_core.bpod import discover_remote_bpod

   for event in discover_remote_bpod(timeout=5.0):
       print(event.kind, event.address, event.properties['serial_number'])

Pass ``timeout=None`` to monitor indefinitely, and ``local=False`` or ``remote=False``
to restrict the search to one of the two mechanisms.


Running state machines remotely
-------------------------------

A remote instance is driven exactly like a local one — the state machine is serialized,
sent over the control channel and executed by the hosting process:

.. code-block:: python
   :caption: Running a state machine on a remote Bpod and collecting its data.

   from bpod_core.bpod import RemoteBpod
   from bpod_core.fsm import StateMachine

   fsm = StateMachine()
   fsm.add_state('blink', timer=1, transitions={'Tup': '>exit'}, actions={'PWM1': 255})

   with RemoteBpod(serial_number='14260000') as bpod:
       for _ in range(10):
           bpod.run(fsm)
       data = bpod.get_data()

The following members of the :class:`~bpod_core.bpod.Bpod` interface are available on a
remote instance:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Member
     - Notes
   * - :meth:`~bpod_core.bpod.RemoteBpod.run`
     - The state machine is serialized and sent to the host, which validates, compiles
       and enqueues it. The call returns once the host has accepted it.
   * - :meth:`~bpod_core.bpod.RemoteBpod.get_data`
     - Returns the trial data as a Polars :class:`~polars.DataFrame` (see
       :doc:`/data_format`), or a :class:`~polars.LazyFrame` with ``lazy=True``.
   * - :meth:`~bpod_core.bpod.RemoteBpod.stop_state_machine`
     - Aborts the state machine currently running on the host.
   * - :meth:`~bpod_core.bpod.RemoteBpod.reset_session_clock`
     - Resets the device's session clock.
   * - :meth:`~bpod_core.bpod.RemoteBpod.set_status_led`
     - Enables or disables the status LED.
   * - :meth:`~bpod_core.bpod.RemoteBpod.update_modules`
     - Re-reads the modules connected to the host's device.
   * - :attr:`~bpod_core.bpod.RemoteBpod.serial_number`,
       :attr:`~bpod_core.bpod.RemoteBpod.version`,
       :attr:`~bpod_core.bpod.RemoteBpod.name`,
       :attr:`~bpod_core.bpod.RemoteBpod.location`
     - Obtained once during the handshake; reading them involves no round-trip.

Only this set of methods is callable remotely — the host rejects requests for anything
else with a :exc:`~bpod_core.bpod.BpodError`. Exceptions raised on the host are
serialized and re-raised on the client: :exc:`RuntimeError` and :exc:`ValueError` keep
their type, as does :exc:`~bpod_core.bpod.BpodError` for
:meth:`~bpod_core.bpod.RemoteBpod.get_data` and
:meth:`~bpod_core.bpod.RemoteBpod.reset_session_clock`. Everything else arrives as a
:exc:`~bpod_core.ipc.RemoteError` carrying the remote type name, message and traceback.

.. tip::

   Trial data is transferred in Arrow IPC format. Over a network connection it is
   LZ4-compressed; for a client on the same machine, compression is skipped in favour
   of throughput.


Subscribing to live events
--------------------------

Data returned by :meth:`~bpod_core.bpod.RemoteBpod.get_data` only becomes available
once a trial has finished. To follow a trial as it unfolds, pass an
``event_callback``, which subscribes the client to the host's events channel:

.. code-block:: python
   :caption: Reacting to live trial events.

   from bpod_core.bpod import RemoteBpod
   from bpod_core.bpod.structs import EventInput, EventStateStart

   def on_event(event):
       match event:
           case EventStateStart(state=state):
               print(f'entered state {state}')
           case EventInput(event=name, time_us=time_us):
               print(f'{name} at {time_us / 1e6:.3f} s')

   with RemoteBpod(serial_number='14260000', event_callback=on_event) as bpod:
       bpod.run(fsm)

Each message is one of the tagged structs in :mod:`bpod_core.bpod.structs`:
:class:`~bpod_core.bpod.structs.EventTrialStart`,
:class:`~bpod_core.bpod.structs.EventStateStart`,
:class:`~bpod_core.bpod.structs.EventInput`,
:class:`~bpod_core.bpod.structs.EventOutput`,
:class:`~bpod_core.bpod.structs.EventStateEnd`,
:class:`~bpod_core.bpod.structs.EventTrialEnd` or
:class:`~bpod_core.bpod.structs.EventTrialEndControl`. Their union is available as
:obj:`~bpod_core.bpod.structs.BpodEventUnion`, which makes ``match`` statements like
the one above exhaustively checkable by a type checker.

.. warning::

   The callback runs on the client's subscription thread. It must not block — anything
   expensive belongs in a queue that another thread drains. Exceptions raised inside
   the callback are logged and otherwise ignored.

Two properties of the ``PUB``/``SUB`` pattern are worth keeping in mind:

* Events published before a client's subscription registers are lost (ZeroMQ's
  "slow joiner" behaviour). Connect before starting the first trial; a host launched
  through ``BPOD_OVERRIDE_REMOTE`` additionally waits up to a second for a subscriber
  to show up.
* The host skips publishing altogether while nobody is subscribed, so a client without
  an ``event_callback`` costs the host nothing.

Events are meant for monitoring, not for record keeping: they are not retransmitted,
and the trial data returned by :meth:`~bpod_core.bpod.RemoteBpod.get_data` remains the
authoritative record.


Building on the IPC layer
-------------------------

The machinery underneath is generic and not tied to the Bpod itself. The
:mod:`bpod_core.ipc` module provides :class:`~bpod_core.ipc.ServiceHost` and
:class:`~bpod_core.ipc.ServiceClient`, which implement the two channels, the discovery,
the handshake and the transport upgrade described above; the message types being
exchanged are supplied by the caller as `msgspec`_ tagged unions. The Bpod service
is simply one instantiation of that pair, using the request, reply and event unions
defined in :mod:`bpod_core.bpod.structs`.

.. seealso::

   :class:`~bpod_core.ipc.ServiceHost`, :class:`~bpod_core.ipc.ServiceClient` and
   :func:`~bpod_core.ipc.iter_services` for the full reference of the underlying IPC
   layer.