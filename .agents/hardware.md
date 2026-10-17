# Hardware considerations

Some functionality communicates with physical Bpod devices.

- Do not assume hardware is available.
- Prefer mockable interfaces.
- Hardware-dependent tests should be isolated.
- Avoid making changes that require hardware unless explicitly requested.
