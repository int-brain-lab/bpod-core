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

.. code-block:: pycon
   :caption: Pydantic complaining about an incorrect parameter for the state timer.

   >>> from bpod_core.fsm import StateMachine
   >>> fsm = StateMachine()
   >>> fsm.add_state(name='MyState', timer=-1)
   Traceback (most recent call last):
     [...]
   pydantic_core._pydantic_core.ValidationError: 1 validation error for StateMachine.add_state
   timer
     Input should be greater than or equal to 0 [type=greater_than_equal, input_value=-1, input_type=int]
       For further information visit https://errors.pydantic.dev/2.11/v/greater_than_equal

This validation mechanism helps catch errors early in the design phase of an experiment.
More detailed validation is performed at runtime, when the specific constraints of the
hardware are known:

.. code-block:: pycon
   :caption: A :exc:`ValueError` is raised when attempting to run a state machine that exceeds the hardware's capabilities.

   >>> from bpod_core.fsm import StateMachine
   >>> from bpod_core.bpod import Bpod
   >>> fsm = StateMachine()
   >>> fsm.add_state(name='MyState', timer=-1)
   >>> fsm.set_global_timer(id=42, duration=5)
   >>> bpod = Bpod()
   >>> bpod.send_state_machine(fsm)
   Traceback (most recent call last):
     [...]
   ValueError: Too many global timers in state machine - hardware supports up to 16 global timers

.. _Pydantic: https://docs.pydantic.dev/latest/


Import and Export
^^^^^^^^^^^^^^^^^
There are several convenient methods to serialize and visualize state machines:

- :meth:`~bpod_core.fsm.StateMachine.to_json` and :meth:`~bpod_core.fsm.StateMachine.to_dict` return in-memory representations as a JSON string and Python dict, respectively.
- :meth:`~bpod_core.fsm.StateMachine.to_file`, depending on the file extension, writes either:

  - ``.json``: a pretty-printed JSON serialization of the :class:`~bpod_core.fsm.StateMachine`, or
  - ``.svg``, ``.png``, ``.pdf``: a rendered state diagram via Graphviz.
- :meth:`~bpod_core.fsm.StateMachine.from_json`, :meth:`~bpod_core.fsm.StateMachine.from_dict`,
  and :meth:`~bpod_core.fsm.StateMachine.from_file` create a StateMachine from serialized data.

.. code-block:: python
   :caption: Import a :class:`~bpod_core.fsm.StateMachine` from a JSON file and export its state diagram as a PNG file.

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
