Changelog
=========

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## UNRELEASED

### Added

- `Bpod.peek_data` - read trial data before trial end.
- `misc.ByteEnum` - an extended `IntEnum` that caches its values as bytes.
- Timer fields in StateMachine now accept timedelta values in addition to floats.
- `bpod` CLI entry point for launching a Bpod instance that can be connected to via TCP
  Unix Sockets.

### Changed

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
    - `ReadThread` for serial communication with the Bpod
    - `EventThread` for handling and structuring the incoming data
    - `SoftcodeThread` for executing soft-codes

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

[0.1.0a8]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a8

[0.1.0a7]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a7

[0.1.0a6]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a6

[0.1.0a5]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a5

[0.1.0a4]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a4

[0.1.0a3]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha.3

[0.1.0a2]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha.2

[0.1.0a1]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha.1

[0.1.0a0]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha
