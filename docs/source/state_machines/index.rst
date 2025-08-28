Finite-State Machines
=====================

This chapter introduces the finite-state machine (FSM) concept and explains how to
create, validate, visualize, import, and export state machines.

What is a Finite State Machine?
-------------------------------
A :wikipedia:`finite-state machine` is a model of computation made up of a finite number
of states and transitions between those states. At any given time the machine is in
exactly one state. Events trigger transitions to other states.

.. graphviz::
   :align: center
   :caption: A state diagram.
             Start and exit nodes are indicated by filled and double circles, respectively.

   digraph example {
       rankdir=LR;
       node [shape=rectangle, fontname="Helvetica, sans-serif", fontsize=11];
       edge [shape=rectangle, fontname="Helvetica, sans-serif", fontsize=10];


       s [label="", shape=circle, style=filled, fillcolor=black, width=0.25];
       x [label="", shape=doublecircle, style=filled, fillcolor=black, width=0.125];

       a [label="State 1"];
       b [label="State 2"];

       s -> a;
       a -> b [label="Event"];
       b -> x;
   }

In behavioral experiments, FSMs can be used to specify trial structure, stimulus
presentation, and response contingencies in a clear and reproducible way. The
`Bpod Finite-State Machine`_ implements an FSM using an :wikipedia:`Arduino`-compatible
:wikipedia:`microcontroller`, allowing for high temporal fidelity not typically
achievable in software alone.

.. _Bpod Finite-State Machine: https://sanworks.github.io/Bpod_Wiki/


The `StateMachine` Data Model
-----------------------------
In bpod-core, an FSM is represented by the :class:`~bpod_core.fsm.StateMachine` class.
It defines states, state transitions, and the actions assigned to each state. It also
introduces Bpod-specific concepts such as state timers, global timers, conditions, and
global counters. Finally, it provides tools for validation, visualization, and
importing/exporting to and from other formats.


Creating a State Machine
^^^^^^^^^^^^^^^^^^^^^^^^
A state machine can be created by importing and instantiating a
:class:`~bpod_core.fsm.StateMachine` object and adding states using its
:meth:`~bpod_core.fsm.StateMachine.add_state` method:

.. literalinclude:: ../../../examples/hello_world.py
   :language: python
   :start-at: from bpod_core.
   :caption: Say hello to your first state machine.

The above commands result in a state machine with the two states  `Hello` and `World`.
In the `Hello` state, the output channel ``PWM1`` is set to 255. After its 1-second
state timer expires—emitting a ``Tup`` event—the state transitions to `World`. Following
another two seconds in the `World` state (which has no output action), the state machine
exits using the ``>exit`` operator:

.. figure:: examples/hello_world.svg
   :align: center

   A basic state machine created in bpod-core.

Using this simple concept you can create arbitrarily complex patterns and behavioral
sequences. See section :ref:`examples` for more examples.


Validation
^^^^^^^^^^
The class :class:`~bpod_core.fsm.StateMachine` and related classes are `Pydantic`_
models. All parameters are strictly typed and come with defined value ranges and
constraints. Field values are coerced to their respective types and validated both
at creation and assignment:

.. _Pydantic: https://docs.pydantic.dev/latest/

.. testsetup:: pydantic-validation-1

   from bpod_core.fsm import StateMachine
   fsm = StateMachine()

.. doctest-code-block::
   :caption: Pydantic complaining when trying to add a state with an invalid timer.
   :group: pydantic-validation-1

   >>> fsm.add_state(name='MyState', timer=-1)
   Traceback (most recent call last):
      ...
   pydantic_core._pydantic_core.ValidationError: 1 validation error for StateMachine.add_state
   timer
     Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-1, input_type=int]
       For further information visit https://errors.pydantic.dev/2.11/v/greater_than_equal

.. testsetup:: pydantic-validation-2

   from bpod_core.fsm import StateMachine
   fsm = StateMachine()

.. doctest-code-block::
   :caption: Assignments are validated as well
   :group: pydantic-validation-2

   >>> fsm.add_state(name='MyState', timer=1)
   >>> fsm.states['MyState'].actions = 42
   Traceback (most recent call last):
      ...
   pydantic_core._pydantic_core.ValidationError: 1 validation error for State
   actions
     Input should be a valid dictionary [type=dict_type, input_value=42, input_type=int]
       For further information visit https://errors.pydantic.dev/2.11/v/dict_type



This validation mechanism helps catch errors early in the design phase of an experiment.
More detailed validation is performed at runtime, when the specific constraints of the
hardware are known:

.. testsetup:: runtime-validation

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
       self.send_state_machine = types.MethodType(original_send, self)
       self.validate_state_machine = types.MethodType(original_validate, self)

   patcher = patch.object(Bpod, "__init__", fake_init)
   patcher.start()
   atexit.register(patcher.stop)

   from bpod_core.bpod import Bpod
   from bpod_core.fsm import StateMachine
   fsm = StateMachine()
   fsm.add_state(name='MyState', timer=1)

.. doctest-code-block::
   :caption: A :exc:`ValueError` is raised when attempting to run a state machine that exceeds the hardware's capabilities.
   :group: runtime-validation

   >>> fsm.set_global_timer(index=20, duration=5)  # this validates OK
   >>> bpod = Bpod()
   >>> bpod.send_state_machine(fsm)
   Traceback (most recent call last):
      ...
   ValueError: Too many global timers in state machine - hardware supports up to 16 global timers


Import and Export
^^^^^^^^^^^^^^^^^
There are several convenient methods to serialize and visualize state machines:

- :meth:`~bpod_core.fsm.StateMachine.to_json` and :meth:`~bpod_core.fsm.StateMachine.to_dict` return in-memory representations as a JSON string and Python dict, respectively.
- :meth:`~bpod_core.fsm.StateMachine.to_file`, depending on the file extension, writes either:

  - ``.json``: a pretty-printed JSON serialization of the :class:`~bpod_core.fsm.StateMachine`, or
  - ``.svg``, ``.png``, ``.pdf``: a rendered state diagram via Graphviz.
- :meth:`~bpod_core.fsm.StateMachine.from_json`, :meth:`~bpod_core.fsm.StateMachine.from_dict`,
  and :meth:`~bpod_core.fsm.StateMachine.from_file` create a StateMachine from serialized data.

.. testcode-code-block:: python
   :caption: A roundtrip from :class:`~bpod_core.fsm.StateMachine` to JSON and back to :class:`~bpod_core.fsm.StateMachine`
   :group: json-roundtrip

   from bpod_core.fsm import StateMachine

   # create a state machine and serialize it as a JSON string
   fsm1 = StateMachine()
   fsm1.add_state(name='Pi', timer=3.1415)
   json_string = fsm1.to_json()

   # create a second, identical state machine from the JSON string
   fsm2 = StateMachine.from_json(json_string)
   assert fsm2 == fsm1

.. testsetup:: file-roundtrip

   import atexit
   from unittest.mock import patch
   from bpod_core.fsm import StateMachine

   patcher = patch('bpod_core.fsm.StateMachine', autospec=True)
   patcher.start()
   atexit.register(patcher.stop)


.. testcode-code-block:: python
   :caption: Importing a :class:`~bpod_core.fsm.StateMachine` from a JSON file and exporting its state diagram as a PNG file.
   :group: file-roundtrip

   from bpod_core.fsm import StateMachine

   fsm = StateMachine.from_file('state_machine.json')
   fsm.to_file('state_machine.png')

.. note::
   Rendering diagrams depends on the Graphviz system libraries.
   See the `Graphviz documentation`_ for installation instructions.

.. _Graphviz documentation: https://graphviz.org/documentation/


.. _examples:

Example State Machines
----------------------

The following examples illustrate usage and features of the :class:`~bpod_core.fsm.StateMachine` class.

.. toctree::
   :maxdepth: 1
   :glob:

   examples/*
