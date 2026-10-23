# Hardware considerations

Some functionality communicates with physical Bpod devices.

- Do not assume hardware is available.
- Prefer mockable interfaces.
- Hardware-dependent tests should be isolated.
- Avoid making changes that require hardware unless explicitly requested.
- Don't work around firmware bugs or questionable firmware design unless the user
  asks; report them instead (firmware file/line, effect, suggested fix) so the
  user can raise them upstream.
