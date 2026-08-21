#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "Resources/Scripts"
import sys
sys.path.insert(0, str(SCRIPTS))
import migrate_runs
import run_storage


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


class MigrateRunsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name) / "recordings"
        self.base.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_legacy(self, name: str, *, active: bool = False) -> Path:
        run = self.base / name
        (run / "chunks").mkdir(parents=True)
        write_jsonl(
            run / "events.jsonl",
            ([{"event": "run_started", "occurred_at_unix": 100.0}]
             if active else [
                 {"event": "run_started", "occurred_at_unix": 100.0},
                 {"event": "recording_finalized", "occurred_at_unix": 200.0},
                 {"event": "postprocess_completed"},
             ]),
        )
        (run / "chunks/transcript.json").write_text(
            json.dumps({"segments": [{"start": 0, "end": 1, "text": "移行検索"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        minutes = run / "minutes/legacy/minutes.md"
        minutes.parent.mkdir(parents=True)
        minutes.write_text("# Legacy\n\n## 移行議事録\n", encoding="utf-8")
        (run / "raw.mp4").write_bytes(b"do not delete")
        return run

    def test_manifest_index_and_retention_epoch_without_moving_media(self) -> None:
        legacy = self.make_legacy("20260820-100000")
        active = self.make_legacy("20260821-100000", active=True)
        now = dt.datetime(2026, 8, 21, 20, tzinfo=dt.timezone(dt.timedelta(hours=9)))

        result = migrate_runs.migrate(self.base, now=now)

        self.assertEqual(result["runs"][legacy.name]["status"], "migrated")
        self.assertEqual(result["runs"][active.name]["status"], "excluded_active")
        manifest = run_storage.read_manifest(legacy)
        self.assertEqual(manifest["retention_started_at"], "2026-08-21T20:00:00+09:00")
        self.assertEqual(manifest["artifacts"]["minutes"], "minutes/legacy/minutes.md")
        self.assertTrue((legacy / "raw.mp4").is_file())
        self.assertFalse((active / "manifest.json").exists())
        self.assertTrue((self.base / "index.db").is_file())

        later = dt.datetime(2026, 8, 22, 20, tzinfo=now.tzinfo)
        migrate_runs.migrate(self.base, now=later)
        self.assertEqual(
            run_storage.read_manifest(legacy)["retention_started_at"],
            "2026-08-21T20:00:00+09:00",
        )

    def test_optional_publish_uses_temporary_vault(self) -> None:
        legacy = self.make_legacy("20260820-100000")
        vault = Path(self.temporary.name) / "vault"
        vault.mkdir()

        result = migrate_runs.migrate(
            self.base,
            now=dt.datetime(2026, 8, 21, tzinfo=dt.timezone.utc),
            vault=vault,
        )

        relative = result["runs"][legacy.name]["vault_note"]
        self.assertIsInstance(relative, str)
        self.assertTrue((vault / relative).is_file())
        self.assertTrue((legacy / "raw.mp4").is_file())


if __name__ == "__main__":
    unittest.main()
