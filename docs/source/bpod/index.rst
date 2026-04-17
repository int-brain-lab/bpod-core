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
   from unittest.mock import MagicMock

   original_run = Bpod.run
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
       self._bpod_finalizer = SimpleNamespace(detach=lambda: None)
       self._serial_device_finalizer = SimpleNamespace(detach=lambda: None)
       self._softcode_thread = MagicMock()
       self._hardware_hash = b''
       self._serial = None
       self._serial_number = '14260000'
       self.run = types.MethodType(original_run, self)
       self.validate_state_machine = types.MethodType(original_validate, self)

   patcher = patch.object(Bpod, "__init__", fake_init)
   patcher.start()
   atexit.register(patcher.stop)

.. testcode-code-block:: python3
   :caption: Using a context manager to connect to a Bpod.
   :group: bpod-context-manager

   from bpod_core.bpod import Bpod

   with Bpod('/dev/ttyACM0') as bpod:
       pass  # do things

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


Running a State Machine
-----------------------

To run a state machine on a Bpod device, use the :meth:`~bpod_core.bpod.Bpod.run` method
of your :class:`~bpod_core.bpod.Bpod` instance. It validates and compiles the state
machine, and finally enqueues it on the Bpod for immediate execution.
Calling :meth:`~bpod_core.bpod.Bpod.get_data` after a state machine run returns the
collected data as a Polars :class:`~polars.DataFrame`.

.. testcode-code-block:: python3
   :caption: Defining and running a state machine.
   :group: bpod-context-manager

   from bpod_core.bpod import Bpod
   from bpod_core.fsm import StateMachine

   # define a state machine
   fsm = StateMachine()
   fsm.add_state('Wait', transitions={'Port1_High': 'LightPort1', 'Port2_High': 'LightPort2'})
   fsm.add_state('LightPort1', timer=1, transitions={'Tup': '>exit'}, actions={'PWM1': 255})
   fsm.add_state('LightPort2', timer=1, transitions={'Tup': '>exit'}, actions={'PWM2': 255})

   # run the state machine
   with Bpod() as bpod:
       bpod.run(fsm)

   # collect the data
   data = bpod.get_data()

.. note::

   In the example above, :meth:`~bpod_core.bpod.Bpod.get_data` is called *outside* the
   context manager. The connection is already closed at that point, but the data queue
   persists on the :class:`~bpod_core.bpod.Bpod` object and is safe to drain after the
   context exits.

.. seealso::

   Refer to :doc:`../state_machines/index` for the full reference of the
   :class:`~bpod_core.fsm.StateMachine` object.


Running Several State Machines
------------------------------

You can run :meth:`~bpod_core.bpod.Bpod.run` several times in quick succession.
:class:`~bpod_core.bpod.Bpod` will automatically enqueue the state machines for
execution on the Bpod hardware. Subsequent state machines will run with zero inter-trial
downtime, allowing for continuous acquisition. When returning the data with
:meth:`~bpod_core.bpod.Bpod.get_data` the data from individual trials will automatically
be concatenated to a continuous Polars :class:`~polars.DataFrame`.

.. testcode-code-block:: python3
   :caption: Running several trials of the same state machine in immediate succession.
   :group: bpod-context-manager

   with Bpod() as bpod:
       for trial in range(100):
           bpod.run(fsm)

   data = bpod.get_data()

Similarly, you can define state machines on-the-fly *within* the loop. The
:meth:`~bpod_core.bpod.Bpod.run` method is non-blocking – as long as the individual
state machines take longer to execute than the time it takes to prepare and upload their
successors, they will run in a continuous fashion.

.. testcode-code-block:: python3
   :name: on_the_fly_fsm
   :caption: Generating state machines on the fly.
   :group: bpod-context-manager

   from random import random, randint

   with Bpod() as bpod:
       for trial in range(100):
           fsm = StateMachine()
           d = random() / 10  # random duration between 0 and 100 ms
           i = randint(0, 255)  # random PWM value between 0 and 255
           fsm.add_state('s1', timer=d, transitions={'Tup': 's2'}, actions={'PWM1': i})
           fsm.add_state('s2', timer=0.1, transitions={'Tup': '>exit'})
           bpod.run(fsm)

   data = bpod.get_data()


Data Format
-----------

.. testsetup:: polars

   import polars as pl
   from pathlib import Path
   from bpod_core.bpod.threads import _TRIAL_DATA_SCHEMA

   pqt_file = Path(_DOCS_STATIC) / "example_dataframe.pqt"
   data = pl.read_parquet(pqt_file)
   assert dict(data.schema) == _TRIAL_DATA_SCHEMA
   pl.Config.set_tbl_width_chars(200)

Data returned by :meth:`~bpod_core.bpod.Bpod.get_data` is organized in tabular form as a
Polars :class:`~polars.DataFrame`. For the state machine in :numref:`on_the_fly_fsm` you
may get something like this:

.. doctest-code-block::
   :caption: A Polars DataFrame as returned by :meth:`~bpod_core.bpod.Bpod.get_data`
   :group: polars

   >>> data
   shape: (1_100, 8)
   ┌────────────────────────────┬───────┬──────────────────┬───────┬─────────────────┬───────┬─────────┬───────┐
   │ time                       ┆ trial ┆ state machine    ┆ state ┆ type            ┆ event ┆ channel ┆ value │
   │ ---                        ┆ ---   ┆ ---              ┆ ---   ┆ ---             ┆ ---   ┆ ---     ┆ ---   │
   │ datetime[μs]               ┆ u16   ┆ cat              ┆ cat   ┆ enum            ┆ cat   ┆ cat     ┆ u8    │
   ╞════════════════════════════╪═══════╪══════════════════╪═══════╪═════════════════╪═══════╪═════════╪═══════╡
   │ 2026-04-16 20:29:12.948426 ┆ 0     ┆ d1af27e5c2b13891 ┆ null  ┆ TrialStart      ┆ null  ┆ null    ┆ null  │
   │ 2026-04-16 20:29:12.948426 ┆ 0     ┆ d1af27e5c2b13891 ┆ s1    ┆ StateStart      ┆ null  ┆ null    ┆ null  │
   │ 2026-04-16 20:29:12.948426 ┆ 0     ┆ d1af27e5c2b13891 ┆ s1    ┆ OutputAction    ┆ null  ┆ PWM1    ┆ 235   │
   │ 2026-04-16 20:29:13.022426 ┆ 0     ┆ d1af27e5c2b13891 ┆ s1    ┆ InputEvent      ┆ Tup   ┆ null    ┆ null  │
   │ 2026-04-16 20:29:13.022426 ┆ 0     ┆ d1af27e5c2b13891 ┆ s1    ┆ StateEnd        ┆ null  ┆ null    ┆ null  │
   │ …                          ┆ …     ┆ …                ┆ …     ┆ …               ┆ …     ┆ …       ┆ …     │
   │ 2026-04-16 20:29:19.121326 ┆ 99    ┆ 1719d07df94acabf ┆ s2    ┆ OutputAction    ┆ null  ┆ PWM1    ┆ 0     │
   │ 2026-04-16 20:29:19.131326 ┆ 99    ┆ 1719d07df94acabf ┆ s2    ┆ InputEvent      ┆ Tup   ┆ null    ┆ null  │
   │ 2026-04-16 20:29:19.131326 ┆ 99    ┆ 1719d07df94acabf ┆ s2    ┆ StateEnd        ┆ null  ┆ null    ┆ null  │
   │ 2026-04-16 20:29:19.131426 ┆ 99    ┆ 1719d07df94acabf ┆ null  ┆ TrialEnd        ┆ null  ┆ null    ┆ null  │
   │ 2026-04-16 20:29:19.131328 ┆ 99    ┆ 1719d07df94acabf ┆ null  ┆ TrialEndControl ┆ null  ┆ null    ┆ null  │
   └────────────────────────────┴───────┴──────────────────┴───────┴─────────────────┴───────┴─────────┴───────┘

The data is organized into one row per event and the following columns:

- ``time`` – absolute Bpod timestamp (:class:`~polars.datatypes.Datetime`)
- ``trial`` – zero-based trial index (:class:`~polars.datatypes.UInt16`)
- ``state machine`` – hash of the state machine (``Categorical``)
- ``state`` - state name (:class:`~polars.datatypes.Categorical`)
- ``type`` – event type (:class:`~polars.datatypes.Enum`)
- ``event`` – input event name, ``null`` for non-input rows (:class:`~polars.datatypes.Categorical`)
- ``channel`` – channel name (:class:`~polars.datatypes.Categorical`)
- ``value`` – channel value (:class:`~polars.datatypes.UInt8`)


Absolute vs. Relative Timestamps
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The data is stored using absolute timestamps rather than relative ones. This avoids
ambiguity when combining data across trials, state machines, or external data sources,
and preserves the original temporal ordering without requiring additional context.
Relative timestamps can always be derived on demand by subtracting the first timestamp
from the ``time`` column. Notice how the inherent unit of the ``time`` column is
preserved during this operation, with the resulting column automatically taking on the
:class:`~polars.datatypes.Duration` type:

.. doctest-code-block::
   :caption: Transforming absolute timestamps into relative timestamps.
   :group: polars

   >>> data.with_columns(pl.col("time") - pl.col("time").first())
   shape: (1_100, 8)
   ┌──────────────┬───────┬──────────────────┬───────┬─────────────────┬───────┬─────────┬───────┐
   │ time         ┆ trial ┆ state machine    ┆ state ┆ type            ┆ event ┆ channel ┆ value │
   │ ---          ┆ ---   ┆ ---              ┆ ---   ┆ ---             ┆ ---   ┆ ---     ┆ ---   │
   │ duration[μs] ┆ u16   ┆ cat              ┆ cat   ┆ enum            ┆ cat   ┆ cat     ┆ u8    │
   ╞══════════════╪═══════╪══════════════════╪═══════╪═════════════════╪═══════╪═════════╪═══════╡
   │ 0µs          ┆ 0     ┆ d1af27e5c2b13891 ┆ null  ┆ TrialStart      ┆ null  ┆ null    ┆ null  │
   │ 0µs          ┆ 0     ┆ d1af27e5c2b13891 ┆ s1    ┆ StateStart      ┆ null  ┆ null    ┆ null  │
   │ 0µs          ┆ 0     ┆ d1af27e5c2b13891 ┆ s1    ┆ OutputAction    ┆ null  ┆ PWM1    ┆ 235   │
   │ 74ms         ┆ 0     ┆ d1af27e5c2b13891 ┆ s1    ┆ InputEvent      ┆ Tup   ┆ null    ┆ null  │
   │ 74ms         ┆ 0     ┆ d1af27e5c2b13891 ┆ s1    ┆ StateEnd        ┆ null  ┆ null    ┆ null  │
   │ …            ┆ …     ┆ …                ┆ …     ┆ …               ┆ …     ┆ …       ┆ …     │
   │ 6s 172900µs  ┆ 99    ┆ 1719d07df94acabf ┆ s2    ┆ OutputAction    ┆ null  ┆ PWM1    ┆ 0     │
   │ 6s 182900µs  ┆ 99    ┆ 1719d07df94acabf ┆ s2    ┆ InputEvent      ┆ Tup   ┆ null    ┆ null  │
   │ 6s 182900µs  ┆ 99    ┆ 1719d07df94acabf ┆ s2    ┆ StateEnd        ┆ null  ┆ null    ┆ null  │
   │ 6s 183ms     ┆ 99    ┆ 1719d07df94acabf ┆ null  ┆ TrialEnd        ┆ null  ┆ null    ┆ null  │
   │ 6s 182902µs  ┆ 99    ┆ 1719d07df94acabf ┆ null  ┆ TrialEndControl ┆ null  ┆ null    ┆ null  │
   └──────────────┴───────┴──────────────────┴───────┴─────────────────┴───────┴─────────┴───────┘


Filtering
^^^^^^^^^

The different columns are designed to facilitate filtering the data. If, for instance
, you wanted to look at all events that affect the output channel ``PWM1``, you could
filter the table like so:

.. doctest-code-block::
   :caption: Filtering by events is straightforward.
   :group: polars

   >>> import polars as pl
   >>> data.filter(pl.col("channel") == "PWM1")
   shape: (200, 8)
   ┌────────────────────────────┬───────┬──────────────────┬───────┬──────────────┬───────┬─────────┬───────┐
   │ time                       ┆ trial ┆ state machine    ┆ state ┆ type         ┆ event ┆ channel ┆ value │
   │ ---                        ┆ ---   ┆ ---              ┆ ---   ┆ ---          ┆ ---   ┆ ---     ┆ ---   │
   │ datetime[μs]               ┆ u16   ┆ cat              ┆ cat   ┆ enum         ┆ cat   ┆ cat     ┆ u8    │
   ╞════════════════════════════╪═══════╪══════════════════╪═══════╪══════════════╪═══════╪═════════╪═══════╡
   │ 2026-04-16 20:29:12.948426 ┆ 0     ┆ d1af27e5c2b13891 ┆ s1    ┆ OutputAction ┆ null  ┆ PWM1    ┆ 235   │
   │ 2026-04-16 20:29:13.022426 ┆ 0     ┆ d1af27e5c2b13891 ┆ s2    ┆ OutputAction ┆ null  ┆ PWM1    ┆ 0     │
   │ 2026-04-16 20:29:13.032526 ┆ 1     ┆ 007c1ac779aa4864 ┆ s1    ┆ OutputAction ┆ null  ┆ PWM1    ┆ 154   │
   │ 2026-04-16 20:29:13.074626 ┆ 1     ┆ 007c1ac779aa4864 ┆ s2    ┆ OutputAction ┆ null  ┆ PWM1    ┆ 0     │
   │ 2026-04-16 20:29:13.084726 ┆ 2     ┆ 4c848e03a2b511e4 ┆ s1    ┆ OutputAction ┆ null  ┆ PWM1    ┆ 214   │
   │ …                          ┆ …     ┆ …                ┆ …     ┆ …            ┆ …     ┆ …       ┆ …     │
   │ 2026-04-16 20:29:18.998726 ┆ 97    ┆ 1257c55134308481 ┆ s2    ┆ OutputAction ┆ null  ┆ PWM1    ┆ 0     │
   │ 2026-04-16 20:29:19.008826 ┆ 98    ┆ 7d51489ce6f844f8 ┆ s1    ┆ OutputAction ┆ null  ┆ PWM1    ┆ 27    │
   │ 2026-04-16 20:29:19.057726 ┆ 98    ┆ 7d51489ce6f844f8 ┆ s2    ┆ OutputAction ┆ null  ┆ PWM1    ┆ 0     │
   │ 2026-04-16 20:29:19.067826 ┆ 99    ┆ 1719d07df94acabf ┆ s1    ┆ OutputAction ┆ null  ┆ PWM1    ┆ 11    │
   │ 2026-04-16 20:29:19.121326 ┆ 99    ┆ 1719d07df94acabf ┆ s2    ┆ OutputAction ┆ null  ┆ PWM1    ┆ 0     │
   └────────────────────────────┴───────┴──────────────────┴───────┴──────────────┴───────┴─────────┴───────┘

Plotting
^^^^^^^^

Using filtering, it becomes relatively straightforward to extract data for plotting.
We use data returned by the state machines in :numref:`on_the_fly_fsm` to produce a
stairstep graph of the ``PWM1`` output channel over relative time:

.. plot::
   :context:
   :include-source: false

   import polars as pl
   from pathlib import Path
   from bpod_core.bpod.threads import _TRIAL_DATA_SCHEMA

   pqt_file = Path(_DOCS_STATIC) / "example_dataframe.pqt"
   data = pl.read_parquet(pqt_file)

.. plot::
   :caption: Visualising values of the ``PWM1`` channel across 10 trials.
   :context:

   import matplotlib.pyplot as plt
   import polars as pl

   # filter data to values of the 'PWM1' channel across the first 10 trials
   filtered = data.filter(
       (pl.col("channel") == "PWM1") &
       (pl.col("trial") <= 10)
   )

   # extract two columns, relative 'time' and 'value'
   x = filtered['time'] - filtered['time'].first()
   y = filtered['value']

   # matplotlib doesn't play nice with timedelta values, so we convert them to float
   x = x.dt.total_seconds(fractional=True)

   # plot
   plt.step(x, y, where='post')
   plt.xlabel('Time (s)')
   plt.ylabel('PWM Value')
   plt.show()


Storing Data
^^^^^^^^^^^^

Since most of the columns consist of `Categorical data <https://docs.pola.rs/user-guide/expressions/categorical-data-and-enums/>`_,
data can be very efficiently written to disk as a `Parquet <https://en.wikipedia.org/wiki/Apache_Parquet>`_
file using :meth:`~polars.DataFrame.write_parquet`:

.. code-block:: python

   data.write_parquet('data.pqt')

If you prefer Comma-Separated Values (CSV)—for example, to inspect the data in a
spreadsheet editor such as Excel—use the :meth:`~polars.DataFrame.write_csv` method
instead:

.. code-block:: python

   data.write_csv('data.csv')

Note that CSV files are typically about an order of magnitude larger than their Parquet
equivalents.


Pandas
^^^^^^

If you prefer `Pandas <https://pandas.pydata.org/>`_ over Polars, you can easily convert
the data to a Pandas :class:`~pandas.DataFrame` using the built-in
:meth:`~polars.DataFrame.to_pandas` method:

.. code-block:: python

   pandas_data = data.to_pandas()


More on Polars
^^^^^^^^^^^^^^

The examples above only cover a small subset of what is possible with Polars. You can
use the full expression system to filter, transform, aggregate, and analyze the data
efficiently, even for large datasets.

For a comprehensive overview of available operations and patterns, refer to the
`Polars documentation <https://docs.pola.rs/>`_.
