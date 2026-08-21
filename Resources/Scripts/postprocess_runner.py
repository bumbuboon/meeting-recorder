#!/usr/bin/env python3
"""Serialize and run Meeting Recorder post-processing under caffeinate."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import transcriber_worker as transcription
import run_storage as storage
import postprocess_followups as followups


def append_event(path: Path, event: str, **fields: object) -> None:
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


def transcript_ready(run_dir: Path) -> bool:
    chunks = run_dir / "chunks"
    transcript = chunks / "transcript.json"
    if (chunks / "WORKER_FAILED").exists() or not (chunks / "WORKER_DONE").exists():
        return False
    if not transcription.valid_transcript(transcript):
        return False
    recorder_state = transcription.fold_recorder_events(
        transcription.read_jsonl(chunks / "recorder.events.jsonl")
    )
    worker_state = transcription.fold_worker_events(
        transcription.read_jsonl(chunks / "worker.events.jsonl")
    )
    return all(
        worker_state.get(key, {}).get("status") == "succeeded"
        and transcription.valid_transcript(transcription.chunk_transcript_path(chunks, key))
        for key in recorder_state["ready"]
    )


def finalized_media_ready(run_dir: Path) -> bool:
    finalization_state = next(
        (
            record.get("event")
            for record in reversed(transcription.read_jsonl(run_dir / "events.jsonl"))
            if record.get("event")
            in {"recording_finalized", "finalization_failed", "finalized_media_invalid", "capture_empty"}
        ),
        None,
    )
    if finalization_state != "recording_finalized":
        return False
    raw = run_dir / "raw.mp4"
    try:
        return raw.is_file() and raw.stat().st_size > 0
    except OSError:
        return False


def resume_transcription(run_dir: Path, worker_script: Path) -> int:
    chunks = run_dir / "chunks"
    if not (chunks / "END").exists():
        return 1
    if transcript_ready(run_dir):
        return 0
    return subprocess.run(
        [sys.executable, str(worker_script), "--run-dir", str(run_dir), "--retry-failed"],
        check=False,
    ).returncode


def run_postprocess(
    run_dir: Path,
    postprocess_script: Path,
    worker_script: Path,
    *,
    resume: bool,
    caffeinate: Path,
) -> int:
    chunks = run_dir / "chunks"
    chunks.mkdir(parents=True, exist_ok=True)
    event_path = chunks / "postprocess.events.jsonl"
    lock_handle = (chunks / "postprocess.lock").open("a+b")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75

        if resume:
            worker_status = resume_transcription(run_dir, worker_script)
            if worker_status == 75:
                return 75
            if worker_status != 0:
                append_event(
                    event_path,
                    "postprocess_failed",
                    stage="transcription_resume",
                    exit_code=worker_status,
                )
                return worker_status

        if not finalized_media_ready(run_dir) or not transcript_ready(run_dir):
            append_event(event_path, "postprocess_failed", stage="prerequisite_gate")
            return 1

        append_event(event_path, "postprocess_started", resume=resume)
        command = [str(caffeinate), "-i", "/bin/bash", str(postprocess_script), str(run_dir)]
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True)
            status = result.returncode
            if isinstance(result.stderr, str) and result.stderr:
                sys.stderr.write(result.stderr)
        except OSError as error:
            append_event(event_path, "postprocess_failed", stage="launch", message=str(error))
            return 70
        if status == 0:
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            try:
                canonical = storage.contained_relative(run_dir, Path(lines[-1]))
            except (IndexError, OSError, ValueError) as error:
                append_event(event_path, "postprocess_failed", stage="canonical_minutes", message=str(error))
                return 1
            append_event(event_path, "postprocess_completed", resume=resume, canonical_minutes=canonical)
            manifest_ready = False
            try:
                storage.generate_manifest(run_dir, canonical_minutes=run_dir / canonical)
                manifest_ready = True
            except (OSError, ValueError) as error:
                append_event(event_path, "manifest_failed", message=str(error))
            if manifest_ready:
                followups.run_followups(run_dir, lock_held=True)
        else:
            append_event(event_path, "postprocess_failed", stage="minutes", exit_code=status)
        return status
    finally:
        lock_handle.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--postprocess-script", type=Path, default=Path(__file__).with_name("meeting_postprocess.sh"))
    parser.add_argument("--worker-script", type=Path, default=Path(__file__).with_name("transcriber_worker.py"))
    parser.add_argument("--caffeinate", type=Path, default=Path("/usr/bin/caffeinate"))
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run_postprocess(
        args.run_dir.expanduser().resolve(),
        args.postprocess_script.expanduser().resolve(),
        args.worker_script.expanduser().resolve(),
        resume=args.resume,
        caffeinate=args.caffeinate.expanduser().resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
