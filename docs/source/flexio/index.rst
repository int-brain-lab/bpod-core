.. meta::
   :description: Using the FlexIO channels of a Bpod 2+ as digital or analog inputs
      and outputs.

FlexIO Channels
===============

.. testsetup:: *

   import polars as pl
   from bpod_core.bpod._threads import _TRIAL_DATA_SCHEMA

   pl.Config.set_tbl_width_chars(200)
   pl.Config.set_tbl_rows(-1)


   def load_data(name, fsm):
       """Trial data recorded for an example, checked against the example's code."""
       data = pl.read_parquet(_DOCS_DATA / 'flexio' / f'{name}.pqt')
       assert dict(data.schema) == _TRIAL_DATA_SCHEMA
       hashes = data['State Machine'].cast(pl.String).unique().to_list()
       assert hashes == [fsm.hash.hex()]
       return data


   def load_analog_data(name):
       """Analog data recorded for an example."""
       analog_data = pl.read_parquet(_DOCS_DATA / 'flexio' / f'{name}_analog.pqt')
       assert dict(analog_data.schema) == {
           'Time': pl.Datetime('us', 'UTC'),
           'Trial': pl.UInt32,
           'Flex1': pl.UInt16,
       }
       return analog_data

.. testcleanup:: *

   pl.Config.restore_defaults()

The `Bpod State Machine r2+ <https://sanworks.io/shop/viewproduct?productID=1036>`_
has four connectors labeled ``Flex1`` to ``Flex4``. Unlike the BNC connectors or the
behavior ports, which have a fixed purpose, each FlexIO connector can do one of four
jobs, which you choose at the start of a session:

.. list-table::
   :header-rows: 1
   :widths: 22 38 40

   * - Job
     - What it does
     - Typical uses
   * - Digital input
     - Detects on/off signals and reports when they change.
     - A beam-break sensor, a TTL signal from another device.
   * - Digital output
     - Sends on/off signals.
     - Triggering a camera, a stimulus, or another device.
   * - Analog input
     - Measures a voltage, continuously and as often as 1000 times per second.
     - A lick sensor, a photodiode, a pressure sensor.
   * - Analog output
     - Produces a voltage that you choose.
     - The brightness of an LED driver, the power of a laser.

.. admonition:: Key Concepts
   :class: tip

   Digital and analog
      A digital signal is either *off* (0 V) or *on* (5 V). An analog signal can take
      any voltage in between.

   Inputs and outputs
      An input brings a signal *into* the Bpod, where it can trigger state transitions.
      An output sends a signal *out*, controlled by the states of your state machine.

   Millivolts
      bpod-core expresses all FlexIO voltages in millivolts, from 0 to 5000: a value of
      2500 means 2.5 V.


Setting Up the Channels
-----------------------

You choose a channel's job through :attr:`~bpod_core.bpod.Bpod.flex_io`, before
running the first state machine:

.. code-block:: python
   :caption: Making ``Flex1`` a digital input.

   from bpod_core.bpod import Bpod
   from bpod_core.bpod.constants import FlexIOChannelType

   with Bpod() as bpod:
       bpod.flex_io['Flex1'].channel_type = FlexIOChannelType.DIGITAL_INPUT

Channels you don't set up stay off. Once the first state machine has run, the channels
keep their jobs until you close the connection to the Bpod. A channel's job also
decides which names it adds to your state machines (see :doc:`/state_machines/index`
for how to list all valid names).

FlexIO channels are used through state machines only: unlike the behavior ports, you
can't read or set them directly from Python.


Digital Outputs
---------------

A FlexIO channel configured as digital output is controlled with the ``Flex{N}`` action:
a value of 1 switches it on (5 V), and 0 switches it off. As with the Bpod's other
outputs, any state that doesn't set the output switches it off, so a short state makes a
short pulse.

In this example, a camera that takes a picture for each trigger pulse is connected to
``Flex1``, which is set up as a digital output. A 10 ms pulse makes the camera take a
picture, right before port 1 lights up:

.. dark-light-figure:: fsm_flexio_digital_output

   A 10 ms trigger pulse, followed by the port light.

.. fsm_codeblock::
   :caption: Triggering a camera on ``Flex1``.
   :group: digital_output
   :filename: fsm_flexio_digital_output.svg
   :linenos:

   from bpod_core.bpod import Bpod
   from bpod_core.bpod.constants import FlexIOChannelType
   from bpod_core.fsm import StateMachine
   from unittest.mock import MagicMock as Bpod  # hide

   fsm = StateMachine()
   fsm.add_state(
       name='TriggerCamera',
       timer=0.01,
       transitions={'Tup': 'LightOn'},
       actions={'Flex1': 1},
   )
   fsm.add_state(
       name='LightOn',
       timer=1,
       transitions={'Tup': '>exit'},
       actions={'PWM1': 255},
   )

   with Bpod() as bpod:
       bpod.flex_io['Flex1'].channel_type = FlexIOChannelType.DIGITAL_OUTPUT
       bpod.run(fsm)

   data = bpod.get_data()

.. testcode:: digital_output
   :hide:

   data = load_data('digital_output', fsm)

.. dropdown:: Trial data of the camera-trigger example
   :icon: table

   .. doctest-code-block::
      :group: digital_output

      >>> data
      shape: (13, 8)
      ┌────────────────────────────────┬───────┬──────────────────┬───────────────┬─────────────────┬───────┬─────────┬───────┐
      │ Time                           ┆ Trial ┆ State Machine    ┆ State         ┆ Type            ┆ Event ┆ Channel ┆ Value │
      │ ---                            ┆ ---   ┆ ---              ┆ ---           ┆ ---             ┆ ---   ┆ ---     ┆ ---   │
      │ datetime[μs, UTC]              ┆ u32   ┆ cat              ┆ cat           ┆ enum            ┆ cat   ┆ cat     ┆ u16   │
      ╞════════════════════════════════╪═══════╪══════════════════╪═══════════════╪═════════════════╪═══════╪═════════╪═══════╡
      │ 2026-10-22 15:12:38.348493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ null          ┆ TrialStart      ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:38.348493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ TriggerCamera ┆ StateStart      ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:38.348493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ TriggerCamera ┆ OutputAction    ┆ null  ┆ Flex1   ┆ 1     │
      │ 2026-10-22 15:12:38.358493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ TriggerCamera ┆ InputEvent      ┆ Tup   ┆ null    ┆ null  │
      │ 2026-10-22 15:12:38.358493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ TriggerCamera ┆ StateEnd        ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:38.358493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ LightOn       ┆ StateStart      ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:38.358493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ LightOn       ┆ OutputAction    ┆ null  ┆ Flex1   ┆ 0     │
      │ 2026-10-22 15:12:38.358493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ LightOn       ┆ OutputAction    ┆ null  ┆ PWM1    ┆ 255   │
      │ 2026-10-22 15:12:39.358493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ LightOn       ┆ InputEvent      ┆ Tup   ┆ null    ┆ null  │
      │ 2026-10-22 15:12:39.358493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ LightOn       ┆ StateEnd        ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:39.358493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ null          ┆ OutputAction    ┆ null  ┆ PWM1    ┆ 0     │
      │ 2026-10-22 15:12:39.358493 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ null          ┆ TrialEnd        ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:39.358495 UTC ┆ 0     ┆ 86d5ab27f2ecd65d ┆ null          ┆ TrialEndControl ┆ null  ┆ null    ┆ null  │
      └────────────────────────────────┴───────┴──────────────────┴───────────────┴─────────────────┴───────┴─────────┴───────┘

The pulse shows up as two output actions on ``Flex1``: 1 when ``TriggerCamera`` begins,
and 0 ten milliseconds later, when ``LightOn`` begins and doesn't set the channel. See
:doc:`/data_format` for a description of all columns.


Digital Inputs
--------------

A FlexIO channel configured as digital input reports an event whenever its signal
changes: ``Flex{N}_High`` when it turns on, ``Flex{N}_Low`` when it turns off. Like any
other event, these can move your state machine to another state, and they are recorded
in the trial data.

In this example, a beam-break sensor is connected to ``Flex1``, which is set up as a
digital input. The state machine waits up to 10 seconds for the subject to break the
light beam, and then lights up port 1 for a second:

.. dark-light-figure:: fsm_flexio_digital_input

   A beam break on ``Flex1`` leads to the ``LightOn`` state.

.. fsm_codeblock::
   :caption: Waiting for a beam-break sensor on ``Flex1``.
   :group: digital_input
   :filename: fsm_flexio_digital_input.svg
   :linenos:

   from bpod_core.bpod import Bpod
   from bpod_core.bpod.constants import FlexIOChannelType
   from bpod_core.fsm import StateMachine
   from unittest.mock import MagicMock as Bpod  # hide

   fsm = StateMachine()
   fsm.add_state(
       name='WaitForBeamBreak',
       timer=10,
       transitions={'Flex1_High': 'LightOn', 'Tup': '>exit'},
   )
   fsm.add_state(
       name='LightOn',
       timer=1,
       transitions={'Tup': '>exit'},
       actions={'PWM1': 255},
   )

   with Bpod() as bpod:
       bpod.flex_io['Flex1'].channel_type = FlexIOChannelType.DIGITAL_INPUT
       bpod.run(fsm)

   data = bpod.get_data()

.. testcode:: digital_input
   :hide:

   data = load_data('digital_input', fsm)

.. dropdown:: Trial data of the beam-break example
   :icon: table

   .. doctest-code-block::
      :group: digital_input

      >>> data
      shape: (12, 8)
      ┌────────────────────────────────┬───────┬──────────────────┬──────────────────┬─────────────────┬────────────┬─────────┬───────┐
      │ Time                           ┆ Trial ┆ State Machine    ┆ State            ┆ Type            ┆ Event      ┆ Channel ┆ Value │
      │ ---                            ┆ ---   ┆ ---              ┆ ---              ┆ ---             ┆ ---        ┆ ---     ┆ ---   │
      │ datetime[μs, UTC]              ┆ u32   ┆ cat              ┆ cat              ┆ enum            ┆ cat        ┆ cat     ┆ u16   │
      ╞════════════════════════════════╪═══════╪══════════════════╪══════════════════╪═════════════════╪════════════╪═════════╪═══════╡
      │ 2026-10-22 15:12:39.706105 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ null             ┆ TrialStart      ┆ null       ┆ null    ┆ null  │
      │ 2026-10-22 15:12:39.706105 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ WaitForBeamBreak ┆ StateStart      ┆ null       ┆ null    ┆ null  │
      │ 2026-10-22 15:12:41.006205 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ WaitForBeamBreak ┆ InputEvent      ┆ Flex1_High ┆ Flex1   ┆ 1     │
      │ 2026-10-22 15:12:41.006205 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ WaitForBeamBreak ┆ StateEnd        ┆ null       ┆ null    ┆ null  │
      │ 2026-10-22 15:12:41.006205 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ LightOn          ┆ StateStart      ┆ null       ┆ null    ┆ null  │
      │ 2026-10-22 15:12:41.006205 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ LightOn          ┆ OutputAction    ┆ null       ┆ PWM1    ┆ 255   │
      │ 2026-10-22 15:12:41.256205 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ LightOn          ┆ InputEvent      ┆ Flex1_Low  ┆ Flex1   ┆ 0     │
      │ 2026-10-22 15:12:42.006205 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ LightOn          ┆ InputEvent      ┆ Tup        ┆ null    ┆ null  │
      │ 2026-10-22 15:12:42.006205 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ LightOn          ┆ StateEnd        ┆ null       ┆ null    ┆ null  │
      │ 2026-10-22 15:12:42.006205 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ null             ┆ OutputAction    ┆ null       ┆ PWM1    ┆ 0     │
      │ 2026-10-22 15:12:42.006205 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ null             ┆ TrialEnd        ┆ null       ┆ null    ┆ null  │
      │ 2026-10-22 15:12:42.006207 UTC ┆ 0     ┆ 0c7473529cdbc1e6 ┆ null             ┆ TrialEndControl ┆ null       ┆ null    ┆ null  │
      └────────────────────────────────┴───────┴──────────────────┴──────────────────┴─────────────────┴────────────┴─────────┴───────┘

The beam break is recorded as a ``Flex1_High`` event, which ends ``WaitForBeamBreak``
and starts ``LightOn``. When the subject leaves the beam again, a ``Flex1_Low`` event
follows, this time in ``LightOn``.


Analog Outputs
--------------

A FlexIO channel configured as analog output is also controlled with the ``Flex{N}``
action, but its value is a voltage in millivolts, from 0 to 5000. Any state that doesn't
set the output returns it to 0 V.

In this example, an LED driver is connected to ``Flex4``, which is set up as an analog
output. The LED is dim, brighter and fully bright, one second each:

.. dark-light-figure:: fsm_flexio_analog_output

   Three brightness levels, one second each.

.. fsm_codeblock::
   :caption: Changing the brightness of an LED through ``Flex4``.
   :group: analog_output
   :filename: fsm_flexio_analog_output.svg
   :linenos:

   from bpod_core.bpod import Bpod
   from bpod_core.bpod.constants import FlexIOChannelType
   from bpod_core.fsm import StateMachine
   from unittest.mock import MagicMock as Bpod  # hide

   fsm = StateMachine()
   fsm.add_state(
       name='Dim',
       timer=1,
       transitions={'Tup': 'Brighter'},
       actions={'Flex4': 1000},
   )
   fsm.add_state(
       name='Brighter',
       timer=1,
       transitions={'Tup': 'FullyBright'},
       actions={'Flex4': 2500},
   )
   fsm.add_state(
       name='FullyBright',
       timer=1,
       transitions={'Tup': '>exit'},
       actions={'Flex4': 5000},
   )

   with Bpod() as bpod:
       bpod.flex_io['Flex4'].channel_type = FlexIOChannelType.ANALOG_OUTPUT
       bpod.run(fsm)

   data = bpod.get_data()

.. testcode:: analog_output
   :hide:

   data = load_data('analog_output', fsm)

.. dropdown:: Trial data of the LED example
   :icon: table

   .. doctest-code-block::
      :group: analog_output

      >>> data
      shape: (16, 8)
      ┌────────────────────────────────┬───────┬──────────────────┬─────────────┬─────────────────┬───────┬─────────┬───────┐
      │ Time                           ┆ Trial ┆ State Machine    ┆ State       ┆ Type            ┆ Event ┆ Channel ┆ Value │
      │ ---                            ┆ ---   ┆ ---              ┆ ---         ┆ ---             ┆ ---   ┆ ---     ┆ ---   │
      │ datetime[μs, UTC]              ┆ u32   ┆ cat              ┆ cat         ┆ enum            ┆ cat   ┆ cat     ┆ u16   │
      ╞════════════════════════════════╪═══════╪══════════════════╪═════════════╪═════════════════╪═══════╪═════════╪═══════╡
      │ 2026-10-22 15:12:42.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ null        ┆ TrialStart      ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:42.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ Dim         ┆ StateStart      ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:42.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ Dim         ┆ OutputAction    ┆ null  ┆ Flex4   ┆ 1000  │
      │ 2026-10-22 15:12:43.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ Dim         ┆ InputEvent      ┆ Tup   ┆ null    ┆ null  │
      │ 2026-10-22 15:12:43.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ Dim         ┆ StateEnd        ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:43.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ Brighter    ┆ StateStart      ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:43.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ Brighter    ┆ OutputAction    ┆ null  ┆ Flex4   ┆ 2501  │
      │ 2026-10-22 15:12:44.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ Brighter    ┆ InputEvent      ┆ Tup   ┆ null    ┆ null  │
      │ 2026-10-22 15:12:44.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ Brighter    ┆ StateEnd        ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:44.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ FullyBright ┆ StateStart      ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:44.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ FullyBright ┆ OutputAction    ┆ null  ┆ Flex4   ┆ 5000  │
      │ 2026-10-22 15:12:45.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ FullyBright ┆ InputEvent      ┆ Tup   ┆ null    ┆ null  │
      │ 2026-10-22 15:12:45.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ FullyBright ┆ StateEnd        ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:45.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ null        ┆ OutputAction    ┆ null  ┆ Flex4   ┆ 0     │
      │ 2026-10-22 15:12:45.248130 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ null        ┆ TrialEnd        ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 15:12:45.248132 UTC ┆ 0     ┆ df3e710ec2d09d79 ┆ null        ┆ TrialEndControl ┆ null  ┆ null    ┆ null  │
      └────────────────────────────────┴───────┴──────────────────┴─────────────┴─────────────────┴───────┴─────────┴───────┘

Each voltage shows up as an output action on ``Flex4`` when its state begins, followed
by a 0 when the trial ends. Values are rounded to the closest voltage the Bpod can
produce.

.. note::
   Throughout bpod-core, voltages are represented as 16-bit integer millivolts rather
   than floating-point volts. This allows the FlexIO system's 12-bit ADC codes to be
   stored both efficiently and losslessly while keeping the values easy to interpret.
   Using floating-point values would require 32 bits, doubling the storage requirements.

   There is a small, mostly theoretical, quantization effect when converting the ADC
   readings to integer millivolts. In practice, this effect is negligible compared with
   other sources of noise. Nevertheless, for completeness, the original ADC codes can
   be reconstructed from the millivolt values as:

   .. code-block:: python

      adc_code = round(millivolts * 4095 / 5000)

   and the corresponding voltage can be calculated as:

   .. code-block:: python

      volts = adc_code * 5 / 4095


Analog Inputs
-------------

A FlexIO channel configured as analog input does two things: it records the voltage
continuously, and it can report events when the voltage passes levels you choose.

Recording a Signal
^^^^^^^^^^^^^^^^^^

The Bpod measures all analog inputs together, up to 1000 times per second. Two
settings, which apply to all analog inputs, control the measurements:

- :attr:`~bpod_core.bpod.abc.AbstractFlexIO.analog_sampling_rate`: the number of
  measurements per second, from 1 to 1000 (default: 1000).
- :attr:`~bpod_core.bpod.abc.AbstractFlexIO.n_reads_per_sample`: how many readings the
  Bpod averages for each measurement, from 1 to 4 (default: 3). More readings make the
  measurement less noisy.

Recording starts with the first trial and continues between trials, until you close
the connection. :meth:`~bpod_core.bpod.Bpod.get_analog_data` returns all measurements
so far. In this example, a light sensor on ``Flex1`` measures port 1's light, which is
on for 5 seconds. ``Flex1`` is set up as an analog input and recorded 500 times per
second:

.. fsm_codeblock::
   :caption: Recording ``Flex1`` 500 times per second.
   :group: recording
   :linenos:

   from bpod_core.bpod import Bpod
   from bpod_core.bpod.constants import FlexIOChannelType
   from bpod_core.fsm import StateMachine
   from unittest.mock import MagicMock as Bpod  # hide

   fsm = StateMachine()
   fsm.add_state(
       name='LightOn',
       timer=5,
       transitions={'Tup': '>exit'},
       actions={'PWM1': 255},
   )

   with Bpod() as bpod:
       bpod.flex_io['Flex1'].channel_type = FlexIOChannelType.ANALOG_INPUT
       bpod.flex_io.analog_sampling_rate = 500
       bpod.run(fsm)

   data = bpod.get_data()
   analog_data = bpod.get_analog_data()

.. testcode:: recording
   :hide:

   data = load_data('recording', fsm)
   analog_data = load_analog_data('recording')

.. dropdown:: Trial data of the light-sensor example
   :icon: table

   .. doctest-code-block::
      :group: recording

      >>> data
      shape: (8, 8)
      ┌────────────────────────────────┬───────┬──────────────────┬─────────┬─────────────────┬───────┬─────────┬───────┐
      │ Time                           ┆ Trial ┆ State Machine    ┆ State   ┆ Type            ┆ Event ┆ Channel ┆ Value │
      │ ---                            ┆ ---   ┆ ---              ┆ ---     ┆ ---             ┆ ---   ┆ ---     ┆ ---   │
      │ datetime[μs, UTC]              ┆ u32   ┆ cat              ┆ cat     ┆ enum            ┆ cat   ┆ cat     ┆ u16   │
      ╞════════════════════════════════╪═══════╪══════════════════╪═════════╪═════════════════╪═══════╪═════════╪═══════╡
      │ 2026-10-22 13:21:02.457608 UTC ┆ 0     ┆ b181e204b461e294 ┆ null    ┆ TrialStart      ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 13:21:02.457608 UTC ┆ 0     ┆ b181e204b461e294 ┆ LightOn ┆ StateStart      ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 13:21:02.457608 UTC ┆ 0     ┆ b181e204b461e294 ┆ LightOn ┆ OutputAction    ┆ null  ┆ PWM1    ┆ 255   │
      │ 2026-10-22 13:21:07.457608 UTC ┆ 0     ┆ b181e204b461e294 ┆ LightOn ┆ InputEvent      ┆ Tup   ┆ null    ┆ null  │
      │ 2026-10-22 13:21:07.457608 UTC ┆ 0     ┆ b181e204b461e294 ┆ LightOn ┆ StateEnd        ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 13:21:07.457608 UTC ┆ 0     ┆ b181e204b461e294 ┆ null    ┆ OutputAction    ┆ null  ┆ PWM1    ┆ 0     │
      │ 2026-10-22 13:21:07.457608 UTC ┆ 0     ┆ b181e204b461e294 ┆ null    ┆ TrialEnd        ┆ null  ┆ null    ┆ null  │
      │ 2026-10-22 13:21:07.457610 UTC ┆ 0     ┆ b181e204b461e294 ┆ null    ┆ TrialEndControl ┆ null  ┆ null    ┆ null  │
      └────────────────────────────────┴───────┴──────────────────┴─────────┴─────────────────┴───────┴─────────┴───────┘

.. testcode:: recording
   :hide:

   pl.Config.set_tbl_rows(10)  # too long to show in full

.. dropdown:: Analog data of the light-sensor example
   :icon: table

   .. doctest-code-block::
      :group: recording

      >>> analog_data
      shape: (2_519, 3)
      ┌────────────────────────────────┬───────┬───────┐
      │ Time                           ┆ Trial ┆ Flex1 │
      │ ---                            ┆ ---   ┆ ---   │
      │ datetime[μs, UTC]              ┆ u32   ┆ u16   │
      ╞════════════════════════════════╪═══════╪═══════╡
      │ 2026-10-22 13:21:02.457708 UTC ┆ 0     ┆ 1048  │
      │ 2026-10-22 13:21:02.459708 UTC ┆ 0     ┆ 1048  │
      │ 2026-10-22 13:21:02.461708 UTC ┆ 0     ┆ 1046  │
      │ 2026-10-22 13:21:02.463708 UTC ┆ 0     ┆ 1048  │
      │ 2026-10-22 13:21:02.465708 UTC ┆ 0     ┆ 1048  │
      │ …                              ┆ …     ┆ …     │
      │ 2026-10-22 13:21:07.485708 UTC ┆ 0     ┆ 0     │
      │ 2026-10-22 13:21:07.487708 UTC ┆ 0     ┆ 0     │
      │ 2026-10-22 13:21:07.489708 UTC ┆ 0     ┆ 0     │
      │ 2026-10-22 13:21:07.491708 UTC ┆ 0     ┆ 0     │
      │ 2026-10-22 13:21:07.493708 UTC ┆ 0     ┆ 0     │
      └────────────────────────────────┴───────┴───────┘

The sensor reads about 1 V while the light is on. Recording continues after the trial
ends, when the light is off again, so the last measurements are close to 0 V.

The data returned by :meth:`~bpod_core.bpod.Bpod.get_analog_data` is a Polars
:class:`~polars.DataFrame` with one row per measurement:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Column
     - Content
   * - ``Time``
     - When the measurement was taken.
   * - ``Trial``
     - The trial during which it was taken. Measurements between trials belong to the
       trial before.
   * - ``Flex{N}``
     - The voltage in millivolts, one column per analog input.

The same clock times your trial events and your analog data, so you can line them up
directly.

Reacting to a Signal
^^^^^^^^^^^^^^^^^^^^

Each analog input has two *thresholds*, which work like tripwires: you set one to a
voltage, and when the signal passes it, the Bpod reports an event — ``Flex{N}_Trig0``
for the first threshold, ``Flex{N}_Trig1`` for the second. Each threshold is either
*rising* (it fires when the signal goes above its voltage) or *falling* (below).

A threshold fires only while it's enabled, and it disables itself when it fires. What
happens next depends on the channel's threshold mode:

- ``MANUAL`` (default): the threshold stays disabled until you enable it again.
- ``LINKED``: when one threshold fires, the other one is enabled. With a rising and a
  falling threshold, the channel then reports each time the signal goes up, and each
  time it comes back down.

.. plot::
   :caption: A signal on ``Flex1`` with a rising threshold at 2.5 V (``Trig0``) and a
      falling threshold at 2 V (``Trig1``); only ``Trig0`` is enabled at the start.
   :include-source: false

   import matplotlib.pyplot as plt
   import numpy as np

   colors = ('#d95f02', '#1f78b4')  # Trig0, Trig1
   t = np.arange(0, 3, 0.001)
   rng = np.random.default_rng(0)
   signal = 1000 + rng.normal(0, 40, t.size)
   for peak in (0.5, 1.4, 2.3):
       signal += 2600 * np.exp(-((t - peak) ** 2) / (2 * 0.12**2))


   def threshold_events(mode):
       armed = [True, False]  # Trig0 (rising, 2500 mV), Trig1 (falling, 2000 mV)
       events = []
       for ti, value in zip(t, signal):
           if armed[0] and value > 2500:
               events.append((ti, value, 0))
               armed[0], armed[1] = False, armed[1] or mode == 'LINKED'
           elif armed[1] and value < 2000:
               events.append((ti, value, 1))
               armed[1], armed[0] = False, armed[0] or mode == 'LINKED'
       return events


   fig, axes = plt.subplots(2, 1, sharex=True)
   for ax, mode in zip(axes, ('MANUAL', 'LINKED')):
       ax.plot(t, signal, color='0.2', linewidth=1)
       ax.axhline(2500, color=colors[0], linestyle='--', linewidth=1.2)
       ax.axhline(2000, color=colors[1], linestyle=':', linewidth=1.2)
       for ti, value, which in threshold_events(mode):
           ax.plot(ti, value, marker='^v'[which], color=colors[which], markersize=7)
       ax.set_title(f'{mode} mode', loc='left', x=0.01, y=1, pad=-14)
       ax.set_ylim(top=4300)  # room for the title above the signal
       ax.set_ylabel('Flex1 (mV)')
   axes[0].plot([], [], '^', color=colors[0], label='Trig0 fires')
   axes[0].plot([], [], 'v', color=colors[1], label='Trig1 fires')
   axes[0].legend(loc='upper right', frameon=False)
   axes[1].set_xlabel('Time (s)')

You set up thresholds through the channel's
:attr:`~bpod_core.bpod.abc.FlexIOChannel.thresholds`. They start out disabled, so a
threshold that should work from the start of a trial also needs to be enabled.

In this example, a lever with a force sensor is connected to ``Flex1``. Its signal
rises when the subject presses the lever, so ``Flex1`` gets a rising threshold at
2.5 V, and the state machine waits for its event to give a reward:

.. dark-light-figure:: fsm_flexio_threshold

   A lever press, detected by ``Flex1``'s first threshold, opens the valve.

.. fsm_codeblock::
   :caption: Rewarding a lever press, detected with a threshold on ``Flex1``.
   :group: threshold
   :filename: fsm_flexio_threshold.svg
   :linenos:

   from bpod_core.bpod import Bpod
   from bpod_core.bpod.constants import FlexIOChannelType, FlexIOThresholdPolarity
   from bpod_core.fsm import StateMachine
   from unittest.mock import MagicMock as Bpod  # hide

   fsm = StateMachine()
   fsm.add_state(
       name='WaitForPress',
       timer=10,
       transitions={'Flex1_Trig0': 'Reward', 'Tup': '>exit'},
   )
   fsm.add_state(
       name='Reward',
       timer=0.1,
       transitions={'Tup': '>exit'},
       actions={'Valve1': 1},
   )

   with Bpod() as bpod:
       force_sensor = bpod.flex_io['Flex1']
       force_sensor.channel_type = FlexIOChannelType.ANALOG_INPUT
       force_sensor.thresholds[0].voltage = 2500  # mV
       force_sensor.thresholds[0].polarity = FlexIOThresholdPolarity.RISING
       force_sensor.thresholds[0].enabled = True
       bpod.run(fsm)

   data = bpod.get_data()

.. testcode:: threshold
   :hide:

   data = load_data('threshold', fsm)

.. dropdown:: Trial data of the lever-press example
   :icon: table

   .. doctest-code-block::
      :group: threshold

      >>> data
      shape: (11, 8)
      ┌────────────────────────────────┬───────┬──────────────────┬──────────────┬─────────────────┬─────────────┬─────────┬───────┐
      │ Time                           ┆ Trial ┆ State Machine    ┆ State        ┆ Type            ┆ Event       ┆ Channel ┆ Value │
      │ ---                            ┆ ---   ┆ ---              ┆ ---          ┆ ---             ┆ ---         ┆ ---     ┆ ---   │
      │ datetime[μs, UTC]              ┆ u32   ┆ cat              ┆ cat          ┆ enum            ┆ cat         ┆ cat     ┆ u16   │
      ╞════════════════════════════════╪═══════╪══════════════════╪══════════════╪═════════════════╪═════════════╪═════════╪═══════╡
      │ 2026-10-22 14:41:33.213841 UTC ┆ 0     ┆ 56b7e20aca4f280e ┆ null         ┆ TrialStart      ┆ null        ┆ null    ┆ null  │
      │ 2026-10-22 14:41:33.213841 UTC ┆ 0     ┆ 56b7e20aca4f280e ┆ WaitForPress ┆ StateStart      ┆ null        ┆ null    ┆ null  │
      │ 2026-10-22 14:41:34.513941 UTC ┆ 0     ┆ 56b7e20aca4f280e ┆ WaitForPress ┆ InputEvent      ┆ Flex1_Trig0 ┆ Flex1   ┆ 2500  │
      │ 2026-10-22 14:41:34.513941 UTC ┆ 0     ┆ 56b7e20aca4f280e ┆ WaitForPress ┆ StateEnd        ┆ null        ┆ null    ┆ null  │
      │ 2026-10-22 14:41:34.513941 UTC ┆ 0     ┆ 56b7e20aca4f280e ┆ Reward       ┆ StateStart      ┆ null        ┆ null    ┆ null  │
      │ 2026-10-22 14:41:34.513941 UTC ┆ 0     ┆ 56b7e20aca4f280e ┆ Reward       ┆ OutputAction    ┆ null        ┆ Valve1  ┆ 1     │
      │ 2026-10-22 14:41:34.613941 UTC ┆ 0     ┆ 56b7e20aca4f280e ┆ Reward       ┆ InputEvent      ┆ Tup         ┆ null    ┆ null  │
      │ 2026-10-22 14:41:34.613941 UTC ┆ 0     ┆ 56b7e20aca4f280e ┆ Reward       ┆ StateEnd        ┆ null        ┆ null    ┆ null  │
      │ 2026-10-22 14:41:34.613941 UTC ┆ 0     ┆ 56b7e20aca4f280e ┆ null         ┆ OutputAction    ┆ null        ┆ Valve1  ┆ 0     │
      │ 2026-10-22 14:41:34.613941 UTC ┆ 0     ┆ 56b7e20aca4f280e ┆ null         ┆ TrialEnd        ┆ null        ┆ null    ┆ null  │
      │ 2026-10-22 14:41:34.613959 UTC ┆ 0     ┆ 56b7e20aca4f280e ┆ null         ┆ TrialEndControl ┆ null        ┆ null    ┆ null  │
      └────────────────────────────────┴───────┴──────────────────┴──────────────┴─────────────────┴─────────────┴─────────┴───────┘

The press is recorded as a ``Flex1_Trig0`` event, which ends ``WaitForPress`` and
starts ``Reward``. Its value is the threshold's voltage in millivolts.

.. rubric:: Enabling and Disabling Thresholds

A state can also enable or disable thresholds while a trial runs, with the
``AnalogThreshEnable`` and ``AnalogThreshDisable`` actions. Both act on the two
thresholds of every channel you choose. To choose channels, add up their values:

.. list-table::
   :header-rows: 1
   :stub-columns: 1
   :widths: auto

   * - Channel
     - ``Flex1``
     - ``Flex2``
     - ``Flex3``
     - ``Flex4``
   * - Value
     - 1
     - 2
     - 4
     - 8

For example, ``{'AnalogThreshEnable': 1 + 4}`` enables the thresholds of ``Flex1`` and
``Flex3``. This is binary encoding with one bit per channel (``Flex1`` is the lowest),
so ``0b0101`` works as well.

In this example, presses only count after a 2-second delay. The threshold is set up
as before, but left disabled: the state machine enables it.

.. dark-light-figure:: fsm_flexio_threshold_action

   ``Flex1``'s thresholds are enabled when ``WaitForPress`` begins.

.. fsm_codeblock::
   :caption: Enabling ``Flex1``'s thresholds after a delay.
   :group: threshold_action
   :filename: fsm_flexio_threshold_action.svg
   :linenos:

   from bpod_core.bpod import Bpod
   from bpod_core.bpod.constants import FlexIOChannelType, FlexIOThresholdPolarity
   from bpod_core.fsm import StateMachine
   from unittest.mock import MagicMock as Bpod  # hide

   fsm = StateMachine()
   fsm.add_state(
       name='Delay',
       timer=2,
       transitions={'Tup': 'WaitForPress'},
   )
   fsm.add_state(
       name='WaitForPress',
       timer=10,
       transitions={'Flex1_Trig0': 'Reward', 'Tup': '>exit'},
       actions={'AnalogThreshEnable': 1},
   )
   fsm.add_state(
       name='Reward',
       timer=0.1,
       transitions={'Tup': '>exit'},
       actions={'Valve1': 1},
   )

   with Bpod() as bpod:
       force_sensor = bpod.flex_io['Flex1']
       force_sensor.channel_type = FlexIOChannelType.ANALOG_INPUT
       force_sensor.thresholds[0].voltage = 2500  # mV
       force_sensor.thresholds[0].polarity = FlexIOThresholdPolarity.RISING
       bpod.run(fsm)

   data = bpod.get_data()

.. testcode:: threshold_action
   :hide:

   data = load_data('threshold_action', fsm)

.. dropdown:: Trial data of the delayed-enable example
   :icon: table

   .. doctest-code-block::
      :group: threshold_action

      >>> data
      shape: (15, 8)
      ┌────────────────────────────────┬───────┬──────────────────┬──────────────┬─────────────────┬─────────────┬────────────────────┬───────┐
      │ Time                           ┆ Trial ┆ State Machine    ┆ State        ┆ Type            ┆ Event       ┆ Channel            ┆ Value │
      │ ---                            ┆ ---   ┆ ---              ┆ ---          ┆ ---             ┆ ---         ┆ ---                ┆ ---   │
      │ datetime[μs, UTC]              ┆ u32   ┆ cat              ┆ cat          ┆ enum            ┆ cat         ┆ cat                ┆ u16   │
      ╞════════════════════════════════╪═══════╪══════════════════╪══════════════╪═════════════════╪═════════════╪════════════════════╪═══════╡
      │ 2026-10-22 13:50:27.805215 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ null         ┆ TrialStart      ┆ null        ┆ null               ┆ null  │
      │ 2026-10-22 13:50:27.805215 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ Delay        ┆ StateStart      ┆ null        ┆ null               ┆ null  │
      │ 2026-10-22 13:50:29.805215 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ Delay        ┆ InputEvent      ┆ Tup         ┆ null               ┆ null  │
      │ 2026-10-22 13:50:29.805215 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ Delay        ┆ StateEnd        ┆ null        ┆ null               ┆ null  │
      │ 2026-10-22 13:50:29.805215 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ WaitForPress ┆ StateStart      ┆ null        ┆ null               ┆ null  │
      │ 2026-10-22 13:50:29.805215 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ WaitForPress ┆ OutputAction    ┆ null        ┆ AnalogThreshEnable ┆ 1     │
      │ 2026-10-22 13:50:30.805315 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ WaitForPress ┆ InputEvent      ┆ Flex1_Trig0 ┆ Flex1              ┆ 2500  │
      │ 2026-10-22 13:50:30.805315 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ WaitForPress ┆ StateEnd        ┆ null        ┆ null               ┆ null  │
      │ 2026-10-22 13:50:30.805315 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ Reward       ┆ StateStart      ┆ null        ┆ null               ┆ null  │
      │ 2026-10-22 13:50:30.805315 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ Reward       ┆ OutputAction    ┆ null        ┆ Valve1             ┆ 1     │
      │ 2026-10-22 13:50:30.905315 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ Reward       ┆ InputEvent      ┆ Tup         ┆ null               ┆ null  │
      │ 2026-10-22 13:50:30.905315 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ Reward       ┆ StateEnd        ┆ null        ┆ null               ┆ null  │
      │ 2026-10-22 13:50:30.905315 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ null         ┆ OutputAction    ┆ null        ┆ Valve1             ┆ 0     │
      │ 2026-10-22 13:50:30.905315 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ null         ┆ TrialEnd        ┆ null        ┆ null               ┆ null  │
      │ 2026-10-22 13:50:30.905333 UTC ┆ 0     ┆ 51ca9d9ab0f63fda ┆ null         ┆ TrialEndControl ┆ null        ┆ null               ┆ null  │
      └────────────────────────────────┴───────┴──────────────────┴──────────────┴─────────────────┴─────────────┴────────────────────┴───────┘

``AnalogThreshEnable`` shows up as an output action when ``WaitForPress`` begins,
followed by the press's ``Flex1_Trig0`` event. An earlier press during ``Delay`` left
no trace: the threshold wasn't enabled yet.


Good to Know
------------

- **Settings are fixed once trials run.** Each channel's job and the sampling rate stay
  the same from the first trial until you close the connection. Thresholds and
  :attr:`~bpod_core.bpod.abc.AbstractFlexIO.n_reads_per_sample` can still be changed
  between trials.
- **Nothing can be changed during a trial.** To enable or disable thresholds during a
  trial, use the ``AnalogThreshEnable`` and ``AnalogThreshDisable`` actions.
- **Stopping a trial pauses recording.**
  :meth:`~bpod_core.bpod.Bpod.stop_state_machine` also stops the analog inputs: they
  record and report threshold events again once you call
  :meth:`~bpod_core.bpod.Bpod.reset_session_clock` and start the next trial.
- **Use the lowest-numbered channels for analog inputs.** Because of a limitation of the
  Bpod's firmware, threshold events are only reliable for analog inputs on ``Flex1``,
  ``Flex2``, and so on, with no channel of another job before them. bpod-core refuses
  thresholds on other channels.
- **No remote access yet.** FlexIO can't be set up through
  :class:`~bpod_core.bpod.RemoteBpod`.

Known Firmware Issues
^^^^^^^^^^^^^^^^^^^^^

The current Bpod firmware has a few issues with FlexIO channels. Fixes have been
proposed, but they aren't part of an official firmware release yet:

- Every state change switches FlexIO outputs to the new state's value, even while a
  global timer controls them (fix:
  `#6 <https://github.com/sanworks/Bpod_StateMachine_Firmware/pull/6>`__).
- A state that doesn't use ``AnalogThreshEnable`` or ``AnalogThreshDisable`` can still
  enable or disable thresholds, if a state at the same position in an earlier state
  machine did. This lasts until the Bpod is switched off (fix:
  `#7 <https://github.com/sanworks/Bpod_StateMachine_Firmware/pull/7>`__).
- A digital input that is already on when a trial starts reports a ``Flex{N}_High``
  event at the start of the trial (fix:
  `#12 <https://github.com/sanworks/Bpod_StateMachine_Firmware/pull/12>`__).

.. admonition:: Take-Home Messages
   :class: tip

   Four jobs
      Each FlexIO channel is a digital or analog input or output, set through
      :attr:`~bpod_core.bpod.Bpod.flex_io` before the first trial.

   Inputs
      Digital inputs report ``Flex{N}_High`` and ``Flex{N}_Low``; analog inputs record
      continuously (see :meth:`~bpod_core.bpod.Bpod.get_analog_data`) and report
      ``Flex{N}_Trig0`` and ``Flex{N}_Trig1`` through their thresholds.

   Outputs
      Set them with the ``Flex{N}`` action: 0 or 1 for digital outputs, millivolts for
      analog outputs.

   Millivolts
      All FlexIO voltages are in millivolts, from 0 to 5000.

.. seealso::

   The API reference of :attr:`~bpod_core.bpod.Bpod.flex_io`,
   :class:`~bpod_core.bpod.abc.FlexIOChannel`,
   :class:`~bpod_core.bpod.abc.FlexIOThreshold` and
   :meth:`~bpod_core.bpod.Bpod.get_analog_data` covers the technical details.
