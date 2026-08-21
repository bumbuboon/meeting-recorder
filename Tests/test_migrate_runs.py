#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


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

    def test_latest_legacy_minutes_transcript_is_adopted_for_index_and_publish(self) -> None:
        legacy = self.make_legacy("20260820-100000")
        (legacy / "chunks/transcript.json").unlink()
        older = legacy / "minutes/older/transcript.json"
        older.parent.mkdir(parents=True)
        older.write_text(json.dumps({"segments": [{"text": "古い文字起こし"}]}), encoding="utf-8")
        newest = legacy / "minutes/newest/transcript_import.json"
        newest.parent.mkdir(parents=True)
        newest.write_text(json.dumps({"segments": [{"text": "救済された文字起こし"}]}), encoding="utf-8")
        os.utime(older, ns=(1_000, 1_000))
        os.utime(newest, ns=(2_000, 2_000))
        vault = Path(self.temporary.name) / "vault"
        vault.mkdir()

        result = migrate_runs.migrate(self.base, vault=vault)

        outcome = result["runs"][legacy.name]
        self.assertEqual(outcome["status"], "migrated")
        adopted = legacy / "chunks/transcript.json"
        self.assertEqual(json.loads(adopted.read_text()), json.loads(newest.read_text()))
        self.assertEqual(run_storage.read_manifest(legacy)["artifacts"]["transcript"], "chunks/transcript.json")
        note = (vault / outcome["vault_note"]).read_text(encoding="utf-8")
        self.assertIn(adopted.resolve().as_uri(), note)
        with migrate_runs.mtg_index.connect_index(self.base) as connection:
            content = connection.execute(
                "SELECT content FROM documents WHERE run_id = ? AND kind = 'transcript'",
                (legacy.name,),
            ).fetchone()[0]
        self.assertEqual(content, "救済された文字起こし")

    def test_missing_all_transcripts_warns_and_skips_with_success_exit(self) -> None:
        legacy = self.make_legacy("20260820-100000")
        (legacy / "chunks/transcript.json").unlink()

        result = migrate_runs.migrate(self.base)

        self.assertEqual(result["runs"][legacy.name]["status"], "skipped_missing_transcript")
        self.assertIn("warning", result["runs"][legacy.name])
        self.assertFalse((legacy / "manifest.json").exists())
        with mock.patch.object(migrate_runs, "migrate", return_value=result):
            self.assertEqual(migrate_runs.main(["--base-dir", str(self.base)]), 0)


if __name__ == "__main__":
    unittest.main()
