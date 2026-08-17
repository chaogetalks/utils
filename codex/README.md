# Codex Session Guard

[简体中文](README.zh-CN.md) · [Project home](../README.md)

Codex Session Guard is a size-audit utility that is read-only with respect to local Codex session data (while its default behavior still writes a local report log). It scans session JSONL files and, when available, combines them with task metadata from `state_5.sqlite` to:

- aggregate session sizes by task, including a parent task and its sub-agents when metadata is available;
- inspect individual session files with `--view files`;
- classify sizes as SAFE, WATCH, WARNING, or CRITICAL using configurable thresholds.

The tool does not read conversation bodies and does not modify Codex session data. It loads complete task metadata from `state_5.sqlite` only when an exclusive, non-mutating read is available. If another process holds the database, the tool does not wait; it immediately falls back to file attributes such as names, sizes, and modification times. In fallback mode, titles and workspaces are unavailable, parent/sub-agent relationships cannot be reconstructed, and task aggregation is limited to identifiers derived from filenames. Session candidates are deduplicated by canonical physical-file identity, so path aliases, symbolic links, and hard links to the same file count only once toward both file count and total size. By default it appends the report to a log, so it is not accurate to claim that the program never writes files.

## Requirements

- Python 3.11 or newer;
- [`uv`](https://docs.astral.sh/uv/);
- a macOS/Linux-style direct executable invocation. The script uses a `uv` shebang and can be invoked as `./codex/codex_session_guard.py`; this describes the invocation form and is not a broader platform-support claim.

From the repository root, use either invocation:

```bash
# Direct macOS/Linux-style shebang invocation
./codex/codex_session_guard.py --no-log

# Explicit uv invocation
uv run codex/codex_session_guard.py --no-log
```

`--no-log` is the privacy-preserving quick start; it prevents a normal scan from creating or appending a report log. Omitting it enables the default log, which appends a plain-text report to `codex/codex_session_guard.log` beside the script. Any custom log target inside the Codex data directory or backed by a hard link is rejected before writing, so database and session files remain unchanged.

## Common commands

Run these from the repository root. JSON output can contain local paths, so the example uses `--no-log`:

```bash
# Inspect individual JSONL session files
uv run codex/codex_session_guard.py --view files --no-log

# Emit machine-readable JSON without appending a log
uv run codex/codex_session_guard.py --json --no-log

# Set WATCH, WARNING, and CRITICAL thresholds in decimal GB
uv run codex/codex_session_guard.py --thresholds 5,10,15 --no-log

# Display at most 10 rows
uv run codex/codex_session_guard.py --top 10 --no-log

# Display only rows at or above 1.5 decimal GB
uv run codex/codex_session_guard.py --min-size 1.5 --no-log

# Scan active tasks only, excluding archived tasks
uv run codex/codex_session_guard.py --active-only --no-log

# Use a specific Codex data directory
uv run codex/codex_session_guard.py --codex-home /path/to/.codex --no-log

# Show the complete help, including --log-file, --color, and --version
uv run codex/codex_session_guard.py --help
```

## Option reference

| Option | Purpose |
| --- | --- |
| `--view tasks\|files` | Aggregate by task or inspect individual JSONL files |
| `--json` | Emit machine-readable JSON |
| `--thresholds A,B,C` | Set three increasing decimal-GB thresholds |
| `--top N` / `-n N` | Display at most N rows |
| `--min-size GB` | Filter out rows below the given decimal-GB size |
| `--active-only` | Exclude archived tasks (archived tasks are included by default) |
| `--codex-home PATH` | Select the Codex data directory |
| `--log` / `--no-log` | Enable or disable appended logging |
| `--help` | Show CLI help |

## Defaults and exit codes

The default view aggregates tasks (`--view tasks`) and includes archived tasks; it displays up to 20 rows (`--top 20`). Thresholds default to 10/15/20 decimal GB. The Codex data directory comes from `$CODEX_HOME` when set, otherwise `~/.codex`. The default log is `codex/codex_session_guard.log` beside the script and is appended to; use `--log-file` for a non-hard-linked path outside the Codex data directory, or `--no-log` to disable logging.

The process computes the highest-risk item across all rows in the selected view before applying the `--top` and `--min-size` display filters. Those filters therefore cannot lower the exit code; output may even contain no rows while the exit code is 10, 15, or 20. For configurable thresholds `A,B,C` (strictly increasing decimal-GB values), the mutually exclusive ranges are: SAFE `< A`; WATCH `A <= size < B`; WARNING `B <= size < C`; CRITICAL `>= C`.

| Exit code | Meaning |
| ---: | --- |
| 0 | SAFE: `size < A` |
| 10 | WATCH: `A <= size < B` |
| 15 | WARNING: `B <= size < C` |
| 20 | CRITICAL: `size >= C` |
| 2 | Invalid or missing Codex home, or log-write failure |

Typer may use its own nonzero exit code for CLI parse errors; do not assume that a parse error has one exact value from this table.

## Privacy

Reports and logs may contain task titles, the full Codex home path, workspace names or paths, session paths (in JSON output when using `--view files`, or in scan errors), thread IDs or prefixes, timestamps, and scan errors. The normal Rich-text `files` view does not display full paths. Prefer `--no-log` for routine use, review terminal or JSON output before sharing, and never share it blindly. The project `.gitignore` ignores `*.log`, but you should still review generated files yourself.

Thanks for using and improving it. [@chaogetalks](https://x.com/chaogetalks) keeps this project open-sourced with love; it is released under the [MIT License](../LICENSE).
