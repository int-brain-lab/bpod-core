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


Running State Machines
----------------------

To run a state machine on a Bpod device, use the :meth:`~bpod_core.bpod.Bpod.run` method
of your :class:`~bpod_core.bpod.Bpod` instance. It validates and compiles the state
machine, and finally enqueues it on the Bpod for immediate execution.
Calling :meth:`~bpod_core.bpod.Bpod.get_data` after a state machine run returns the
collected data as a Polars :class:`~polars.DataFrame` (see :doc:`/data_format`).

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


Zero-Downtime Execution
^^^^^^^^^^^^^^^^^^^^^^^

You can invoke :meth:`~bpod_core.bpod.Bpod.run` several times in quick succession. Each
call automatically blocks until the hardware is ready to accept the next state machine.
The Bpod starts executing the transferred state machine as soon as possible—either right
away if no trial is running, or as soon as the preceding trial ends—enabling
back-to-back execution of state machines with zero inter-trial downtime and continuous
acquisition.

.. dark-light-figure:: /_static/bpod_run

   Timeline of four consecutive :meth:`~bpod_core.bpod.Bpod.run` calls across the Host
   PC and Bpod.

When calling :meth:`~bpod_core.bpod.Bpod.get_data`, data from individual trials is
automatically concatenated to a continuous Polars :class:`~polars.DataFrame`. In the
following example, a single state machine is executed 100 times:

.. testcode-code-block:: python3
   :caption: Running several trials of the same state machine in immediate succession.
   :group: bpod-context-manager

   with Bpod() as bpod:
       for trial in range(100):
           bpod.run(fsm)

   data = bpod.get_data()

On-The-Fly Definition
^^^^^^^^^^^^^^^^^^^^^

Similarly, you can define state machines on-the-fly *within* the loop. The
:meth:`~bpod_core.bpod.Bpod.run` method is non-blocking (unless the Bpod is not
yet ready to accept a transfer) and the individual state machines will run continuously,
as long as preparing and uploading a state machine's successor takes less time than the
current state machine takes to execute.

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

Retrieval of Partial Data
^^^^^^^^^^^^^^^^^^^^^^^^^

Finally, it is possible to retrieve a partial copy of a state machine's data while it's
still running. To do so, use the :meth:`~bpod_core.bpod.Bpod.peek_data` method. In the
following example, we use two state machines per trial. The first measures the duration
of an input event, while the second one returns an output action of identical duration.
:meth:`~bpod_core.bpod.Bpod.peek_data` blocks until one of the ``trigger_states`` has
been reached—in this case the ``pause`` state—and then returns the data collected up to
that point. This way, you can use the results from one state machine to prepare the next
without introducing idle time between state machine runs. The final call to
:meth:`~bpod_core.bpod.Bpod.get_data` will collect data across all trials.

.. testcode-code-block:: python3
   :name: peek_data_fsm
   :caption: Using the :meth:`~bpod_core.bpod.Bpod.peek_data` method
   :group: bpod-context-manager

   import polars as pl

   from bpod_core.fsm import StateMachine
   from bpod_core.bpod import Bpod

   # construct first state machine
   fsm1 = StateMachine()
   fsm1.add_state('wait', transitions={'Port1_High': 'measure'})     # wait for 'Port1_High'
   fsm1.add_state('measure', transitions={'Port1_Low': 'pause'})     # wait for 'Port1_Low'
   fsm1.add_state('pause', timer=0.5, transitions={'Tup': '>exit'})  # pause, then exit

   with Bpod() as bpod:
       for i in range(10):
           # run first state machine
           bpod.run(fsm1, trial_number=i)

           # peek at data once 'pause' state has been reached
           runtime_data = bpod.peek_data(trigger_states=['pause'])

           # calculate duration of 'Port1_High'
           duration = runtime_data.filter(pl.col("channel") == "Port1")['time'].diff().last()

           # construct and run second state machine on the fly
           fsm2 = StateMachine()
           fsm2.add_state('echo', timer=duration, transitions={'Tup': '>exit'}, actions={'PWM1': 255})
           bpod.run(fsm2, trial_number=i)

   data = bpod.get_data()  # collect data across all state machine runs
