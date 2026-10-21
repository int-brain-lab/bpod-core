Changelog
=========

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## UNRELEASED

### Added

- support for FlexIO channels (Bpod Finite State Machine r2+): `Bpod.flex_io`
  configures each channel as digital or analog input or output, along with analog
  thresholds and the sampling rate; `Bpod.get_analog_data` returns the continuously
  sampled analog input in millivolts. Flex channel events and analog-output actions
  can be used in state machines, and global timers can drive Flex outputs.
- `Bpod.action_names`: names of all actions a state machine can currently set,
  mirroring `Bpod.input_event_names`.
- state machine validation rejects non-integer action values before compilation.
- `ExtendedSerial.temporary_timeout`: context manager for temporarily setting a timeout
  for serial reads.
- Optimizations for agentic development (`AGENTS.md`, `.mcp.json`, `llms.txt`, etc.)
- `filtering.match_filter` for matching values against scalar, regex or collection
  criteria.
- `ipc.iter_services`, `ipc.discover` and `ipc.ServiceIterator` accept regex and
  collection criteria for `properties`.
- `misc.SuggestionMapping`: a read-only mapping that suggests similar keys on failed
  lookup, for fixed sets of keys (e.g. hardware channels) that should never be mutated
  after construction.

### Changed

- `com.find_ports` takes explicit keyword-only filters instead of `**filters`;
  unknown filter names now raise `TypeError` instead of silently matching nothing.
- `com.find_ports` and service discovery ignore filters set to `None` instead of
  matching only `None`-valued or missing attributes.
- `Bpod.inputs`/`Bpod.outputs`/`Bpod.modules` are now read-only `misc.SuggestionMapping`
  instead of mutable dicts, preventing accidental corruption.
- Module-not-found errors now suggest the closest matching module name, matching the
  error style already used for input/output channels.
- methods that query or reconfigure the Bpod (e.g., `set_status_led`, `update_modules`,
  `Input.read`, `Module.load_serial_message`) raise a `BpodError` while a state machine
  is running, instead of corrupting the trial's data.
- `send_softcode` no longer sends a softcode while no state machine is running, since
  the Bpod ignores it then; it still logs a warning.
- the `trial` column of trial data is `UInt32` instead of `UInt16`.
- state machine action values can be up to 65535 on Bpod r2+ (still 255 on earlier
  models), and the `value` column of trial data is `UInt16` instead of `UInt8`, to hold
  FlexIO analog-output values in millivolts.
- trial data is read from the Bpod in chunks rather than message by message, which
  lowers the CPU load at high event and softcode rates.
- live trial events are published in batches rather than one message per event,
  lowering the CPU load while a client is subscribed. `RemoteBpod`'s `event_callback`
  still receives single events.
- polars 2 is supported (`polars>=1.32.3,<3`).
- lowered the minimum versions of most dependencies to the oldest ones tested to work.
  pydantic now requires 2.8 or newer: older 2.x versions could be installed but failed
  on import.

### Removed

- `misc.SuggestionDict`, superseded by `misc.SuggestionMapping` (no longer used anywhere
  in the library).
- `com.FilterValue`, superseded by `filtering.Filter`.
- the `rechunk` parameter of `get_data`: call `.rechunk()` on the result instead.
- the unused `qtpy` dependency.
- `Bpod.serial2`: the FlexIO serial connection is now private.

### Fixed

- starting a service could fail with `FileNotFoundError` while another process on the
  same machine closed its last service.
- `RemoteBpod.get_data` raised a `DeprecationWarning` with polars 1.43.2 or newer.
- leaving a `Bpod` context did not close its IPC service, so its sockets stayed bound
  until garbage collection and reconnecting right away fell back to random ports.
- leaving a `Bpod` context did not end its softcode thread, so every connection left
  an idle thread behind.
- a `Bpod` that failed to connect, e.g., due to unsupported firmware, kept its serial
  port open until garbage collection and left its softcode thread behind.
- a `Bpod` or subscribed `RemoteBpod` dropped without closing it, an unfinished
  `iter_services` iterator, e.g., after a `break`, and any object whose handler for a
  `ServiceHost`, `ServiceClient` or `Bpod.set_softcode_handler` referred back to it
  were never garbage-collected, so their connections and threads ran until Python
  exited.
- a failed `RemoteBpod` handshake, e.g., due to a version mismatch, or a failed
  Zeroconf start in `iter_services` left connections or threads running.
- closing a `Bpod`, `ServiceHost` or `ServiceClient` from within its own handler hung
  or raised a `RuntimeError`.
- closing a `ServiceHost` or `LocalServiceAdvertisement` could remove the files of a
  newer one with the same UUID.
- unplugging the Bpod during a trial went unnoticed: `wait()` returned as if the trial
  had ended normally. It now raises a `BpodError`, and `close()` tolerates the
  vanished port.
- a blocking `Bpod.peek_data()` could, rarely, hang when its trial ended without
  reaching a trigger state.
- closing a `Bpod` could disconnect before the last softcodes were handled.
- module events and softcodes never reached the state machine. Serial events are
  now allocated to modules as in Bpod's MATLAB software and sent to the state
  machine when modules are updated. This also fixes misnumbered input events with
  modules requesting a non-default number of events.
- `Input.override` didn't send the input's index, so the Bpod took the first byte of
  the next command as the override state. While no state machine is running, the
  override is no longer sent, since the Bpod would ignore it; a warning is logged
  instead.
- `is_running` was briefly `False` while a queued state machine was starting, although
  the Bpod was already running it; `stop_state_machine()` then did nothing.
- closing a `Bpod` checked the reply to its disconnect request against the wrong value
  and could wait forever for it. It now waits at most 0.2 s and logs a warning if the
  Bpod doesn't confirm.
- the `TrialEnd` time of a queued trial was one state machine cycle late (100 µs on a
  Bpod 2+), because it was derived from a firmware cycle count that also includes the
  cycle in which the previous trial ended. It now comes from the trial's exit event,
  like all other event times.
- a trial number above 65535 or below 0 made `get_data()` fail, losing the trials it
  had collected. `run()` now rejects trial numbers outside 0 to 4,294,967,295.
- `GlobalTimerTrig` and `GlobalTimerCancel` acted on the wrong global timers for
  indices of 2 or more: the Bpod reads their values as one bit per timer, but they were
  sent as index plus one. Indices of global timers that haven't been set are now
  rejected.

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
