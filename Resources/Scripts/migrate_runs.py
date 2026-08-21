#!/usr/bin/env python3
"""Attach Phase 5 manifests and rebuild the derived index for existing runs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import sys
from typing import Any

import mtg_index
import obsidian_publish
import run_storage as storage


def migrate_run(
    run_dir: Path,
    *,
    retention_started_at: str,
    vault: Path | None = None,
) -> dict[str, Any]:
    if mtg_index.is_active_run(run_dir):
        return {"status": "excluded_active"}
    previous = storage.read_manifest(run_dir)
    _, minutes = mtg_index.artifact_paths(run_dir, previous)
    retention = previous.get("retention_started_at")
    if not isinstance(retention, str) or not retention:
        retention = retention_started_at
    manifest = storage.generate_manifest(
        run_dir,
        canonical_minutes=minutes,
        retention_started_at=retention,
    )
    published = None
    if vault is not None and manifest.get("state") == "completed" and minutes is not None:
        published = obsidian_publish.publish(run_dir, vault, manifest, minutes)
        if published is not None:
            manifest = storage.generate_manifest(
                run_dir,
                canonical_minutes=minutes,
                retention_started_at=retention,
                vault_note=published,
            )
    return {
        "status": "migrated",
        "state": manifest.get("state"),
        "minutes": manifest.get("artifacts", {}).get("minutes"),
        "vault_note": published,
    }


def migrate(
    base_dir: Path,
    *,
    now: dt.datetime | None = None,
    vault: Path | None = None,
) -> dict[str, Any]:
    timestamp = (now or dt.datetime.now().astimezone()).astimezone().isoformat(timespec="seconds")
    outcomes: dict[str, Any] = {}
    for run_dir in mtg_index.iter_run_directories(base_dir):
        try:
            outcomes[run_dir.name] = migrate_run(
                run_dir,
                retention_started_at=timestamp,
                vault=vault,
            )
        except Exception as error:
            outcomes[run_dir.name] = {"status": "failed", "error": str(error)}
    index = mtg_index.rebuild_index(base_dir)
    return {"runs": outcomes, "index": index, "retention_started_at": timestamp}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=Path.home() / "Movies/meeting-recordings")
    parser.add_argument("--publish", action="store_true", help="also publish completed runs to the configured vault")
    parser.add_argument("--vault-dir", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    vault_value = args.vault_dir or (Path(value) if (value := os.environ.get("MEETING_VAULT_DIR")) else None)
    if args.publish and vault_value is None:
        print("migration failed: --publish requires --vault-dir or MEETING_VAULT_DIR", file=sys.stderr)
        return 2
    try:
        result = migrate(
            args.base_dir.expanduser().resolve(),
            vault=vault_value.expanduser().resolve() if args.publish and vault_value is not None else None,
        )
    except Exception as error:
        print(f"migration failed: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        for run_id, outcome in result["runs"].items():
            print(f"{run_id}\t{outcome['status']}")
        print(f"index\t{result['index']['indexed']} indexed, {result['index']['excluded_active']} active excluded")
    return 1 if any(value["status"] == "failed" for value in result["runs"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
