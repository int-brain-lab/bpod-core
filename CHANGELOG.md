Changelog
=========

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0a5] - 2025-09-03

### Added

- `fsm.to_file` and `fsm.from_file` for export/import of state machines.
- YAML import/export for state machines
- Initial work on documentation: Finite-State Machines

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

[0.1.0a5]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a5
[0.1.0a4]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0a4
[0.1.0a3]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha.3
[0.1.0a2]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha.2
[0.1.0a1]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha.1
[0.1.0a0]: https://github.com/int-brain-lab/bpod-core/releases/tag/0.1.0-alpha
