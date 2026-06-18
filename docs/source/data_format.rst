Data Format
===========

.. testsetup:: polars

   import polars as pl
   import pandas as pd
   from pathlib import Path
   from bpod_core.bpod._threads import _TRIAL_DATA_SCHEMA

   pqt_file = Path(_DOCS_STATIC) / "example_dataframe.pqt"
   data = pl.read_parquet(pqt_file)
   assert dict(data.schema) == _TRIAL_DATA_SCHEMA
   pl.Config.set_tbl_width_chars(200)
   pd.set_option('display.width', 200)
   pd.set_option('display.max_columns', None)

Data returned by :meth:`~bpod_core.bpod.Bpod.get_data` is organized in tabular form as a
Polars :class:`~polars.DataFrame` with the following columns:

.. list-table::
   :header-rows: 1

   * - Name
     - Datatype
     - Description
   * - ``time``
     - :class:`~polars.datatypes.Datetime`
     - absolute timestamp, see section `Timestamps`_ below.
   * - ``trial``
     - :class:`~polars.datatypes.UInt16`
     - zero-based trial index.
   * - ``state machine``
     - :class:`~polars.datatypes.Categorical`
     - state machine hash, see :meth:`~bpod_core.fsm.StateMachine.hash`.
   * - ``state``
     - :class:`~polars.datatypes.Categorical`
     - name of the current :class:`~bpod_core.fsm.State`.
   * - ``type``
     - :class:`~polars.datatypes.Enum`
     - type of the current event.
   * - ``event``
     - :class:`~polars.datatypes.Categorical`
     - input event name; only for events of type ``InputEvent``.
   * - ``channel``
     - :class:`~polars.datatypes.Categorical`
     - name of the respective input or output channel.
   * - ``value``
     - :class:`~polars.datatypes.UInt8`
     - value of the channel.

For the state machine in :numref:`on_the_fly_fsm` the returned
:class:`~polars.DataFrame` could look like this:

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


Timestamps
----------

The data is stored using absolute timestamps rather than relative ones. This avoids
ambiguity when combining data across trials, state machines, or external data sources,
and preserves the original temporal ordering without requiring additional context.

Absolute time is computed from the Bpod's hardware timer (microseconds since boot),
offset by the host computer's system time at instantiation of the
:class:`~bpod_core.bpod.Bpod` class. Because the two clocks are independent, they will
drift apart over time. You can call :meth:`~bpod_core.bpod.Bpod.reset_session_clock`
outside of a state machine run to resynchronize them.

Relative timestamps can be derived on demand by subtracting the first timestamp from the
``time`` column. The inherent unit of the ``time`` column is preserved during this
operation, and the result automatically takes on the :class:`~polars.datatypes.Duration`
type:

.. doctest-code-block::
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
---------

The different columns are designed to facilitate filtering the data. If, for instance,
you wanted to look at all events that affect the output channel ``PWM1``, you could
filter the table like so:

.. doctest-code-block::
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
--------

Filtering makes it straightforward to extract data for plotting. We use data returned
by the state machines in :numref:`on_the_fly_fsm` to produce a stairstep graph of the
``PWM1`` output channel over relative time:

.. plot::
   :context:
   :include-source: false

   import polars as pl
   from pathlib import Path
   from bpod_core.bpod._threads import _TRIAL_DATA_SCHEMA

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

.. seealso::

   See the
   `Polars documentation <https://docs.pola.rs/user-guide/misc/visualization/>`__
   for details about the support for various visualization libraries.


Storing Data
------------

Because most of the columns consist of `Categorical data <https://docs.pola.rs/user-guide/expressions/categorical-data-and-enums/>`_,
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
------

If you prefer `Pandas <https://pandas.pydata.org/>`__ over
`Polars <https://pola.rs/>`__, you can easily convert the data to a
:class:`pandas.DataFrame` using the built-in :meth:`~polars.DataFrame.to_pandas` method:

.. doctest-code-block::
   :group: polars

   >>> data.to_pandas()
                              time  trial     state machine state             type event channel  value
   0    2026-04-16 20:29:12.948426      0  d1af27e5c2b13891   NaN       TrialStart   NaN     NaN    NaN
   1    2026-04-16 20:29:12.948426      0  d1af27e5c2b13891    s1       StateStart   NaN     NaN    NaN
   2    2026-04-16 20:29:12.948426      0  d1af27e5c2b13891    s1     OutputAction   NaN    PWM1  235.0
   3    2026-04-16 20:29:13.022426      0  d1af27e5c2b13891    s1       InputEvent   Tup     NaN    NaN
   4    2026-04-16 20:29:13.022426      0  d1af27e5c2b13891    s1         StateEnd   NaN     NaN    NaN
   ...                         ...    ...               ...   ...              ...   ...     ...    ...
   1095 2026-04-16 20:29:19.121326     99  1719d07df94acabf    s2     OutputAction   NaN    PWM1    0.0
   1096 2026-04-16 20:29:19.131326     99  1719d07df94acabf    s2       InputEvent   Tup     NaN    NaN
   1097 2026-04-16 20:29:19.131326     99  1719d07df94acabf    s2         StateEnd   NaN     NaN    NaN
   1098 2026-04-16 20:29:19.131426     99  1719d07df94acabf   NaN         TrialEnd   NaN     NaN    NaN
   1099 2026-04-16 20:29:19.131328     99  1719d07df94acabf   NaN  TrialEndControl   NaN     NaN    NaN
   <BLANKLINE>
   [1100 rows x 8 columns]

.. note::

   This operation requires that both `pandas <https://pandas.pydata.org/>`__ and
   `PyArrow <https://arrow.apache.org/docs/python/>`__ are installed.


More on Polars
--------------

The examples above only cover a small subset of what is possible with Polars. You can
use the full expression system to filter, transform, aggregate, and analyze the data
efficiently, even for large datasets. For a comprehensive overview of available
operations and patterns, refer to the `Polars documentation <https://docs.pola.rs/>`__.
