#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "obsidian_publish", ROOT / "Resources/Scripts/obsidian_publish.py"
)
assert SPEC is not None and SPEC.loader is not None
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


class ObsidianPublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.run = root / "20260821-154836"
        self.vault = root / "vault"
        self.minutes_dir = self.run / "minutes/20260821-155150-raw"
        (self.minutes_dir / "images").mkdir(parents=True)
        (self.run / "chunks").mkdir()
        self.vault.mkdir()
        self.minutes = self.minutes_dir / "minutes.md"
        self.minutes.write_text("# raw meeting minutes\n\n## 診療 方針/確認\n\n本文\n", encoding="utf-8")
        (self.minutes_dir / "interpret_output.json").write_text(
            json.dumps({"sections": [{"title": "診療 方針/確認"}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        (self.minutes_dir / "images/frame_0001.jpg").write_bytes(b"old")
        (self.run / "chunks/transcript.json").write_text(
            json.dumps({"segments": [{"start": 0, "end": 1, "text": "診療"}]}),
            encoding="utf-8",
        )
        self.manifest = {
            "run_id": self.run.name,
            "started_at": "2026-08-21T15:48:36+09:00",
            "title": "診療 方針/確認",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_publish_frontmatter_images_and_idempotency(self) -> None:
        relative = publisher.publish(self.run, self.vault, self.manifest, self.minutes)
        self.assertEqual(relative, "Meetings/2026/2026-08-21_診療 方針-確認.md")
        note = self.vault / relative
        text = note.read_text(encoding="utf-8")
        self.assertIn("type: meeting_minutes", text)
        self.assertIn(f"source_run: {self.run.name}", text)
        self.assertIn("updated:", text)
        image = self.vault / "Meetings/images" / self.run.name / "frame_0001.jpg"
        self.assertEqual(image.read_bytes(), b"old")

        (self.minutes_dir / "images/frame_0001.jpg").write_bytes(b"new")
        self.manifest["vault_note"] = relative
        again = publisher.publish(
            self.run,
            self.vault,
            self.manifest,
            self.minutes,
            now=dt.datetime(2026, 8, 21, 18, tzinfo=dt.timezone(dt.timedelta(hours=9))),
        )
        self.assertEqual(again, relative)
        self.assertIn("updated: 2026-08-21T18:00:00+09:00", note.read_text(encoding="utf-8"))
        self.assertEqual(image.read_bytes(), b"new")
        self.assertEqual(len(list((self.vault / "Meetings/2026").glob("*.md"))), 1)

    def test_collision_and_user_note_are_never_overwritten(self) -> None:
        directory = self.vault / "Meetings/2026"
        directory.mkdir(parents=True)
        collision = directory / "2026-08-21_診療 方針-確認.md"
        original = "---\ncreated_by: user\nsource_run: another\n---\n\n手書き\n"
        collision.write_text(original, encoding="utf-8")

        relative = publisher.publish(self.run, self.vault, self.manifest, self.minutes)

        self.assertEqual(relative, "Meetings/2026/2026-08-21_診療 方針-確認-2.md")
        self.assertEqual(collision.read_text(encoding="utf-8"), original)

    def test_user_note_with_matching_source_run_is_still_never_overwritten(self) -> None:
        directory = self.vault / "Meetings/2026"
        directory.mkdir(parents=True)
        user_note = directory / "claimed.md"
        original = f"---\ncreated_by: user\nsource_run: {self.run.name}\n---\n\n手書き\n"
        user_note.write_text(original, encoding="utf-8")

        relative = publisher.publish(self.run, self.vault, self.manifest, self.minutes)

        self.assertNotEqual(relative, "Meetings/2026/claimed.md")
        self.assertEqual(user_note.read_text(encoding="utf-8"), original)

    def test_matching_source_run_wins_over_stale_manifest_path(self) -> None:
        directory = self.vault / "Meetings/2026"
        directory.mkdir(parents=True)
        existing = directory / "renamed.md"
        existing.write_text(
            f"---\ncreated_by: agent\nsource_run: {self.run.name}\n---\nold\n",
            encoding="utf-8",
        )
        self.manifest["vault_note"] = "Meetings/2026/stale.md"

        relative = publisher.publish(self.run, self.vault, self.manifest, self.minutes)

        self.assertEqual(relative, "Meetings/2026/renamed.md")
        self.assertFalse((directory / "stale.md").exists())

    def test_unset_vault_is_successful_skip_and_parent_path_is_rejected(self) -> None:
        self.assertIsNone(publisher.publish(self.run, None, self.manifest, self.minutes))
        self.manifest["vault_note"] = "Meetings/../escape.md"
        with self.assertRaises(ValueError):
            publisher.publish(self.run, self.vault, self.manifest, self.minutes)


if __name__ == "__main__":
    unittest.main()
