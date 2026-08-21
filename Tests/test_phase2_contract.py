#!/usr/bin/env python3
"""Source-level guards for the Phase 2 stop and durability ordering contract."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "Sources/MeetingRecorder/main.swift").read_text(encoding="utf-8")
CHUNKER = (ROOT / "Sources/MeetingRecorder/AudioChunker.swift").read_text(encoding="utf-8")


class Phase2ContractTest(unittest.TestCase):
    def test_stop_order_is_capture_barrier_chunk_drain_raw_finalize_ack(self) -> None:
        finish = MAIN[MAIN.index("func finish(exitCode:"):MAIN.index("private func validateFinalizedOutput")]
        ordered = [
            "stream.stopCapture()",
            "queue.sync",
            "audioChunker?.stopAndDrain()",
            "writer.finishWriting()",
            "transcriberWorker.waitForDone(timeoutSeconds: 300)",
            "schedulePostProcess(exitCode)",
        ]
        positions = [finish.index(token) for token in ordered]
        self.assertEqual(positions, sorted(positions))

    def test_end_is_created_after_join_even_when_chunking_failed(self) -> None:
        drain = CHUNKER[CHUNKER.index("func stopAndDrain()") : CHUNKER.index("private func consume()")]
        self.assertLess(drain.index("stopped.wait()"), drain.index('createAtomicSentinel(named: "END")'))
        self.assertNotIn("if failure == nil", drain)

    def test_wav_rename_precedes_ready_event(self) -> None:
        finalize = CHUNKER[CHUNKER.index("private func finalize(id:") : CHUNKER.index("private func recordDrop")]
        self.assertLess(finalize.index("rename(temporary.path, destination.path)"), finalize.index('events.append(event: "chunk_ready"'))

    def test_source_watermarks_gate_live_finalization(self) -> None:
        self.assertIn("maximumFrameBySource", CHUNKER)
        self.assertIn("min(systemWatermark, microphoneWatermark)", CHUNKER)
        self.assertIn("CMSampleBufferGetPresentationTimeStamp(pending[$0].sampleBuffer)", CHUNKER)


if __name__ == "__main__":
    unittest.main()
