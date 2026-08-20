# Visual regression snapshots

These few SVGs are intentionally tracked even though bulk benchmark artifacts
are ignored. They let code review compare the emitted schematic itself, not
only routing counters.

Regenerate them with:

```bash
.venv/bin/python tests/tools/update_visual_regressions.py
```

`i2c_terminal_limit.svg` records the current `> 8`-terminal routing fallback.
Its adjacent metrics file is expected to name `SDA` and `SCL` in
`critical_stub_nets` until the bus/trunk router work removes that limitation.
