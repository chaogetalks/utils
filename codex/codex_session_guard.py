#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "rich>=14.2.0,<15",
#   "typer>=0.16.0,<1",
# ]
# ///

"""Fast, read-only size audit for local Codex Desktop sessions."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat as stat_module
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Iterable, TextIO

import typer
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


APP_NAME = "Codex Session Guard"
VERSION = "1.0.0"
DECIMAL_GB = 1_000_000_000
UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:\.jsonl)?$",
    re.IGNORECASE,
)


class ColorMode(str, Enum):
    auto = "auto"
    always = "always"
    never = "never"


class ViewMode(str, Enum):
    tasks = "tasks"
    files = "files"


class UnsafeLogDestinationError(ValueError):
    pass


@dataclass(frozen=True)
class ThreadMeta:
    thread_id: str
    rollout_path: Path
    title: str
    workspace: str
    updated_at: int
    archived: bool
    source: str


@dataclass(frozen=True)
class SessionFile:
    thread_id: str
    path: Path
    size_bytes: int
    title: str
    workspace: str
    updated_at: int
    archived: bool
    source: str


CandidateKey = tuple[str, int, int] | tuple[str, str]


@dataclass(frozen=True)
class _CandidateOrigin:
    path: Path
    origin_path: Path
    file_stat: os.stat_result
    thread_id: str
    archived: bool
    metadata: ThreadMeta | None


@dataclass(frozen=True)
class TaskRecord:
    thread_id: str
    size_bytes: int
    file_count: int
    agent_count: int
    title: str
    workspace: str
    updated_at: int
    archived: bool


@dataclass(frozen=True)
class Risk:
    rank: int
    label: str
    icon: str
    style: str
    exit_code: int


RISKS = (
    Risk(0, "SAFE", "●", "green", 0),
    Risk(1, "WATCH", "◆", "yellow", 10),
    Risk(2, "WARNING", "▲", "dark_orange3", 15),
    Risk(3, "CRITICAL", "✖", "bold red", 20),
)


app = typer.Typer(
    add_completion=False,
    help=f"{APP_NAME} — spot oversized Codex Desktop tasks before they become a problem.",
    rich_markup_mode="rich",
    pretty_exceptions_enable=False,
)


def clean_text(value: str, fallback: str) -> str:
    cleaned = " ".join((value or "").split())
    return cleaned or fallback


def session_id_from_path(path: Path) -> str:
    match = UUID_RE.search(path.name)
    return match.group(1).lower() if match else path.stem


def _normalized_path(path: Path) -> str:
    return os.path.normcase(str(path))


def validate_log_destination(proposed_path: Path, codex_home: Path) -> Path:
    """Return one canonical log path only when it cannot target Codex data."""
    try:
        canonical = proposed_path.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise UnsafeLogDestinationError(
            f"could not safely resolve {proposed_path}: {exc}"
        ) from exc

    if canonical == codex_home or codex_home in canonical.parents:
        raise UnsafeLogDestinationError(
            f"{canonical} is inside the Codex data directory"
        )

    try:
        target_stat = canonical.stat()
    except FileNotFoundError:
        return canonical
    except OSError as exc:
        raise UnsafeLogDestinationError(
            f"could not safely inspect {canonical}: {exc}"
        ) from exc

    if target_stat.st_nlink > 1:
        raise UnsafeLogDestinationError(
            f"{canonical} is an existing target with multiple hard links"
        )
    return canonical


def _candidate_key(path: Path, file_stat: os.stat_result) -> CandidateKey:
    if file_stat.st_dev and file_stat.st_ino:
        return ("inode", file_stat.st_dev, file_stat.st_ino)
    return ("path", _normalized_path(path))


def format_size(size_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size_bytes)
    for unit in units:
        if value < 1000 or unit == units[-1]:
            precision = 0 if unit in {"B", "KB"} else 2
            return f"{value:.{precision}f} {unit}"
        value /= 1000
    return f"{size_bytes} B"


def parse_thresholds(raw: str) -> tuple[int, int, int]:
    try:
        values = tuple(float(item.strip()) for item in raw.split(","))
    except ValueError as exc:
        raise typer.BadParameter("use three comma-separated GB values, e.g. 10,15,20") from exc

    if len(values) != 3 or any(value <= 0 for value in values):
        raise typer.BadParameter("thresholds must contain exactly three positive GB values")
    if values != tuple(sorted(values)) or len(set(values)) != 3:
        raise typer.BadParameter("thresholds must be strictly increasing")
    return tuple(round(value * DECIMAL_GB) for value in values)  # type: ignore[return-value]


def risk_for(size_bytes: int, thresholds: tuple[int, int, int]) -> Risk:
    if size_bytes >= thresholds[2]:
        return RISKS[3]
    if size_bytes >= thresholds[1]:
        return RISKS[2]
    if size_bytes >= thresholds[0]:
        return RISKS[1]
    return RISKS[0]


def make_console(color: ColorMode, file: TextIO | None = None) -> Console:
    if file is not None:
        return Console(file=file, color_system=None, force_terminal=False, highlight=False, width=140)
    if color is ColorMode.always:
        return Console(force_terminal=True, color_system="truecolor")
    if color is ColorMode.never:
        return Console(no_color=True, highlight=False)
    return Console()


def load_database(
    codex_home: Path,
) -> tuple[dict[str, ThreadMeta], dict[str, list[str]], str]:
    database = codex_home / "state_5.sqlite"
    if not database.is_file():
        return {}, {}, "not found — using filenames only"

    metadata: dict[str, ThreadMeta] = {}
    children: dict[str, list[str]] = defaultdict(list)
    try:
        with closing(
            sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=0)
        ) as connection:
            connection.row_factory = sqlite3.Row
            locking_mode = connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()
            if locking_mode is None or str(locking_mode[0]).lower() != "exclusive":
                raise sqlite3.OperationalError("exclusive locking mode unavailable")

            rows = connection.execute(
                """
                SELECT id, rollout_path, title, cwd, updated_at, archived, source
                FROM threads
                """
            ).fetchall()
            for row in rows:
                thread_id = str(row["id"]).lower()
                metadata[thread_id] = ThreadMeta(
                    thread_id=thread_id,
                    rollout_path=Path(row["rollout_path"]).expanduser(),
                    title=clean_text(row["title"], "Untitled task"),
                    workspace=clean_text(row["cwd"], "—"),
                    updated_at=int(row["updated_at"] or 0),
                    archived=bool(row["archived"]),
                    source=str(row["source"] or ""),
                )

            edge_rows = connection.execute(
                "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
            ).fetchall()
            for edge in edge_rows:
                children[str(edge["parent_thread_id"]).lower()].append(
                    str(edge["child_thread_id"]).lower()
                )
        return metadata, dict(children), f"loaded {len(metadata):,} task records"
    except (sqlite3.Error, OSError) as exc:
        return {}, {}, f"unavailable ({exc}) — using filenames only"


def discover_sessions(
    codex_home: Path,
    metadata: dict[str, ThreadMeta],
    include_archived: bool,
) -> tuple[list[SessionFile], list[str]]:
    directories = [(codex_home / "sessions", False)]
    if include_archived:
        directories.append((codex_home / "archived_sessions", True))

    candidates: dict[CandidateKey, list[_CandidateOrigin]] = defaultdict(list)
    errors: list[str] = []

    def add_candidate(
        path: Path,
        *,
        archived: bool,
        item: ThreadMeta | None = None,
    ) -> None:
        try:
            canonical = path.resolve(strict=True)
            file_stat = canonical.stat()
        except (OSError, RuntimeError) as exc:
            errors.append(f"Could not inspect {path}: {exc}")
            return

        if not stat_module.S_ISREG(file_stat.st_mode):
            errors.append(f"Could not inspect {path}: not a regular file")
            return

        candidates[_candidate_key(canonical, file_stat)].append(
            _CandidateOrigin(
                path=canonical,
                origin_path=path,
                file_stat=file_stat,
                thread_id=item.thread_id.lower() if item else session_id_from_path(path),
                archived=item.archived if item else archived,
                metadata=item,
            )
        )

    for directory, archived in directories:
        if not directory.exists():
            continue
        try:
            paths = sorted(directory.rglob("*.jsonl"), key=_normalized_path)
            for path in paths:
                add_candidate(path, archived=archived)
        except OSError as exc:
            errors.append(f"Could not scan {directory}: {exc}")

    eligible_metadata = {
        thread_id: item
        for thread_id, item in metadata.items()
        if include_archived or not item.archived
    }
    metadata_items = sorted(
        eligible_metadata.values(),
        key=lambda item: (item.thread_id.lower(), _normalized_path(item.rollout_path)),
    )
    for item in metadata_items:
        add_candidate(item.rollout_path, archived=item.archived, item=item)

    sessions: list[SessionFile] = []
    for origins in candidates.values():
        metadata_origins = [origin for origin in origins if origin.metadata is not None]
        if metadata_origins:
            selected = min(
                metadata_origins,
                key=lambda origin: (
                    origin.thread_id,
                    _normalized_path(origin.origin_path),
                    _normalized_path(origin.path),
                ),
            )
            meta = selected.metadata
        else:
            selected = min(
                origins,
                key=lambda origin: (
                    origin.archived,
                    _normalized_path(origin.path),
                    _normalized_path(origin.origin_path),
                    origin.thread_id,
                ),
            )
            meta = eligible_metadata.get(selected.thread_id)

        thread_id = meta.thread_id.lower() if meta else selected.thread_id
        archived = meta.archived if meta else selected.archived
        if archived and not include_archived:
            continue
        sessions.append(
            SessionFile(
                thread_id=thread_id,
                path=selected.path,
                size_bytes=selected.file_stat.st_size,
                title=meta.title if meta else "Unknown / orphaned session",
                workspace=meta.workspace if meta else "—",
                updated_at=(
                    meta.updated_at
                    if meta and meta.updated_at
                    else int(selected.file_stat.st_mtime)
                ),
                archived=archived,
                source=meta.source if meta else "unknown",
            )
        )
    return sorted(
        sessions,
        key=lambda item: (-item.size_bytes, item.thread_id, _normalized_path(item.path)),
    ), errors


def aggregate_tasks(
    sessions: list[SessionFile], children: dict[str, list[str]]
) -> list[TaskRecord]:
    by_id: dict[str, list[SessionFile]] = defaultdict(list)
    for session in sessions:
        by_id[session.thread_id].append(session)
    for grouped_sessions in by_id.values():
        grouped_sessions.sort(
            key=lambda item: (
                item.archived,
                _normalized_path(item.path),
                item.title,
                item.workspace,
                item.source,
                item.updated_at,
            )
        )

    child_ids = {child for values in children.values() for child in values if child in by_id}
    roots = sorted(thread_id for thread_id in by_id if thread_id not in child_ids)
    records: list[TaskRecord] = []

    def descendants(root_id: str, excluded: set[str]) -> list[str]:
        found: list[str] = []
        pending = [root_id]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited or current in excluded or current not in by_id:
                continue
            visited.add(current)
            found.append(current)
            pending.extend(children.get(current, ()))
        return found

    covered: set[str] = set()

    def add_record(root_id: str) -> None:
        ids = descendants(root_id, covered)
        if not ids:
            return
        covered.update(ids)
        root = by_id[root_id][0]
        files = [session for thread_id in ids for session in by_id[thread_id]]
        records.append(
            TaskRecord(
                thread_id=root_id,
                size_bytes=sum(session.size_bytes for session in files),
                file_count=len(files),
                agent_count=max(0, len(ids) - 1),
                title=root.title,
                workspace=root.workspace,
                updated_at=max(session.updated_at for session in files),
                archived=root.archived,
            )
        )

    for root_id in roots:
        add_record(root_id)
    for thread_id in sorted(by_id):
        if thread_id not in covered:
            add_record(thread_id)

    return sorted(
        records,
        key=lambda item: (-item.size_bytes, item.thread_id, item.title, item.workspace),
    )


def make_meter(size_bytes: int, critical_bytes: int, risk: Risk, width: int = 10) -> Text:
    filled = min(width, round((size_bytes / critical_bytes) * width)) if critical_bytes else 0
    meter = Text()
    meter.append("━" * filled, style=risk.style)
    meter.append("─" * (width - filled), style="dim")
    return meter


def make_header(
    codex_home: Path,
    database_status: str,
    session_count: int,
    task_count: int,
    total_bytes: int,
    largest_bytes: int,
    largest_risk: Risk,
) -> Panel:
    stats = Table.grid(expand=True, padding=(0, 2))
    stats.add_column(ratio=1)
    stats.add_column(ratio=1)
    stats.add_column(ratio=1)
    stats.add_column(ratio=1)
    stats.add_row(
        Text(f"{task_count:,}\nTASKS", style="bold cyan", justify="center"),
        Text(f"{session_count:,}\nFILES", style="bold blue", justify="center"),
        Text(f"{format_size(total_bytes)}\nTOTAL", style="bold magenta", justify="center"),
        Text(
            f"{format_size(largest_bytes)}\n{largest_risk.icon} {largest_risk.label}",
            style=largest_risk.style,
            justify="center",
        ),
    )
    details = Text()
    details.append("Codex home  ", style="dim")
    details.append(str(codex_home))
    details.append("\nMetadata    ", style="dim")
    details.append(database_status)
    return Panel(
        Group(stats, Text(""), details),
        title="[bold bright_cyan]◈ CODEX SESSION GUARD[/]",
        subtitle=f"[dim]{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}[/]",
        border_style="bright_cyan",
        padding=(1, 2),
    )


def make_table(
    rows: Iterable[TaskRecord | SessionFile],
    thresholds: tuple[int, int, int],
    view: ViewMode,
    compact: bool,
) -> Table:
    table = Table(
        title="TASK TOTALS · parent + sub-agents" if view is ViewMode.tasks else "INDIVIDUAL SESSION FILES",
        box=box.ROUNDED,
        border_style="bright_black",
        header_style="bold bright_white",
        show_lines=False,
        expand=True,
        pad_edge=False,
    )
    table.add_column("RISK", width=11, no_wrap=True)
    table.add_column("SIZE", justify="right", width=11, no_wrap=True)
    if not compact:
        table.add_column("LOAD", width=10, no_wrap=True)
    if view is ViewMode.tasks:
        table.add_column("AGT", justify="right", width=5, no_wrap=True)
    table.add_column("STATE", width=9, no_wrap=True)
    table.add_column("UPDATED", width=10, no_wrap=True)
    table.add_column("TITLE", ratio=3, overflow="ellipsis", no_wrap=True)
    if not compact:
        table.add_column("WORKSPACE", ratio=1, overflow="ellipsis", no_wrap=True)
        table.add_column("ID", width=8, no_wrap=True, style="dim")

    for row in rows:
        risk = risk_for(row.size_bytes, thresholds)
        updated = datetime.fromtimestamp(row.updated_at).strftime("%Y-%m-%d") if row.updated_at else "—"
        cells: list[Text | str] = [
            Text(f"{risk.icon} {risk.label}", style=risk.style),
            Text(format_size(row.size_bytes), style=risk.style),
        ]
        if not compact:
            cells.append(make_meter(row.size_bytes, thresholds[2], risk))
        if view is ViewMode.tasks:
            cells.append(str(row.agent_count))
        cells.extend(
            [
                Text("ARCHIVED" if row.archived else "ACTIVE", style="dim" if row.archived else "cyan"),
                updated,
                Text(row.title),
            ]
        )
        if not compact:
            cells.extend(
                [
                    Text(Path(row.workspace).name if row.workspace != "—" else "—", style="dim"),
                    row.thread_id[:8],
                ]
            )
        table.add_row(*cells)
    return table


def make_footer(
    thresholds: tuple[int, int, int],
    counts: dict[str, int],
    log_path: Path | None,
    errors: list[str],
) -> Group:
    legend = Text()
    legend.append("THRESHOLDS  ", style="bold")
    legend.append(f"◆ watch {format_size(thresholds[0])}", style=RISKS[1].style)
    legend.append("   ")
    legend.append(f"▲ warning {format_size(thresholds[1])}", style=RISKS[2].style)
    legend.append("   ")
    legend.append(f"✖ critical {format_size(thresholds[2])}", style=RISKS[3].style)
    legend.append("\nRESULT      ", style="bold")
    legend.append(
        f"{counts['safe']} safe  ·  {counts['watch']} watch  ·  "
        f"{counts['warning']} warning  ·  {counts['critical']} critical"
    )
    if log_path:
        legend.append("\nPLAIN LOG   ", style="bold")
        legend.append(str(log_path), style="cyan")

    panels: list[Panel] = [Panel(legend, border_style="bright_black", padding=(0, 1))]
    if errors:
        error_text = Text("\n".join(f"• {error}" for error in errors[:8]), style="yellow")
        if len(errors) > 8:
            error_text.append(f"\n• … and {len(errors) - 8} more", style="yellow")
        panels.append(Panel(error_text, title="Scan notes", border_style="yellow"))
    return Group(*panels)


def serialize_row(row: TaskRecord | SessionFile, thresholds: tuple[int, int, int]) -> dict[str, object]:
    payload = asdict(row)
    payload["path"] = str(payload["path"]) if "path" in payload else None
    payload["size"] = format_size(row.size_bytes)
    payload["risk"] = risk_for(row.size_bytes, thresholds).label.lower()
    payload["updated_at_iso"] = (
        datetime.fromtimestamp(row.updated_at).astimezone().isoformat() if row.updated_at else None
    )
    return payload


@app.command(context_settings={"help_option_names": ["-h", "--help"]})
def main(
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", help="Codex data directory. Defaults to $CODEX_HOME or ~/.codex."),
    ] = None,
    thresholds: Annotated[
        str,
        typer.Option("--thresholds", help="Watch, warning, and critical thresholds in decimal GB."),
    ] = "10,15,20",
    top: Annotated[int, typer.Option("--top", "-n", min=1, help="Maximum rows to display.")] = 20,
    min_size: Annotated[
        float,
        typer.Option("--min-size", min=0, help="Only display rows at or above this size in GB."),
    ] = 0.0,
    view: Annotated[
        ViewMode,
        typer.Option("--view", help="Show aggregate task totals or individual JSONL files."),
    ] = ViewMode.tasks,
    include_archived: Annotated[
        bool,
        typer.Option("--include-archived/--active-only", help="Include archived Codex tasks."),
    ] = True,
    log: Annotated[
        bool,
        typer.Option("--log/--no-log", help="Write an ANSI-free plain-text log beside the script."),
    ] = True,
    log_file: Annotated[
        Path | None,
        typer.Option("--log-file", help="Custom plain-text log path."),
    ] = None,
    color: Annotated[
        ColorMode,
        typer.Option("--color", help="Terminal color mode."),
    ] = ColorMode.auto,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON instead of the Rich report."),
    ] = False,
    version: Annotated[
        bool | None,
        typer.Option("--version", is_eager=True, help="Show version and exit."),
    ] = None,
) -> None:
    """Scan local Codex session sizes without reading conversation contents."""
    if version:
        typer.echo(f"{APP_NAME} {VERSION}")
        raise typer.Exit()

    terminal = make_console(color)
    threshold_bytes = parse_thresholds(thresholds)
    home = (codex_home or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser().resolve()
    if not home.is_dir():
        terminal.print(f"[bold red]Codex home not found:[/] {home}")
        raise typer.Exit(2)

    metadata, children, database_status = load_database(home)
    sessions, errors = discover_sessions(home, metadata, include_archived)
    tasks = aggregate_tasks(sessions, children)
    all_rows: list[TaskRecord | SessionFile] = list(tasks if view is ViewMode.tasks else sessions)
    visible_rows = [row for row in all_rows if row.size_bytes >= min_size * DECIMAL_GB][:top]

    largest_bytes = max((row.size_bytes for row in all_rows), default=0)
    largest_risk = risk_for(largest_bytes, threshold_bytes)
    counts = {risk.label.lower(): 0 for risk in RISKS}
    for row in all_rows:
        counts[risk_for(row.size_bytes, threshold_bytes).label.lower()] += 1

    proposed_log_path = (log_file or Path(__file__).with_suffix(".log")) if log else None
    try:
        selected_log_path = (
            validate_log_destination(proposed_log_path, home)
            if proposed_log_path is not None
            else None
        )
    except UnsafeLogDestinationError as exc:
        typer.echo(f"Unsafe log destination: {exc}", err=True)
        raise typer.Exit(2) from exc
    run_time = datetime.now().astimezone().isoformat()

    if json_output:
        result = {
            "app": APP_NAME,
            "version": VERSION,
            "scanned_at": run_time,
            "codex_home": str(home),
            "view": view.value,
            "thresholds_gb": [value / DECIMAL_GB for value in threshold_bytes],
            "summary": {
                "tasks": len(tasks),
                "files": len(sessions),
                "total_bytes": sum(session.size_bytes for session in sessions),
                "largest_bytes": largest_bytes,
                "highest_risk": largest_risk.label.lower(),
                "counts": counts,
            },
            "rows": [serialize_row(row, threshold_bytes) for row in visible_rows],
            "notes": errors,
            "exit_code": largest_risk.exit_code,
        }
        output = json.dumps(result, ensure_ascii=False, indent=2)
        typer.echo(output)
        if selected_log_path:
            try:
                selected_log_path.parent.mkdir(parents=True, exist_ok=True)
                with selected_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(output + "\n")
            except OSError as exc:
                terminal.print(f"[yellow]Could not write log:[/] {exc}", stderr=True)
                raise typer.Exit(2) from exc
        raise typer.Exit(largest_risk.exit_code)

    header = make_header(
        home,
        database_status,
        len(sessions),
        len(tasks),
        sum(session.size_bytes for session in sessions),
        largest_bytes,
        largest_risk,
    )
    compact_table = terminal.width < 110
    table = make_table(visible_rows, threshold_bytes, view, compact=compact_table)
    if not visible_rows:
        empty_cells = ["—", "—"]
        if not compact_table:
            empty_cells.append("—")
        if view is ViewMode.tasks:
            empty_cells.append("—")
        empty_cells.extend(["—", "—", "No matching sessions"])
        if not compact_table:
            empty_cells.extend(["—", "—"])
        table.add_row(*empty_cells)
    footer = make_footer(threshold_bytes, counts, selected_log_path, errors)
    report = Group(header, Text(""), table, Text(""), footer)
    terminal.print(report)

    if selected_log_path:
        try:
            selected_log_path.parent.mkdir(parents=True, exist_ok=True)
            with selected_log_path.open("a", encoding="utf-8") as handle:
                log_console = make_console(ColorMode.never, handle)
                log_console.rule(f"{APP_NAME} · {run_time}")
                log_console.print(report)
                log_console.print()
        except OSError as exc:
            terminal.print(f"[yellow]Could not write log:[/] {exc}", stderr=True)
            raise typer.Exit(2) from exc

    raise typer.Exit(largest_risk.exit_code)


if __name__ == "__main__":
    app()
