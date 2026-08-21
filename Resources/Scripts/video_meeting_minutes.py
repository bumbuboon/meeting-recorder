#!/usr/bin/env python3
"""Create screenshot-backed Markdown minutes from a meeting video."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def run_template(template: str, mapping: dict[str, Path]) -> None:
    parts = shlex.split(template.format(**{k: str(v) for k, v in mapping.items()}))
    if not parts:
        raise SystemExit("Empty command template")
    run(parts)


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return value[:80] or "meeting"


def ffprobe_duration(video: Path) -> float:
    output = subprocess.check_output(
        [
            "ffprobe",
            "-hide_banner",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        text=True,
    ).strip()
    return float(output)


def seconds_to_stamp(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def coerce_seconds(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return default
    text = str(value).strip()
    match = re.search(r"\d+(?:\.\d+)?(?::\d+(?:\.\d+)?){0,2}", text)
    if not match:
        return default
    try:
        return parse_stamp(match.group(0))
    except Exception:
        return default


def parse_stamp(value: str) -> float:
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        h, m, s = parts
    elif len(parts) == 2:
        h, m, s = "0", parts[0], parts[1]
    else:
        return float(value)
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_srt_or_vtt(text: str) -> list[dict[str, Any]]:
    text = text.replace("\ufeff", "")
    blocks = re.split(r"\n\s*\n", text.strip(), flags=re.MULTILINE)
    segments: list[dict[str, Any]] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        start_raw, end_raw = [part.strip().split(" ")[0] for part in lines[timing_index].split("-->", 1)]
        body = " ".join(lines[timing_index + 1 :])
        if body:
            segments.append(
                {
                    "start": parse_stamp(start_raw),
                    "end": parse_stamp(end_raw),
                    "speaker": "",
                    "text": html.unescape(re.sub(r"<[^>]+>", "", body)),
                }
            )
    return segments


def normalize_segment(item: Any, index: int) -> dict[str, Any]:
    if isinstance(item, str):
        return {"start": float(index), "end": float(index + 1), "speaker": "", "text": item}
    if not isinstance(item, dict):
        return {"start": float(index), "end": float(index + 1), "speaker": "", "text": str(item)}
    start = item.get("start", item.get("start_time", item.get("start_seconds", item.get("timestamp", index))))
    start_seconds = coerce_seconds(start)
    end = item.get("end", item.get("end_time", item.get("end_seconds", start_seconds + 1)))
    text = item.get("text", item.get("transcript", item.get("utterance", "")))
    speaker = item.get("speaker", item.get("speaker_name", item.get("participant", item.get("channel", ""))))
    return {"start": start_seconds, "end": coerce_seconds(end), "speaker": str(speaker or ""), "text": str(text or "").strip()}


def load_transcript(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in {".srt", ".vtt"}:
        return parse_srt_or_vtt(raw)
    if suffix == ".json":
        data = json.loads(raw)
        if isinstance(data, dict):
            transcript = data.get("transcript") if isinstance(data.get("transcript"), dict) else {}
            candidates = data.get("segments") or transcript.get("segments") or data.get("utterances") or data.get("results") or data.get("items") or []
        else:
            candidates = data
        normalized = [normalize_segment(item, i) for i, item in enumerate(candidates)]
        return [item for item in normalized if item["text"]]
    if suffix == ".csv":
        rows = csv.DictReader(raw.splitlines())
        return [normalize_segment(row, i) for i, row in enumerate(rows)]
    paragraphs = [line.strip() for line in raw.splitlines() if line.strip()]
    return [{"start": i * 30.0, "end": (i + 1) * 30.0, "speaker": "", "text": line} for i, line in enumerate(paragraphs)]


def init_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table if not exists runs (
          id text primary key,
          video_path text not null,
          output_dir text not null,
          created_at text not null,
          duration_seconds real
        );
        create table if not exists transcript_segments (
          run_id text not null,
          idx integer not null,
          start real not null,
          end real not null,
          speaker text,
          text text not null,
          primary key (run_id, idx)
        );
        create table if not exists frames (
          run_id text not null,
          frame_id text not null,
          timestamp real not null,
          path text not null,
          reason text not null,
          primary key (run_id, frame_id)
        );
        create table if not exists minute_sections (
          run_id text not null,
          idx integer not null,
          title text not null,
          timestamp real,
          frame_id text,
          summary text,
          bullets_json text,
          primary key (run_id, idx)
        );
        """
    )
    return conn


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def load_default_env() -> None:
    for path in (Path.cwd() / ".env.local", Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env.local", Path(__file__).resolve().parents[3] / ".env"):
        load_env_file(path)


def openai_key() -> str:
    load_default_env()
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise SystemExit("OPENAI_API_KEY is not set")
    return key


def openai_json_request(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.openai.com{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {openai_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error {exc.code}: {detail}") from exc


def openai_multipart_request(path: str, fields: dict[str, str], files: dict[str, Path]) -> dict[str, Any]:
    boundary = f"----video-meeting-minutes-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for name, path_value in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{path_value.name}"\r\n'.encode(),
                b"Content-Type: application/octet-stream\r\n\r\n",
                path_value.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    body = b"".join(chunks)
    req = urllib.request.Request(
        f"https://api.openai.com{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {openai_key()}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error {exc.code}: {detail}") from exc


def transcribe_openai(audio_path: Path, transcript_path: Path, model: str, fallback_model: str | None = None) -> list[dict[str, Any]]:
    fields = {"model": model, "response_format": "verbose_json", "timestamp_granularities[]": "segment"}
    try:
        data = openai_multipart_request("/v1/audio/transcriptions", fields, {"file": audio_path})
        used_model = model
    except RuntimeError:
        if not fallback_model:
            raise
        fields["model"] = fallback_model
        data = openai_multipart_request("/v1/audio/transcriptions", fields, {"file": audio_path})
        used_model = fallback_model
    candidates = data.get("segments") or []
    if candidates:
        segments = [normalize_segment(item, i) for i, item in enumerate(candidates) if normalize_segment(item, i)["text"]]
    else:
        text = str(data.get("text") or "").strip()
        segments = [{"start": 0.0, "end": 1.0, "speaker": "", "text": text}] if text else []
    transcript_path.write_text(json.dumps({"segments": segments, "model_used": used_model, "raw": data}, ensure_ascii=False, indent=2), encoding="utf-8")
    return segments


def extract_audio(video: Path, audio: Path) -> None:
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)])


def extract_frame(video: Path, timestamp: float, path: Path) -> None:
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-update",
            "1",
            "-q:v",
            "2",
            str(path),
        ]
    )


def choose_timestamps(duration: float, segments: list[dict[str, Any]], interval: float) -> list[tuple[float, str]]:
    points: dict[int, str] = {}
    last_frame_time = max(0, int(duration - 0.25))
    t = 0.0
    while t <= last_frame_time:
        points[int(t)] = "interval"
        t += interval
    for seg in segments:
        text = seg["text"]
        keywords = ("共有", "スライド", "画面", "デモ", "決定", "TODO", "宿題", "重要", "確認", "論点")
        if any(word in text for word in keywords):
            points[int(max(0, seg["start"]))] = "transcript-cue"
    return [(float(k), v) for k, v in sorted(points.items()) if k <= last_frame_time]


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def default_sections(segments: list[dict[str, Any]], frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not segments:
        return [{"title": "Meeting", "timestamp": 0, "summary": "Transcript was not available.", "bullets": [], "frame_id": frames[0]["frame_id"] if frames else None}]
    sections: list[dict[str, Any]] = []
    bucket_size = 8
    for i in range(0, len(segments), bucket_size):
        bucket = segments[i : i + bucket_size]
        start = bucket[0]["start"]
        nearby_frame = min(frames, key=lambda f: abs(f["timestamp"] - start)) if frames else None
        title_text = bucket[0]["text"][:40].strip(" 。、,.") or f"Section {len(sections) + 1}"
        sections.append(
            {
                "title": title_text,
                "timestamp": start,
                "frame_id": nearby_frame["frame_id"] if nearby_frame else None,
                "summary": " ".join(seg["text"] for seg in bucket)[:700],
                "bullets": [
                    f"{seg['speaker'] + ': ' if seg['speaker'] else ''}{seg['text']}"
                    for seg in bucket[:5]
                ],
            }
        )
    return sections


def interpret_sections(command: str, prompt_path: Path, out_path: Path) -> dict[str, Any]:
    run_template(command, {"prompt": prompt_path, "out": out_path})
    data = json.loads(out_path.read_text(encoding="utf-8"))
    sections = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(sections, list):
        raise ValueError("Interpretation output must be an object with sections")
    return data


def extract_response_text(data: dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    parts: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def parse_json_text(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def interpret_openai(model: str, prompt_path: Path, out_path: Path, fallback_model: str | None = None, *, planned_frames: bool = False) -> list[dict[str, Any]]:
    prompt = prompt_path.read_text(encoding="utf-8")
    if planned_frames:
        instruction = (
            "You create Japanese meeting minutes from transcript segments only. "
            "Do not assume OCR or image understanding. Return only valid JSON with a top-level sections array. "
            "Each section must include title, timestamp, capture_timestamp, summary, and bullets. "
            "timestamp is the start time of the topic. capture_timestamp is the exact video second where a useful screenshot should be taken, inferred from the transcript only. "
            "Choose capture_timestamp near topic changes, slide/screen changes, demonstrations, important claims, decisions, or action items. "
            "Split sections at actual topic boundaries and scale the section count to the meeting length: "
            "as a guideline, roughly one section per 5 to 10 minutes of meeting (e.g. a 60 minute meeting yields about 6 to 12 sections), "
            "with more sections when topics change quickly and fewer when one topic dominates. "
            "Use numeric seconds, not ranges."
        )
    else:
        instruction = (
            "You create Japanese meeting minutes from transcript segments and candidate video frames. "
            "Return only valid JSON with a top-level sections array. "
            "Each section must include title, timestamp, frame_id, summary, and bullets. "
            "Use frame_id values only from the input. Prefer important decisions, action items, topic shifts, screen-share or slide changes."
        )
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": prompt},
        ],
        "text": {"format": {"type": "json_object"}},
    }
    try:
        data = openai_json_request("/v1/responses", payload)
        used_model = model
    except RuntimeError:
        if not fallback_model:
            raise
        payload["model"] = fallback_model
        data = openai_json_request("/v1/responses", payload)
        used_model = fallback_model
    text = extract_response_text(data)
    parsed = parse_json_text(text)
    if isinstance(parsed, dict):
        parsed["_openai_model_used"] = used_model
    out_path.write_text(json.dumps({"parsed": parsed, "raw": data}, ensure_ascii=False, indent=2), encoding="utf-8")
    sections = parsed.get("sections") if isinstance(parsed, dict) else parsed
    if not isinstance(sections, list):
        raise ValueError("OpenAI interpretation output must include a sections array")
    return sections


def clamp_timestamp(timestamp: float, duration: float) -> float:
    return min(max(0.0, timestamp), max(0.0, duration - 0.25))


def extract_planned_frames(video: Path, images_dir: Path, sections: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    frame_records: list[dict[str, Any]] = []
    for idx, section in enumerate(sections, start=1):
        timestamp = coerce_seconds(
            section.get("capture_timestamp", section.get("frame_timestamp", section.get("timestamp"))),
        )
        timestamp = clamp_timestamp(timestamp, duration)
        frame_id = f"frame_{idx:04d}"
        frame_path = images_dir / f"{frame_id}.jpg"
        extract_frame(video, timestamp, frame_path)
        section["frame_id"] = frame_id
        section["capture_timestamp"] = timestamp
        frame_records.append({"frame_id": frame_id, "timestamp": timestamp, "path": f"images/{frame_path.name}", "reason": "llm-capture-timestamp"})
    return frame_records


def extract_interval_frames(video: Path, images_dir: Path, duration: float, segments: list[dict[str, Any]], interval: float) -> list[dict[str, Any]]:
    frame_records: list[dict[str, Any]] = []
    previous_hash = ""
    for idx, (timestamp, reason) in enumerate(choose_timestamps(duration, segments, interval), start=1):
        frame_id = f"frame_{idx:04d}"
        frame_path = images_dir / f"{frame_id}.jpg"
        extract_frame(video, timestamp, frame_path)
        digest = hash_file(frame_path)
        if digest == previous_hash:
            frame_path.unlink(missing_ok=True)
            continue
        previous_hash = digest
        frame_records.append({"frame_id": frame_id, "timestamp": timestamp, "path": f"images/{frame_path.name}", "reason": reason})
    return frame_records


def write_markdown(path: Path, video: Path, sections: list[dict[str, Any]], frames_by_id: dict[str, dict[str, Any]]) -> None:
    lines = [f"# {video.stem} meeting minutes", "", f"- Source: `{video}`", f"- Generated: {iso_now()}", ""]
    for section in sections:
        stamp = seconds_to_stamp(coerce_seconds(section.get("timestamp")))
        lines.extend([f"## {section.get('title') or stamp}", "", f"- Time: `{stamp}`", ""])
        frame_id = section.get("frame_id")
        frame = frames_by_id.get(str(frame_id)) if frame_id else None
        if frame:
            lines.extend([f"![{frame_id}]({frame['path']})", ""])
        summary = section.get("summary")
        if summary:
            lines.extend([str(summary), ""])
        bullets = section.get("bullets") or []
        for bullet in bullets:
            lines.append(f"- {bullet}")
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--out", type=Path, default=Path("meeting-minutes-output"))
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--transcribe-cmd")
    parser.add_argument("--transcribe-script", type=Path)
    parser.add_argument("--transcribe-model", default="mlx-community/whisper-large-v3-turbo")
    parser.add_argument("--openai-transcribe", action="store_true")
    parser.add_argument("--openai-transcribe-model", default="gpt-4o-mini-transcribe")
    parser.add_argument("--openai-transcribe-fallback-model", default="whisper-1")
    parser.add_argument("--max-transcribe-minutes", type=float, help="Fail before transcription when the video is longer than this many minutes.")
    parser.add_argument("--interpret-cmd")
    parser.add_argument(
        "--interpret-planned-frames",
        action="store_true",
        help="Ask --interpret-cmd to plan capture timestamps from transcript only, then extract those frames.",
    )
    parser.add_argument("--openai-interpret", action="store_true")
    parser.add_argument("--openai-model", default="gpt-5-mini")
    parser.add_argument("--openai-fallback-model", default="gpt-5-mini")
    parser.add_argument("--frame-interval", type=float, default=60.0)
    args = parser.parse_args()

    video = args.video.expanduser().resolve()
    if not video.exists():
        raise SystemExit(f"Video not found: {video}")

    run_id = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{slug(video.stem)}"
    run_dir = args.out.expanduser().resolve() / run_id
    images_dir = run_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    audio_path = run_dir / "audio.wav"
    transcript_path = run_dir / "transcript_import.json"
    db_path = run_dir / "meeting_minutes.sqlite"
    minutes_path = run_dir / "minutes.md"

    duration = ffprobe_duration(video)
    if args.max_transcribe_minutes is not None and duration > args.max_transcribe_minutes * 60:
        raise SystemExit(f"Video is {duration / 60:.1f} minutes, longer than --max-transcribe-minutes={args.max_transcribe_minutes}.")
    if args.transcript:
        transcript_source = args.transcript.expanduser().resolve()
        segments = load_transcript(transcript_source)
        transcript_path.write_text(json.dumps({"segments": segments}, ensure_ascii=False, indent=2), encoding="utf-8")
    elif args.transcribe_script:
        extract_audio(video, audio_path)
        script = args.transcribe_script.expanduser().resolve()
        run([str(script), str(audio_path), str(transcript_path)])
        segments = load_transcript(transcript_path)
    elif args.transcribe_cmd:
        extract_audio(video, audio_path)
        run_template(args.transcribe_cmd, {"audio": audio_path, "out": transcript_path})
        segments = load_transcript(transcript_path)
    elif args.openai_transcribe:
        extract_audio(video, audio_path)
        segments = transcribe_openai(audio_path, transcript_path, args.openai_transcribe_model, args.openai_transcribe_fallback_model)
    else:
        segments = []
        transcript_path.write_text(json.dumps({"segments": []}, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.interpret_cmd or args.openai_interpret:
        planned_frames = args.interpret_planned_frames if args.interpret_cmd else True
        prompt_path = run_dir / "interpret_prompt.json"
        interpreted_path = run_dir / "interpret_output.json"
        prompt: dict[str, Any] = {"segments": segments}
        if planned_frames:
            prompt["frame_selection"] = "Choose capture_timestamp values from transcript only. No OCR or vision input is available."
            frame_records = []
        else:
            frame_records = extract_interval_frames(video, images_dir, duration, segments, args.frame_interval)
            prompt["frames"] = frame_records
        prompt_path.write_text(
            json.dumps(prompt, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            if args.interpret_cmd:
                interpretation = interpret_sections(args.interpret_cmd, prompt_path, interpreted_path)
                sections = interpretation["sections"]
            else:
                sections = interpret_openai(
                    args.openai_model,
                    prompt_path,
                    interpreted_path,
                    args.openai_fallback_model,
                    planned_frames=True,
                )
        except (Exception, SystemExit) as exc:
            print(f"warning: interpretation failed; using default sections: {exc}", file=sys.stderr)
            frame_records = extract_interval_frames(video, images_dir, duration, segments, args.frame_interval)
            sections = default_sections(segments, frame_records)
        else:
            if planned_frames:
                frame_records = extract_planned_frames(video, images_dir, sections, duration)
    else:
        frame_records = extract_interval_frames(video, images_dir, duration, segments, args.frame_interval)
        sections = default_sections(segments, frame_records)

    conn = init_db(db_path)
    conn.execute("insert into runs values (?, ?, ?, ?, ?)", (run_id, str(video), str(run_dir), iso_now(), duration))
    conn.executemany(
        "insert into transcript_segments values (?, ?, ?, ?, ?, ?)",
        [(run_id, i, seg["start"], seg["end"], seg.get("speaker", ""), seg["text"]) for i, seg in enumerate(segments)],
    )
    conn.executemany(
        "insert into frames values (?, ?, ?, ?, ?)",
        [(run_id, f["frame_id"], f["timestamp"], f["path"], f["reason"]) for f in frame_records],
    )
    conn.executemany(
        "insert into minute_sections values (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run_id,
                i,
                str(section.get("title") or f"Section {i + 1}"),
                coerce_seconds(section.get("timestamp")),
                str(section.get("frame_id") or ""),
                str(section.get("summary") or ""),
                json.dumps(section.get("bullets") or [], ensure_ascii=False),
            )
            for i, section in enumerate(sections)
        ],
    )
    conn.commit()
    conn.close()

    write_markdown(minutes_path, video, sections, {f["frame_id"]: f for f in frame_records})
    print(minutes_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
