Using the Bpod Class
====================

The :class:`~bpod_core.bpod.Bpod` class is bpod-core's entry point for communicating
with a `Bpod`_ device. It opens a serial connection, sends compiled state machines to
the hardware, controls execution, and returns trial data.

.. _Bpod: https://sanworks.github.io/Bpod_Wiki/

Connecting to a Device
----------------------

:class:`~bpod_core.bpod.Bpod` can connect to a device by ``port``, ``serial_number``,
or — when both are omitted — by auto-discovering the first available Bpod on the system.
In most use cases it's best to use the class as a `context manager`_: it guarantees the
serial connection is closed and any running trial is allowed to finish on exit.

.. _context manager: https://docs.python.org/3/library/stdtypes.html#typecontextmanager

.. testsetup:: bpod-context-manager

   import atexit
   import types
   from unittest.mock import patch
   from types import SimpleNamespace
   from bpod_core.bpod import Bpod

   original_send = Bpod.send_state_machine
   original_validate = Bpod.validate_state_machine

   def fake_init(self, *args, **kwargs):
       self._disable_all_module_relays = lambda: None
       self._hardware = SimpleNamespace(
           max_states=256,
           n_global_timers=16,
           n_global_counters=16,
           n_conditions=64,
           cycle_frequency=1000,
       )
       self._read_thread = None
       self._bpod_finalizer = SimpleNamespace(detach=lambda: None)
       self._serial_device_finalizer = SimpleNamespace(detach=lambda: None)
       self._serial = None
       self._serial_number = '14260000'
       self.send_state_machine = types.MethodType(original_send, self)
       self.validate_state_machine = types.MethodType(original_validate, self)

   patcher = patch.object(Bpod, "__init__", fake_init)
   patcher.start()
   atexit.register(patcher.stop)

.. doctest-code-block::
   :caption: Using a context manager to connect to a Bpod.
   :group: bpod-context-manager

   >>> from bpod_core.bpod import Bpod
   >>> with Bpod('/dev/ttyACM0') as bpod:
   ...     pass  # do things

.. tip::

   Serial port names such as ``/dev/ttyACM0`` are assigned by the OS and may change
   across reboots; the serial number, however, is a stable hardware identifier.

   If you do not know your device's serial number yet, connect once (auto-discover or
   by ``port``) and read :attr:`~bpod_core.bpod.Bpod.serial_number` off the object.
   Then use it in all subsequent scripts for a stable, reboot-proof connection:

   .. doctest-code-block::
      :group: bpod-context-manager

      >>> with Bpod() as bpod:
      ...     bpod.serial_number
      '14260000'

      >>> with Bpod(serial_number='14260000') as bpod:
      ...     pass  # do things ...

Sending and Running a State Machine
------------------------------------

Sending and running state machines are two separate steps:
:meth:`~bpod_core.bpod.Bpod.send_state_machine` compiles the FSM and
transfers it to the device;
:meth:`~bpod_core.bpod.Bpod.run_state_machine` starts its execution. Keeping them
separate allows the next trial's FSM to be pre-loaded while the current one is
still running (see :ref:`multi-trial-loop`).

By default :meth:`~bpod_core.bpod.Bpod.run_state_machine` blocks until the
trial completes:

.. literalinclude:: ../../../examples/minimal_examples/run_global_timer.py
   :language: python
   :start-at: fsm = StateMachine()

.. note::

   :meth:`~bpod_core.bpod.Bpod.get_data` is called *outside* the ``with``
   block in the example above. The connection is already closed at that point,
   but the data queue persists on the ``bpod`` object and is safe to drain
   after the context exits.

.. seealso::

   Refer to :doc:`../state_machines/index` for the full reference of the
   :class:`~bpod_core.fsm.StateMachine` object.

.. _multi-trial-loop:

Multi-Trial Loop
----------------

Pass ``blocking=False`` to return immediately after starting the trial. Use
:meth:`~bpod_core.bpod.Bpod.wait` to synchronise before retrieving data and
sending the next FSM:

.. code-block:: python

   from bpod_core.bpod import Bpod

   N_TRIALS = 100

   with Bpod() as bpod:
       bpod.send_state_machine(build_fsm(trial=0))
       bpod.run_state_machine(blocking=False)

       for trial in range(1, N_TRIALS):
           bpod.wait()
           data = bpod.get_data()
           process(data)
           bpod.send_state_machine(build_fsm(trial=trial))
           bpod.run_state_machine(blocking=False)

       bpod.wait()

   all_data = bpod.get_data(concat=True)

For the tightest possible inter-trial intervals, pass ``run_asap=True`` to
:meth:`~bpod_core.bpod.Bpod.send_state_machine`. The device then starts the
new FSM the instant the current trial exits, without waiting for a serial
round-trip. The trade-off is that you must commit to the next FSM *before*
inspecting the previous trial's data.

.. tip::

   Use ``blocking=False`` + :meth:`~bpod_core.bpod.Bpod.wait` when you need
   to process data or adjust parameters between trials. Use ``run_asap=True``
   when minimising inter-trial interval is the priority.

Retrieving and working with Trial Data
--------------------------------------

:meth:`~bpod_core.bpod.Bpod.get_data` pops one trial from the data queue, blocking until
data is available and returning the data as a Polars :class:`~polars.DataFrame`.
Each call to :meth:`~bpod_core.bpod.Bpod.get_data` returns a :class:`~polars.DataFrame`
with one row per event and the following columns:

- ``time`` – absolute Bpod timestamp (``Datetime(time_unit='us')``)
- ``trial`` – zero-based trial index (``UInt16``)
- ``state`` - state name (``Categorical``)
- ``type`` – event type (``Enum``)
- ``event`` – input event name, ``null`` for non-input rows (``Categorical``)
- ``channel`` – channel name (``Categorical``)
- ``value`` – channel value (``UInt8``)

To work with the data, standard Polars expressions apply. For example, to extract all
state transitions:

.. code-block:: python

   state_transitions = trial_data.filter(
       pl.col('type').is_in(['StateStart', 'StateEnd'])
   )

Or to get only input events:

.. code-block:: python

   inputs = trial_data.filter(pl.col('type') == 'InputEvent')

If you prefer to have a Pandas :class:`~pandas.DataFrame`, the data is easily converted:

.. code-block:: python

   pandas_dataframe = trial_data.to_pandas()

To efficiently store the trial data to disk, use the Parquet format:

.. code-block:: python

   pandas_dataframe = trial_data.write_parquet('filename.pqt')

Refer to the `Polars documentation <https://docs.pola.rs/>`_ for further details.
