#!/usr/bin/env python3
"""Durable, restartable worker for Meeting Recorder audio chunks."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Iterable
import uuid


READY_EVENTS = {"chunk_ready", "ready"}
ATTEMPT_EVENTS = {"chunk_transcription_attempt", "transcription_attempt"}
SUCCESS_EVENTS = {"chunk_transcription_succeeded", "transcription_succeeded", "success"}
FAILED_ATTEMPT_EVENTS = {
    "chunk_transcription_attempt_failed",
    "transcription_attempt_failed",
}
TERMINAL_FAILURE_EVENTS = {"chunk_transcription_failed", "transcription_failed", "failed"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read complete JSONL records, stopping at the first malformed line.

    Recorder and worker records are each emitted with one write. A malformed
    final line therefore represents a torn tail; anything after it is not
    considered durable history.
    """
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


def chunk_key(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"invalid chunk id: {value!r}")
    key = str(value)
    if not key:
        raise ValueError("chunk id must not be empty")
    return key


def chunk_sort_key(value: str) -> tuple[int, int | str]:
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


def event_chunk_id(record: dict[str, Any]) -> str:
    return chunk_key(record.get("chunk_id", record.get("id")))


def fold_recorder_events(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    ready: dict[str, dict[str, Any]] = {}
    gaps: list[dict[str, Any]] = []
    session_offset: dict[str, Any] | None = None
    for record in records:
        event = record.get("event")
        if event in READY_EVENTS:
            try:
                key = event_chunk_id(record)
                start_abs = float(record["start_abs"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(start_abs):
                continue
            normalized = dict(record)
            normalized["chunk_id"] = key
            normalized["start_abs"] = start_abs
            ready[key] = normalized
        elif event in {"chunk_gap", "chunk_drop_gap", "drop_gap", "audio_gap"}:
            gaps.append(dict(record))
        elif event in {"session_offset", "chunk_session_offset"}:
            session_offset = dict(record)
    return {"ready": ready, "gaps": gaps, "session_offset": session_offset}


def fold_worker_events(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for record in records:
        event = record.get("event")
        if event not in ATTEMPT_EVENTS | SUCCESS_EVENTS | FAILED_ATTEMPT_EVENTS | TERMINAL_FAILURE_EVENTS:
            continue
        try:
            key = event_chunk_id(record)
        except ValueError:
            continue
        chunk = state.setdefault(key, {"attempts": 0, "status": "pending"})
        attempt = record.get("attempt")
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt >= 0:
            chunk["attempts"] = max(chunk["attempts"], attempt)
        if event in SUCCESS_EVENTS:
            chunk["status"] = "succeeded"
            chunk["record"] = dict(record)
        elif event in TERMINAL_FAILURE_EVENTS:
            chunk["status"] = "failed"
            chunk["record"] = dict(record)
        elif event in ATTEMPT_EVENTS and chunk["status"] != "succeeded":
            chunk["status"] = "attempting"
        elif event in FAILED_ATTEMPT_EVENTS and chunk["status"] != "succeeded":
            chunk["status"] = "pending"
    return state


def append_event(path: Path, event: str, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "meeting-recorder.worker-event.v1",
        "event": event,
        "event_id": str(uuid.uuid4()),
        "occurred_at_unix": time.time(),
        "pid": os.getpid(),
        **fields,
    }
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError(f"short event write: {written} of {len(payload)} bytes")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_sentinel(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    fsync_directory(path.parent)


def valid_transcript(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        return False
    for segment in payload["segments"]:
        if not isinstance(segment, dict):
            return False
        start = segment.get("start")
        end = segment.get("end")
        if (
            isinstance(start, bool)
            or not isinstance(start, (int, float))
            or isinstance(end, bool)
            or not isinstance(end, (int, float))
            or not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end < start
            or not isinstance(segment.get("text"), str)
        ):
            return False
    return True


def chunk_audio_path(run_dir: Path, ready: dict[str, Any], key: str) -> Path:
    raw_path = ready.get("path", ready.get("audio_path"))
    if isinstance(raw_path, str) and raw_path:
        path = Path(raw_path)
        return path if path.is_absolute() else run_dir / path
    try:
        filename = f"chunk_{int(key):04d}.wav"
    except ValueError:
        filename = f"chunk_{key}.wav"
    return run_dir / "audio-chunks" / filename


def chunk_transcript_path(chunks_dir: Path, key: str) -> Path:
    try:
        filename = f"chunk_{int(key):04d}.transcript.json"
    except ValueError:
        filename = f"chunk_{key}.transcript.json"
    return chunks_dir / filename


def combined_transcript(
    recorder_state: dict[str, Any], worker_state: dict[str, dict[str, Any]], chunks_dir: Path
) -> dict[str, list[dict[str, Any]]]:
    combined: list[dict[str, Any]] = []
    for key in sorted(recorder_state["ready"], key=chunk_sort_key):
        if worker_state.get(key, {}).get("status") != "succeeded":
            raise ValueError(f"chunk {key} has not succeeded")
        transcript_path = chunk_transcript_path(chunks_dir, key)
        try:
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid transcript for chunk {key}: {error}") from error
        segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(segments, list):
            raise ValueError(f"invalid transcript segments for chunk {key}")
        offset = recorder_state["ready"][key]["start_abs"]
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                raise ValueError(f"chunk {key} segment {index} is not an object")
            start = segment.get("start")
            end = segment.get("end")
            text = segment.get("text")
            if (
                isinstance(start, bool)
                or not isinstance(start, (int, float))
                or isinstance(end, bool)
                or not isinstance(end, (int, float))
                or not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end < start
                or not isinstance(text, str)
            ):
                raise ValueError(f"chunk {key} segment {index} is invalid")
            combined.append({"start": offset + float(start), "end": offset + float(end), "text": text})
    return {"segments": combined}


def assemble(run_dir: Path, output: Path | None = None) -> Path:
    chunks_dir = run_dir / "chunks"
    recorder_state = fold_recorder_events(read_jsonl(chunks_dir / "recorder.events.jsonl"))
    worker_state = fold_worker_events(read_jsonl(chunks_dir / "worker.events.jsonl"))
    destination = output or chunks_dir / "transcript.json"
    atomic_json(destination, combined_transcript(recorder_state, worker_state, chunks_dir))
    return destination


def transcribe_chunk(
    run_dir: Path,
    chunks_dir: Path,
    ready: dict[str, Any],
    key: str,
    transcribe_script: Path,
    timeout: float,
) -> tuple[bool, str]:
    audio_path = chunk_audio_path(run_dir, ready, key)
    final_path = chunk_transcript_path(chunks_dir, key)
    temporary = chunks_dir / f".{final_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        result = subprocess.run(
            [sys.executable, str(transcribe_script), str(audio_path), str(temporary)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return False, f"exit {result.returncode}: {detail}"[:2000]
        if not valid_transcript(temporary):
            return False, "transcriber produced invalid whisper-compatible JSON"
        os.replace(temporary, final_path)
        descriptor = os.open(final_path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        fsync_directory(chunks_dir)
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:g} seconds"
    except OSError as error:
        return False, str(error)
    finally:
        temporary.unlink(missing_ok=True)


def run_worker(
    run_dir: Path,
    transcribe_script: Path,
    timeout: float,
    max_attempts: int,
    backoff_base: float,
    poll_interval: float,
    retry_failed: bool = False,
) -> int:
    chunks_dir = run_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = chunks_dir / "worker.lock"
    lock_handle = lock_path.open("a+b")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75

        event_path = chunks_dir / "worker.events.jsonl"
        end_path = chunks_dir / "END"
        done_path = chunks_dir / "WORKER_DONE"
        failed_path = chunks_dir / "WORKER_FAILED"
        initial_recorder_state = fold_recorder_events(read_jsonl(chunks_dir / "recorder.events.jsonl"))
        initial_worker_state = fold_worker_events(read_jsonl(event_path))
        if done_path.exists() and all(
            initial_worker_state.get(key, {}).get("status") == "succeeded"
            and valid_transcript(chunk_transcript_path(chunks_dir, key))
            for key in initial_recorder_state["ready"]
        ):
            assemble(run_dir)
            return 0
        if not retry_failed and failed_path.exists() and any(
            initial_worker_state.get(key, {}).get("status") == "failed"
            for key in initial_recorder_state["ready"]
        ):
            return 1
        done_path.unlink(missing_ok=True)
        failed_path.unlink(missing_ok=True)
        while True:
            recorder_state = fold_recorder_events(read_jsonl(chunks_dir / "recorder.events.jsonl"))
            worker_state = fold_worker_events(read_jsonl(event_path))

            for key in sorted(recorder_state["ready"], key=chunk_sort_key):
                state = worker_state.get(key, {"attempts": 0, "status": "pending"})
                transcript_path = chunk_transcript_path(chunks_dir, key)
                if state["status"] == "succeeded" and valid_transcript(transcript_path):
                    continue
                if not retry_failed and (state["status"] == "failed" or state["attempts"] >= max_attempts):
                    if state["status"] != "failed":
                        append_event(
                            event_path,
                            "chunk_transcription_failed",
                            chunk_id=key,
                            attempt=state["attempts"],
                            error="maximum attempts already exhausted",
                        )
                    continue

                first_attempt = state["attempts"] + 1
                last_attempt = state["attempts"] + max_attempts if retry_failed else max_attempts
                for attempt in range(first_attempt, last_attempt + 1):
                    append_event(event_path, "chunk_transcription_attempt", chunk_id=key, attempt=attempt)
                    succeeded, error = transcribe_chunk(
                        run_dir,
                        chunks_dir,
                        recorder_state["ready"][key],
                        key,
                        transcribe_script,
                        timeout,
                    )
                    if succeeded:
                        append_event(
                            event_path,
                            "chunk_transcription_succeeded",
                            chunk_id=key,
                            attempt=attempt,
                            transcript_path=str(transcript_path.relative_to(run_dir)),
                        )
                        break
                    append_event(
                        event_path,
                        "chunk_transcription_attempt_failed",
                        chunk_id=key,
                        attempt=attempt,
                        error=error,
                    )
                    if attempt == last_attempt:
                        append_event(
                            event_path,
                            "chunk_transcription_failed",
                            chunk_id=key,
                            attempt=attempt,
                            error=error,
                        )
                    else:
                        time.sleep(backoff_base * (2 ** (attempt - first_attempt)))

            if end_path.exists():
                final_recorder_state = fold_recorder_events(read_jsonl(chunks_dir / "recorder.events.jsonl"))
                final_worker_state = fold_worker_events(read_jsonl(event_path))
                terminal = all(
                    final_worker_state.get(key, {}).get("status") in {"succeeded", "failed"}
                    for key in final_recorder_state["ready"]
                )
                if terminal:
                    all_succeeded = all(
                        final_worker_state.get(key, {}).get("status") == "succeeded"
                        and valid_transcript(chunk_transcript_path(chunks_dir, key))
                        for key in final_recorder_state["ready"]
                    )
                    if all_succeeded:
                        assemble(run_dir)
                    append_event(
                        event_path,
                        "transcription_drained" if all_succeeded else "transcription_failed",
                        all_succeeded=all_succeeded,
                        chunk_count=len(final_recorder_state["ready"]),
                    )
                    if all_succeeded:
                        failed_path.unlink(missing_ok=True)
                        atomic_sentinel(done_path)
                        return 0
                    atomic_sentinel(failed_path)
                    atomic_sentinel(done_path)
                    return 1
            time.sleep(poll_interval)
    finally:
        lock_handle.close()


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative finite number")
    return parsed


def parse_run_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.set_defaults(command="run")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument(
        "--transcribe-script",
        type=Path,
        default=Path(__file__).with_name("kanary_transcribe.py"),
    )
    parser.add_argument("--timeout", type=positive_float, default=300.0)
    parser.add_argument("--max-attempts", type=int, choices=range(1, 4), default=3)
    parser.add_argument("--backoff-base", type=nonnegative_float, default=1.0)
    parser.add_argument("--poll-interval", type=positive_float, default=0.25)
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Start one new bounded attempt cycle for chunks that previously exhausted retries.",
    )
    return parser.parse_args(argv)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "run":
        # Kept for hand-operated compatibility; the app uses --run-dir.
        args.pop(0)
    if not args or args[0] != "assemble":
        return parse_run_args(args)
    parser = argparse.ArgumentParser(description="Build a whisper-compatible transcript")
    parser.set_defaults(command="assemble")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("output", nargs="?", type=Path)
    return parser.parse_args(args[1:])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "assemble":
            assemble(args.run_dir.resolve(), args.output.resolve() if args.output else None)
            return 0
        return run_worker(
            args.run_dir.resolve(),
            args.transcribe_script.resolve(),
            args.timeout,
            args.max_attempts,
            args.backoff_base,
            args.poll_interval,
            args.retry_failed,
        )
    except (OSError, ValueError) as error:
        print(f"transcription worker failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
