#!/usr/bin/env python3
"""Derived SQLite/FTS index for Meeting Recorder run bundles.

Run directories are the source of truth.  This module deliberately owns no
lifecycle state and can always reconstruct ``index.db`` from those files.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterator


INDEX_SCHEMA_VERSION = 2
INDEX_FILENAME = "index.db"
FINALIZATION_EVENTS = {
    "recording_finalized",
    "finalization_failed",
    "finalized_media_invalid",
    "capture_empty",
}


class IndexUnavailable(RuntimeError):
    """The local SQLite does not provide the required FTS capability."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                try:
                    value = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    break
                if not isinstance(value, dict):
                    break
                records.append(value)
    except OSError:
        pass
    return records


def check_fts5_trigram(connection: sqlite3.Connection | None = None) -> None:
    """Fail explicitly unless this Python's SQLite supports FTS5 trigram."""
    owns_connection = connection is None
    database = connection or sqlite3.connect(":memory:")
    try:
        database.execute(
            "CREATE VIRTUAL TABLE temp.__mtg_trigram_check "
            "USING fts5(content, tokenize='trigram')"
        )
        database.execute("INSERT INTO temp.__mtg_trigram_check(content) VALUES (?)", ("日本語検索",))
        matched = database.execute(
            "SELECT count(*) FROM temp.__mtg_trigram_check WHERE content MATCH ?", ("日本語",)
        ).fetchone()[0]
        if matched != 1:
            raise sqlite3.OperationalError("trigram MATCH self-test failed")
        database.execute("DROP TABLE temp.__mtg_trigram_check")
    except sqlite3.Error as error:
        raise IndexUnavailable("SQLite FTS5 trigram tokenizer is required") from error
    finally:
        if owns_connection:
            database.close()


def _create_schema(connection: sqlite3.Connection) -> None:
    check_fts5_trigram(connection)
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=FULL;
        CREATE TABLE metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            run_path TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            duration_seconds REAL,
            state TEXT NOT NULL,
            title TEXT NOT NULL,
            disposition TEXT,
            confidence REAL,
            reason TEXT,
            transcript_path TEXT,
            minutes_path TEXT,
            vault_note TEXT
        );
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('transcript', 'minutes')),
            content TEXT NOT NULL,
            UNIQUE(run_id, kind)
        );
        CREATE VIRTUAL TABLE documents_fts USING fts5(
            run_id UNINDEXED,
            kind UNINDEXED,
            content,
            tokenize='trigram'
        );
        CREATE INDEX documents_run_id ON documents(run_id);
        """
    )
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES ('index_schema_version', ?)",
        (str(INDEX_SCHEMA_VERSION),),
    )


def _within_run(run_dir: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(run_dir.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _manifest_artifact(run_dir: Path, manifest: dict[str, Any], key: str) -> Path | None:
    artifacts = manifest.get("artifacts")
    value = artifacts.get(key) if isinstance(artifacts, dict) else None
    if isinstance(value, dict):
        value = value.get("path")
    if not isinstance(value, str) or not value:
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = run_dir / relative
    return candidate if _within_run(run_dir, candidate) and candidate.is_file() else None


def _legacy_minutes(run_dir: Path) -> Path | None:
    direct = run_dir / "minutes.md"
    if direct.is_file():
        return direct
    candidates = [path for path in (run_dir / "minutes").glob("*/minutes.md") if path.is_file()]
    if not candidates:
        return None
    # Migration fallback only. New runs must record the canonical path in manifest.
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, str(path)))


def artifact_paths(run_dir: Path, manifest: dict[str, Any]) -> tuple[Path | None, Path | None]:
    transcript = _manifest_artifact(run_dir, manifest, "transcript")
    if transcript is None:
        transcript = next(
            (path for path in (run_dir / "transcript/transcript.json", run_dir / "chunks/transcript.json") if path.is_file()),
            None,
        )
    minutes = _manifest_artifact(run_dir, manifest, "minutes") or _legacy_minutes(run_dir)
    return transcript, minutes


def _transcript_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if isinstance(value, dict):
        segments = value.get("segments")
        if isinstance(segments, list):
            return "\n".join(
                text.strip()
                for segment in segments
                if isinstance(segment, dict)
                for text in [segment.get("text")]
                if isinstance(text, str) and text.strip()
            )
        text = value.get("text")
        return text if isinstance(text, str) else ""
    return ""


def _minutes_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _latest_finalization_event(run_dir: Path) -> dict[str, Any] | None:
    records = read_jsonl(run_dir / "events.jsonl")
    return next((record for record in reversed(records) if record.get("event") in FINALIZATION_EVENTS), None)


def is_active_run(run_dir: Path) -> bool:
    """Conservatively treat a run without a durable finalization event as active."""
    return _latest_finalization_event(run_dir) is None


def _run_started_at(run_dir: Path, manifest: dict[str, Any]) -> str | None:
    for key in ("started_at", "start_time"):
        value = manifest.get(key)
        if isinstance(value, str):
            return value
    try:
        parsed = datetime.strptime(run_dir.name, "%Y%m%d-%H%M%S").astimezone()
    except ValueError:
        return None
    return parsed.isoformat()


def _run_state(run_dir: Path, manifest: dict[str, Any], finalization: dict[str, Any]) -> str:
    state = manifest.get("state")
    if isinstance(state, str) and state:
        return state
    return "finalized" if finalization.get("event") == "recording_finalized" else str(finalization.get("event"))


def _title(manifest: dict[str, Any], minutes_text: str, run_id: str) -> str:
    value = manifest.get("title")
    if isinstance(value, str) and value.strip():
        return value.strip()
    for line in minutes_text.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    return run_id


def load_run(run_dir: Path) -> tuple[dict[str, Any], list[tuple[str, str]]] | None:
    if not run_dir.is_dir():
        return None
    finalization = _latest_finalization_event(run_dir)
    if finalization is None:
        return None
    manifest = read_json(run_dir / "manifest.json")
    transcript_path, minutes_path = artifact_paths(run_dir, manifest)
    transcript_text = _transcript_text(transcript_path)
    minutes_text = _minutes_text(minutes_path)
    completed_at = manifest.get("completed_at")
    if not isinstance(completed_at, str):
        occurred = finalization.get("occurred_at") or finalization.get("timestamp")
        completed_at = occurred if isinstance(occurred, str) else None
    duration = manifest.get("duration_seconds")
    if not isinstance(duration, (int, float)):
        duration = None
    vault_note = manifest.get("vault_note")
    if not isinstance(vault_note, str):
        vault_note = None
    disposition = manifest.get("disposition")
    if disposition not in {"keep", "test"}:
        disposition = None
    confidence = manifest.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = None
    reason = manifest.get("reason")
    if not isinstance(reason, str):
        reason = None
    metadata = {
        "run_id": run_dir.name,
        "run_path": str(run_dir.resolve()),
        "started_at": _run_started_at(run_dir, manifest),
        "completed_at": completed_at,
        "duration_seconds": duration,
        "state": _run_state(run_dir, manifest, finalization),
        "title": _title(manifest, minutes_text, run_dir.name),
        "disposition": disposition,
        "confidence": confidence,
        "reason": reason,
        "transcript_path": str(transcript_path.resolve()) if transcript_path else None,
        "minutes_path": str(minutes_path.resolve()) if minutes_path else None,
        "vault_note": vault_note,
    }
    documents = [("transcript", transcript_text), ("minutes", minutes_text)]
    return metadata, [(kind, content) for kind, content in documents if content]


def _insert_run(connection: sqlite3.Connection, run_dir: Path) -> bool:
    loaded = load_run(run_dir)
    if loaded is None:
        return False
    metadata, documents = loaded
    connection.execute(
        """INSERT INTO runs(
               run_id, run_path, started_at, completed_at, duration_seconds, state,
               title, disposition, confidence, reason, transcript_path, minutes_path, vault_note
           ) VALUES (
               :run_id, :run_path, :started_at, :completed_at, :duration_seconds, :state,
               :title, :disposition, :confidence, :reason, :transcript_path, :minutes_path, :vault_note
           )""",
        metadata,
    )
    for kind, content in documents:
        connection.execute(
            "INSERT INTO documents(run_id, kind, content) VALUES (?, ?, ?)",
            (metadata["run_id"], kind, content),
        )
        connection.execute(
            "INSERT INTO documents_fts(run_id, kind, content) VALUES (?, ?, ?)",
            (metadata["run_id"], kind, content),
        )
    return True


def iter_run_directories(base_dir: Path) -> Iterator[Path]:
    if not base_dir.is_dir():
        return
    for path in sorted(base_dir.iterdir()):
        if path.is_dir() and path.name != ".state" and not path.name.startswith("."):
            yield path


def rebuild_index(base_dir: Path) -> dict[str, int]:
    """Build beside the destination, then atomically replace it."""
    base_dir.mkdir(parents=True, exist_ok=True)
    destination = base_dir / INDEX_FILENAME
    descriptor, temporary_name = tempfile.mkstemp(prefix=".index.", suffix=".tmp", dir=base_dir)
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    indexed = 0
    excluded_active = 0
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            _create_schema(connection)
            connection.commit()
            connection.execute("BEGIN")
            for run_dir in iter_run_directories(base_dir):
                if is_active_run(run_dir):
                    excluded_active += 1
                    continue
                if _insert_run(connection, run_dir):
                    indexed += 1
            connection.commit()
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        directory_descriptor = os.open(base_dir, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return {"indexed": indexed, "excluded_active": excluded_active}


def _valid_index(index_path: Path) -> bool:
    try:
        with closing(sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return False
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='index_schema_version'"
            ).fetchone()
            if row != (str(INDEX_SCHEMA_VERSION),):
                return False
            connection.execute("SELECT count(*) FROM documents_fts").fetchone()
            return True
    except sqlite3.Error:
        return False


def ensure_index(base_dir: Path) -> Path:
    check_fts5_trigram()
    index_path = base_dir / INDEX_FILENAME
    if not index_path.is_file() or not _valid_index(index_path):
        rebuild_index(base_dir)
    return index_path


def update_index_for_run(base_dir: Path, run_dir: Path) -> bool:
    """Incremental postprocess hook; errors are left to the caller to isolate/log."""
    base_dir = base_dir.expanduser().resolve()
    run_dir = run_dir.expanduser().resolve()
    try:
        run_dir.relative_to(base_dir)
    except ValueError as error:
        raise ValueError("run directory must be inside base directory") from error
    index_path = ensure_index(base_dir)
    with closing(sqlite3.connect(index_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM documents_fts WHERE run_id = ?", (run_dir.name,))
        connection.execute("DELETE FROM runs WHERE run_id = ?", (run_dir.name,))
        inserted = False if is_active_run(run_dir) else _insert_run(connection, run_dir)
        connection.commit()
    return inserted


def delete_index_run(base_dir: Path, run_id: str) -> None:
    """Remove one run from the derived index in a durable transaction."""
    base_dir = base_dir.expanduser().resolve()
    index_path = ensure_index(base_dir)
    with closing(sqlite3.connect(index_path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM documents_fts WHERE run_id = ?", (run_id,))
        connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        connection.commit()


def connect_index(base_dir: Path) -> sqlite3.Connection:
    path = ensure_index(base_dir)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def literal_like_pattern(query: str) -> str:
    return "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def fts_literal_query(query: str) -> str:
    return '"' + query.replace('"', '""') + '"'
