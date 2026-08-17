# chaogetalks-utils

[简体中文](README.zh-CN.md)

A collection of practical utilities, open-sourced with love.

## Utilities

| Utility | Description |
| --- | --- |
| [Codex Session Guard](codex/README.md) | A read-only size audit for local Codex Desktop sessions; it does not modify session data. |

## Quick start

```bash
./codex/codex_session_guard.py --no-log
```

See the [Codex Session Guard guide](codex/README.md) for usage and options.

The tool does not modify session data, but its default operation writes a local report log. Use `--no-log` to avoid logging.

This project is released under the [MIT License](LICENSE). The utilities are provided as-is; assess their suitability for your environment and use them at your own risk.

Made and maintained with love by [@chaogetalks](https://x.com/chaogetalks).
