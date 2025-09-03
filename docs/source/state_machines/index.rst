Finite-State Machines
=====================

This chapter introduces the finite-state machine (FSM) concept and explains how to
create, validate, visualize, import, and export state machines with bpod-core.

What is a Finite State Machine?
-------------------------------
A :wikipedia:`finite-state machine` (FSM) is a model of computation made up of a finite
number of *states* and *transitions* between those states. At any given moment, the
machine is in exactly one state, and certain *events* cause it to move, or transition,
to another state. Think of the finite-state machine as a flowchart with a list of named
boxes (states) and arrows (transitions) between them—this type of flowchart is called a
*state diagram*.

.. graphviz::
   :caption: A state diagram.

   digraph {
      S1 [label="State 1"];
      S2 [label="State 2"];

      S1 -> S2 [label="Event"];
   }

In practice, finite-state machines are used to model a wide range of systems where
behavior depends on a sequence of events, making the logic easier to design and
understand. A simple light switch offers an intuitive example of a finite-state machine.
It has only two states—*Off* and *On*—and two events that trigger transitions between
them: *flip up* and *flip down*. At any given moment, the switch is in exactly one
state, and performing the corresponding flip moves the system to the other.

.. graphviz::
   :caption: Flip up, flip down—ad infinitum.

   digraph {
      a [label="Off"];
      b [label="On"];

      a -> b [label="flip up"];
      a -> b [style="invis"];
      b -> a [label="flip down"];
   }

While the light switch illustrates a finite-state machine with no clearly defined start
or end, many real-world processes have natural beginnings and endings. The scientific
publication process provides an example. It begins in the *Draft* state, progresses
through the *Review* state, and—after a few revision—(hopefully) concludes with a
*Publication*. In a state diagram, the *entry* to the finite-state machine is typically
indicated by a filled circle, while the *exit* is shown with a double circle:

.. graphviz::
   :caption: If only the reviewers ever agreed ...

   digraph {
      s [label="", shape=circle, style=filled, fillcolor=black, width=0.25];
      x [label="", shape=doublecircle, style=filled, fillcolor=black, width=0.125];

      a [label="Draft"];
      b [label="Review"];
      c [label="Publication"];


      s -> a
      a -> b [label="submittal"];
      a -> b [style="invis"];
      b -> a [label="R&R"];
      b -> c [label="approval"];
      b -> x [label="rejection"];
      c -> x
   }

Finite-state machines can be a powerful tool in the design of behavioral experiments.
In this context, they can be used to specify trial structure, stimulus presentation, and
response contingencies in a clear and reproducible way. Each state represents a specific
phase of the experiment, and transitions are triggered by events such as a subject’s
action or a timer. Timers are particularly useful to control the duration of states and
the timing of their associated output actions—such as turning on a light, sounding a
buzzer, or delivering a reward.

.. graphviz::
   :caption: A simple trial sequence implemented as a finite-state machine.

   digraph {
      s [label="", shape=circle, style=filled, fillcolor=black, width=0.25];
      x [label="", shape=doublecircle, style=filled, fillcolor=black, width=0.125];

      a [label="Stimulus"];
      b [label="Wait"];
      c [label="Reward"];
      d [label="Buzzer"];
      e [label="End"];

      s -> a
      a -> b [label="timeout"];
      b -> c [label="lever pressed"];
      b -> d [label="timeout"];
      c -> e [label="timeout"];
      d -> e [label="timeout"];
      e -> x [label="timeout"];

      { rank=same; c; d; }
   }

The finite-state machine pictured above represents a single trial in a behavioral
experiment. It heavily relies on timers to define both the duration of states and their
associated output actions. The *Stimulus* ends automatically when its timer expires,
moving the subject into the *Wait* state. From there, the trial can proceed in two ways:
if the subject performs the required action (pressing a lever) within the allotted time
of the *Wait* state, the machine transitions to *Reward*; otherwise, a *Buzzer* signals
a missed opportunity. The durations of both the reward and the buzzer are again governed
by their respective timers, after which either state transitions to the trial’s *End*
state and, finally, to the trial’s exit.

.. admonition:: Key Concepts
   :class: tip

   State
      A specific configuration of the system at a given moment.

   Event
      A trigger that causes the system to transition from one state to another.

   Transition
      The movement of the system from one state to another in response to an event.

   Output action
      A controlled action that occurs with the onset of the state.

   Timer
      A mechanism that, after a set interval, generates an event which may trigger a
      transition.


.. In behavioral experiments, FSMs can be used to specify trial structure, stimulus
.. presentation, and response contingencies in a clear and reproducible way. The
.. `Bpod Finite-State Machine`_ implements an FSM using an :wikipedia:`Arduino`-compatible
.. :wikipedia:`microcontroller`, allowing for high temporal fidelity not typically
.. achievable in software alone.

.. _Bpod Finite-State Machine: https://sanworks.github.io/Bpod_Wiki/


The `StateMachine` Data Model
-----------------------------
In bpod-core, an FSM is represented by the :class:`~bpod_core.fsm.StateMachine` class.
It defines states, state transitions, and the output actions assigned to each state. It
also introduces Bpod-specific concepts such as state timers, global timers, conditions,
and global counters. Finally, it provides tools for validation, visualization, and
importing/exporting to and from other formats.


Creating a State Machine
^^^^^^^^^^^^^^^^^^^^^^^^
A state machine is created by instantiating a :class:`~bpod_core.fsm.StateMachine`
object and adding states with its :meth:`~bpod_core.fsm.StateMachine.add_state` method:

.. fsm_codeblock::
   :group: hello_world
   :filename: hello_world_01.svg

   from bpod_core.fsm import StateMachine

   fsm = StateMachine()
   fsm.add_state(name='Hello')

The commands above create a state machine with a single state named `Hello`. The very
first state that is added to a state machine automatically becomes the *entry state* —
this is where execution begins. Every state comes with a *state timer*, which can be
used to generate timer-based events. By default, this timer is set to 0 s, meaning the
state will immediately trigger a timeout event. The created state machine can be
visualized using the following state diagram:

.. figure:: hello_world_01.svg

   Well hello!

See the section `Import and Export`_ for details on how such state diagrams are
generated. To make things a bit more interesting, let's add a second state named
`World`, this time with a 1 s state timer. Normally, we could just call the
:meth:`~bpod_core.fsm.StateMachine.add_state` method again. However, for the sake of
demonstration, we’ll use a different approach by adding a new entry directly to
our state machine's :attr:`~bpod_core.fsm.StateMachine.states` dictionary.

.. fsm_codeblock::
   :group: hello_world
   :filename: hello_world_02.svg

   fsm.states['World'] = {'timer': 1}

Now our state machine contains two states: `Hello` and `World`. However, they are not
yet connected, which is obvious in the state diagram below:

.. figure:: hello_world_02.svg

   That doesn't look right.

As you can see, we're missing a *transition* from `Hello` to `World`. Fortunately,
transitions can be added later by directly modifying the fields of the
:class:`~bpod_core.fsm.StateMachine` instance.
Let's add a transition from `Hello` to `World`, triggered by the end of `Hello`'s
state timer. While we're at it, we'll also change `Hello`'s state timer to 1.5 s.
Finally, we'll add a transition from `World` to the special exit state:

.. fsm_codeblock::
   :group: hello_world
   :filename: hello_world_03.svg

   fsm.states['Hello'].transitions = {'Tup': 'World'}
   fsm.states['Hello'].timer = 1.5
   fsm.states['World'].transitions = {'Tup': '>exit'}

The end of a state timer is signaled by the ``Tup`` event (short for *time up*).
A state's transitions are defined in a Python :class:`dict`, where keys are the
triggering events (e.g. `Tup`), and values are the transition targets (either another
state's name or an operator such as ``>exit`` or ``>back``).

.. figure:: hello_world_03.svg

   Getting there.

At this point, our state machine is functional: it moves from `Hello` to `World` after
1.5 seconds, and then exits after 1 more second. However, it still doesn’t *do*
anything, because we haven’t defined any output actions yet. Let’s fix that by adding
*actions* to each state. Actions define what happens when a state is active, such as
turning on an output channel:

.. fsm_codeblock::
   :group: hello_world
   :filename: hello_world_04.svg

   fsm.states['Hello'].actions = {'BNC1': 1}
   fsm.states['World'].actions = {'BNC2': 1}

And with that, our `Hello, World!` example is complete:

.. figure:: hello_world_04.svg

   Our finite-state machine is complete.

In the previous examples, we first added bare-bones states and then modified them
afterward (changing timers, adding transitions, assigning actions). This was done purely
for demonstration purposes, so you could see that states behave like regular Python
objects and can be manipulated at any time. In practice, however, you would usually
choose the more straightforward approach: specify everything a state needs (its timer,
transitions, and actions) directly in the call to
:meth:`~bpod_core.fsm.StateMachine.add_state`:

.. testcode:: hello_world
   :hide:

   fsm_original = fsm.copy()

.. fsm_codeblock::
   :group: hello_world

   from bpod_core.fsm import StateMachine

   fsm = StateMachine()
   fsm.add_state(name='Hello', timer=1.5, transitions={'Tup': 'World'}, actions={'BNC1': 1})
   fsm.add_state(name='World', timer=1.0, transitions={'Tup': '>exit'}, actions={'BNC2': 1})

.. testcode:: hello_world
   :hide:

   assert fsm_original == fsm

Using this simple concept you can create arbitrarily complex patterns and behavioral
sequences. See the section :ref:`examples` for more examples.

.. admonition:: Take-Home Messages
   :class: tip

   Adding states
      Use :meth:`~bpod_core.fsm.StateMachine.add_state` with parameters ``name``,
      ``timer``, ``transitions``, and ``actions``.

   Modifying states
      You can create and modify states by directly manipulating the fields of
      :class:`~bpod_core.fsm.StateMachine`.

   Entry state
      The first state you add is always the entry point of the state machine.

   State Timers
      Every state has a timer (default 0 s), which triggers the ``Tup`` events on
      expiry.

   Transitions
      States define transitions in a Python :class:`dict`, mapping events to targets.

   Actions
      States can perform output actions (e.g., activate a port or channel) while active.


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

- :meth:`~bpod_core.fsm.StateMachine.to_json`,
  :meth:`~bpod_core.fsm.StateMachine.to_yaml` and
  :meth:`~bpod_core.fsm.StateMachine.to_dict` return in-memory representations as a JSON
  string, YAML string and Python dict, respectively.
- :meth:`~bpod_core.fsm.StateMachine.to_digraph` returns a Graphviz
  :class:`~graphviz.Digraph` instance which can be used to render the state diagram, for
  instance in a Jupyter notebook.
- :meth:`~bpod_core.fsm.StateMachine.to_file`, depending on the file extension, writes
  either:

  - ``.json``: a JSON serialization of the :class:`~bpod_core.fsm.StateMachine`,
  - ``.yaml``, ``.yml``: a YAML serialization of the :class:`~bpod_core.fsm.StateMachine`,
  - ``.svg``, ``.png``, ``.pdf``: a rendered state diagram via Graphviz.
- :meth:`~bpod_core.fsm.StateMachine.from_json`, :meth:`~bpod_core.fsm.StateMachine.from_dict`,
  and :meth:`~bpod_core.fsm.StateMachine.from_file` create a StateMachine from serialized data.

.. testcode-code-block:: python3
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


.. testcode-code-block:: python3
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
