Changelog
=========

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `Bpod.wait(timeout=...)` raises a `TimeoutError` when the state machine hasn't
  finished in time.
- `stop_state_machine(timeout=...)` waits at most `timeout` seconds (one by default) for
  the Bpod to end the trial and raises a `TimeoutError` otherwise.

### Fixed

- `Bpod.close()` finishes its cleanup before raising a connection lost during the
  trial.
- validation messages for invalid state names and operators no longer substitute
  braces in the input, e.g. `{pattern}`.
- `misc.ValidatedDict` stores a coerced key in its validated form, e.g. `'1'` as `1`.
- connecting discards discovery bytes before the handshake, which could otherwise fail
  with firmware that sends one when the port is opened; discovery raises DTR once
  the port is open (`verify_serial_discovery(raise_dtr=True)`) to request that byte,
  so connecting no longer waits for the next periodic one.
- closing a `Bpod`, or any IPC host or client, no longer waits up to 100 ms for its
  event loop's receive timeout: the loop is woken by a message.
- a `Bpod` created after closing another one for the same device takes over its IPC
  service, network advertisement included, instead of failing to bind the same
  addresses.
- a process's first trial no longer ends about 10 ms late: `numpy.ma`, which polars 2.1
  imports on its first DataFrame conversion, is imported with bpod-core.
- every handshake ends with a session clock reset, also on reopening the connection; a
  reset the firmware answers late (about 50 ms on Bpod r2.0) is repeated, so
  timestamps stay aligned to the host clock.
- a `RemoteBpod` connecting by one of the host machine's own network addresses is
  recognized as local: it upgrades to the IPC transport and skips compressing trial
  data. The upgrade is verified and reverted if the host doesn't answer there.
- a service that disappears and comes back is reported again by `iter_services` and
  `discover_remote_bpod`; a record changing its address or port is reported as a removal
  and an addition.
- `RemoteBpod.close()` returns within a second when the host is gone, instead of
  waiting for a goodbye reply forever.
- a host without a route to the internet advertises one of its interface addresses via
  Zeroconf instead of the loopback address.
- `RemoteError` survives pickling, e.g. when raised in a worker process.

### Changed

- building state machines is two to three times faster, loading them from a file up
  to seven times, and `Bpod.run` returns about 40% sooner. Validating and compiling
  state machines and `Bpod.get_data` gained 10-20%.
- connecting to a Bpod enumerates the serial ports once instead of three times;
  `com.SerialDevice` also accepts a `ListPortInfo` as port.
- the builder methods of `StateMachine` validate their arguments through the models
  instead of `validate_call`: validation errors are titled after the model (e.g.
  `State`) rather than the method, unknown keyword arguments raise a `TypeError`, and
  a `Transitions` or `Actions` instance passed to `add_state` is stored by reference.
- state names and operators must be strings; `bytes` are no longer coerced.
- the IPC transport is used wherever libzmq supports it, instead of only on POSIX.
- the reader hands the packets of one serial read to the event thread as one batch:
  fewer thread wake-ups at high event rates.
- the reader takes everything pending on the port in one read
  (`com.ExtendedSerial.read_waiting`, direct on POSIX), halving its CPU time.
- the warning about a timing violation reports how far the host fell behind in
  reading the serial port: a full buffer stalls the Bpod's state machine cycle.
- all IPC hosts and service browsers of a process share one Zeroconf instance,
  started on first use and closed at exit; closing a remote host no longer waits for
  its goodbye packets.
- `wait()` also waits for the trial's softcode handlers to have run.
- `wait()` and `stop_state_machine()` can be interrupted with Ctrl+C while they wait
  for a trial, despite polars' signal handler.
- leaving the `Bpod` context manager on an exception stops a running state machine
  instead of waiting for it, as does `Bpod.close()` when its wait is interrupted; if
  the Bpod doesn't end the trial, the reader gives it up.

## [0.1.0a15] - 2026-10-23

### Added

- FlexIO channels (Bpod Finite State Machine r2+): `Bpod.flex_io` configures each
  channel as digital or analog input or output, with analog thresholds and the
  sampling rate, and `Bpod.get_analog_data` returns the sampled analog input in
  millivolts. State machines can use Flex events and outputs, and global timers can
  drive Flex outputs.
- `Bpod.action_names`: the actions a state machine can currently set, like
  `Bpod.input_event_names` for events.
- `ExtendedSerial.temporary_timeout`: a context manager that changes the read timeout
  temporarily.
- `ipc.iter_services`, `ipc.discover` and `ipc.ServiceIterator` accept regex and
  collection criteria for `properties`, like `com.find_ports` (see `misc.Filter`).
- `misc.SuggestionMapping`: a read-only mapping that suggests similar keys on failed
  lookups.

### Changed

- `com.find_ports` takes explicit keyword-only filters instead of `**filters` and
  raises a `TypeError` for unknown ones. It and service discovery ignore filters set to
  `None`.
- `Bpod.inputs`, `Bpod.outputs` and `Bpod.modules` are read-only
  `misc.SuggestionMapping`s, so misspelled module names get a suggestion, too.
- FlexIO channels are no longer listed in `Bpod.inputs` and `Bpod.outputs`; use
  `Bpod.flex_io`.
- methods that query or reconfigure the Bpod (e.g., `set_status_led`, `update_modules`,
  `Input.read`, `Module.load_serial_message`) raise a `BpodError` while a state machine
  is running, instead of corrupting the trial's data.
- `send_softcode` and `Input.override` send nothing while no state machine is running,
  since the Bpod would ignore it; they log a warning instead.
- trial data columns are named in title case (`Time`, `Trial`, `State Machine`,
  `State`, `Type`, `Event`, `Channel`, `Value`). `Trial` is `UInt32` instead of
  `UInt16`, and `Value` is `UInt16` instead of `UInt8`, to hold FlexIO analog-output
  values in millivolts.
- lower CPU load: trial data is read from the Bpod in chunks, and live trial events are
  published in batches. `RemoteBpod`'s `event_callback` still receives single events.
- building, validating and compiling state machines is faster, and takes about half
  the time for large ones.
- polars 2 is supported (`polars>=1.32.3,<3`), and most dependencies accept versions
  down to the oldest ones tested. pydantic requires 2.8 or newer, since older 2.x
  versions failed on import.

### Removed

- `misc.SuggestionDict` (use `misc.SuggestionMapping`) and `com.FilterValue` (use
  `misc.Filter`).
- the `rechunk` parameter of `get_data`: call `.rechunk()` on the result instead.
- `Bpod.serial2`: the FlexIO serial connection is now private.
- the unused `qtpy` dependency.

### Fixed

- module events and softcodes never reached the state machine. Serial events are now
  allocated to modules as in Bpod's MATLAB software, which also fixes misnumbered input
  events with modules requesting a non-default number of events.
- events in the same state machine cycle as a transition were evaluated in the new
  state, so trial data could attribute them to the wrong state or record transitions
  the Bpod never made.
- `GlobalTimerTrig` and `GlobalTimerCancel` acted on the wrong global timers for
  indices of 2 or more.
- trial data showed valves and wire outputs as staying on when a state began that
  didn't set them, although the Bpod switches them off.
- the `TrialEnd` time of a queued trial was one state machine cycle late (100 µs on a
  Bpod 2+).
- state machine validation missed values the Bpod can't receive: softcodes above 254,
  references to global timers and counters that haven't been set (`GlobalTimerTrig`,
  `GlobalTimerCancel`, `GlobalCounterReset`, onset triggers), and unknown global
  counter events and condition channels.
- a trial number above 65535 or below 0 made `get_data()` fail, losing the collected
  trials. `run()` now rejects trial numbers outside 0 to 4,294,967,295.
- a `run()` that raised before sending, e.g., for an invalid state machine, still used
  up a trial number, so the next trial skipped one.
- `run()` without a state machine reran the last one even after the hardware
  configuration had changed, e.g., through `update_modules()`. It now raises a
  `RuntimeError`.
- `get_data()` right after `stop_state_machine()` could raise "No trial data available"
  while the stopped trial was still being processed.
- `Input.override` didn't send the input's index, so the Bpod took the next command's
  first byte as the override state.
- `is_running` was briefly `False` while a queued state machine was starting, so
  `stop_state_machine()` could do nothing.
- unplugging the Bpod during a trial went unnoticed. `wait()` now raises a `BpodError`.
- a blocking `peek_data()` could, rarely, hang when its trial ended without reaching a
  trigger state.
- closing a `Bpod` could wait forever for the Bpod to confirm the disconnect, or
  disconnect before the last softcodes were handled.
- leaving a `Bpod` context didn't close its IPC service or end its softcode thread, and
  a `Bpod` that failed to connect kept its serial port open. Objects dropped without
  closing (a `Bpod`, a subscribed `RemoteBpod`, an unfinished `iter_services` iterator,
  or anything a handler refers back to), a failed `RemoteBpod` handshake and a failed
  Zeroconf start left connections and threads running.
- closing a `Bpod`, `ServiceHost` or `ServiceClient` from within its own handler hung or
  raised a `RuntimeError`.
- starting a service could fail with `FileNotFoundError` while another process closed
  its last service, and closing a `ServiceHost` or `LocalServiceAdvertisement` could
  remove the files of a newer one with the same UUID.
- validating or compiling state machines in several threads at once (e.g., two Bpods
  in one process, or a `RemoteBpod` next to the host's own script) could fail or
  corrupt the caches they share.
- `RemoteBpod.get_data` raised a `DeprecationWarning` with polars 1.43.2 or newer.

## [0.1.0a14] - 2026-07-24

### Added

- `Bpod` publishes live trial events over the PUB/SUB channel as a tagged union of
  typed messages.
- `RemoteBpod` accepts an `event_callback` that receives each live trial-event
  message.
- `ServiceHost.get_metadata` for accessing per-request connection metadata from within
  a request handler.
- `ServiceHost.publish` for broadcasting events over the PUB/SUB channel;
  `ServiceClient` decodes them per its new `event_type` parameter.
- `ServiceHost` remembers the TCP ports last bound for a caller-supplied `uuid`
  (stored in the user state directory) and reuses them on restart; explicit
  `port_rep` / `port_pub` arguments take precedence.
`ServiceHost.has_subscribers`: the host tracks PUB/SUB subscriptions and
  `publish` drops messages before encoding while nobody is listening.
- `ServiceClient.request` accepts an optional `timeout`; the client recovers cleanly
  after a timed-out request.

### Changed

- Add UTC timezone to trial DataFrame timestamps.
- `ServiceClient` adopts the host's serialization format during the
  handshake, replacing the per-request trial-and-error fallback.
- the WELCOME handshake carries the PUB channel's TCP port instead of full TCP
  addresses.
- errors raised by `bpod_core.ipc` are now uniformly `ServiceError` subclasses
- remote method calls (`BpodRequestCall` / `BpodRequestData`) are validated against
  an explicit allowlist instead of dispatching to arbitrary attributes.

### Fixed

- `ServiceClient` no longer blocks indefinitely when the host dies mid-request.
- exceptions with non-serializable attributes no longer break error replies from
  `ServiceHost`.
- resolved several shutdown races between `close()` and the event threads / `publish()`.
- `ServiceIterator.close()` now terminates an iteration blocked in `__next__`
  (e.g., with `timeout=None`) instead of leaving the consumer waiting forever.

## [0.1.0a13] - 2026-06-18

### Changed

- further work on bringing `RemoteBpod` to parity with `Bpod`.
- `ExtendedSerial.read_struct` and `write_struct` methods accept precompiled structs.
- reduced public interface of `bpod_core.ipc`.
- improved documentation.

## [0.1.0a12] - 2026-04-30

Just some minor improvements ...

### Changed

- further refinements to documentation.
- cleaned up rendering of Graphviz graphs.

### Removed

- removed unused `misc.DocstringInheritanceMixin`

## [0.1.0a11] - 2026-04-24

### Added

- `ExtendedSerial.read_struct_iter` and `ExtendedSerial.stream_struct` methods.
- `misc.SuggestionDict` for key lookup with typo suggestions.
- added optional `trial_number` parameter to `Bpod.run` method.

### Changed

- replaced `NamedTuples` with `misc.SuggestionDict` for `Bpod.inputs` and
  `Bpod.outputs`
- refactored Sphinx documentation and API reference.

## [0.1.0a10] - 2026-04-16

### Added

- Bpod.address property — exposes the ZeroMQ address of the running instance.
- `Bpod.peek_data` - read trial data before trial end.
- `misc.ByteEnum` - an extended `IntEnum` that caches its values as bytes.
- Timer fields in StateMachine now accept timedelta values in addition to floats.
- `bpod` CLI entry point for launching a Bpod instance that can be connected to via TCP
  Unix Sockets.
- `state machine` column added to trial data output (hash of the FSM)

### Changed

- renamed `Bpod.send_state_machine` to `Bpod.run`.
- removed `Bpod.run_state_machine`.
- switched to Polars `LazyFrame` for storing trial data.
- replaced use of `Queue` with `SimpleQueue`.
- switched `Bpod.module` field from `NamedTuple` to `dict`.
- replaced hashlib.blake2b with xxhash for state machine hashing.
- cache validation and compilation of state machines.
- `StateMachine.hash` now returns `bytes` instead of `str`.

## [0.1.0a9] - 2026-04-02

### Added

- `bpod.is_ready` property indicating whether a compiled FSM is loaded and ready to run
- `bpod.is_queued` property indicating whether an FSM is queued to run after the current
  one
- `bpod.get_data` for returning trial data after a state machine run.

### Changed

- renamed `BNC` input events and output actions to `TTLIn` and
  `TTLOut`.
- replaced MD5 hashing of `fsm.StateMachine` with blake2b.
- improved readability of `ValidationError` messages in `fsm.StateMachine`.
- state machine runs are now handled by three separate threads:
    - `ReadThread` for serial communication with the Bpod,
    - `EventThread` for handling and structuring the incoming data, and
    - `SoftcodeThread` for executing soft-codes.

### Fixed

- reorganization of finalizers for better garbage collection.

## [0.1.0a8] - 2026-03-04

Just a quick bugfix release ...

### Changed

- `ipc.ServiceHost`: set Zeroconf to listen on all interfaces, reorder `WelcomeData`
  fields, and improve type hints.
- event handler callback type hint to return `None` instead of `Any`.
- refactor CI workflows.

## [0.1.0a7] - 2026-03-01

### Added

- `com.find_ports` for finding serial ports that match given filter criteria.
- `com.verify_serial_discovery` for checking if a serial device sends an expected
  discovery message.
- convenience functions for reading and writing integers in `com.ExtendedSerial`.
- `misc.extend_packed` for extending a bytearray by multiple values of the same format.
- `misc.DocstringInheritanceMixin` for inheriting docstrings from base classes.
- `ipc.LocalServiceAdvertisement`: local service advertisement and discovery
- `ipc.iter_services` and `ipc.ServiceIterator` for discovering services.
- `bpod.discover_remote_bpod` for discovering remote Bpod instances.
- more examples.

### Changed

- more verbose debug logging during state machine runs.
- replace unmaintained `appdirs` dependency with `platformdirs`.
- switch to zero-based indexing for global counters, timers, conditions and soft-codes.
- added file locking to `misc.SettingsDict`.
- switch to using ZMQ multipart messages for communication.
- renamed `ipc.DualChannelHost` / `ipc.DualChannelClient` to `ipc.ServiceHost` /
  `ipc.ServiceClient`.
- restructured `bpod` module into several submodules.
- zero-copy behavior on send path of `ipc.ServiceClient` and receive path of
  `ipc.ServiceHost`.

### Fixed

- decoupled finalizers and event loops from instance references.

## [0.1.0a6] - 2025-10-29

### Added

- Initial work on cached state machine validation: `fsm.StateMachine.check`

### Changed

- `com.ExtendedSerial` no longer supports NumPy types, integers, strings and iterables.
- refactoring of `com.ChunkedSerialReader`

### Removed

- `com.to_bytes`
- dependency on Pandas

## [0.1.0a5] - 2025-09-03

### Added

- `fsm.to_file` and `fsm.from_file` for export/import of state machines.
- YAML import/export for state machines
- Work on documentation: Finite-State Machines

### Changed

- moved state machine structure from msgspec Struct back to Pydantic Model
- merged `fsm_types` module back into `fsm`
- renamed parameters for `add_state` method: `state_change_conditions` -> `transitions`
  and `output_actions` -> `actions`

## [0.1.0a4] - 2025-08-25

### Added

- `ipc.DualChannelHost` and `ipc.DualChannelClient`
- JSON import/export for state machines
- example state machines

### Changed

- improved export of state machines to graphviz
- improved settings management
- moved state machine structure from Pydantic Model to msgspec Struct

## [0.1.0a3] - 2025-08-04

### Added

- state machine assembly
- state machine thread
- ZMQ communication module
- basic settings management

### Changed

- switched package management from PDM to uv.

## [0.1.0a2] - 2025-05-07

### Added

- pydantic model for state machine

### Changed

- further refactoring of the code
- renamed `serial` module to `com` to avoid confusion with PySerial.

## [0.1.0a1] - 2025-04-22

### Changed

- major reorganization of the existing codebase.
- renamed `serial_extensions` module to `serial`.

### Removed

- removed `SerialSingleton` class.

## [0.1.0a0] - 2025-04-17

First alpha release. Nothing works.

[0.1.0a15]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a15

[0.1.0a14]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a14

[0.1.0a13]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a13

[0.1.0a12]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a12

[0.1.0a11]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a11

[0.1.0a10]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a10

[0.1.0a9]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a9

[0.1.0a8]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a8

[0.1.0a7]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a7

[0.1.0a6]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a6

[0.1.0a5]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a5

[0.1.0a4]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a4

[0.1.0a3]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha.3

[0.1.0a2]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha.2

[0.1.0a1]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha.1

[0.1.0a0]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha
