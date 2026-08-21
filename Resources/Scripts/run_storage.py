#!/usr/bin/env python3
"""Durable manifest and media-retention helpers for Meeting Recorder runs."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
import uuid


MANIFEST_SCHEMA_VERSION = 1
RETENTION_SECONDS = 7 * 24 * 60 * 60
TERMINAL_EVENTS = {"finalization_failed", "finalized_media_invalid", "capture_empty"}
POSTPROCESS_EVENTS = {"postprocess_started", "postprocess_completed", "postprocess_failed"}
CHUNK_WAV_RE = re.compile(r"chunk_[0-9]{4}\.wav\Z")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        for raw_line in handle:
            try:
                value = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                break
            if not isinstance(value, dict):
                break
            records.append(value)
    return records


def event_timestamp(record: dict[str, Any] | None) -> float | None:
    if not record:
        return None
    value = record.get("occurred_at_unix")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    value = record.get("occurred_at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso_timestamp(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).astimezone().isoformat(timespec="seconds")


def fold_run(run_dir: Path) -> dict[str, Any]:
    """Fold lifecycle and postprocess logs using the established precedence."""
    lifecycle = read_jsonl(run_dir / "events.jsonl")
    postprocess = read_jsonl(run_dir / "chunks/postprocess.events.jsonl")
    finalization = next(
        (record for record in reversed(lifecycle) if record.get("event") in TERMINAL_EVENTS | {"recording_finalized"}),
        None,
    )
    postprocess_record = next(
        (record for record in reversed(postprocess) if record.get("event") in POSTPROCESS_EVENTS),
        None,
    )
    if postprocess_record is None:
        postprocess_record = next(
            (record for record in reversed(lifecycle) if record.get("event") in POSTPROCESS_EVENTS),
            None,
        )
    finalization_event = finalization.get("event") if finalization else None
    postprocess_event = postprocess_record.get("event") if postprocess_record else None
    if finalization_event in TERMINAL_EVENTS:
        state = str(finalization_event)
    elif postprocess_event == "postprocess_completed":
        state = "completed"
    elif postprocess_event == "postprocess_failed":
        state = "postprocess_failed"
    elif finalization_event == "recording_finalized":
        state = "postprocess_interrupted"
    else:
        state = "recording"
    started = next((record for record in lifecycle if record.get("event") == "run_started"), lifecycle[0] if lifecycle else None)
    run_id = started.get("run_id") if started and isinstance(started.get("run_id"), str) else run_dir.name
    return {
        "state": state,
        "run_id": run_id,
        "started": started,
        "finalization": finalization,
        "postprocess": postprocess_record,
    }


def contained_relative(run_dir: Path, path: Path, *, require_file: bool = True) -> str:
    root = run_dir.resolve(strict=True)
    candidate = path if path.is_absolute() else run_dir / path
    resolved = candidate.resolve(strict=require_file)
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"path escapes run directory: {path}")
    if require_file and not resolved.is_file():
        raise ValueError(f"artifact is not a regular file: {path}")
    relative = resolved.relative_to(root)
    if ".." in relative.parts:
        raise ValueError(f"parent traversal is not allowed: {path}")
    return relative.as_posix()


def canonical_minutes_from_fold(run_dir: Path, folded: dict[str, Any] | None = None) -> Path | None:
    record = (folded or fold_run(run_dir)).get("postprocess")
    value = record.get("canonical_minutes") if isinstance(record, dict) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        relative = contained_relative(run_dir, Path(value))
    except (OSError, ValueError):
        return None
    return run_dir / relative


def canonical_minutes(run_dir: Path, folded: dict[str, Any] | None = None) -> Path | None:
    """Use the durable event path, with a manifest fallback for migrated legacy runs."""
    state = folded or fold_run(run_dir)
    canonical = canonical_minutes_from_fold(run_dir, state)
    if canonical is not None or state.get("state") != "completed":
        return canonical
    artifacts = read_manifest(run_dir).get("artifacts")
    value = artifacts.get("minutes") if isinstance(artifacts, dict) else None
    if not isinstance(value, str):
        return None
    try:
        return run_dir / contained_relative(run_dir, Path(value))
    except (OSError, ValueError):
        return None


def read_manifest(run_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _artifact(run_dir: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return contained_relative(run_dir, path)
    except (OSError, ValueError):
        return None


def _title(minutes: Path | None) -> str | None:
    if minutes is None:
        return None
    interpreted = minutes.with_name("interpret_output.json")
    try:
        value = json.loads(interpreted.read_text(encoding="utf-8"))
        sections = value.get("sections") if isinstance(value, dict) else value
        if isinstance(sections, list):
            for section in sections:
                title = section.get("title") if isinstance(section, dict) else None
                if isinstance(title, str) and title.strip():
                    return title.strip()
    except (OSError, json.JSONDecodeError):
        pass
    try:
        for line in minutes.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip() or None
    except OSError:
        pass
    return None


def atomic_json(path: Path, value: object) -> None:
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short manifest write")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def generate_manifest(
    run_dir: Path,
    *,
    canonical_minutes: Path | None = None,
    retention_started_at: str | None = None,
    media_deleted_at: str | None = None,
    vault_note: str | None = None,
) -> dict[str, Any]:
    run_dir = run_dir.resolve(strict=True)
    folded = fold_run(run_dir)
    previous = read_manifest(run_dir)
    minutes = canonical_minutes or canonical_minutes_from_fold(run_dir, folded)
    minutes_relative = _artifact(run_dir, minutes)
    transcript_relative = _artifact(run_dir, run_dir / "chunks/transcript.json")
    images_relative = None
    if minutes is not None:
        images = minutes.parent / "images"
        if images.is_dir():
            images_relative = contained_relative(run_dir, images, require_file=False)
    started_at = event_timestamp(folded["started"])
    completed_at = event_timestamp(folded["postprocess"]) if folded["state"] == "completed" else None
    finalization_at = event_timestamp(folded["finalization"])
    duration_value = folded["finalization"].get("duration_seconds") if folded["finalization"] else None
    if isinstance(duration_value, (int, float)) and not isinstance(duration_value, bool) and math.isfinite(duration_value):
        duration = max(0.0, float(duration_value))
    else:
        duration = max(0.0, finalization_at - started_at) if started_at is not None and finalization_at is not None else None
    raw = run_dir / "raw.mp4"
    legacy = run_dir / "meeting.mp4"
    manifest = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": folded["run_id"],
        "started_at": iso_timestamp(started_at),
        "completed_at": iso_timestamp(completed_at),
        "duration_seconds": duration,
        "state": folded["state"],
        "artifacts": {
            "transcript": transcript_relative,
            "minutes": minutes_relative,
            "images": images_relative,
        },
        "media": {
            "raw_mp4": raw.is_file() and not raw.is_symlink(),
            "meeting_mp4": legacy.is_file() and not legacy.is_symlink(),
        },
        "title": _title(minutes),
        "vault_note": vault_note if vault_note is not None else previous.get("vault_note"),
        "retention_started_at": retention_started_at if retention_started_at is not None else previous.get("retention_started_at"),
        "media_deleted_at": media_deleted_at if media_deleted_at is not None else previous.get("media_deleted_at"),
    }
    if all(key in previous for key in ("title", "disposition", "confidence", "reason")):
        manifest.update({key: previous[key] for key in ("title", "disposition", "confidence", "reason")})
    atomic_json(run_dir / "manifest.json", manifest)
    return manifest


def _safe_media_path(run_dir: Path, name: str) -> Path | None:
    if name not in {"raw.mp4", "meeting.mp4"}:
        return None
    root = run_dir.resolve(strict=True)
    candidate = run_dir / name
    if candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        if candidate.resolve(strict=True).parent != root:
            return None
    except OSError:
        return None
    return candidate


def _cleanup_media_locked(run_dir: Path, *, now: float | None = None) -> bool:
    folded = fold_run(run_dir)
    manifest = read_manifest(run_dir)
    canonical = canonical_minutes(run_dir, folded)
    if folded["state"] != "completed" or canonical is None:
        return False
    completed_at = event_timestamp(folded["postprocess"])
    retention_value = manifest.get("retention_started_at")
    retention_at = None
    if isinstance(retention_value, str):
        try:
            retention_at = datetime.fromisoformat(retention_value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    # Migration explicitly starts a fresh seven-day window. Legacy completion
    # timestamps may be real and much older, but must not override that window.
    threshold = retention_at if retention_at is not None else completed_at
    current = time.time() if now is None else now
    if threshold is None or current - threshold < RETENTION_SECONDS:
        return False
    deleted = False
    for name in ("raw.mp4", "meeting.mp4"):
        candidate = _safe_media_path(run_dir, name)
        if candidate is not None:
            candidate.unlink()
            deleted = True
    if deleted:
        generate_manifest(run_dir, media_deleted_at=iso_timestamp(current))
    return deleted


def cleanup_media(run_dir: Path, *, now: float | None = None) -> bool:
    chunks = run_dir / "chunks"
    chunks.mkdir(exist_ok=True)
    lock_handle = (chunks / "postprocess.lock").open("a+b")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return _cleanup_media_locked(run_dir, now=now)
    finally:
        lock_handle.close()


def repair_manifest(run_dir: Path) -> str:
    """Repair the manifest cache without applying retention."""
    chunks = run_dir / "chunks"
    chunks.mkdir(exist_ok=True)
    lock_handle = (chunks / "postprocess.lock").open("a+b")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "skip:locked"
        folded = fold_run(run_dir)
        canonical_path = canonical_minutes(run_dir, folded)
        if folded["state"] != "completed" or canonical_path is None:
            return "maintenance:ok"
        previous = read_manifest(run_dir)
        canonical = contained_relative(run_dir, canonical_path)
        expected_completed = iso_timestamp(event_timestamp(folded["postprocess"]))
        expected_media = {
            "raw_mp4": _safe_media_path(run_dir, "raw.mp4") is not None,
            "meeting_mp4": _safe_media_path(run_dir, "meeting.mp4") is not None,
        }
        stale = (
            previous.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION
            or previous.get("state") != "completed"
            or previous.get("completed_at") != expected_completed
            or not isinstance(previous.get("artifacts"), dict)
            or previous["artifacts"].get("minutes") != canonical
            or previous.get("media") != expected_media
        )
        if stale:
            generate_manifest(run_dir)
            return "maintenance:manifest_repaired"
        return "maintenance:ok"
    finally:
        lock_handle.close()


def maintain_run(run_dir: Path, *, now: float | None = None) -> str:
    chunks = run_dir / "chunks"
    chunks.mkdir(exist_ok=True)
    lock_handle = (chunks / "postprocess.lock").open("a+b")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "skip:locked"
        folded = fold_run(run_dir)
        repaired = False
        canonical_path = canonical_minutes(run_dir, folded)
        if folded["state"] == "completed" and canonical_path is not None:
            previous = read_manifest(run_dir)
            canonical = contained_relative(run_dir, canonical_path)
            expected_completed = iso_timestamp(event_timestamp(folded["postprocess"]))
            expected_media = {
                "raw_mp4": _safe_media_path(run_dir, "raw.mp4") is not None,
                "meeting_mp4": _safe_media_path(run_dir, "meeting.mp4") is not None,
            }
            if (
                previous.get("manifest_schema_version") != MANIFEST_SCHEMA_VERSION
                or previous.get("state") != "completed"
                or previous.get("completed_at") != expected_completed
                or not isinstance(previous.get("artifacts"), dict)
                or previous["artifacts"].get("minutes") != canonical
                or previous.get("media") != expected_media
            ):
                generate_manifest(run_dir)
                repaired = True
            deleted = _cleanup_media_locked(run_dir, now=now)
            if deleted:
                return "maintenance:media_deleted"
        return "maintenance:manifest_repaired" if repaired else "maintenance:ok"
    finally:
        lock_handle.close()


def cleanup_chunk_wav(run_dir: Path, key: str) -> bool:
    """Delete only the canonical chunk WAV after its success event is durable."""
    if os.environ.get("MEETING_KEEP_CHUNK_WAV") == "1":
        return False
    try:
        filename = f"chunk_{int(key):04d}.wav"
    except ValueError:
        return False
    if not CHUNK_WAV_RE.fullmatch(filename):
        return False
    audio_dir = run_dir / "audio-chunks"
    candidate = audio_dir / filename
    if candidate.is_symlink() or not candidate.is_file():
        return False
    try:
        if candidate.resolve(strict=True).parent != audio_dir.resolve(strict=True):
            return False
    except OSError:
        return False
    candidate.unlink()
    return True
