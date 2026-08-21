#!/usr/bin/env python3
from __future__ import annotations

from contextlib import closing
import importlib.util
from importlib.machinery import SourceFileLoader
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "Resources/Scripts"
sys.path.insert(0, str(SCRIPTS))
import mtg_index


def load_cli():
    loader = SourceFileLoader("mtg_cli", str(SCRIPTS / "mtg"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


mtg_cli = load_cli()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_event(path: Path, event: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"event": event}) + "\n", encoding="utf-8")


class MeetingIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_run(
        self,
        name: str,
        *,
        transcript: str = "日本語の全文です。検索できます。",
        minutes: str = "# 議事録\n重要な決定です。",
        finalized: bool = True,
    ) -> Path:
        run = self.base / name
        run.mkdir()
        if finalized:
            write_event(run / "events.jsonl", "recording_finalized")
        else:
            write_event(run / "events.jsonl", "recording_started")
        write_json(run / "transcript/transcript.json", {"segments": [{"text": transcript}]})
        (run / "minutes").mkdir()
        (run / "minutes/minutes.md").write_text(minutes, encoding="utf-8")
        write_json(
            run / "manifest.json",
            {
                "manifest_schema_version": 1,
                "run_id": name,
                "started_at": "2026-08-21T10:00:00+09:00",
                "completed_at": "2026-08-21T10:10:00+09:00",
                "duration_seconds": 600,
                "state": "completed",
                "title": "索引テスト",
                "disposition": "keep",
                "confidence": 0.94,
                "reason": "実会議の内容です",
                "artifacts": {
                    "transcript": "transcript/transcript.json",
                    "minutes": "minutes/minutes.md",
                },
            },
        )
        return run

    def cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "mtg"), "--base-dir", str(self.base), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_rebuild_excludes_active_run_and_all_read_commands_support_json(self) -> None:
        self.make_run("20260821-100000")
        self.make_run("20260821-110000", finalized=False)

        rebuilt = self.cli("index", "--rebuild", "--json")
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        self.assertEqual(json.loads(rebuilt.stdout)["excluded_active"], 1)

        listed = json.loads(self.cli("list", "--json").stdout)
        self.assertEqual([item["run_id"] for item in listed], ["20260821-100000"])
        global_json = self.cli("--json", "list")
        self.assertEqual(global_json.returncode, 0, global_json.stderr)
        self.assertIsInstance(json.loads(global_json.stdout), list)
        shown = json.loads(self.cli("show", "20260821-10", "--json").stdout)
        self.assertEqual(shown["title"], "索引テスト")
        self.assertEqual(shown["disposition"], "keep")
        self.assertEqual(shown["confidence"], 0.94)
        self.assertEqual(shown["reason"], "実会議の内容です")

        searched = json.loads(self.cli("search", "日本語", "--json").stdout)
        self.assertEqual(searched["method"], "fts5_trigram")
        self.assertEqual(searched["results"][0]["run_id"], "20260821-100000")

    def test_two_character_japanese_search_uses_literal_like(self) -> None:
        self.make_run("run", transcript="猫犬について話す")
        result = self.cli("search", "猫犬", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["method"], "literal_like")
        self.assertEqual(len(payload["results"]), 1)

    def test_like_wildcards_are_literal_for_one_character_queries(self) -> None:
        self.make_run("literal", transcript="割合は10%です")
        self.make_run("plain", transcript="percent sign is absent")
        percent = json.loads(self.cli("search", "%", "--json").stdout)
        underscore = json.loads(self.cli("search", "_", "--json").stdout)
        self.assertEqual({item["run_id"] for item in percent["results"]}, {"literal"})
        self.assertEqual(underscore["results"], [])

    def test_corrupt_and_schema_mismatched_indexes_rebuild_automatically(self) -> None:
        self.make_run("recover")
        (self.base / "index.db").write_bytes(b"not sqlite")
        with closing(mtg_index.connect_index(self.base)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 1)

        with closing(sqlite3.connect(self.base / "index.db")) as connection:
            connection.execute("UPDATE metadata SET value='999' WHERE key='index_schema_version'")
            connection.commit()
        with closing(mtg_index.connect_index(self.base)) as connection:
            self.assertEqual(
                connection.execute("SELECT value FROM metadata WHERE key='index_schema_version'").fetchone()[0],
                str(mtg_index.INDEX_SCHEMA_VERSION),
            )

    def test_failed_rebuild_keeps_previous_database_and_removes_tmp(self) -> None:
        self.make_run("stable")
        mtg_index.rebuild_index(self.base)
        before = (self.base / "index.db").read_bytes()
        with mock.patch.object(mtg_index, "_insert_run", side_effect=RuntimeError("injected")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                mtg_index.rebuild_index(self.base)
        self.assertEqual((self.base / "index.db").read_bytes(), before)
        self.assertEqual(list(self.base.glob(".index.*.tmp")), [])

    def test_capability_failure_does_not_replace_existing_index(self) -> None:
        self.make_run("stable")
        mtg_index.rebuild_index(self.base)
        before = (self.base / "index.db").read_bytes()
        with mock.patch.object(
            mtg_index, "check_fts5_trigram", side_effect=mtg_index.IndexUnavailable("missing")
        ):
            with self.assertRaises(mtg_index.IndexUnavailable):
                mtg_index.rebuild_index(self.base)
        self.assertEqual((self.base / "index.db").read_bytes(), before)

    def test_incremental_update_is_public_and_replaces_run_documents(self) -> None:
        run = self.make_run("incremental", transcript="変更前の文章")
        self.assertTrue(mtg_index.update_index_for_run(self.base, run))
        write_json(run / "transcript/transcript.json", {"segments": [{"text": "変更後の文章"}]})
        self.assertTrue(mtg_index.update_index_for_run(self.base, run))
        result = json.loads(self.cli("search", "変更後", "--json").stdout)
        self.assertEqual(len(result["results"]), 1)
        old = json.loads(self.cli("search", "変更前", "--json").stdout)
        self.assertEqual(old["results"], [])

    def test_open_json_invokes_system_open_and_reports_target(self) -> None:
        self.make_run("open-me")
        mtg_index.rebuild_index(self.base)
        args = mtg_cli.parser().parse_args(
            ["--base-dir", str(self.base), "open", "open-me", "--json"]
        )
        with mock.patch.object(mtg_cli.subprocess, "run") as opened, mock.patch("builtins.print"):
            opened.return_value.returncode = 0
            status = args.handler(self.base, args)
        self.assertEqual(status, 0)
        self.assertEqual(opened.call_args.args[0][0], "/usr/bin/open")
        self.assertTrue(opened.call_args.args[0][1].endswith("minutes/minutes.md"))


if __name__ == "__main__":
    unittest.main()
