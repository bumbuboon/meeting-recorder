from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "chunker_prototype.py"
SPEC = importlib.util.spec_from_file_location("chunker_prototype", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
chunker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = chunker
SPEC.loader.exec_module(chunker)


class ChunkPlanTest(unittest.TestCase):
    def test_forward_overlap_and_final_partial(self) -> None:
        chunks = chunker.build_chunk_plan(133.0, Path("/tmp/chunks"))
        self.assertEqual([(c.start_abs, c.end_abs) for c in chunks], [
            (0.0, 60.0), (58.0, 120.0), (118.0, 133.0),
        ])

    def test_pts_drift_changes_physical_start_and_manifest(self) -> None:
        chunks = chunker.build_chunk_plan(125.0, Path("/tmp/chunks"), pts_drift_seconds=0.125)
        self.assertEqual(chunks[1].start_abs, 58.125)
        self.assertEqual(chunks[1].end_abs, 120.125)
        self.assertEqual(chunks[1].dedup_boundary, 60.0)

    def test_workdir_must_be_under_tmp(self) -> None:
        with self.assertRaises(ValueError):
            chunker.require_tmp_directory(Path("/Users/example/output"))
        self.assertTrue(str(chunker.require_tmp_directory(Path("/tmp/example"))).startswith("/private/tmp/"))


class MergeTest(unittest.TestCase):
    def test_dedup_boundary_is_less_than_or_equal(self) -> None:
        chunks = [
            chunker.ChunkSpec(0, 0.0, 60.0, 0.0, "/tmp/chunk0.wav"),
            chunker.ChunkSpec(1, 58.0, 120.0, 60.0, "/tmp/chunk1.wav"),
        ]
        original = chunker.load_kanary

        def fake_load(path: Path):
            if "0000" in path.name:
                return [chunker.Segment(59.0, 60.0, "previous")]
            return [
                chunker.Segment(0.0, 2.0, "equal"),       # global end == 60
                chunker.Segment(0.5, 2.001, "crossing"),  # global end > 60
                chunker.Segment(2.5, 3.0, "after"),
            ]

        chunker.load_kanary = fake_load
        try:
            merged, decisions = chunker.merge_chunks(chunks, Path("/tmp/transcripts"))
        finally:
            chunker.load_kanary = original
        self.assertEqual([item.text for item in merged], ["previous", "crossing", "after"])
        equal = next(item for item in decisions if item["text"] == "equal")
        self.assertEqual(equal["decision"], "drop")


class MeasurementTest(unittest.TestCase):
    def test_alignment_reports_missing_and_extra(self) -> None:
        metrics = chunker.alignment_metrics("ABCDEF", "ABXDEFZ")
        self.assertEqual(metrics["missing_characters"], 1)
        self.assertEqual(metrics["extra_characters"], 2)

    def test_boundary_window_records_fragmentation(self) -> None:
        whole = [chunker.Segment(59.0, 61.0, "whole")]
        merged = [
            chunker.Segment(59.0, 60.2, "part1", 0),
            chunker.Segment(60.0, 61.0, "part2", 1),
        ]
        result = chunker.boundary_analysis(whole, merged, 90.0, hop=60.0, window=5.0)
        self.assertEqual(result[0]["whole_crossing_segment_count"], 1)
        self.assertEqual(result[0]["chunked_crossing_segment_count"], 1)
        self.assertEqual(result[0]["chunk_ids_present"], [0, 1])


if __name__ == "__main__":
    unittest.main()
