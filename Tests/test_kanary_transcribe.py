#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "Resources" / "Scripts" / "kanary_transcribe.py"
SPEC = importlib.util.spec_from_file_location("kanary_transcribe", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
kanary_transcribe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(kanary_transcribe)


FAKE_KANARY = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

mode = os.environ.get("FAKE_KANARY_MODE", "normal")
out = Path(sys.argv[sys.argv.index("--out") + 1])
if mode == "nonzero":
    print("synthetic kanary failure", file=sys.stderr)
    raise SystemExit(17)
if mode == "hang":
    time.sleep(5)
if mode == "broken":
    out.write_text("{not-json", encoding="utf-8")
    raise SystemExit(0)
segments = [] if mode == "empty" else [
    {"track": "mixed", "start_seconds": 0.25, "end_seconds": 1.5,
     "confidence": 0.9, "text": "テスト発話"}
]
out.write_text(json.dumps({
    "schema_version": 3,
    "duration": 2.0,
    "transcript": {"tracks": [], "segments": segments, "diagnostics": []},
}), encoding="utf-8")
'''


class KanaryTranscribeAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.fake_kanary = self.bin_dir / "kanary"
        self.fake_kanary.write_text(FAKE_KANARY, encoding="utf-8")
        self.fake_kanary.chmod(self.fake_kanary.stat().st_mode | stat.S_IXUSR)
        self.audio = self.root / "fixture.wav"
        with wave.open(str(self.audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8_000)
            output.writeframes(b"\0\0" * 80)
        self.output = self.root / "transcript.json"
        self.original_path = os.environ.get("PATH", "")
        self.original_mode = os.environ.get("FAKE_KANARY_MODE")
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{self.original_path}"

    def tearDown(self) -> None:
        os.environ["PATH"] = self.original_path
        if self.original_mode is None:
            os.environ.pop("FAKE_KANARY_MODE", None)
        else:
            os.environ["FAKE_KANARY_MODE"] = self.original_mode
        self.temp_dir.cleanup()

    def run_mode(self, mode: str) -> int:
        os.environ["FAKE_KANARY_MODE"] = mode
        return kanary_transcribe.main([str(self.audio), str(self.output)])

    def test_normal_fixture(self) -> None:
        self.assertEqual(self.run_mode("normal"), 0)
        self.assertEqual(
            json.loads(self.output.read_text(encoding="utf-8")),
            {"segments": [{"start": 0.25, "end": 1.5, "text": "テスト発話"}]},
        )

    def test_empty_segments_fixture(self) -> None:
        self.assertEqual(self.run_mode("empty"), 0)
        self.assertEqual(json.loads(self.output.read_text(encoding="utf-8")), {"segments": []})

    def test_broken_json_fixture(self) -> None:
        self.assertNotEqual(self.run_mode("broken"), 0)
        self.assertFalse(self.output.exists())

    def test_nonzero_exit_fixture(self) -> None:
        self.assertNotEqual(self.run_mode("nonzero"), 0)
        self.assertFalse(self.output.exists())

    def test_hang_triggers_timeout_fixture(self) -> None:
        original_timeout = kanary_transcribe.MIN_TIMEOUT_SECONDS
        kanary_transcribe.MIN_TIMEOUT_SECONDS = 0.1
        try:
            self.assertEqual(self.run_mode("hang"), 124)
        finally:
            kanary_transcribe.MIN_TIMEOUT_SECONDS = original_timeout
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
