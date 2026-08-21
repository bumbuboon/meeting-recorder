#!/usr/bin/env python3
"""Run Kanary CLI and write whisper-compatible transcript JSON."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave


MIN_TIMEOUT_SECONDS = 120.0


def audio_duration_seconds(audio_path: Path) -> float:
    """Return audio duration, using stdlib for WAV and ffprobe otherwise."""
    try:
        with wave.open(str(audio_path), "rb") as audio:
            frame_rate = audio.getframerate()
            if frame_rate <= 0:
                raise ValueError("WAV frame rate must be positive")
            return audio.getnframes() / frame_rate
    except (wave.Error, EOFError):
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(result.stdout.strip())


def convert_kanary_payload(payload: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(payload, dict):
        raise ValueError("Kanary output must be a JSON object")
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != 3:
        raise ValueError(f"unsupported Kanary schema_version: {schema_version!r}")

    transcript = payload.get("transcript")
    if not isinstance(transcript, dict):
        raise ValueError("Kanary output is missing transcript object")
    source_segments = transcript.get("segments")
    if not isinstance(source_segments, list):
        raise ValueError("Kanary output transcript.segments must be an array")

    segments: list[dict[str, object]] = []
    for index, source in enumerate(source_segments):
        if not isinstance(source, dict):
            raise ValueError(f"segment {index} must be an object")
        start = source.get("start_seconds")
        end = source.get("end_seconds")
        text = source.get("text")
        if isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start):
            raise ValueError(f"segment {index} has invalid start_seconds")
        if isinstance(end, bool) or not isinstance(end, (int, float)) or not math.isfinite(end):
            raise ValueError(f"segment {index} has invalid end_seconds")
        if start < 0 or end < start:
            raise ValueError(f"segment {index} has invalid time range")
        if not isinstance(text, str):
            raise ValueError(f"segment {index} has invalid text")
        segments.append({"start": float(start), "end": float(end), "text": text})
    return {"segments": segments}


def temporary_output_path(output_path: Path, suffix: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=suffix, dir=output_path.parent
    )
    os.close(descriptor)
    path = Path(raw_path)
    path.unlink()
    return path


def transcribe(audio_path: Path, output_path: Path) -> None:
    if not audio_path.is_file():
        raise ValueError(f"audio file does not exist: {audio_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    duration = audio_duration_seconds(audio_path)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f"audio duration must be positive: {duration!r}")
    timeout_seconds = max(duration * 2, MIN_TIMEOUT_SECONDS)

    kanary_output = temporary_output_path(output_path, ".kanary.tmp")
    converted_output = temporary_output_path(output_path, ".converted.tmp")
    try:
        subprocess.run(
            ["kanary", "transcribe", str(audio_path), "--out", str(kanary_output)],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        with kanary_output.open(encoding="utf-8") as handle:
            converted = convert_kanary_payload(json.load(handle))
        converted_output.write_text(
            json.dumps(converted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(converted_output, output_path)
    finally:
        kanary_output.unlink(missing_ok=True)
        converted_output.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (2, 3):
        print("usage: kanary_transcribe.py <audio> <out.json> [ignored-model]", file=sys.stderr)
        return 64
    try:
        transcribe(Path(args[0]).expanduser().resolve(), Path(args[1]).expanduser().resolve())
    except subprocess.TimeoutExpired as error:
        print(f"kanary transcription timed out after {error.timeout} seconds", file=sys.stderr)
        return 124
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip()
        print(f"kanary transcription failed with exit {error.returncode}: {detail}", file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"kanary transcription failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
