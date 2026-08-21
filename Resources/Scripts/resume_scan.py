#!/usr/bin/env python3
"""Find resumable Meeting Recorder runs and restart post-processing."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


TERMINAL_EVENTS = {"finalization_failed", "finalized_media_invalid", "capture_empty"}


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


def classify_run(run_dir: Path) -> str:
    lifecycle = read_jsonl(run_dir / "events.jsonl")
    postprocess = read_jsonl(run_dir / "chunks/postprocess.events.jsonl")
    lifecycle_events = [record.get("event") for record in lifecycle]
    postprocess_events = [record.get("event") for record in postprocess]

    finalization_state = next(
        (event for event in reversed(lifecycle_events) if event in TERMINAL_EVENTS | {"recording_finalized"}),
        None,
    )
    if finalization_state in TERMINAL_EVENTS:
        terminal = str(finalization_state)
        return f"terminal:{terminal}"
    if finalization_state != "recording_finalized":
        return "skip:not_finalized"
    postprocess_state = next(
        (event for event in reversed(postprocess_events) if event in {"postprocess_started", "postprocess_completed", "postprocess_failed"}),
        None,
    )
    if postprocess_state is None:
        postprocess_state = next(
            (event for event in reversed(lifecycle_events) if event in {"postprocess_started", "postprocess_completed", "postprocess_failed"}),
            None,
        )
    if postprocess_state == "postprocess_completed":
        return "skip:completed"
    if postprocess_state == "postprocess_failed":
        return "resume:failed"
    return "resume:interrupted"


def notify_terminal(run_dir: Path, state: str) -> None:
    message = f"自動再試行できない録画があります: {run_dir.name} ({state})"
    subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            'on run argv\ndisplay notification (item 1 of argv) with title "Meeting Recorder" subtitle "録画エラー"\nend run',
            message,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def scan(base_dir: Path, runner_script: Path) -> dict[str, str]:
    outcomes: dict[str, str] = {}
    if not base_dir.is_dir():
        return outcomes
    for run_dir in sorted((path for path in base_dir.iterdir() if path.is_dir() and path.name != ".state")):
        classification = classify_run(run_dir)
        outcomes[run_dir.name] = classification
        if classification.startswith("terminal:"):
            notify_terminal(run_dir, classification.removeprefix("terminal:"))
            continue
        if not classification.startswith("resume:"):
            continue
        status = subprocess.run(
            [sys.executable, str(runner_script), "--run-dir", str(run_dir), "--resume"],
            check=False,
        ).returncode
        outcomes[run_dir.name] = "skip:locked" if status == 75 else f"resumed:{status}"
    return outcomes


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", required=True, type=Path)
    parser.add_argument("--runner-script", type=Path, default=Path(__file__).with_name("postprocess_runner.py"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    outcomes = scan(args.base_dir.expanduser().resolve(), args.runner_script.expanduser().resolve())
    for name, outcome in outcomes.items():
        print(f"{name}\t{outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
