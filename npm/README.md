# silkcode (npx launcher)

```bash
npx silkcode gui
```

[Silk Code](https://github.com/RupertCloud/SilkCode) is a model-agnostic AI
coding agent — phone-first GUI, permission gates on every risky action,
provenance-scanned tool output — written in Python. This package is its npm
doorway: on first run it finds a Python 3.10+ on your machine and installs
Silk Code and its headless Chromium into an isolated environment
(`~/.silkcode/runtime`, or `%LOCALAPPDATA%\SilkCode\runtime` on Windows);
every later run starts instantly. It has **zero npm dependencies** — the
whole launcher is one auditable file.

Requires Python 3.10+ on PATH (or set `SILKCODE_PYTHON=/path/to/python`).
`SILKCODE_INSTALL_DIR` overrides where the runtime lives.

Silk Code updates itself from inside the app; the npm package only ever
bootstraps and launches, so there is no version drift to manage here.

Full documentation: https://github.com/RupertCloud/SilkCode#readme
