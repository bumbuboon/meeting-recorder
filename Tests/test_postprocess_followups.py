#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
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
import postprocess_followups as followups
import run_storage as storage


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


class PostprocessFollowupsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name) / "recordings"
        self.run = self.base / "20260821-154836"
        self.vault = Path(self.temporary.name) / "vault"
        self.vault.mkdir()
        minutes = self.run / "minutes/generated/minutes.md"
        images = minutes.parent / "images"
        images.mkdir(parents=True)
        (images / "frame_0001.jpg").write_bytes(b"image")
        minutes.write_text("# Meeting\n\n## 日本語検索\n\n![frame](images/frame_0001.jpg)\n", encoding="utf-8")
        (minutes.parent / "interpret_output.json").write_text(
            json.dumps({"sections": [{"title": "日本語検索"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        (self.run / "chunks/transcript.json").parent.mkdir(exist_ok=True)
        (self.run / "chunks/transcript.json").write_text(
            json.dumps({"segments": [{"start": 0, "end": 1, "text": "日本語検索の全文"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        write_jsonl(
            self.run / "events.jsonl",
            [
                {"event": "run_started", "occurred_at_unix": 1_776_744_516},
                {"event": "recording_finalized", "occurred_at_unix": 1_776_744_576},
            ],
        )
        write_jsonl(
            self.run / "chunks/postprocess.events.jsonl",
            [
                {
                    "event": "postprocess_completed",
                    "occurred_at_unix": 1_776_744_600,
                    "canonical_minutes": "minutes/generated/minutes.md",
                }
            ],
        )
        storage.generate_manifest(self.run, canonical_minutes=minutes)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_publish_then_index_and_maintenance_is_idempotent(self) -> None:
        with mock.patch.dict(os.environ, {"MEETING_VAULT_DIR": str(self.vault)}):
            outcome = followups.run_followups(self.run)
            again = followups.run_followups(self.run)
        self.assertEqual(outcome, {"publish": "completed", "index": "completed"})
        self.assertEqual(again, {"publish": "skipped", "index": "skipped"})
        manifest = storage.read_manifest(self.run)
        note = self.vault / manifest["vault_note"]
        self.assertTrue(note.is_file())
        self.assertIn(
            f"../images/{self.run.name}/frame_0001.jpg",
            note.read_text(encoding="utf-8"),
        )
        events = storage.read_jsonl(self.run / "chunks/postprocess.events.jsonl")
        self.assertEqual([record["event"] for record in events[-2:]], ["publish_completed", "index_completed"])
        self.assertTrue((self.base / "index.db").is_file())

    def test_publish_failure_is_isolated_and_retried(self) -> None:
        with mock.patch.dict(os.environ, {"MEETING_VAULT_DIR": str(self.vault)}), \
             mock.patch.object(followups.obsidian_publish, "publish", side_effect=OSError("vault offline")):
            failed = followups.run_followups(self.run)
        self.assertEqual(failed, {"publish": "failed", "index": "completed"})
        self.assertEqual(storage.fold_run(self.run)["state"], "completed")
        events = storage.read_jsonl(self.run / "chunks/postprocess.events.jsonl")
        self.assertIn("publish_failed", [record["event"] for record in events])

        with mock.patch.dict(os.environ, {"MEETING_VAULT_DIR": str(self.vault)}):
            retried = followups.run_followups(self.run)
        self.assertEqual(retried["publish"], "completed")
        self.assertEqual(retried["index"], "skipped")

    def test_unset_vault_skips_publish_without_failure(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            outcome = followups.run_followups(self.run)
        self.assertEqual(outcome["publish"], "skipped")
        events = storage.read_jsonl(self.run / "chunks/postprocess.events.jsonl")
        self.assertNotIn("publish_failed", [record["event"] for record in events])

    def test_index_failure_is_warning_only_and_retried(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(followups.mtg_index, "ensure_index", side_effect=OSError("index full")):
            failed = followups.run_followups(self.run)
        self.assertEqual(failed["index"], "failed")
        self.assertEqual(storage.fold_run(self.run)["state"], "completed")
        self.assertIn(
            "index_failed",
            [record["event"] for record in storage.read_jsonl(self.run / "chunks/postprocess.events.jsonl")],
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            retried = followups.run_followups(self.run)
        self.assertEqual(retried["index"], "completed")

if __name__ == "__main__":
    unittest.main()
