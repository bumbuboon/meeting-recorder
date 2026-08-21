#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "Resources/Scripts"
sys.path.insert(0, str(SCRIPTS))
import triage_runs


class TriageRunsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.base = root / "recordings"
        self.vault = root / "vault"
        self.base.mkdir()
        self.vault.mkdir()
        self.fake_log = root / "codex-calls.jsonl"
        self.fake_prompt = root / "codex-prompt.txt"
        self.fake = root / "fake-codex"
        self.fake.write_text(
            """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

args = sys.argv[1:]
prompt = sys.stdin.read()
Path(os.environ["FAKE_CODEX_PROMPT"]).write_text(prompt, encoding="utf-8")
log = Path(os.environ["FAKE_CODEX_LOG"])
with log.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
if os.environ.get("FAKE_CODEX_TIMEOUT") == "1":
    time.sleep(1)
output = Path(args[args.index("-o") + 1])
calls = len(log.read_text(encoding="utf-8").splitlines())
value = (
    {"title": "会議", "disposition": "keep", "confidence": 0.7, "reason": "不十分"}
    if calls == 1
    else {"title": "新薬導入方針の検討", "disposition": "keep", "confidence": 0.93, "reason": "導入条件と担当分担を具体的に相談しています"}
)
output.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
""",
            encoding="utf-8",
        )
        self.fake.chmod(0o755)
        self.old_env = os.environ.get("FAKE_CODEX_LOG")
        self.old_prompt_env = os.environ.get("FAKE_CODEX_PROMPT")
        os.environ["FAKE_CODEX_LOG"] = str(self.fake_log)
        os.environ["FAKE_CODEX_PROMPT"] = str(self.fake_prompt)

    def tearDown(self) -> None:
        if self.old_env is None:
            os.environ.pop("FAKE_CODEX_LOG", None)
        else:
            os.environ["FAKE_CODEX_LOG"] = self.old_env
        if self.old_prompt_env is None:
            os.environ.pop("FAKE_CODEX_PROMPT", None)
        else:
            os.environ["FAKE_CODEX_PROMPT"] = self.old_prompt_env
        self.temporary.cleanup()

    def make_run(self, name: str, texts: list[str]) -> Path:
        run = self.base / name
        (run / "chunks").mkdir(parents=True)
        (run / "events.jsonl").write_text(
            json.dumps({"event": "recording_finalized", "occurred_at": "2026-08-21T15:49:00+09:00"}) + "\n",
            encoding="utf-8",
        )
        (run / "chunks/transcript.json").write_text(
            json.dumps({"segments": [{"text": text} for text in texts]}, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest = {
            "manifest_schema_version": 1,
            "run_id": name,
            "started_at": "2026-08-21T15:48:36+09:00",
            "state": "completed",
            "artifacts": {"transcript": "chunks/transcript.json", "minutes": None, "images": None},
            "media": {"raw_mp4": False, "meeting_mp4": False},
            "title": "旧題",
            "vault_note": None,
        }
        (run / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return run

    def make_note(self, relative: str, run_id: str, *, created_by: str = "agent", status: str | None = None, body: str = "") -> Path:
        note = self.vault / relative
        note.parent.mkdir(parents=True, exist_ok=True)
        fields = ["---", f"created_by: {created_by}", f"source_run: {run_id}"]
        if status:
            fields.append(f"status: {status}")
        fields.extend(["---", "", body, ""])
        note.write_text("\n".join(fields), encoding="utf-8")
        return note

    def calls(self) -> list[list[str]]:
        if not self.fake_log.exists():
            return []
        return [json.loads(line) for line in self.fake_log.read_text(encoding="utf-8").splitlines()]

    def test_retry_flags_manifest_rename_collision_links_index_and_idempotency(self) -> None:
        run = self.make_run(
            "20260821-154836",
            [
                "今日は新薬導入の対象患者と安全性の確認について相談します。",
                "来月からの導入条件を整理し、担当者とフォローアップ方法を決めます。",
                "薬剤部との調整は田中さんが担当し、次回会議で進捗を共有します。",
            ],
        )
        old = self.make_note(
            "Meetings/2026/2026-08-21_旧題.md",
            run.name,
            status="trash_candidate",
        )
        collision = self.make_note(
            "Meetings/2026/2026-08-21_新薬導入方針の検討.md",
            "another-run",
            created_by="user",
            body="手書き",
        )
        agent_link = self.make_note(
            "Meetings/2026/linking-agent.md",
            "linking-run",
            body="[[2026-08-21_旧題#決定|参照]]",
        )
        user_link = self.make_note(
            "Meetings/2026/linking-user.md",
            "user-run",
            created_by="user",
            body="[[2026-08-21_旧題]]",
        )

        result = triage_runs.triage(self.base, vault=self.vault, codex=str(self.fake))

        self.assertEqual(result["runs"][run.name]["status"], "triaged")
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        for args in calls:
            self.assertEqual(args[0], "exec")
            self.assertEqual(args[args.index("-m") + 1], "gpt-5.6-luna")
            self.assertIn("--skip-git-repo-check", args)
            self.assertIn("--ephemeral", args)
            self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
            self.assertEqual(args[args.index("-C") + 1], str(run))
            self.assertEqual(args[args.index("--output-schema") + 1], str(SCRIPTS / "triage_result.schema.json"))
            self.assertEqual(args[-1], "-")
        prompt = self.fake_prompt.read_text(encoding="utf-8")
        for phrase in ("挨拶", "動作確認", "数十秒", "機能検証だと自称", "実会議", "新薬導入"):
            self.assertIn(phrase, prompt)
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["title"], "新薬導入方針の検討")
        self.assertEqual(manifest["disposition"], "keep")
        self.assertEqual(manifest["confidence"], 0.93)
        self.assertIsNone(manifest["disposition_flagged_at"])
        renamed = self.vault / "Meetings/2026/2026-08-21_新薬導入方針の検討-2.md"
        self.assertFalse(old.exists())
        self.assertTrue(renamed.exists())
        self.assertNotIn("status:", renamed.read_text(encoding="utf-8"))
        self.assertEqual(manifest["vault_note"], "Meetings/2026/2026-08-21_新薬導入方針の検討-2.md")
        self.assertIn("[[2026-08-21_新薬導入方針の検討-2#決定|参照]]", agent_link.read_text(encoding="utf-8"))
        self.assertIn("[[2026-08-21_旧題]]", user_link.read_text(encoding="utf-8"))
        self.assertIn("手書き", collision.read_text(encoding="utf-8"))
        self.assertTrue((self.base / "index.db").is_file())
        with sqlite3.connect(self.base / "index.db") as connection:
            title = connection.execute("SELECT title FROM runs WHERE run_id = ?", (run.name,)).fetchone()[0]
        self.assertEqual(title, "新薬導入方針の検討")
        self.assertFalse(list(run.glob(".triage.*.json")))

        again = triage_runs.triage(self.base, vault=self.vault, codex=str(self.fake))

        self.assertEqual(again["runs"][run.name]["status"], "unchanged")
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(len(list((self.vault / "Meetings/2026").glob("2026-08-21_新薬導入方針の検討-2.md"))), 1)

    def test_empty_or_tiny_transcript_skips_llm_and_sets_trash_candidate(self) -> None:
        run = self.make_run("20260821-160000", ["テスト", "はい"])
        note = self.make_note("Meetings/2026/2026-08-21_旧題.md", run.name)

        result = triage_runs.triage(self.base, vault=self.vault, codex="must-not-run")

        self.assertEqual(result["runs"][run.name]["disposition"], "test")
        self.assertEqual(self.calls(), [])
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["title"], "文字起こし不十分な録音テスト")
        self.assertIsNotNone(manifest["disposition_flagged_at"])
        renamed = self.vault / manifest["vault_note"]
        self.assertFalse(note.exists())
        self.assertIn("status: trash_candidate", renamed.read_text(encoding="utf-8"))

    def test_locked_run_is_skipped_without_overwriting_manifest(self) -> None:
        run = self.make_run("20260821-170000", ["十分に長い実会議の議題について具体的に相談を進めています。"])
        before = (run / "manifest.json").read_bytes()
        lock = (run / "chunks/postprocess.lock").open("a+b")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = triage_runs.triage(self.base, codex="must-not-run")
        finally:
            lock.close()

        self.assertEqual(result["runs"][run.name]["status"], "skipped_locked")
        self.assertEqual((run / "manifest.json").read_bytes(), before)

    def test_reused_keep_result_preserves_user_override(self) -> None:
        run = self.make_run("20260821-173000", ["実会議について十分に長い相談内容です。"])
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        manifest.update(
            title="ユーザーが保持した録音",
            disposition="keep",
            confidence=0.99,
            reason="Obsidianで保持に変更済みです",
            disposition_override="keep",
            disposition_flagged_at=None,
        )
        (run / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        result = triage_runs.triage(self.base, codex="must-not-run")

        self.assertEqual(result["runs"][run.name]["status"], "unchanged")
        updated = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(updated["disposition_override"], "keep")

    def test_timeout_setting_and_schema_are_validated_before_codex(self) -> None:
        run = self.make_run("20260821-180000", ["実会議について十分に長い相談内容が文字起こしされています。"])
        old = os.environ.get("MEETING_CODEX_TIMEOUT_SECONDS")
        os.environ["MEETING_CODEX_TIMEOUT_SECONDS"] = "zero"
        try:
            result = triage_runs.triage(self.base, codex="must-not-run")
        finally:
            if old is None:
                os.environ.pop("MEETING_CODEX_TIMEOUT_SECONDS", None)
            else:
                os.environ["MEETING_CODEX_TIMEOUT_SECONDS"] = old
        self.assertEqual(result["runs"][run.name]["status"], "failed")
        self.assertIn("must be numeric", result["runs"][run.name]["error"])

        broken = Path(self.temporary.name) / "broken-schema.json"
        broken.write_text("not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid triage schema JSON"):
            triage_runs.triage(self.base, codex="must-not-run", schema=broken)

    def test_codex_timeout_is_retried_once(self) -> None:
        run = self.make_run("20260821-190000", ["実会議の議題と担当分担について十分に長く相談しています。"])
        old_timeout = os.environ.get("MEETING_CODEX_TIMEOUT_SECONDS")
        os.environ["MEETING_CODEX_TIMEOUT_SECONDS"] = "0.2"
        try:
            with mock.patch.object(
                triage_runs.subprocess,
                "run",
                side_effect=triage_runs.subprocess.TimeoutExpired("codex", 0.2),
            ) as run_codex:
                result = triage_runs.triage(self.base, codex=str(self.fake))
        finally:
            if old_timeout is None:
                os.environ.pop("MEETING_CODEX_TIMEOUT_SECONDS", None)
            else:
                os.environ["MEETING_CODEX_TIMEOUT_SECONDS"] = old_timeout

        self.assertEqual(result["runs"][run.name]["status"], "failed")
        self.assertIn("timed out", result["runs"][run.name]["error"])
        self.assertEqual(run_codex.call_count, 2)


if __name__ == "__main__":
    unittest.main()
