#!/usr/bin/env python3
"""Find resumable Meeting Recorder runs and restart post-processing."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_storage as storage
import postprocess_followups as followups
import trash_cleanup


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
    folded = storage.fold_run(run_dir)
    state = folded["state"]
    if state in TERMINAL_EVENTS:
        terminal = str(state)
        return f"terminal:{terminal}"
    if state == "recording":
        return "skip:not_finalized"
    if state == "completed":
        return "skip:completed"
    if state == "postprocess_failed":
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


def scan(base_dir: Path, runner_script: Path, *, vault: Path | None = None) -> dict[str, str]:
    outcomes = trash_cleanup.cleanup_tombstones(base_dir, vault=vault)
    if not base_dir.is_dir():
        return outcomes
    for run_dir in sorted(
        path for path in base_dir.iterdir()
        if path.is_dir() and path.name != ".state" and not path.name.startswith(".")
    ):
        try:
            trash = trash_cleanup.cleanup_run(run_dir, vault=vault)
        except Exception as error:
            outcomes[run_dir.name] = f"maintenance:trash_failed:{error}"
            continue
        if trash == "deleted":
            outcomes[run_dir.name] = "maintenance:trash_deleted"
            continue
        maintenance = storage.maintain_run(run_dir)
        classification = classify_run(run_dir)
        if classification == "skip:completed":
            followups.run_followups(run_dir)
        outcomes[run_dir.name] = maintenance if maintenance != "maintenance:ok" else classification
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
    parser.add_argument("--vault-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    vault = args.vault_dir or (Path(value) if (value := os.environ.get("MEETING_VAULT_DIR")) else None)
    outcomes = scan(
        args.base_dir.expanduser().resolve(),
        args.runner_script.expanduser().resolve(),
        vault=vault.expanduser().resolve() if vault is not None else None,
    )
    for name, outcome in outcomes.items():
        print(f"{name}\t{outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
