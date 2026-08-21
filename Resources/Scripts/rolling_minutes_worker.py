#!/usr/bin/env python3
"""Refresh disposable meeting-minutes drafts while recording is in progress."""

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
import uuid
from typing import Any, Iterable


SUCCESS_EVENTS = {"chunk_transcription_succeeded", "chunk_succeeded", "transcription_succeeded"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                try:
                    value = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    break
                if isinstance(value, dict):
                    records.append(value)
    except FileNotFoundError:
        pass
    return records


def append_event(path: Path, event: str, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "meeting-recorder.minutes-worker-event.v1",
        "event": event,
        "event_id": str(uuid.uuid4()),
        "occurred_at_unix": time.time(),
        "pid": os.getpid(),
        **fields,
    }
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short event write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def chunk_key(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("invalid chunk id")
    if isinstance(value, int) or isinstance(value, str) and value.strip():
        return str(value).strip()
    raise ValueError("invalid chunk id")


def chunk_sort_key(value: str) -> tuple[int, Any]:
    try:
        return (0, int(value))
    except ValueError:
        return (1, value)


def completed_chunks(chunks_dir: Path) -> dict[str, float]:
    starts: dict[str, float] = {}
    for record in read_jsonl(chunks_dir / "recorder.events.jsonl"):
        if record.get("event") not in {"chunk_ready", "audio_chunk_ready"}:
            continue
        try:
            key = chunk_key(record.get("chunk_id", record.get("id")))
            start = float(record["start_abs"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(start):
            starts[key] = start

    succeeded: set[str] = set()
    for record in read_jsonl(chunks_dir / "worker.events.jsonl"):
        if record.get("event") in SUCCESS_EVENTS:
            try:
                succeeded.add(chunk_key(record.get("chunk_id", record.get("id"))))
            except ValueError:
                continue
    return {key: starts[key] for key in starts.keys() & succeeded}


def transcript_path(chunks_dir: Path, key: str) -> Path:
    try:
        return chunks_dir / f"chunk_{int(key):04d}.transcript.json"
    except ValueError:
        return chunks_dir / f"chunk_{key}.transcript.json"


def segments_for_chunks(chunks_dir: Path, chunks: dict[str, float], keys: Iterable[str]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for key in sorted(keys, key=chunk_sort_key):
        try:
            payload = json.loads(transcript_path(chunks_dir, key).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_segments = payload.get("segments") if isinstance(payload, dict) else None
        if not isinstance(raw_segments, list):
            continue
        offset = chunks[key]
        for raw in raw_segments:
            if not isinstance(raw, dict):
                continue
            try:
                start = float(raw["start"]) + offset
                end = float(raw["end"]) + offset
            except (KeyError, TypeError, ValueError):
                continue
            text = raw.get("text")
            if not math.isfinite(start) or not math.isfinite(end) or end < start or not isinstance(text, str):
                continue
            segment: dict[str, Any] = {"start": start, "end": end, "text": text}
            if isinstance(raw.get("speaker"), str):
                segment["speaker"] = raw["speaker"]
            segments.append(segment)
    return segments


def valid_sections(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    sections = payload.get("sections") if isinstance(payload, dict) else None
    if not isinstance(sections, list) or not sections:
        raise ValueError("interpretation output must contain a non-empty sections array")
    if not all(isinstance(section, dict) for section in sections):
        raise ValueError("each section must be an object")
    return sections


def render_markdown(sections: list[dict[str, Any]]) -> str:
    lines = ["# 議事録（録画中ドラフト）", "", "> 録画中に自動更新される暫定版です。停止後の最終議事録を正としてください。", ""]
    for index, section in enumerate(sections, start=1):
        title = str(section.get("title") or f"セクション {index}").strip()
        lines.extend([f"## {title}", ""])
        summary = str(section.get("summary") or "").strip()
        if summary:
            lines.extend([summary, ""])
        bullets = section.get("bullets")
        if isinstance(bullets, list):
            for bullet in bullets:
                text = str(bullet).strip()
                if text:
                    lines.append(f"- {text}")
            if bullets:
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            data = text.encode("utf-8")
            if os.write(descriptor, data) != len(data):
                raise OSError("short draft write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def refresh_draft(
    run_dir: Path,
    interpret_script: Path,
    completed: dict[str, float],
    pending_keys: set[str],
    timeout: float,
) -> bool:
    chunks_dir = run_dir / "chunks"
    delta = segments_for_chunks(chunks_dir, completed, pending_keys)
    if not delta:
        return False
    cumulative = segments_for_chunks(chunks_dir, completed, completed.keys())
    draft_path = run_dir / "minutes-draft.md"
    previous = draft_path.read_text(encoding="utf-8") if draft_path.exists() else ""
    prompt = {
        "segments": cumulative,
        "new_segments": delta,
        "rolling": True,
        "current_draft": previous,
        "previous_draft": previous,
        "draft_mode": "rolling",
        "instructions": (
            "Update the Japanese meeting-minutes draft using the new transcript segments. "
            "Preserve still-valid content from previous_draft. Return sections JSON only. "
            "This is a disposable rolling draft; do not request or infer video frames."
        ),
    }
    prompt_path = chunks_dir / f".minutes-prompt.{os.getpid()}.{uuid.uuid4().hex}.json"
    output_path = chunks_dir / f".minutes-output.{os.getpid()}.{uuid.uuid4().hex}.json"
    try:
        prompt_path.write_text(json.dumps(prompt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result = subprocess.run(
            [str(interpret_script), str(prompt_path), str(output_path)],
            cwd=run_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            return False
        atomic_text(draft_path, render_markdown(valid_sections(output_path)))
        return True
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return False
    finally:
        prompt_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)


def run_worker(run_dir: Path, interpret_script: Path, interval: float, timeout: float) -> int:
    chunks_dir = run_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    lock_handle = (chunks_dir / "minutes-worker.lock").open("a+b")
    try:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75
        events = chunks_dir / "minutes-worker.events.jsonl"
        append_event(events, "minutes_worker_started", interval_seconds=interval)
        incorporated: set[str] = set()
        next_refresh = time.monotonic() + interval
        while True:
            completed = completed_chunks(chunks_dir)
            pending = set(completed) - incorporated
            stopping = (chunks_dir / "END").exists() and (chunks_dir / "WORKER_DONE").exists()
            now = time.monotonic()
            if pending and (now >= next_refresh or stopping):
                if refresh_draft(run_dir, interpret_script, completed, pending, timeout):
                    incorporated.update(pending)
                    append_event(events, "minutes_draft_updated", chunk_count=len(incorporated))
                else:
                    append_event(events, "minutes_interpretation_skipped", pending_chunk_count=len(pending))
                next_refresh = now + interval
            if stopping:
                append_event(events, "minutes_worker_stopped", pending_chunk_count=len(set(completed) - incorporated))
                return 0
            time.sleep(min(1.0, max(0.01, interval / 20)))
    finally:
        lock_handle.close()


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--interpret-script", type=Path, default=Path(__file__).with_name("interpret_codex.sh"))
    parser.add_argument("--interval", type=positive_float, default=300.0)
    parser.add_argument("--timeout", type=positive_float, default=660.0)
    args = parser.parse_args(argv)
    return run_worker(args.run_dir.expanduser().resolve(), args.interpret_script.expanduser().resolve(), args.interval, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
