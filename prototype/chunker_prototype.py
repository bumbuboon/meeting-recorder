#!/usr/bin/env python3
"""Independent Phase 2 WAV chunker/Kanary measurement prototype.

All generated media and transcripts are confined to a caller-supplied /tmp
directory.  The source recording is opened read-only.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Any, Iterable


SCHEMA_VERSION = 3


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    chunk_id: int | None = None


@dataclass(frozen=True)
class ChunkSpec:
    chunk_id: int
    start_abs: float
    end_abs: float
    dedup_boundary: float
    path: str


def run_command(command: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def require_tmp_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    tmp_root = Path("/tmp").resolve()
    if resolved == tmp_root or tmp_root not in resolved.parents:
        raise ValueError(f"work directory must be below /tmp: {resolved}")
    return resolved


def probe_media(path: Path) -> tuple[float, float]:
    result = run_command([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=start_time,duration", "-of", "json", str(path),
    ], timeout=30)
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise ValueError(f"no audio stream: {path}")
    stream = streams[0]
    start = float(stream.get("start_time") or 0.0)
    duration = float(stream.get("duration") or 0.0)
    if duration <= 0 or not math.isfinite(duration):
        raise ValueError(f"invalid audio duration: {duration!r}")
    return start, start + duration


def prepare_canonical_audio(source: Path, output: Path) -> dict[str, float]:
    """Create 16 kHz mono PCM aligned to the raw session origin (t=0)."""
    audio_start, timeline_end = probe_media(source)
    delay_ms = max(0, round(audio_start * 1000))
    output.parent.mkdir(parents=True, exist_ok=True)
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
        "-map", "0:a:0", "-af",
        f"asetpts=PTS-STARTPTS,adelay={delay_ms}:all=1,apad,atrim=0:{timeline_end:.6f}",
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(output),
    ], timeout=max(120.0, timeline_end * 0.5))
    return {"source_audio_start": audio_start, "timeline_duration": timeline_end}


def build_chunk_plan(
    duration: float,
    output_dir: Path,
    *,
    hop: float = 60.0,
    overlap: float = 2.0,
    pts_drift_seconds: float = 0.0,
) -> list[ChunkSpec]:
    if duration <= 0 or hop <= 0 or overlap < 0 or overlap >= hop:
        raise ValueError("duration/hop/overlap values are invalid")
    count = max(1, math.ceil(duration / hop))
    chunks: list[ChunkSpec] = []
    for chunk_id in range(count):
        drift = 0.0 if chunk_id == 0 else pts_drift_seconds
        theoretical_start = 0.0 if chunk_id == 0 else chunk_id * hop - overlap
        start = max(0.0, theoretical_start + drift)
        end = min(duration, (chunk_id + 1) * hop + drift)
        if end <= start:
            continue
        chunks.append(ChunkSpec(
            chunk_id=chunk_id,
            start_abs=start,
            end_abs=end,
            dedup_boundary=chunk_id * hop,
            path=str(output_dir / f"chunk_{chunk_id:04d}.wav"),
        ))
    return chunks


def extract_chunks(canonical_audio: Path, chunks: Iterable[ChunkSpec]) -> None:
    for chunk in chunks:
        output = Path(chunk.path)
        if output.is_file():
            continue
        output.parent.mkdir(parents=True, exist_ok=True)
        run_command([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", f"{chunk.start_abs:.6f}", "-i", str(canonical_audio),
            "-t", f"{chunk.end_abs - chunk.start_abs:.6f}",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(output),
        ], timeout=120)


def validate_kanary(payload: Any) -> list[Segment]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"expected Kanary schema_version {SCHEMA_VERSION}")
    transcript = payload.get("transcript")
    raw_segments = transcript.get("segments") if isinstance(transcript, dict) else None
    if not isinstance(raw_segments, list):
        raise ValueError("missing transcript.segments")
    segments: list[Segment] = []
    for index, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            raise ValueError(f"segment {index} is not an object")
        start = float(item["start_seconds"])
        end = float(item["end_seconds"])
        text = item["text"]
        if not isinstance(text, str) or start < 0 or end < start:
            raise ValueError(f"invalid segment {index}")
        segments.append(Segment(start, end, text))
    return segments


def load_kanary(path: Path) -> list[Segment]:
    with path.open(encoding="utf-8") as handle:
        return validate_kanary(json.load(handle))


def transcribe_one(audio: Path, output: Path, *, timeout: float, resume: bool) -> dict[str, Any]:
    if resume and output.is_file():
        load_kanary(output)
        return {"elapsed_seconds": 0.0, "cached": True}
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    Path(temporary).unlink()
    started = time.monotonic()
    try:
        run_command(["kanary", "transcribe", str(audio), "--out", temporary], timeout=timeout)
        load_kanary(Path(temporary))
        os.replace(temporary, output)
        return {"elapsed_seconds": time.monotonic() - started, "cached": False}
    finally:
        Path(temporary).unlink(missing_ok=True)


def transcribe_chunks(
    chunks: list[ChunkSpec], transcript_dir: Path, *, jobs: int, resume: bool
) -> tuple[list[dict[str, Any]], float]:
    def task(chunk: ChunkSpec) -> dict[str, Any]:
        timing = transcribe_one(
            Path(chunk.path), transcript_dir / f"chunk_{chunk.chunk_id:04d}.kanary.json",
            timeout=max(120.0, (chunk.end_abs - chunk.start_abs) * 2), resume=resume,
        )
        return {
            "chunk_id": chunk.chunk_id,
            "audio_duration_seconds": chunk.end_abs - chunk.start_abs,
            **timing,
        }

    started = time.monotonic()
    timings: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {executor.submit(task, chunk): chunk.chunk_id for chunk in chunks}
        for future in as_completed(futures):
            timings.append(future.result())
    return sorted(timings, key=lambda item: item["chunk_id"]), time.monotonic() - started


def merge_chunks(chunks: list[ChunkSpec], transcript_dir: Path) -> tuple[list[Segment], list[dict[str, Any]]]:
    merged: list[Segment] = []
    decisions: list[dict[str, Any]] = []
    for chunk in sorted(chunks, key=lambda item: item.chunk_id):
        relative = load_kanary(transcript_dir / f"chunk_{chunk.chunk_id:04d}.kanary.json")
        for segment in relative:
            global_segment = Segment(
                start=chunk.start_abs + segment.start,
                end=chunk.start_abs + segment.end,
                text=segment.text,
                chunk_id=chunk.chunk_id,
            )
            dropped = chunk.chunk_id > 0 and global_segment.end <= chunk.dedup_boundary
            decisions.append({
                **asdict(global_segment),
                "dedup_boundary": chunk.dedup_boundary,
                "decision": "drop" if dropped else "keep",
            })
            if not dropped:
                merged.append(global_segment)
    return merged, decisions


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(character for character in normalized if not character.isspace() and not unicodedata.category(character).startswith("P"))


def joined_text(segments: Iterable[Segment]) -> str:
    return "".join(segment.text for segment in segments)


def alignment_metrics(reference: str, candidate: str) -> dict[str, Any]:
    reference_normalized = normalize_text(reference)
    candidate_normalized = normalize_text(candidate)
    # The full 78-minute meeting is large enough that disabling SequenceMatcher's
    # popular-character filter can become quadratic on repeated Japanese text.
    matcher = SequenceMatcher(None, reference_normalized, candidate_normalized, autojunk=True)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    missing = len(reference_normalized) - matched
    extra = len(candidate_normalized) - matched
    return {
        "method": "NFKC normalized-character SequenceMatcher matching blocks (autojunk enabled for full-meeting scalability)",
        "reference_characters": len(reference_normalized),
        "candidate_characters": len(candidate_normalized),
        "matched_characters": matched,
        "missing_characters": missing,
        "extra_characters": extra,
        "missing_rate": missing / max(1, len(reference_normalized)),
        "duplicate_or_extra_rate": extra / max(1, len(reference_normalized)),
        "note": "extra characters are a conservative proxy and may include recognition substitutions, not only overlap duplicates",
    }


def segments_in_window(segments: Iterable[Segment], start: float, end: float) -> list[Segment]:
    return [segment for segment in segments if segment.end > start and segment.start < end]


def boundary_analysis(
    reference: list[Segment], candidate: list[Segment], duration: float, *, hop: float, window: float
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    boundary = hop
    while boundary < duration:
        full_window = segments_in_window(reference, boundary - window, boundary + window)
        chunk_window = segments_in_window(candidate, boundary - window, boundary + window)
        whole_crossing = sum(item.start < boundary < item.end for item in full_window)
        chunked_crossing = sum(item.start < boundary < item.end for item in chunk_window)
        chunk_ids = sorted({item.chunk_id for item in chunk_window if item.chunk_id is not None})
        if whole_crossing and len(chunk_ids) >= 2:
            fragmentation = "whole reference crosses boundary; chunk result contains segments from both adjacent chunks"
        elif whole_crossing and not chunked_crossing:
            fragmentation = "whole reference crosses boundary; no merged segment crosses it (boundary split observed)"
        elif whole_crossing:
            fragmentation = "whole and merged result both contain a boundary-crossing segment"
        else:
            fragmentation = "whole reference has no boundary-crossing segment in this window"
        results.append({
            "boundary_seconds": boundary,
            "window_seconds": [boundary - window, boundary + window],
            "whole_segments": [asdict(item) for item in full_window],
            "chunked_segments": [asdict(item) for item in chunk_window],
            "whole_crossing_segment_count": whole_crossing,
            "chunked_crossing_segment_count": chunked_crossing,
            "chunk_ids_present": chunk_ids,
            "fragmentation_observation": fragmentation,
            "window_alignment": alignment_metrics(joined_text(full_window), joined_text(chunk_window)),
        })
        boundary += hop
    return results


def compatible_segments(segments: Iterable[Segment]) -> list[dict[str, Any]]:
    return [asdict(segment) for segment in segments]


def measure(
    whole: list[Segment], merged: list[Segment], decisions: list[dict[str, Any]], duration: float,
    *, hop: float, overlap: float, pts_drift_seconds: float, boundary_window: float,
) -> dict[str, Any]:
    dropped = [item for item in decisions if item["decision"] == "drop"]
    return {
        "configuration": {
            "hop_seconds": hop,
            "forward_overlap_seconds": overlap,
            "pts_drift_seconds_after_chunk_0": pts_drift_seconds,
            "global_time_rule": "global = start_abs + rel",
            "dedup_rule": "for chunk i, drop segment when global end <= i * hop",
        },
        "overall_alignment": alignment_metrics(joined_text(whole), joined_text(merged)),
        "segment_counts": {"whole": len(whole), "merged": len(merged), "dedup_dropped": len(dropped)},
        "boundary_windows": boundary_analysis(whole, merged, duration, hop=hop, window=boundary_window),
    }


def make_fixture(source_audio: Path, output: Path, *, source_start: float, clip_duration: float, at: float, duration: float) -> None:
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{source_start:.3f}", "-t", f"{clip_duration:.3f}", "-i", str(source_audio),
        "-af", f"adelay={round(at * 1000)}:all=1,apad,atrim=0:{duration:.3f}",
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(output),
    ], timeout=120)


def make_one_sided_fixture(
    source_audio: Path, output: Path, *, source_start: float, silent_side: str, duration: float = 12.0
) -> None:
    speech_label, silence_label = ("FR", "FL") if silent_side == "left" else ("FL", "FR")
    filter_graph = (
        f"[0:a]atrim=0:{duration},asetpts=PTS-STARTPTS[speech];"
        f"anullsrc=r=16000:cl=mono,atrim=0:{duration}[silence];"
        f"[speech]pan=stereo|{speech_label}=c0[s];"
        f"[silence]pan=stereo|{silence_label}=c0[z];[s][z]amix=inputs=2:normalize=0[out]"
    )
    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{source_start:.3f}", "-t", f"{duration:.3f}", "-i", str(source_audio),
        "-filter_complex", filter_graph, "-map", "[out]", "-ar", "16000",
        "-c:a", "pcm_s16le", str(output),
    ], timeout=120)


def command_fixtures(args: argparse.Namespace) -> int:
    source = args.input.expanduser().resolve()
    work = require_tmp_directory(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    canonical = work / "source-canonical.wav"
    source_metadata = prepare_canonical_audio(source, canonical)
    cases = [
        ("boundary_59.wav", 56.0, 67.0, "same 6 s speech excerpt centered on t=59"),
        ("boundary_60.wav", 57.0, 68.0, "same 6 s speech excerpt centered on t=60"),
        ("boundary_61.wav", 58.0, 69.0, "same 6 s speech excerpt centered on t=61"),
        ("final_partial.wav", 67.0, 73.0, "73 s total; final chunk is partial"),
        ("pts_drift.wav", 57.0, 125.0, "run with --pts-drift-ms 125"),
    ]
    fixture_records: list[dict[str, Any]] = []
    for name, at, duration, purpose in cases:
        output = work / name
        make_fixture(canonical, output, source_start=args.source_start, clip_duration=6.0, at=at, duration=duration)
        fixture_records.append({"name": name, "duration": duration, "purpose": purpose})
    for side in ("left", "right"):
        name = f"{side}_silent.wav"
        make_one_sided_fixture(canonical, work / name, source_start=args.source_start, silent_side=side)
        fixture_records.append({"name": name, "duration": 12.0, "purpose": f"{side} channel silent"})
    manifest = {
        "source": str(source),
        "source_metadata": source_metadata,
        "source_excerpt_start": args.source_start,
        "fixtures": fixture_records,
    }
    atomic_json(work / "fixtures.json", manifest)
    print(work / "fixtures.json")
    return 0


def command_run(args: argparse.Namespace) -> int:
    source = args.input.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"input does not exist: {source}")
    work = require_tmp_directory(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    canonical = work / "canonical.wav"
    metadata_path = work / "source.json"
    if args.resume and canonical.is_file() and metadata_path.is_file():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    else:
        metadata = {"source": str(source), **prepare_canonical_audio(source, canonical)}
        atomic_json(metadata_path, metadata)
    duration = float(metadata["timeline_duration"])
    pts_drift = args.pts_drift_ms / 1000.0
    chunks = build_chunk_plan(
        duration, work / "audio-chunks", hop=args.hop, overlap=args.overlap,
        pts_drift_seconds=pts_drift,
    )
    atomic_json(work / "chunks.json", {"chunks": [asdict(item) for item in chunks]})
    extract_chunks(canonical, chunks)
    transcript_dir = work / "transcripts"
    chunk_timings, chunk_wall_time = transcribe_chunks(
        chunks, transcript_dir, jobs=args.jobs, resume=args.resume
    )
    timing_report: dict[str, Any] = {
        "parallel_jobs": args.jobs,
        "chunk_wall_seconds": chunk_wall_time,
        "chunks": chunk_timings,
    }
    merged, decisions = merge_chunks(chunks, transcript_dir)
    atomic_json(work / "merged-transcript.json", {"segments": compatible_segments(merged)})
    atomic_json(work / "dedup-decisions.json", {"segments": decisions})
    if args.skip_whole:
        atomic_json(work / "transcription-timings.json", timing_report)
        print(work / "merged-transcript.json")
        return 0
    whole_path = args.whole_transcript.expanduser().resolve() if args.whole_transcript else work / "whole.kanary.json"
    if args.whole_transcript is None:
        timing_report["whole"] = transcribe_one(
            canonical, whole_path, timeout=max(120.0, duration * 2), resume=args.resume
        )
    else:
        timing_report["whole"] = {"elapsed_seconds": 0.0, "cached": True, "provided": True}
    timing_report["whole"]["audio_duration_seconds"] = duration
    atomic_json(work / "transcription-timings.json", timing_report)
    whole = load_kanary(whole_path)
    report = measure(
        whole, merged, decisions, duration, hop=args.hop, overlap=args.overlap,
        pts_drift_seconds=pts_drift, boundary_window=args.boundary_window,
    )
    report["artifacts"] = {
        "source": str(source), "workdir": str(work), "whole_transcript": str(whole_path),
        "merged_transcript": str(work / "merged-transcript.json"),
        "dedup_decisions": str(work / "dedup-decisions.json"),
        "transcription_timings": str(work / "transcription-timings.json"),
    }
    atomic_json(work / "measurements.json", report)
    print(work / "measurements.json")
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__)
    subcommands = top.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="chunk, transcribe, merge, and compare")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--workdir", type=Path, required=True, help="must be below /tmp")
    run.add_argument("--hop", type=float, default=60.0)
    run.add_argument("--overlap", type=float, default=2.0)
    run.add_argument("--jobs", type=int, default=3)
    run.add_argument("--pts-drift-ms", type=float, default=0.0)
    run.add_argument("--boundary-window", type=float, default=5.0)
    run.add_argument("--whole-transcript", type=Path, help="reuse an existing schema v3 Kanary whole-file JSON")
    run.add_argument("--skip-whole", action="store_true", help="stop after deterministic merge")
    run.add_argument("--resume", action="store_true", help="reuse validated Kanary outputs")
    run.set_defaults(function=command_run)
    fixtures = subcommands.add_parser("make-fixtures", help="make reproducible boundary/silence/drift fixtures")
    fixtures.add_argument("--input", type=Path, required=True)
    fixtures.add_argument("--workdir", type=Path, required=True, help="must be below /tmp")
    fixtures.add_argument("--source-start", type=float, default=300.0)
    fixtures.set_defaults(function=command_fixtures)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if getattr(args, "jobs", 1) < 1:
        raise ValueError("--jobs must be >= 1")
    return args.function(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        print(f"chunker prototype failed: command exited {error.returncode}{suffix}", file=sys.stderr)
        raise SystemExit(1)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(f"chunker prototype failed: {error}", file=sys.stderr)
        raise SystemExit(1)
