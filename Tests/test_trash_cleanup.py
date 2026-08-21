#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "Resources/Scripts"
sys.path.insert(0, str(SCRIPTS))
import mtg_index
import run_storage
import trash_cleanup


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


class TrashCleanupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.base = root / "recordings"
        self.vault = root / "vault"
        self.base.mkdir()
        self.vault.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_run(self, name: str, *, flagged_at: float = 1_000.0, note: bool = True, status: str | None = "trash_candidate") -> Path:
        run = self.base / name
        run.mkdir()
        write_jsonl(
            run / "events.jsonl",
            [
                {"event": "run_started", "run_id": f"source-{name}", "occurred_at_unix": 100.0},
                {"event": "recording_finalized", "occurred_at_unix": 500.0},
            ],
        )
        minutes = run / "minutes/generated/minutes.md"
        minutes.parent.mkdir(parents=True)
        minutes.write_text("# 録音テスト\n\nbody\n", encoding="utf-8")
        transcript = run / "chunks/transcript.json"
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"segments":[{"text":"test"}]}\n', encoding="utf-8")
        (run / "raw.mp4").write_bytes(b"media")
        write_jsonl(
            run / "chunks/postprocess.events.jsonl",
            [{"event": "postprocess_completed", "occurred_at_unix": 600.0, "canonical_minutes": "minutes/generated/minutes.md"}],
        )
        manifest = run_storage.generate_manifest(run, canonical_minutes=minutes)
        run_storage.apply_triage(
            manifest,
            {"title": "録音機能の動作確認", "disposition": "test", "confidence": 0.99, "reason": "録音テストです"},
            now=flagged_at,
        )
        manifest["vault_root"] = str(self.vault)
        manifest["vault_note"] = f"Meetings/2026/{name}.md"
        run_storage.atomic_json(run / "manifest.json", manifest)
        if note:
            note_path = self.vault / manifest["vault_note"]
            note_path.parent.mkdir(parents=True, exist_ok=True)
            fields = ["---", "created_by: agent", f"source_run: source-{name}"]
            if status is not None:
                fields.append(f"status: {status}")
            fields.extend(["---", "", "minutes", ""])
            note_path.write_text("\n".join(fields), encoding="utf-8")
        images = self.vault / "Meetings/images" / f"source-{name}"
        images.mkdir(parents=True)
        (images / "frame_0001.jpg").write_bytes(b"frame")
        return run

    def test_six_days_waits_then_seven_days_deletes_run_vault_and_index(self) -> None:
        run = self.make_run("20260822-100000")
        mtg_index.rebuild_index(self.base)
        threshold = 1_000.0 + trash_cleanup.TRASH_SECONDS

        self.assertEqual(trash_cleanup.cleanup_run(run, now=threshold - 1), "waiting")
        self.assertTrue(run.exists())
        self.assertEqual(trash_cleanup.cleanup_run(run, now=threshold), "deleted")

        self.assertFalse(run.exists())
        self.assertFalse(self.vault.joinpath("Meetings/2026/20260822-100000.md").exists())
        self.assertFalse(self.vault.joinpath("Meetings/images/source-20260822-100000").exists())
        with sqlite3.connect(self.base / "index.db") as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM runs WHERE run_id = ?", (run.name,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM documents_fts WHERE run_id = ?", (run.name,)).fetchone()[0], 0)

    def test_removed_status_cancels_and_updates_manifest_to_keep(self) -> None:
        run = self.make_run("20260822-110000", status=None)

        self.assertEqual(trash_cleanup.cleanup_run(run, now=1_000.0 + trash_cleanup.TRASH_SECONDS), "cancelled")

        manifest = run_storage.read_manifest(run)
        self.assertEqual(manifest["disposition"], "keep")
        self.assertIsNone(manifest["disposition_flagged_at"])
        self.assertEqual(manifest["disposition_override"], "keep")
        self.assertTrue((run / "chunks/transcript.json").exists())

        # A later maintenance regeneration must not resurrect the stale Luna test result.
        minutes = run / manifest["artifacts"]["minutes"]
        minutes.with_name("interpret_output.json").write_text(
            json.dumps(
                {
                    "meeting_title": "録音機能の動作確認",
                    "disposition": "test",
                    "confidence": 0.99,
                    "reason": "録音テストです",
                    "sections": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        regenerated = run_storage.generate_manifest(run, canonical_minutes=minutes)
        self.assertEqual(regenerated["disposition"], "keep")
        self.assertEqual(regenerated["disposition_override"], "keep")
        self.assertIsNone(regenerated["disposition_flagged_at"])

    def test_missing_vault_note_uses_manifest_and_partial_final_delete_converges(self) -> None:
        run = self.make_run("20260822-120000", note=False)
        mtg_index.rebuild_index(self.base)
        real_remove = trash_cleanup._safe_remove_tombstone
        with mock.patch.object(trash_cleanup, "_safe_remove_tombstone", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                trash_cleanup.cleanup_run(run, now=1_000.0 + trash_cleanup.TRASH_SECONDS)

        tombstone = self.base / ".trash-20260822-120000.deleting"
        self.assertTrue(tombstone.exists())
        self.assertFalse(run.exists())
        with mock.patch.object(trash_cleanup, "_safe_remove_tombstone", side_effect=real_remove):
            outcomes = trash_cleanup.cleanup_tombstones(self.base, vault=self.vault)
        self.assertEqual(outcomes[tombstone.name], "deleted")
        self.assertFalse(tombstone.exists())

    def test_unmarked_lookalike_tombstone_is_never_deleted(self) -> None:
        lookalike = self.base / ".trash-unrelated.deleting"
        lookalike.mkdir()
        (lookalike / "important.txt").write_text("keep", encoding="utf-8")

        outcomes = trash_cleanup.cleanup_tombstones(self.base, vault=self.vault)

        self.assertTrue(outcomes[lookalike.name].startswith("failed:"))
        self.assertEqual((lookalike / "important.txt").read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
