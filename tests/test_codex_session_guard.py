#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "rich>=14.2.0,<15",
#   "typer>=0.16.0,<1",
# ]
# ///

from __future__ import annotations

import errno
import hashlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Callable
from unittest.mock import patch

from typer.testing import CliRunner


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "codex" / "codex_session_guard.py"


def load_guard_module() -> ModuleType:
    module_name = "codex_session_guard_under_test"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


guard = load_guard_module()


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT,
            title TEXT,
            cwd TEXT,
            updated_at INTEGER,
            archived INTEGER,
            source TEXT
        );
        CREATE TABLE thread_spawn_edges (
            parent_thread_id TEXT,
            child_thread_id TEXT
        );
        """
    )


def file_snapshot(path: Path) -> tuple[str, int, int]:
    stat = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_size, stat.st_mtime_ns


def link_or_skip(
    test_case: unittest.TestCase,
    operation: Callable[[], None],
    description: str,
) -> None:
    try:
        operation()
    except (AttributeError, NotImplementedError) as exc:
        test_case.skipTest(f"{description} are unsupported: {exc}")
    except OSError as exc:
        unavailable_errors = {
            getattr(errno, "EACCES", -1),
            getattr(errno, "ENOSYS", -1),
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
            getattr(errno, "EPERM", -1),
        }
        if exc.errno in unavailable_errors:
            test_case.skipTest(f"{description} are unavailable: {exc}")
        raise


def make_session(
    thread_id: str,
    path: Path,
    size_bytes: int,
    *,
    updated_at: int = 0,
) -> object:
    return guard.SessionFile(
        thread_id=thread_id,
        path=path,
        size_bytes=size_bytes,
        title=f"Task {thread_id}",
        workspace="/tmp/work",
        updated_at=updated_at,
        archived=False,
        source="app",
    )


class LoadDatabaseTests(unittest.TestCase):
    def test_active_wal_falls_back_without_changing_database_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            thread_id = "11111111-1111-4111-8111-111111111111"
            session_path = codex_home / "sessions" / "2026" / "08" / "17" / f"{thread_id}.jsonl"
            session_path.parent.mkdir(parents=True)
            session_path.write_text('{"type":"event"}\n', encoding="utf-8")

            database = codex_home / "state_5.sqlite"
            writer = sqlite3.connect(database)
            try:
                self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                create_schema(writer)
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute(
                    """
                    INSERT INTO threads
                        (id, rollout_path, title, cwd, updated_at, archived, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (thread_id, str(session_path), "WAL task", "/tmp/work", 123, 0, "cli"),
                )
                writer.commit()

                database_files = (database, Path(f"{database}-wal"), Path(f"{database}-shm"))
                before = {path: file_snapshot(path) for path in database_files}

                metadata, children, status = guard.load_database(codex_home)

                self.assertEqual(metadata, {})
                self.assertEqual(children, {})
                self.assertIn("using filenames only", status)
                self.assertEqual(
                    {path: file_snapshot(path) for path in database_files},
                    before,
                )
                sessions, errors = guard.discover_sessions(
                    codex_home, metadata, include_archived=False
                )
                self.assertEqual(errors, [])
                self.assertEqual(
                    [session.path for session in sessions],
                    [session_path.resolve(strict=True)],
                )
            finally:
                writer.close()

    def test_idle_rollback_journal_loads_metadata_and_children(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            database = codex_home / "state_5.sqlite"
            parent_id = "22222222-2222-4222-8222-222222222222"
            child_id = "33333333-3333-4333-8333-333333333333"
            parent_path = codex_home / "sessions" / f"{parent_id}.jsonl"
            child_path = codex_home / "sessions" / f"{child_id}.jsonl"

            with closing(sqlite3.connect(database)) as writer:
                create_schema(writer)
                writer.executemany(
                    """
                    INSERT INTO threads
                        (id, rollout_path, title, cwd, updated_at, archived, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (parent_id, str(parent_path), "  Parent   task  ", "/work/parent", 456, 0, "app"),
                        (child_id, str(child_path), "Child task", "/work/child", 457, 0, "subagent"),
                    ),
                )
                writer.execute(
                    """
                    INSERT INTO thread_spawn_edges (parent_thread_id, child_thread_id)
                    VALUES (?, ?)
                    """,
                    (parent_id, child_id),
                )
                writer.commit()

            metadata, children, status = guard.load_database(codex_home)

            self.assertEqual(set(metadata), {parent_id, child_id})
            self.assertEqual(metadata[parent_id].title, "Parent task")
            self.assertEqual(metadata[parent_id].rollout_path, parent_path)
            self.assertEqual(children, {parent_id: [child_id]})
            self.assertEqual(status, "loaded 2 task records")

    def test_missing_tables_fall_back_to_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            database = codex_home / "state_5.sqlite"
            with closing(sqlite3.connect(database)) as writer:
                writer.execute("CREATE TABLE unrelated (id INTEGER)")
                writer.commit()

            metadata, children, status = guard.load_database(codex_home)

            self.assertEqual(metadata, {})
            self.assertEqual(children, {})
            self.assertIn("no such table: threads", status)
            self.assertIn("using filenames only", status)


class DiscoverSessionsTests(unittest.TestCase):
    def test_symlinked_rollout_path_is_deduplicated_and_canonicalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            codex_home = temporary_root / "codex-home"
            aliased_home = temporary_root / "codex-home-alias"
            thread_id = "44444444-4444-4444-8444-444444444444"
            session_path = (
                codex_home
                / "sessions"
                / "2026"
                / "08"
                / "17"
                / f"{thread_id}.jsonl"
            )
            session_path.parent.mkdir(parents=True)
            session_path.write_bytes(b"x" * 1_500)
            link_or_skip(
                self,
                lambda: aliased_home.symlink_to(codex_home, target_is_directory=True),
                "symbolic links",
            )

            rollout_path = aliased_home / session_path.relative_to(codex_home)
            metadata = {
                thread_id: guard.ThreadMeta(
                    thread_id=thread_id,
                    rollout_path=rollout_path,
                    title="Aliased task",
                    workspace="/tmp/work",
                    updated_at=123,
                    archived=False,
                    source="app",
                )
            }

            sessions, errors = guard.discover_sessions(
                codex_home, metadata, include_archived=False
            )

            self.assertEqual(errors, [])
            self.assertEqual(
                (len(sessions), sum(session.size_bytes for session in sessions)),
                (1, 1_500),
            )
            self.assertEqual(sessions[0].path, session_path.resolve(strict=True))

    def test_hard_linked_active_and_archived_paths_count_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            thread_id = "55555555-5555-4555-8555-555555555555"
            session_path = codex_home / "sessions" / f"{thread_id}.jsonl"
            archived_path = codex_home / "archived_sessions" / f"{thread_id}.jsonl"
            session_path.parent.mkdir()
            archived_path.parent.mkdir()
            session_path.write_bytes(b"y" * 1_500)
            link_or_skip(
                self,
                lambda: os.link(session_path, archived_path),
                "hard links",
            )

            sessions, errors = guard.discover_sessions(
                codex_home, {}, include_archived=True
            )

            self.assertEqual(errors, [])
            self.assertEqual(
                (len(sessions), sum(session.size_bytes for session in sessions)),
                (1, 1_500),
            )

    def test_partial_inode_identity_falls_back_to_normalized_paths(self) -> None:
        first_path = Path("/synthetic/first.jsonl")
        second_path = Path("/synthetic/second.jsonl")

        first_key = guard._candidate_key(
            first_path, SimpleNamespace(st_dev=0, st_ino=91)
        )
        second_key = guard._candidate_key(
            second_path, SimpleNamespace(st_dev=0, st_ino=91)
        )
        missing_inode_key = guard._candidate_key(
            first_path, SimpleNamespace(st_dev=17, st_ino=0)
        )

        self.assertNotEqual(first_key, second_key)
        self.assertEqual(first_key, ("path", os.path.normcase(str(first_path))))
        self.assertEqual(missing_inode_key, first_key)

    def test_symlink_loop_runtime_error_becomes_inspect_note(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            thread_id = "66666666-6666-4666-8666-666666666666"
            loop_path = codex_home / "loop.jsonl"
            metadata = {
                thread_id: guard.ThreadMeta(
                    thread_id=thread_id,
                    rollout_path=loop_path,
                    title="Loop task",
                    workspace="/tmp/work",
                    updated_at=123,
                    archived=False,
                    source="app",
                )
            }

            with patch.object(Path, "resolve", side_effect=RuntimeError("symlink loop")):
                sessions, errors = guard.discover_sessions(
                    codex_home, metadata, include_archived=False
                )

            self.assertEqual(sessions, [])
            self.assertEqual(
                errors,
                [f"Could not inspect {loop_path}: symlink loop"],
            )

    def test_archived_symlink_keeps_archived_origin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            thread_id = "77777777-7777-4777-8777-777777777777"
            target_path = codex_home / "external" / "canonical-target.jsonl"
            archived_path = codex_home / "archived_sessions" / f"{thread_id}.jsonl"
            target_path.parent.mkdir()
            archived_path.parent.mkdir()
            target_path.write_bytes(b"archive")
            link_or_skip(
                self,
                lambda: archived_path.symlink_to(target_path),
                "symbolic links",
            )

            sessions, errors = guard.discover_sessions(
                codex_home, {}, include_archived=True
            )

            self.assertEqual(errors, [])
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].path, target_path.resolve(strict=True))
            self.assertEqual(sessions[0].thread_id, thread_id)
            self.assertTrue(sessions[0].archived)

    def test_metadata_alias_preserves_authoritative_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            metadata_id = "88888888-8888-4888-8888-888888888888"
            target_id = "99999999-9999-4999-8999-999999999999"
            target_path = codex_home / "external" / f"{target_id}.jsonl"
            alias_path = codex_home / "sessions" / "metadata-alias.jsonl"
            target_path.parent.mkdir()
            alias_path.parent.mkdir()
            target_path.write_bytes(b"metadata")
            link_or_skip(
                self,
                lambda: alias_path.symlink_to(target_path),
                "symbolic links",
            )
            metadata = {
                metadata_id: guard.ThreadMeta(
                    thread_id=metadata_id,
                    rollout_path=alias_path,
                    title="Authoritative title",
                    workspace="/metadata/workspace",
                    updated_at=987654,
                    archived=True,
                    source="subagent",
                )
            }

            sessions, errors = guard.discover_sessions(
                codex_home, metadata, include_archived=True
            )

            self.assertEqual(errors, [])
            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session.path, target_path.resolve(strict=True))
            self.assertEqual(session.thread_id, metadata_id)
            self.assertEqual(session.title, "Authoritative title")
            self.assertEqual(session.workspace, "/metadata/workspace")
            self.assertEqual(session.updated_at, 987654)
            self.assertTrue(session.archived)
            self.assertEqual(session.source, "subagent")

    def test_metadata_zero_timestamp_uses_file_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            thread_id = "99999999-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            session_path = codex_home / "sessions" / f"{thread_id}.jsonl"
            session_path.parent.mkdir()
            session_path.write_bytes(b"timestamp")
            metadata = {
                thread_id: guard.ThreadMeta(
                    thread_id=thread_id,
                    rollout_path=session_path,
                    title="Zero timestamp",
                    workspace="/tmp/work",
                    updated_at=0,
                    archived=False,
                    source="app",
                )
            }
            expected_mtime = int(session_path.stat().st_mtime)

            sessions, errors = guard.discover_sessions(
                codex_home, metadata, include_archived=False
            )

            self.assertEqual(errors, [])
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].updated_at, expected_mtime)

    def test_duplicate_metadata_identity_uses_stable_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            target_path = codex_home / "external" / "physical.jsonl"
            first_path = codex_home / "aliases" / "first.jsonl"
            second_path = codex_home / "aliases" / "second.jsonl"
            target_path.parent.mkdir()
            first_path.parent.mkdir()
            target_path.write_bytes(b"duplicate-metadata")
            link_or_skip(
                self,
                lambda: first_path.symlink_to(target_path),
                "symbolic links",
            )
            link_or_skip(
                self,
                lambda: second_path.symlink_to(target_path),
                "symbolic links",
            )
            first_id = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            second_id = "22222222-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            metadata = {
                second_id: guard.ThreadMeta(
                    second_id,
                    second_path,
                    "Second metadata",
                    "/second",
                    2,
                    False,
                    "app",
                ),
                first_id: guard.ThreadMeta(
                    first_id,
                    first_path,
                    "First metadata",
                    "/first",
                    1,
                    False,
                    "cli",
                ),
            }

            sessions, errors = guard.discover_sessions(
                codex_home, metadata, include_archived=False
            )

            self.assertEqual(errors, [])
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].thread_id, first_id)
            self.assertEqual(sessions[0].title, "First metadata")

    def test_metadata_directory_is_not_a_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            thread_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            rollout_directory = codex_home / "sessions" / "not-a-file"
            rollout_directory.mkdir(parents=True)
            metadata = {
                thread_id: guard.ThreadMeta(
                    thread_id=thread_id,
                    rollout_path=rollout_directory,
                    title="Directory task",
                    workspace="/tmp/work",
                    updated_at=123,
                    archived=False,
                    source="app",
                )
            }

            sessions, errors = guard.discover_sessions(
                codex_home, metadata, include_archived=False
            )

            self.assertEqual(sessions, [])
            self.assertEqual(
                errors,
                [f"Could not inspect {rollout_directory}: not a regular file"],
            )

    def test_active_origin_survives_archived_metadata_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            active_id = "abababab-abab-4bab-8bab-abababababab"
            archived_id = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
            active_path = codex_home / "sessions" / f"{active_id}.jsonl"
            archived_path = codex_home / "archived_sessions" / f"{archived_id}.jsonl"
            active_path.parent.mkdir()
            archived_path.parent.mkdir()
            active_path.write_bytes(b"active")
            link_or_skip(
                self,
                lambda: os.link(active_path, archived_path),
                "hard links",
            )
            metadata = {
                archived_id: guard.ThreadMeta(
                    thread_id=archived_id,
                    rollout_path=archived_path,
                    title="Archived alias",
                    workspace="/tmp/archive",
                    updated_at=123,
                    archived=True,
                    source="app",
                )
            }

            sessions, errors = guard.discover_sessions(
                codex_home, metadata, include_archived=False
            )

            self.assertEqual(errors, [])
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].thread_id, active_id)
            self.assertEqual(sessions[0].path, active_path.resolve(strict=True))
            self.assertFalse(sessions[0].archived)

    def test_active_only_ignores_missing_archived_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            archived_id = "dededede-dede-4ede-8ede-dededededede"
            missing_path = codex_home / "archived_sessions" / f"{archived_id}.jsonl"
            metadata = {
                archived_id: guard.ThreadMeta(
                    thread_id=archived_id,
                    rollout_path=missing_path,
                    title="Missing archive",
                    workspace="/tmp/archive",
                    updated_at=123,
                    archived=True,
                    source="app",
                )
            }

            sessions, errors = guard.discover_sessions(
                codex_home, metadata, include_archived=False
            )

            self.assertEqual(sessions, [])
            self.assertEqual(errors, [])

    def test_active_origin_survives_matching_archived_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            thread_id = "efefefef-efef-4fef-8fef-efefefefefef"
            active_path = codex_home / "sessions" / f"{thread_id}.jsonl"
            archived_path = codex_home / "archived_sessions" / f"{thread_id}.jsonl"
            active_path.parent.mkdir()
            archived_path.parent.mkdir()
            active_path.write_bytes(b"active")
            link_or_skip(
                self,
                lambda: os.link(active_path, archived_path),
                "hard links",
            )
            metadata = {
                thread_id: guard.ThreadMeta(
                    thread_id=thread_id,
                    rollout_path=archived_path,
                    title="Archived alias",
                    workspace="/tmp/archive",
                    updated_at=123,
                    archived=True,
                    source="app",
                )
            }

            sessions, errors = guard.discover_sessions(
                codex_home, metadata, include_archived=False
            )

            self.assertEqual(errors, [])
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].thread_id, thread_id)
            self.assertFalse(sessions[0].archived)

    def test_equal_size_sessions_have_stable_path_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            sessions_directory = codex_home / "sessions"
            sessions_directory.mkdir()
            first_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            second_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            first_path = sessions_directory / f"{first_id}.jsonl"
            second_path = sessions_directory / f"{second_id}.jsonl"
            first_path.write_bytes(b"same")
            second_path.write_bytes(b"same")

            with patch.object(
                Path, "rglob", return_value=iter([second_path, first_path])
            ):
                sessions, errors = guard.discover_sessions(
                    codex_home, {}, include_archived=False
                )

            self.assertEqual(errors, [])
            self.assertEqual(
                [session.path for session in sessions],
                [first_path.resolve(strict=True), second_path.resolve(strict=True)],
            )

    def test_hard_link_representative_prefers_stable_active_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_home:
            codex_home = Path(temporary_home)
            sessions_directory = codex_home / "sessions"
            sessions_directory.mkdir()
            first_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            second_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
            first_path = sessions_directory / f"{first_id}.jsonl"
            second_path = sessions_directory / f"{second_id}.jsonl"
            second_path.write_bytes(b"hard-link")
            link_or_skip(
                self,
                lambda: os.link(second_path, first_path),
                "hard links",
            )

            with patch.object(
                Path, "rglob", return_value=iter([second_path, first_path])
            ):
                sessions, errors = guard.discover_sessions(
                    codex_home, {}, include_archived=False
                )

            self.assertEqual(errors, [])
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].path, first_path.resolve(strict=True))
            self.assertEqual(sessions[0].thread_id, first_id)


class AggregateTasksTests(unittest.TestCase):
    def test_distinct_files_with_same_thread_id_all_contribute(self) -> None:
        thread_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        sessions = [
            make_session(thread_id, Path("/sessions/first.jsonl"), 100, updated_at=10),
            make_session(thread_id, Path("/sessions/second.jsonl"), 200, updated_at=20),
        ]

        records = guard.aggregate_tasks(sessions, {})

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].size_bytes, 300)
        self.assertEqual(records[0].file_count, 2)
        self.assertEqual(records[0].agent_count, 0)
        self.assertEqual(records[0].updated_at, 20)

    def test_two_node_cycle_produces_one_complete_task(self) -> None:
        first_id = "11111111-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        second_id = "22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        sessions = [
            make_session(first_id, Path("/sessions/first.jsonl"), 100, updated_at=10),
            make_session(second_id, Path("/sessions/second.jsonl"), 200, updated_at=20),
        ]

        records = guard.aggregate_tasks(
            sessions,
            {first_id: [second_id], second_id: [first_id]},
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].thread_id, first_id)
        self.assertEqual(records[0].size_bytes, 300)
        self.assertEqual(records[0].file_count, 2)
        self.assertEqual(records[0].agent_count, 1)
        self.assertEqual(records[0].updated_at, 20)

    def test_self_cycle_produces_one_single_file_task(self) -> None:
        thread_id = "33333333-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        sessions = [
            make_session(thread_id, Path("/sessions/self.jsonl"), 125, updated_at=30)
        ]

        records = guard.aggregate_tasks(sessions, {thread_id: [thread_id]})

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].thread_id, thread_id)
        self.assertEqual(records[0].size_bytes, 125)
        self.assertEqual(records[0].file_count, 1)
        self.assertEqual(records[0].agent_count, 0)

    def test_shared_child_is_not_counted_under_two_ordinary_roots(self) -> None:
        first_root = "44444444-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        second_root = "55555555-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        child_id = "66666666-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        sessions = [
            make_session(first_root, Path("/sessions/first-root.jsonl"), 10),
            make_session(second_root, Path("/sessions/second-root.jsonl"), 20),
            make_session(child_id, Path("/sessions/child.jsonl"), 100),
        ]

        records = guard.aggregate_tasks(
            sessions,
            {first_root: [child_id], second_root: [child_id]},
        )

        self.assertEqual(sum(record.size_bytes for record in records), 130)
        self.assertEqual(sum(record.file_count for record in records), 3)
        by_id = {record.thread_id: record for record in records}
        self.assertEqual(by_id[first_root].size_bytes, 110)
        self.assertEqual(by_id[first_root].agent_count, 1)
        self.assertEqual(by_id[second_root].size_bytes, 20)
        self.assertEqual(by_id[second_root].agent_count, 0)


class LogDestinationSafetyTests(unittest.TestCase):
    def invoke_guard(self, codex_home: Path, *arguments: str) -> object:
        return CliRunner().invoke(
            guard.app,
            ["--codex-home", str(codex_home), "--color", "never", *arguments],
        )

    def test_rejects_database_as_log_without_modifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            codex_home = Path(temporary_directory) / "codex-home"
            codex_home.mkdir()
            database = codex_home / "state_5.sqlite"
            with closing(sqlite3.connect(database)) as writer:
                create_schema(writer)
                writer.commit()
            before = file_snapshot(database)

            result = self.invoke_guard(codex_home, "--log-file", str(database))

            self.assertEqual(result.exit_code, 2)
            self.assertIn("Unsafe log destination", result.stderr)
            self.assertEqual(file_snapshot(database), before)

    def test_rejects_discovered_session_as_log_without_modifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            codex_home = Path(temporary_directory) / "codex-home"
            session = (
                codex_home
                / "sessions"
                / "2026"
                / "08"
                / "17"
                / "11111111-2222-4333-8444-555555555555.jsonl"
            )
            session.parent.mkdir(parents=True)
            session.write_text('{"type":"event"}\n', encoding="utf-8")
            before = file_snapshot(session)

            result = self.invoke_guard(codex_home, "--log-file", str(session))

            self.assertEqual(result.exit_code, 2)
            self.assertIn("Unsafe log destination", result.stderr)
            self.assertEqual(file_snapshot(session), before)

    def test_rejects_external_symlink_to_protected_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            codex_home = temporary_root / "codex-home"
            codex_home.mkdir()
            database = codex_home / "state_5.sqlite"
            with closing(sqlite3.connect(database)) as writer:
                create_schema(writer)
                writer.commit()
            alias = temporary_root / "outside.log"
            link_or_skip(self, lambda: alias.symlink_to(database), "symbolic links")
            before = file_snapshot(database)

            result = self.invoke_guard(codex_home, "--log-file", str(alias))

            self.assertEqual(result.exit_code, 2)
            self.assertIn("Unsafe log destination", result.stderr)
            self.assertEqual(file_snapshot(database), before)

    def test_rejects_external_hard_link_to_protected_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            codex_home = temporary_root / "codex-home"
            session = (
                codex_home
                / "sessions"
                / "66666666-7777-4888-8999-aaaaaaaaaaaa.jsonl"
            )
            session.parent.mkdir(parents=True)
            session.write_text('{"type":"event"}\n', encoding="utf-8")
            alias = temporary_root / "outside.log"
            link_or_skip(self, lambda: os.link(session, alias), "hard links")
            before = file_snapshot(session)

            result = self.invoke_guard(codex_home, "--log-file", str(alias))

            self.assertEqual(result.exit_code, 2)
            self.assertIn("Unsafe log destination", result.stderr)
            self.assertEqual(file_snapshot(session), before)

    def test_rejects_new_log_path_inside_codex_home_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            codex_home = Path(temporary_directory) / "codex-home"
            codex_home.mkdir()
            log_path = codex_home / "reports" / "guard.log"

            result = self.invoke_guard(codex_home, "--log-file", str(log_path))

            self.assertEqual(result.exit_code, 2)
            self.assertIn("Unsafe log destination", result.stderr)
            self.assertFalse(log_path.exists())
            self.assertFalse(log_path.parent.exists())

    def test_safe_external_log_path_succeeds_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            codex_home = temporary_root / "codex-home"
            codex_home.mkdir()
            log_path = temporary_root / "reports" / "guard.log"

            result = self.invoke_guard(codex_home, "--log-file", str(log_path))

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertTrue(log_path.is_file())
            self.assertIn(guard.APP_NAME, log_path.read_text(encoding="utf-8"))

    def test_no_log_ignores_unsafe_log_file_without_modifying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            codex_home = Path(temporary_directory) / "codex-home"
            codex_home.mkdir()
            database = codex_home / "state_5.sqlite"
            with closing(sqlite3.connect(database)) as writer:
                create_schema(writer)
                writer.commit()
            before = file_snapshot(database)

            result = self.invoke_guard(
                codex_home, "--no-log", "--log-file", str(database)
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("Unsafe log destination", result.stderr)
            self.assertEqual(file_snapshot(database), before)


class FixturePortabilityTests(unittest.TestCase):
    def test_link_helper_skips_capability_failures_only(self) -> None:
        capability_errors = (
            PermissionError(errno.EACCES, "permission denied"),
            OSError(errno.EPERM, "operation not permitted"),
            OSError(getattr(errno, "ENOTSUP", errno.EINVAL), "unsupported"),
            NotImplementedError("not implemented"),
        )
        for exc in capability_errors:
            with self.subTest(exc=exc):
                with self.assertRaises(unittest.SkipTest):
                    link_or_skip(
                        self,
                        lambda exc=exc: (_ for _ in ()).throw(exc),
                        "test links",
                    )

        with self.assertRaises(FileNotFoundError):
            link_or_skip(
                self,
                lambda: (_ for _ in ()).throw(
                    FileNotFoundError(errno.ENOENT, "missing parent")
                ),
                "test links",
            )


if __name__ == "__main__":
    unittest.main()
