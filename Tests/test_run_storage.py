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
MODULE_PATH = ROOT / "Resources/Scripts/run_storage.py"
SPEC = importlib.util.spec_from_file_location("run_storage_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
storage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(storage)


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


class RunStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run = Path(self.temporary.name) / "20260821-120000"
        self.run.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def completed_run(self, completed_at: float = 1_000.0) -> Path:
        write_jsonl(
            self.run / "events.jsonl",
            [
                {"event": "run_started", "run_id": "run-uuid", "occurred_at_unix": 100.0},
                {"event": "recording_finalized", "occurred_at_unix": 500.0},
                # The lifecycle copy must not override the chunks fold.
                {"event": "postprocess_failed", "occurred_at_unix": completed_at + 1},
            ],
        )
        minutes = self.run / "minutes/attempt-2/minutes.md"
        minutes.parent.mkdir(parents=True)
        minutes.write_text("# Canonical title\n\nbody\n", encoding="utf-8")
        write_jsonl(
            self.run / "chunks/postprocess.events.jsonl",
            [
                {"event": "postprocess_started", "occurred_at_unix": completed_at - 1},
                {
                    "event": "postprocess_completed",
                    "occurred_at_unix": completed_at,
                    "canonical_minutes": "minutes/attempt-2/minutes.md",
                },
            ],
        )
        transcript = self.run / "chunks/transcript.json"
        transcript.write_text('{"segments": []}\n', encoding="utf-8")
        return minutes

    def test_manifest_uses_fold_and_separate_schema_version(self) -> None:
        minutes = self.completed_run()
        manifest = storage.generate_manifest(self.run, canonical_minutes=minutes)
        self.assertEqual(manifest["manifest_schema_version"], 1)
        self.assertNotIn("schema_version", manifest)
        self.assertEqual(manifest["state"], "completed")
        self.assertEqual(manifest["run_id"], "run-uuid")
        self.assertEqual(manifest["artifacts"]["minutes"], "minutes/attempt-2/minutes.md")
        self.assertEqual(manifest["artifacts"]["transcript"], "chunks/transcript.json")
        self.assertEqual(manifest["title"], "Canonical title")
        self.assertEqual(manifest["duration_seconds"], 400.0)

    def test_fold_falls_back_to_lifecycle_when_chunk_log_has_no_state(self) -> None:
        write_jsonl(
            self.run / "events.jsonl",
            [
                {"event": "recording_finalized"},
                {"event": "postprocess_completed", "canonical_minutes": "minutes.md"},
            ],
        )
        write_jsonl(self.run / "chunks/postprocess.events.jsonl", [{"event": "manifest_failed"}])
        (self.run / "minutes.md").write_text("# old run\n", encoding="utf-8")
        self.assertEqual(storage.fold_run(self.run)["state"], "completed")

    def test_containment_rejects_parent_and_symlink_escape(self) -> None:
        outside = Path(self.temporary.name) / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        with self.assertRaises(ValueError):
            storage.contained_relative(self.run, Path("../outside.md"))
        link = self.run / "minutes.md"
        link.symlink_to(outside)
        with self.assertRaises(ValueError):
            storage.contained_relative(self.run, link)

    def test_maintenance_repairs_missing_manifest_from_completed_event(self) -> None:
        self.completed_run()
        self.assertFalse((self.run / "manifest.json").exists())
        self.assertEqual(storage.maintain_run(self.run, now=1_001.0), "maintenance:manifest_repaired")
        self.assertEqual(json.loads((self.run / "manifest.json").read_text())["state"], "completed")

    def test_maintenance_repairs_stale_manifest_for_new_canonical_minutes(self) -> None:
        old = self.completed_run()
        storage.generate_manifest(self.run, canonical_minutes=old)
        new = self.run / "minutes/attempt-3/minutes.md"
        new.parent.mkdir(parents=True)
        new.write_text("# newer\n", encoding="utf-8")
        records = storage.read_jsonl(self.run / "chunks/postprocess.events.jsonl")
        records.append(
            {
                "event": "postprocess_completed",
                "occurred_at_unix": 2_000.0,
                "canonical_minutes": "minutes/attempt-3/minutes.md",
            }
        )
        write_jsonl(self.run / "chunks/postprocess.events.jsonl", records)
        self.assertEqual(storage.maintain_run(self.run, now=2_001.0), "maintenance:manifest_repaired")
        manifest = json.loads((self.run / "manifest.json").read_text())
        self.assertEqual(manifest["artifacts"]["minutes"], "minutes/attempt-3/minutes.md")

    def test_retention_deletes_only_exact_regular_media_after_seven_days(self) -> None:
        self.completed_run(completed_at=1_000.0)
        raw = self.run / "raw.mp4"
        legacy = self.run / "meeting.mp4"
        raw.write_bytes(b"raw")
        legacy.write_bytes(b"legacy")
        nearby = self.run / "raw.mp4.backup"
        nearby.write_bytes(b"keep")
        self.assertFalse(storage.cleanup_media(self.run, now=1_000.0 + storage.RETENTION_SECONDS - 1))
        self.assertTrue(raw.exists())
        self.assertTrue(storage.cleanup_media(self.run, now=1_000.0 + storage.RETENTION_SECONDS))
        self.assertFalse(raw.exists())
        self.assertFalse(legacy.exists())
        self.assertTrue(nearby.exists())
        manifest = json.loads((self.run / "manifest.json").read_text())
        self.assertIsNotNone(manifest["media_deleted_at"])
        self.assertEqual(manifest["media"], {"meeting_mp4": False, "raw_mp4": False})
        self.assertFalse(storage.cleanup_media(self.run, now=2_000_000.0))

    def test_retention_requires_per_run_lock(self) -> None:
        import fcntl

        self.completed_run(completed_at=1.0)
        raw = self.run / "raw.mp4"
        raw.write_bytes(b"keep")
        lock_handle = (self.run / "chunks/postprocess.lock").open("a+b")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertFalse(storage.cleanup_media(self.run, now=2_000_000.0))
        finally:
            lock_handle.close()
        self.assertTrue(raw.exists())

    def test_retention_converges_after_partial_unlink_failure(self) -> None:
        self.completed_run(completed_at=1.0)
        raw = self.run / "raw.mp4"
        legacy = self.run / "meeting.mp4"
        raw.write_bytes(b"raw")
        legacy.write_bytes(b"legacy")
        real_unlink = Path.unlink

        def fail_legacy(path: Path, *args: object, **kwargs: object) -> None:
            if path == legacy:
                raise OSError("synthetic unlink failure")
            real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=fail_legacy):
            with self.assertRaises(OSError):
                storage.cleanup_media(self.run, now=2_000_000.0)
        self.assertFalse(raw.exists())
        self.assertTrue(legacy.exists())
        self.assertTrue(storage.cleanup_media(self.run, now=2_000_001.0))
        self.assertFalse(legacy.exists())
        self.assertIsNotNone(json.loads((self.run / "manifest.json").read_text())["media_deleted_at"])

    def test_retention_skips_unfinished_missing_minutes_and_symlink(self) -> None:
        self.completed_run(completed_at=1.0)
        minutes = self.run / "minutes/attempt-2/minutes.md"
        minutes.unlink()
        raw = self.run / "raw.mp4"
        raw.write_bytes(b"keep")
        self.assertFalse(storage.cleanup_media(self.run, now=2_000_000.0))
        self.assertTrue(raw.exists())

        minutes.write_text("# restored\n", encoding="utf-8")
        raw.unlink()
        outside = Path(self.temporary.name) / "outside.mp4"
        outside.write_bytes(b"outside")
        raw.symlink_to(outside)
        self.assertFalse(storage.cleanup_media(self.run, now=2_000_000.0))
        self.assertTrue(raw.is_symlink())
        self.assertTrue(outside.exists())

    def test_retention_skips_failed_run_even_with_minutes(self) -> None:
        self.completed_run(completed_at=1.0)
        write_jsonl(
            self.run / "chunks/postprocess.events.jsonl",
            [{"event": "postprocess_failed", "occurred_at_unix": 2.0, "canonical_minutes": "minutes/attempt-2/minutes.md"}],
        )
        raw = self.run / "raw.mp4"
        raw.write_bytes(b"keep")
        self.assertFalse(storage.cleanup_media(self.run, now=2_000_000.0))
        self.assertTrue(raw.exists())

    def test_migration_retention_started_at_is_preserved_without_fake_completion(self) -> None:
        self.completed_run(completed_at=1_000.0)
        records = storage.read_jsonl(self.run / "chunks/postprocess.events.jsonl")
        records[-1].pop("occurred_at_unix")
        write_jsonl(self.run / "chunks/postprocess.events.jsonl", records)
        raw = self.run / "raw.mp4"
        raw.write_bytes(b"raw")
        storage.generate_manifest(self.run, retention_started_at="1970-01-01T00:33:20+00:00")
        manifest = json.loads((self.run / "manifest.json").read_text())
        self.assertIsNone(manifest["completed_at"])
        self.assertFalse(storage.cleanup_media(self.run, now=2_000.0 + storage.RETENTION_SECONDS - 1))
        self.assertTrue(storage.cleanup_media(self.run, now=2_000.0 + storage.RETENTION_SECONDS))

    def test_migration_retention_epoch_overrides_old_real_completion_time(self) -> None:
        self.completed_run(completed_at=1_000.0)
        raw = self.run / "raw.mp4"
        raw.write_bytes(b"raw")
        storage.generate_manifest(self.run, retention_started_at="1970-01-01T00:33:20+00:00")

        self.assertFalse(storage.cleanup_media(self.run, now=2_000.0 + storage.RETENTION_SECONDS - 1))
        self.assertTrue(raw.exists())
        self.assertTrue(storage.cleanup_media(self.run, now=2_000.0 + storage.RETENTION_SECONDS))

    def test_chunk_cleanup_exact_name_and_keep_flag(self) -> None:
        audio = self.run / "audio-chunks"
        audio.mkdir()
        wav = audio / "chunk_0003.wav"
        wav.write_bytes(b"wav")
        self.assertTrue(storage.cleanup_chunk_wav(self.run, "3"))
        self.assertFalse(wav.exists())

        wav.write_bytes(b"wav")
        with mock.patch.dict(os.environ, {"MEETING_KEEP_CHUNK_WAV": "1"}):
            self.assertFalse(storage.cleanup_chunk_wav(self.run, "3"))
        self.assertTrue(wav.exists())
        odd = audio / "chunk_evil.wav"
        odd.write_bytes(b"odd")
        self.assertFalse(storage.cleanup_chunk_wav(self.run, "evil"))
        self.assertTrue(odd.exists())


if __name__ == "__main__":
    unittest.main()
