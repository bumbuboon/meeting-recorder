#!/usr/bin/env python3
"""Seven-day trash-candidate cleanup with Obsidian cancellation support."""

from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any

import mtg_index
import obsidian_publish
import run_storage


TRASH_SECONDS = 7 * 24 * 60 * 60
TOMBSTONE_PREFIX = ".trash-"
TOMBSTONE_SUFFIX = ".deleting"
MARKER_NAME = ".trash-delete-marker.json"
STATUS_RE = re.compile(r"(?m)^status\s*:\s*['\"]?([^'\"\n]+)['\"]?\s*$")


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _vault_root(manifest: dict[str, Any], explicit: Path | None) -> Path | None:
    value: Path | None = None
    if isinstance(manifest.get("vault_root"), str):
        value = Path(manifest["vault_root"])
    elif explicit is not None:
        value = explicit
    if value is None:
        return None
    resolved = value.expanduser().resolve()
    return resolved if resolved.is_dir() else None


def _candidate_note(vault: Path | None, manifest: dict[str, Any]) -> Path | None:
    relative = manifest.get("vault_note")
    if not isinstance(relative, str) or not relative:
        return None
    if vault is None:
        raise ValueError("vault root is unavailable for a manifested vault note")
    run_id = str(manifest.get("run_id") or "")
    existing = obsidian_publish.find_existing(vault / "Meetings", run_id)
    if existing is not None:
        return existing
    note = obsidian_publish.safe_relative(vault, Path(relative))
    if not note.exists():
        return None
    if note.is_symlink() or not note.is_file():
        raise ValueError("vault note is not a regular non-symlink file")
    if obsidian_publish.source_run(note) != run_id or obsidian_publish.created_by(note) != "agent":
        raise ValueError("vault note ownership does not match the run")
    return note


def _note_status(note: Path) -> str | None:
    text = note.read_text(encoding="utf-8")
    if not text.startswith("---\n") or (end := text.find("\n---\n", 4)) < 0:
        return None
    match = STATUS_RE.search(text[4:end])
    return match.group(1).strip() if match else None


def _cancel(manifest_path: Path, manifest: dict[str, Any]) -> None:
    manifest["disposition"] = "keep"
    manifest["disposition_flagged_at"] = None
    manifest["disposition_override"] = "keep"
    run_storage.atomic_json(manifest_path, manifest)


def _remove_vault_artifacts(vault: Path | None, manifest: dict[str, Any], note: Path | None) -> None:
    if note is not None:
        if note.is_symlink() or not note.is_file():
            raise ValueError("vault note changed before deletion")
        if obsidian_publish.source_run(note) != str(manifest.get("run_id") or ""):
            raise ValueError("vault note source_run changed before deletion")
        note.unlink()
        _fsync_directory(note.parent)
    if vault is None:
        return
    run_id = str(manifest.get("run_id") or "")
    if not run_id or Path(run_id).name != run_id:
        raise ValueError("unsafe source run identifier")
    images = obsidian_publish.safe_relative(vault, Path("Meetings/images") / run_id)
    if not images.exists():
        return
    if images.is_symlink() or not images.is_dir():
        raise ValueError("vault images target is not a regular directory")
    shutil.rmtree(images)
    _fsync_directory(images.parent)


def _original_name(tombstone: Path) -> str:
    name = tombstone.name
    if not name.startswith(TOMBSTONE_PREFIX) or not name.endswith(TOMBSTONE_SUFFIX):
        raise ValueError("invalid trash tombstone name")
    original = name[len(TOMBSTONE_PREFIX) : -len(TOMBSTONE_SUFFIX)]
    if not original or Path(original).name != original:
        raise ValueError("invalid original run name")
    return original


def _read_marker(tombstone: Path) -> dict[str, Any]:
    marker_path = tombstone / MARKER_NAME
    if marker_path.is_symlink() or not marker_path.is_file():
        raise ValueError("trash tombstone marker is missing")
    try:
        value = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("trash tombstone marker is invalid") from error
    original = _original_name(tombstone)
    if (
        not isinstance(value, dict)
        or value.get("schema") != "meeting-recorder.trash-tombstone.v1"
        or value.get("original_name") != original
        or not isinstance(value.get("run_id"), str)
        or not isinstance(value.get("external_completed"), bool)
    ):
        raise ValueError("trash tombstone marker does not match its directory")
    return value


def _safe_remove_tombstone(tombstone: Path) -> None:
    base = tombstone.parent.resolve(strict=True)
    if tombstone.is_symlink() or not tombstone.is_dir():
        raise ValueError("trash tombstone is not a directory")
    if tombstone.resolve(strict=True).parent != base:
        raise ValueError("trash tombstone escapes the recordings directory")
    marker = tombstone / MARKER_NAME
    manifest = tombstone / "manifest.json"
    for current, directories, files in os.walk(tombstone, topdown=False):
        current_path = Path(current)
        for filename in files:
            candidate = current_path / filename
            if candidate in {marker, manifest}:
                continue
            candidate.unlink()
        for directory in directories:
            (current_path / directory).rmdir()
    manifest.unlink(missing_ok=True)
    marker.unlink()
    tombstone.rmdir()
    _fsync_directory(base)


def _finish_tombstone(tombstone: Path, *, vault: Path | None) -> str:
    original_name = _original_name(tombstone)
    marker = _read_marker(tombstone)
    manifest = run_storage.read_manifest(tombstone)
    if not manifest:
        if marker["external_completed"]:
            _safe_remove_tombstone(tombstone)
            return "deleted"
        raise ValueError("trash tombstone manifest is missing before external cleanup")
    if str(manifest.get("run_id") or "") != marker["run_id"]:
        raise ValueError("trash tombstone manifest does not match its marker")
    resolved_vault = _vault_root(manifest, vault)
    note = _candidate_note(resolved_vault, manifest)
    if note is not None and _note_status(note) != "trash_candidate":
        _cancel(tombstone / "manifest.json", manifest)
        (tombstone / MARKER_NAME).unlink()
        restored = tombstone.parent / original_name
        if restored.exists():
            raise FileExistsError(f"cannot restore cancelled run: {restored}")
        os.replace(tombstone, restored)
        _fsync_directory(restored.parent)
        mtg_index.update_index_for_run(restored.parent, restored)
        return "cancelled"
    _remove_vault_artifacts(resolved_vault, manifest, note)
    mtg_index.delete_index_run(tombstone.parent, original_name)
    marker["external_completed"] = True
    run_storage.atomic_json(tombstone / MARKER_NAME, marker)
    _safe_remove_tombstone(tombstone)
    return "deleted"


def cleanup_run(run_dir: Path, *, vault: Path | None = None, now: float | None = None) -> str:
    """Delete one eligible run or return a stable non-destructive outcome."""
    if run_dir.is_symlink() or not run_dir.is_dir():
        return "not_eligible"
    base = run_dir.parent.resolve(strict=True)
    if run_dir.resolve(strict=True).parent != base:
        return "not_eligible"
    manifest = run_storage.read_manifest(run_dir)
    flagged_at = _timestamp(manifest.get("disposition_flagged_at"))
    current = time.time() if now is None else now
    if manifest.get("disposition") != "test" or flagged_at is None:
        return "not_eligible"
    if current - flagged_at < TRASH_SECONDS:
        return "waiting"
    if run_storage.fold_run(run_dir).get("state") != "completed":
        return "not_eligible"
    chunks = run_dir / "chunks"
    if chunks.is_symlink():
        return "not_eligible"
    chunks.mkdir(exist_ok=True)
    lock_handle = (chunks / "postprocess.lock").open("a+b")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "locked"
        # Re-read under lock so triage changes cannot race the deletion gate.
        manifest = run_storage.read_manifest(run_dir)
        if manifest.get("disposition") != "test" or _timestamp(manifest.get("disposition_flagged_at")) != flagged_at:
            return "not_eligible"
        resolved_vault = _vault_root(manifest, vault)
        note = _candidate_note(resolved_vault, manifest)
        if note is not None and _note_status(note) != "trash_candidate":
            _cancel(run_dir / "manifest.json", manifest)
            mtg_index.update_index_for_run(base, run_dir)
            return "cancelled"
        tombstone = base / f"{TOMBSTONE_PREFIX}{run_dir.name}{TOMBSTONE_SUFFIX}"
        if tombstone.exists():
            raise FileExistsError(f"trash tombstone already exists: {tombstone}")
        run_storage.atomic_json(
            run_dir / MARKER_NAME,
            {
                "schema": "meeting-recorder.trash-tombstone.v1",
                "original_name": run_dir.name,
                "run_id": str(manifest.get("run_id") or ""),
                "external_completed": False,
            },
        )
        os.replace(run_dir, tombstone)
        _fsync_directory(base)
        return _finish_tombstone(tombstone, vault=resolved_vault)
    finally:
        lock_handle.close()


def cleanup_tombstones(base_dir: Path, *, vault: Path | None = None) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    if not base_dir.is_dir():
        return outcomes
    for tombstone in sorted(base_dir.glob(f"{TOMBSTONE_PREFIX}*{TOMBSTONE_SUFFIX}")):
        try:
            outcomes[tombstone.name] = _finish_tombstone(tombstone, vault=vault)
        except Exception as error:
            outcomes[tombstone.name] = f"failed:{error}"
    return outcomes
