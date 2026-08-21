#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "Resources" / "Scripts" / "transcriber_worker.py"
SPEC = importlib.util.spec_from_file_location("transcriber_worker", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


FAKE_TRANSCRIBER = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

audio = Path(sys.argv[1])
output = Path(sys.argv[2])
counter = Path(os.environ["FAKE_COUNTER"])
try:
    invocation = int(counter.read_text()) + 1
except FileNotFoundError:
    invocation = 1
counter.write_text(str(invocation))
if os.environ.get("FAIL_FIRST") == "1" and invocation == 1:
    Path(os.environ["FAKE_STARTED"]).touch()
    time.sleep(0.8)
    raise SystemExit(9)
output.write_text(json.dumps({
    "segments": [{"start": 0.25, "end": 0.75, "text": audio.stem}],
}), encoding="utf-8")
'''


def json_line(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"


class FoldTest(unittest.TestCase):
    def test_truncated_tail_is_ignored_and_duplicate_latest_ready_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_bytes(
                json_line({"event": "chunk_ready", "chunk_id": 0, "start_abs": 0.0})
                + json_line({"event": "chunk_ready", "chunk_id": 0, "start_abs": 1.5})
                + json_line({"event": "chunk_drop_gap", "duration_seconds": 0.25})
                + b'{"event":"chunk_ready"'
                + json_line({"event": "chunk_ready", "chunk_id": 1, "start_abs": 120.0})
            )
            state = worker.fold_recorder_events(worker.read_jsonl(path))
            self.assertEqual(set(state["ready"]), {"0"})
            self.assertEqual(state["ready"]["0"]["start_abs"], 1.5)
            self.assertEqual(state["gaps"], [{"event": "chunk_drop_gap", "duration_seconds": 0.25}])

    def test_duplicate_worker_success_is_idempotent_and_assembly_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            chunks = run_dir / "chunks"
            chunks.mkdir()
            recorder_records = [
                {"event": "chunk_ready", "chunk_id": "1", "start_abs": 120.0},
                {"event": "chunk_ready", "chunk_id": "0", "start_abs": 0.0},
                {"event": "chunk_ready", "chunk_id": "0", "start_abs": 0.5},
            ]
            worker_records = [
                {"event": "chunk_transcription_attempt", "chunk_id": "0", "attempt": 1},
                {"event": "chunk_transcription_succeeded", "chunk_id": "0", "attempt": 1},
                {"event": "chunk_transcription_succeeded", "chunk_id": "0", "attempt": 1},
                {"event": "chunk_transcription_succeeded", "chunk_id": "1", "attempt": 1},
            ]
            (chunks / "recorder.events.jsonl").write_bytes(b"".join(map(json_line, recorder_records)))
            (chunks / "worker.events.jsonl").write_bytes(b"".join(map(json_line, worker_records)))
            (chunks / "chunk_0000.transcript.json").write_text(
                json.dumps({"segments": [{"start": 1, "end": 2, "text": "zero"}]}),
                encoding="utf-8",
            )
            (chunks / "chunk_0001.transcript.json").write_text(
                json.dumps({"segments": [{"start": 2, "end": 3, "text": "one"}]}),
                encoding="utf-8",
            )
            output = worker.assemble(run_dir)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {
                    "segments": [
                        {"start": 1.5, "end": 2.5, "text": "zero"},
                        {"start": 122.0, "end": 123.0, "text": "one"},
                    ]
                },
            )


class WorkerRestartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temporary.name)
        self.chunks = self.run_dir / "chunks"
        self.audio_chunks = self.run_dir / "audio-chunks"
        self.chunks.mkdir()
        self.audio_chunks.mkdir()
        (self.audio_chunks / "chunk_0000.wav").write_bytes(b"fixture")
        (self.chunks / "recorder.events.jsonl").write_bytes(
            json_line(
                {
                    "event": "chunk_ready",
                    "chunk_id": 0,
                    "start_abs": 4.0,
                    "path": "audio-chunks/chunk_0000.wav",
                }
            )
        )
        (self.chunks / "END").touch()
        self.fake_transcriber = self.run_dir / "fake_transcriber.py"
        self.fake_transcriber.write_text(FAKE_TRANSCRIBER, encoding="utf-8")
        self.counter = self.run_dir / "counter"
        self.started = self.run_dir / "started"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self) -> list[str]:
        return [
            sys.executable,
            str(MODULE_PATH),
            "--run-dir",
            str(self.run_dir),
            "--transcribe-script",
            str(self.fake_transcriber),
            "--timeout",
            "3",
            "--backoff-base",
            "0",
            "--poll-interval",
            "0.01",
        ]

    def environment(self, fail_first: bool) -> dict[str, str]:
        environment = dict(os.environ)
        environment["FAKE_COUNTER"] = str(self.counter)
        environment["FAKE_STARTED"] = str(self.started)
        environment["FAIL_FIRST"] = "1" if fail_first else "0"
        return environment

    def test_kill_then_restart_processes_only_unfinished_chunk(self) -> None:
        first = subprocess.Popen(self.command(), env=self.environment(fail_first=True))
        deadline = time.monotonic() + 5
        while not self.started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.started.exists(), "first attempt never started")
        first.send_signal(signal.SIGKILL)
        first.wait(timeout=2)
        time.sleep(1.0)  # Allow the orphaned fixture subprocess to exit.

        resumed = subprocess.run(
            self.command(),
            env=self.environment(fail_first=True),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertTrue((self.chunks / "WORKER_DONE").is_file())
        self.assertEqual(int(self.counter.read_text()), 2)

        records = worker.read_jsonl(self.chunks / "worker.events.jsonl")
        attempts = [record["attempt"] for record in records if record["event"] == "chunk_transcription_attempt"]
        successes = [record for record in records if record["event"] == "chunk_transcription_succeeded"]
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(len(successes), 1)
        self.assertFalse((self.audio_chunks / "chunk_0000.wav").exists())
        self.assertEqual(
            json.loads((self.chunks / "transcript.json").read_text(encoding="utf-8")),
            {"segments": [{"start": 4.25, "end": 4.75, "text": "chunk_0000"}]},
        )
        event_count = len(records)

        again = subprocess.run(
            self.command(),
            env=self.environment(fail_first=False),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(int(self.counter.read_text()), 2)
        self.assertEqual(len(worker.read_jsonl(self.chunks / "worker.events.jsonl")), event_count)

    def test_permanent_failure_writes_failed_and_drained_ack(self) -> None:
        always_fails = self.run_dir / "always_fails.py"
        always_fails.write_text("raise SystemExit(12)\n", encoding="utf-8")
        command = [
            sys.executable,
            str(MODULE_PATH),
            "--run-dir",
            str(self.run_dir),
            "--transcribe-script",
            str(always_fails),
            "--timeout",
            "3",
            "--max-attempts",
            "1",
            "--backoff-base",
            "0",
            "--poll-interval",
            "0.01",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue((self.chunks / "WORKER_FAILED").is_file())
        self.assertTrue((self.chunks / "WORKER_DONE").is_file())
        self.assertFalse((self.chunks / "transcript.json").exists())

        resumed = subprocess.run(
            self.command() + ["--retry-failed"],
            env=self.environment(fail_first=False),
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse((self.chunks / "WORKER_FAILED").exists())
        self.assertTrue((self.chunks / "transcript.json").is_file())

    def test_retry_cycle_resets_exponential_backoff(self) -> None:
        (self.chunks / "worker.events.jsonl").write_bytes(
            json_line({"event": "chunk_transcription_failed", "chunk_id": 0, "attempt": 6})
        )
        (self.chunks / "WORKER_FAILED").touch()
        with mock.patch.object(worker, "transcribe_chunk", return_value=(False, "synthetic")), \
             mock.patch.object(worker.time, "sleep") as sleep:
            status = worker.run_worker(
                self.run_dir,
                self.fake_transcriber,
                timeout=3,
                max_attempts=3,
                backoff_base=1,
                poll_interval=0.01,
                retry_failed=True,
            )
        self.assertEqual(status, 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_success_event_is_fsynced_before_chunk_wav_cleanup(self) -> None:
        observed: list[str] = []
        real_append = worker.append_event

        def append(path: Path, event: str, **fields: object) -> None:
            real_append(path, event, **fields)
            if event == "chunk_transcription_succeeded":
                observed.append("success_durable")

        def cleanup(run_dir: Path, key: str) -> bool:
            records = worker.read_jsonl(self.chunks / "worker.events.jsonl")
            self.assertEqual(records[-1]["event"], "chunk_transcription_succeeded")
            observed.append("cleanup")
            return True

        with mock.patch.object(worker, "append_event", side_effect=append), \
             mock.patch.object(worker.storage, "cleanup_chunk_wav", side_effect=cleanup), \
             mock.patch.dict(os.environ, self.environment(fail_first=False), clear=True):
            status = worker.run_worker(
                self.run_dir,
                self.fake_transcriber,
                timeout=3,
                max_attempts=3,
                backoff_base=0,
                poll_interval=0.01,
            )
        self.assertEqual(status, 0)
        self.assertEqual(observed, ["success_durable", "cleanup"])


if __name__ == "__main__":
    unittest.main()
