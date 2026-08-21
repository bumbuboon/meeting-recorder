#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "Resources/Scripts/video_meeting_minutes.py"
spec = importlib.util.spec_from_file_location("video_meeting_minutes", SCRIPT)
minutes = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(minutes)


class TranscriptImportTest(unittest.TestCase):
    def test_transcript_input_skips_full_length_audio_extraction_and_uses_raw_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw.mp4"
            raw.touch()
            transcript = root / "transcript.json"
            transcript.write_text(
                json.dumps({"segments": [{"start": 0, "end": 1, "text": "確認しました"}]}),
                encoding="utf-8",
            )
            output = root / "minutes"
            argv = [str(SCRIPT), str(raw), "--out", str(output), "--transcript", str(transcript)]

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(minutes, "ffprobe_duration", return_value=5.0), \
                 mock.patch.object(minutes, "extract_audio") as extract_audio, \
                 mock.patch.object(minutes, "extract_interval_frames", return_value=[]):
                self.assertEqual(minutes.main(), 0)

            extract_audio.assert_not_called()
            run_dir = next(path for path in output.iterdir() if path.is_dir())
            self.assertFalse((run_dir / "audio.wav").exists())
            markdown = (run_dir / "minutes.md").read_text(encoding="utf-8")
            self.assertIn(f"- Source: `{raw.resolve()}`", markdown)

    def test_external_interpreter_can_plan_frames_from_transcript_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw.mp4"
            raw.touch()
            transcript = root / "transcript.json"
            transcript.write_text(
                json.dumps({"segments": [{"start": 0, "end": 10, "text": "画面を共有します"}]}),
                encoding="utf-8",
            )
            fake_interpreter = root / "fake_interpreter.py"
            fake_interpreter.write_text(
                """import json
import pathlib
import sys

prompt = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding=\"utf-8\"))
assert \"segments\" in prompt
assert \"frame_selection\" in prompt
assert \"frames\" not in prompt
pathlib.Path(sys.argv[2]).write_text(json.dumps({\"sections\": [{
    \"title\": \"共有\", \"timestamp\": 0, \"capture_timestamp\": 7,
    \"summary\": \"画面共有\", \"bullets\": [\"確認\"]
}]}), encoding=\"utf-8\")
""",
                encoding="utf-8",
            )
            output = root / "minutes"
            command = f"{sys.executable} {fake_interpreter} {{prompt}} {{out}}"
            argv = [
                str(SCRIPT), str(raw), "--out", str(output), "--transcript", str(transcript),
                "--interpret-cmd", command, "--interpret-planned-frames",
            ]

            def fake_extract_frame(_video: Path, timestamp: float, path: Path) -> None:
                self.assertEqual(timestamp, 7.0)
                path.write_bytes(b"frame")

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(minutes, "ffprobe_duration", return_value=20.0), \
                 mock.patch.object(minutes, "extract_frame", side_effect=fake_extract_frame):
                self.assertEqual(minutes.main(), 0)

            run_dir = next(path for path in output.iterdir() if path.is_dir())
            prompt = json.loads((run_dir / "interpret_prompt.json").read_text(encoding="utf-8"))
            self.assertNotIn("frames", prompt)
            self.assertTrue((run_dir / "images/frame_0001.jpg").exists())
            self.assertIn("![frame_0001](images/frame_0001.jpg)", (run_dir / "minutes.md").read_text(encoding="utf-8"))

    def test_external_interpreter_failure_falls_back_to_default_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw.mp4"
            raw.touch()
            transcript = root / "transcript.json"
            transcript.write_text(
                json.dumps({"segments": [{"start": 2, "end": 3, "text": "代替議事録"}]}),
                encoding="utf-8",
            )
            output = root / "minutes"
            argv = [
                str(SCRIPT), str(raw), "--out", str(output), "--transcript", str(transcript),
                "--interpret-cmd", "false {prompt} {out}", "--interpret-planned-frames",
            ]
            fallback_frames = [{"frame_id": "fallback", "timestamp": 2.0, "path": "images/fallback.jpg", "reason": "interval"}]

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(minutes, "ffprobe_duration", return_value=5.0), \
                 mock.patch.object(minutes, "extract_interval_frames", return_value=fallback_frames), \
                 mock.patch("sys.stderr") as stderr:
                self.assertEqual(minutes.main(), 0)

            self.assertIn("warning: interpretation failed", "".join(call.args[0] for call in stderr.write.call_args_list if call.args))
            run_dir = next(path for path in output.iterdir() if path.is_dir())
            markdown = (run_dir / "minutes.md").read_text(encoding="utf-8")
            self.assertIn("代替議事録", markdown)
            self.assertIn("![fallback](images/fallback.jpg)", markdown)

    def test_openai_interpreter_failure_falls_back_to_default_sections(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "raw.mp4"
            raw.touch()
            transcript = root / "transcript.json"
            transcript.write_text(
                json.dumps({"segments": [{"start": 0, "end": 1, "text": "OpenAI代替"}]}),
                encoding="utf-8",
            )
            output = root / "minutes"
            argv = [str(SCRIPT), str(raw), "--out", str(output), "--transcript", str(transcript), "--openai-interpret"]

            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(minutes, "ffprobe_duration", return_value=5.0), \
                 mock.patch.object(minutes, "interpret_openai", side_effect=RuntimeError("API unavailable")), \
                 mock.patch.object(minutes, "extract_interval_frames", return_value=[]), \
                 mock.patch("sys.stderr") as stderr:
                self.assertEqual(minutes.main(), 0)

            self.assertIn("warning: interpretation failed", "".join(call.args[0] for call in stderr.write.call_args_list if call.args))
            run_dir = next(path for path in output.iterdir() if path.is_dir())
            self.assertIn("OpenAI代替", (run_dir / "minutes.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
