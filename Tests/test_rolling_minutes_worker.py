#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "Resources/Scripts/rolling_minutes_worker.py"
SPEC = importlib.util.spec_from_file_location("rolling_minutes_worker", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


FAKE_INTERPRETER = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

prompt = Path(sys.argv[1])
output = Path(sys.argv[2])
counter = Path(os.environ["FAKE_COUNTER"])
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
payload = json.loads(prompt.read_text())
Path(os.environ["CAPTURED_PROMPT"]).write_text(json.dumps(payload, ensure_ascii=False))
if os.environ.get("FAIL_FIRST") == "1" and count == 1:
    raise SystemExit(9)
output.write_text(json.dumps({"sections": [{
    "title": "更新", "timestamp": 0, "capture_timestamp": 0,
    "summary": payload["segments"][0]["text"], "bullets": ["確認事項"]
}]}, ensure_ascii=False))
'''


def line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False) + "\n"


class RollingMinutesWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temporary.name)
        self.chunks = self.run_dir / "chunks"
        self.chunks.mkdir()
        (self.chunks / "recorder.events.jsonl").write_text(
            line({"event": "chunk_ready", "chunk_id": 0, "start_abs": 120.0}), encoding="utf-8"
        )
        (self.chunks / "worker.events.jsonl").write_text(
            line({"event": "chunk_transcription_succeeded", "chunk_id": 0}), encoding="utf-8"
        )
        (self.chunks / "chunk_0000.transcript.json").write_text(
            json.dumps({"segments": [{"start": 1, "end": 2, "text": "新しい論点"}]}), encoding="utf-8"
        )
        self.interpreter = self.run_dir / "fake_interpreter.py"
        self.interpreter.write_text(FAKE_INTERPRETER, encoding="utf-8")
        self.interpreter.chmod(0o755)
        self.counter = self.run_dir / "counter"
        self.captured = self.run_dir / "captured.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self) -> list[str]:
        return [
            sys.executable, str(MODULE_PATH), "--run-dir", str(self.run_dir),
            "--interpret-script", str(self.interpreter), "--interval", "0.1", "--timeout", "2",
        ]

    def environment(self, fail_first: bool = False) -> dict[str, str]:
        environment = dict(os.environ)
        environment["FAKE_COUNTER"] = str(self.counter)
        environment["CAPTURED_PROMPT"] = str(self.captured)
        environment["FAIL_FIRST"] = "1" if fail_first else "0"
        return environment

    def test_completed_chunk_becomes_atomic_markdown_draft_and_end_stops_worker(self) -> None:
        (self.chunks / "END").touch()
        (self.chunks / "WORKER_DONE").touch()
        result = subprocess.run(self.command(), env=self.environment(), capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        draft = (self.run_dir / "minutes-draft.md").read_text(encoding="utf-8")
        self.assertIn("# 議事録（録画中ドラフト）", draft)
        self.assertIn("新しい論点", draft)
        prompt = json.loads(self.captured.read_text(encoding="utf-8"))
        self.assertEqual(prompt["segments"][0]["start"], 121.0)
        self.assertEqual(prompt["new_segments"], prompt["segments"])
        self.assertTrue(prompt["rolling"])
        self.assertEqual(prompt["current_draft"], "")
        self.assertEqual(prompt["previous_draft"], "")
        events = worker.read_jsonl(self.chunks / "minutes-worker.events.jsonl")
        self.assertEqual(events[-1]["event"], "minutes_worker_stopped")

    def test_interpret_failure_keeps_existing_draft_and_retries_next_period(self) -> None:
        original = "existing draft\n"
        (self.run_dir / "minutes-draft.md").write_text(original, encoding="utf-8")
        process = subprocess.Popen(self.command(), env=self.environment(fail_first=True))
        deadline = time.monotonic() + 3
        while (not self.counter.exists() or int(self.counter.read_text()) < 2) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.counter.exists())
        self.assertGreaterEqual(int(self.counter.read_text()), 2)
        (self.chunks / "END").touch()
        (self.chunks / "WORKER_DONE").touch()
        self.assertEqual(process.wait(timeout=3), 0)
        draft = (self.run_dir / "minutes-draft.md").read_text(encoding="utf-8")
        self.assertNotEqual(draft, original)
        prompt = json.loads(self.captured.read_text(encoding="utf-8"))
        self.assertEqual(prompt["previous_draft"], original)
        events = [record["event"] for record in worker.read_jsonl(self.chunks / "minutes-worker.events.jsonl")]
        self.assertIn("minutes_interpretation_skipped", events)
        self.assertIn("minutes_draft_updated", events)

    def test_lock_prevents_second_worker(self) -> None:
        lock = (self.chunks / "minutes-worker.lock").open("a+b")
        try:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(worker.run_worker(self.run_dir, self.interpreter, 0.1, 1), 75)
        finally:
            lock.close()


if __name__ == "__main__":
    unittest.main()
