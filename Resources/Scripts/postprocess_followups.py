#!/usr/bin/env python3
"""Failure-isolated Obsidian publish and derived-index stages."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any
import uuid

import mtg_index
import obsidian_publish
import run_storage as storage


def append_event(path: Path, event: str, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "meeting-recorder.postprocess-event.v1",
        "event": event,
        "event_id": str(uuid.uuid4()),
        "occurred_at_unix": time.time(),
        "occurred_at": storage.iso_timestamp(time.time()),
        "pid": os.getpid(),
        **fields,
    }
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short postprocess event write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_is_current(records: list[dict[str, Any]], success_event: str) -> bool:
    last_core = max(
        (index for index, record in enumerate(records) if record.get("event") == "postprocess_completed"),
        default=-1,
    )
    last_success = max(
        (index for index, record in enumerate(records) if record.get("event") == success_event),
        default=-1,
    )
    return last_core >= 0 and last_success > last_core


def _publish_current(vault: Path, manifest: dict[str, Any]) -> bool:
    relative = manifest.get("vault_note")
    run_id = manifest.get("run_id")
    if not isinstance(relative, str) or not isinstance(run_id, str):
        return False
    try:
        note = obsidian_publish.safe_relative(vault, Path(relative))
    except ValueError:
        return False
    return note.is_file() and not note.is_symlink() and obsidian_publish.source_run(note) == run_id


def _execute_locked(run_dir: Path) -> dict[str, str]:
    event_path = run_dir / "chunks/postprocess.events.jsonl"
    records = storage.read_jsonl(event_path)
    folded = storage.fold_run(run_dir)
    canonical = storage.canonical_minutes(run_dir, folded)
    manifest = storage.read_manifest(run_dir)
    if folded["state"] != "completed" or canonical is None or not manifest:
        return {"publish": "skipped", "index": "skipped"}

    outcomes = {"publish": "skipped", "index": "skipped"}
    vault_value = os.environ.get("MEETING_VAULT_DIR")
    if vault_value:
        vault = Path(vault_value).expanduser().resolve()
        if not (_stage_is_current(records, "publish_completed") and _publish_current(vault, manifest)):
            try:
                relative = obsidian_publish.publish(run_dir, vault, manifest, canonical)
                if relative is not None:
                    manifest = storage.generate_manifest(
                        run_dir,
                        canonical_minutes=canonical,
                        vault_note=relative,
                        vault_root=str(vault),
                    )
                    append_event(event_path, "publish_completed", vault_note=relative)
                    outcomes["publish"] = "completed"
            except Exception as error:  # stage boundary: core completion must survive any publish failure
                append_event(event_path, "publish_failed", message=str(error))
                outcomes["publish"] = "failed"

    base_dir = run_dir.parent
    try:
        index_path = mtg_index.ensure_index(base_dir)
        if not _stage_is_current(records, "index_completed"):
            indexed = mtg_index.update_index_for_run(base_dir, run_dir)
            append_event(event_path, "index_completed", indexed=indexed, index=str(index_path))
            outcomes["index"] = "completed"
    except Exception as error:  # derived index is always recoverable; never roll back core
        append_event(event_path, "index_failed", message=str(error))
        outcomes["index"] = "failed"
    return outcomes


def run_followups(run_dir: Path, *, lock_held: bool = False) -> dict[str, str]:
    run_dir = run_dir.expanduser().resolve()
    if lock_held:
        return _execute_locked(run_dir)
    chunks = run_dir / "chunks"
    chunks.mkdir(exist_ok=True)
    lock_handle = (chunks / "postprocess.lock").open("a+b")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"publish": "locked", "index": "locked"}
        return _execute_locked(run_dir)
    finally:
        lock_handle.close()
