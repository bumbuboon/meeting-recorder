#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


scan_module = load("resume_scan", ROOT / "Resources/Scripts/resume_scan.py")
runner_module = load("postprocess_runner", ROOT / "Resources/Scripts/postprocess_runner.py")


def write_events(path: Path, *events: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"event": event}) + "\n" for event in events), encoding="utf-8")


class ResumeScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.calls = self.base / "calls.txt"
        self.fake_runner = self.base / "fake_runner.py"
        self.fake_runner.write_text(
            "import pathlib,sys\n"
            f"pathlib.Path({str(self.calls)!r}).open('a').write(sys.argv[sys.argv.index('--run-dir') + 1] + '\\n')\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_run(self, name: str, lifecycle: tuple[str, ...], postprocess: tuple[str, ...] = ()) -> Path:
        run = self.base / name
        write_events(run / "events.jsonl", *lifecycle)
        if postprocess:
            write_events(run / "chunks/postprocess.events.jsonl", *postprocess)
        return run

    def make_ready_run(self, name: str = "run") -> Path:
        run = self.make_run(name, ("recording_finalized",))
        chunks = run / "chunks"
        chunks.mkdir(exist_ok=True)
        (run / "raw.mp4").write_bytes(b"media")
        (chunks / "recorder.events.jsonl").write_text(
            json.dumps({"event": "chunk_ready", "chunk_id": 0, "start_abs": 0.0}) + "\n",
            encoding="utf-8",
        )
        (chunks / "worker.events.jsonl").write_text(
            json.dumps({"event": "chunk_transcription_succeeded", "chunk_id": 0, "attempt": 1}) + "\n",
            encoding="utf-8",
        )
        transcript = json.dumps({"segments": [{"start": 0, "end": 1, "text": "ok"}]})
        (chunks / "chunk_0000.transcript.json").write_text(transcript, encoding="utf-8")
        (chunks / "transcript.json").write_text(transcript, encoding="utf-8")
        (chunks / "WORKER_DONE").touch()
        return run

    def test_failed_and_interrupted_runs_are_reexecuted_but_completed_is_skipped(self) -> None:
        self.make_run("failed", ("recording_finalized", "postprocess_failed"))
        self.make_run("interrupted", ("recording_finalized",))
        self.make_run("completed", ("recording_finalized",), ("postprocess_completed",))

        outcomes = scan_module.scan(self.base, self.fake_runner)

        called = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual({Path(path).name for path in called}, {"failed", "interrupted"})
        self.assertEqual(outcomes["completed"], "skip:completed")
        self.assertEqual(outcomes["failed"], "resumed:0")

    def test_terminal_runs_notify_without_retry(self) -> None:
        for event in scan_module.TERMINAL_EVENTS:
            self.make_run(event, (event, "postprocess_failed"))

        with mock.patch.object(scan_module, "notify_terminal") as notify:
            outcomes = scan_module.scan(self.base, self.fake_runner)

        self.assertFalse(self.calls.exists())
        self.assertEqual(notify.call_count, len(scan_module.TERMINAL_EVENTS))
        self.assertTrue(all(value.startswith("terminal:") for value in outcomes.values()))

    def test_malformed_tail_is_ignored(self) -> None:
        run = self.make_run("torn", ("recording_finalized",), ("postprocess_completed",))
        with (run / "chunks/postprocess.events.jsonl").open("ab") as handle:
            handle.write(b'{"event":"postprocess_failed"')
        self.assertEqual(scan_module.classify_run(run), "skip:completed")

    def test_scan_skips_when_per_run_lock_is_held(self) -> None:
        run = self.make_run("locked", ("recording_finalized", "postprocess_failed"))
        chunks = run / "chunks"
        chunks.mkdir(exist_ok=True)
        lock_handle = (chunks / "postprocess.lock").open("a+b")
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            outcomes = scan_module.scan(self.base, ROOT / "Resources/Scripts/postprocess_runner.py")
        finally:
            lock_handle.close()
        self.assertEqual(outcomes["locked"], "skip:locked")
        self.assertFalse((chunks / "postprocess.events.jsonl").exists())

    def test_runner_wraps_postprocess_in_caffeinate_i(self) -> None:
        run = self.make_ready_run()
        minutes = run / "minutes/new/minutes.md"
        minutes.parent.mkdir(parents=True)
        minutes.write_text("# title\n", encoding="utf-8")
        with mock.patch.object(runner_module.subprocess, "run") as execute:
            execute.return_value.returncode = 0
            execute.return_value.stdout = str(minutes) + "\n"
            status = runner_module.run_postprocess(
                run,
                self.base / "postprocess.sh",
                self.base / "worker.py",
                resume=False,
                caffeinate=Path("/usr/bin/caffeinate"),
            )
        self.assertEqual(status, 0)
        command = execute.call_args.args[0]
        self.assertEqual(command[:3], ["/usr/bin/caffeinate", "-i", "/bin/bash"])

    def test_runner_requires_folded_chunk_success_before_started(self) -> None:
        run = self.make_ready_run()
        (run / "chunks/worker.events.jsonl").write_text("", encoding="utf-8")

        status = runner_module.run_postprocess(
            run,
            self.base / "postprocess.sh",
            self.base / "worker.py",
            resume=False,
            caffeinate=Path("/usr/bin/caffeinate"),
        )

        self.assertEqual(status, 1)
        events = runner_module.transcription.read_jsonl(run / "chunks/postprocess.events.jsonl")
        self.assertEqual([event["event"] for event in events], ["postprocess_failed"])

    def test_worker_lock_contention_is_deferred_without_postprocess_event(self) -> None:
        run = self.make_ready_run()
        with mock.patch.object(runner_module, "resume_transcription", return_value=75):
            status = runner_module.run_postprocess(
                run,
                self.base / "postprocess.sh",
                self.base / "worker.py",
                resume=True,
                caffeinate=Path("/usr/bin/caffeinate"),
            )
        self.assertEqual(status, 75)
        self.assertFalse((run / "chunks/postprocess.events.jsonl").exists())

    def test_resume_records_started_only_after_transcription_gate(self) -> None:
        run = self.make_ready_run()

        def worker_finished(*_args):
            self.assertFalse((run / "chunks/postprocess.events.jsonl").exists())
            return 0

        with mock.patch.object(runner_module, "resume_transcription", side_effect=worker_finished), \
             mock.patch.object(runner_module.subprocess, "run") as execute:
            minutes = run / "minutes/new/minutes.md"
            minutes.parent.mkdir(parents=True)
            minutes.write_text("# title\n", encoding="utf-8")
            execute.return_value.returncode = 0
            execute.return_value.stdout = str(minutes) + "\n"
            status = runner_module.run_postprocess(
                run,
                self.base / "postprocess.sh",
                self.base / "worker.py",
                resume=True,
                caffeinate=Path("/usr/bin/caffeinate"),
            )
        self.assertEqual(status, 0)
        events = runner_module.transcription.read_jsonl(run / "chunks/postprocess.events.jsonl")
        self.assertEqual(
            [event["event"] for event in events],
            ["postprocess_started", "postprocess_completed", "index_completed"],
        )
        completed = next(event for event in events if event["event"] == "postprocess_completed")
        self.assertEqual(completed["canonical_minutes"], "minutes/new/minutes.md")
        self.assertTrue((run / "manifest.json").is_file())

    def test_manifest_failure_does_not_roll_back_durable_core_completion(self) -> None:
        run = self.make_ready_run()
        minutes = run / "minutes/new/minutes.md"
        minutes.parent.mkdir(parents=True)
        minutes.write_text("# title\n", encoding="utf-8")
        with mock.patch.object(runner_module.subprocess, "run") as execute, \
             mock.patch.object(runner_module.storage, "generate_manifest", side_effect=OSError("disk full")):
            execute.return_value.returncode = 0
            execute.return_value.stdout = str(minutes) + "\n"
            execute.return_value.stderr = ""
            status = runner_module.run_postprocess(
                run,
                self.base / "postprocess.sh",
                self.base / "worker.py",
                resume=False,
                caffeinate=Path("/usr/bin/caffeinate"),
            )
        self.assertEqual(status, 0)
        events = runner_module.transcription.read_jsonl(run / "chunks/postprocess.events.jsonl")
        self.assertEqual(
            [event["event"] for event in events],
            ["postprocess_started", "postprocess_completed", "manifest_failed"],
        )
        self.assertEqual(scan_module.classify_run(run), "skip:completed")


if __name__ == "__main__":
    unittest.main()
