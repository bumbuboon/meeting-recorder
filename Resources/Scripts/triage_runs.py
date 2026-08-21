#!/usr/bin/env python3
"""Classify manifested runs, reconcile their agent notes, and rebuild the index."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any

import mtg_index
import meeting_triage
import obsidian_publish
import run_storage


TRIAGE_KEYS = meeting_triage.MANIFEST_KEYS
MIN_LLM_CHARACTERS = meeting_triage.MIN_LLM_CHARACTERS
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(#[^\]|]+)?(\|[^\]]+)?\]\]")


def validate_schema(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"triage schema is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid triage schema JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError("triage schema JSON must be an object")
    properties = value.get("properties")
    required = value.get("required")
    if (
        not isinstance(properties, dict)
        or set(properties) != set(TRIAGE_KEYS)
        or not isinstance(required, list)
        or set(required) != set(TRIAGE_KEYS)
        or value.get("additionalProperties") is not False
    ):
        raise ValueError("triage schema does not define the exact required fields")


def codex_timeout() -> float:
    raw = os.environ.get("MEETING_CODEX_TIMEOUT_SECONDS", "300")
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError("MEETING_CODEX_TIMEOUT_SECONDS must be numeric") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError("MEETING_CODEX_TIMEOUT_SECONDS must be a finite positive number")
    return value


def transcript_segments(run_dir: Path, manifest: dict[str, Any]) -> list[str]:
    artifacts = manifest.get("artifacts")
    relative = artifacts.get("transcript") if isinstance(artifacts, dict) else None
    if not isinstance(relative, str) or not relative:
        return []
    try:
        path = run_dir / run_storage.contained_relative(run_dir, Path(relative))
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    segments = value.get("segments") if isinstance(value, dict) else None
    if not isinstance(segments, list):
        return []
    return [
        text.strip()
        for segment in segments
        if isinstance(segment, dict)
        and isinstance((text := segment.get("text")), str)
        and text.strip()
    ]


def transcript_sample(segments: list[str]) -> str:
    """Return the opening plus evenly spaced representative utterances."""
    if not segments:
        return ""
    opening = segments[:12]
    remaining = segments[len(opening):]
    representative: list[str] = []
    if remaining:
        count = min(12, len(remaining))
        indices = sorted(
            {round(index * (len(remaining) - 1) / max(1, count - 1)) for index in range(count)}
        )
        representative = [remaining[index] for index in indices]
    text = "\n".join(opening + representative)
    return text[:12_000]


def validate_result(value: object) -> dict[str, Any]:
    return meeting_triage.validate_result(value)


def existing_result(manifest: dict[str, Any]) -> dict[str, Any] | None:
    if not all(key in manifest for key in TRIAGE_KEYS):
        return None
    try:
        return validate_result({key: manifest[key] for key in TRIAGE_KEYS})
    except ValueError:
        return None


def classify(run_dir: Path, segments: list[str], *, codex: str, schema: Path) -> dict[str, Any]:
    sample = transcript_sample(segments)
    if len(re.sub(r"\s+", "", sample)) < MIN_LLM_CHARACTERS:
        return meeting_triage.insufficient_transcript_result()
    prompt = f"""次の Meeting Recorder 文字起こしを整理してください。
JSON Schema に厳密に従ってください。
{meeting_triage.CLASSIFICATION_INSTRUCTION.replace("meeting_title", "title")}

文字起こし（冒頭と代表サンプル）:
{sample}
"""
    timeout = codex_timeout()
    last_error = "codex did not produce a valid result"
    for _attempt in range(2):
        descriptor, temporary_name = tempfile.mkstemp(prefix=".triage.", suffix=".json", dir=run_dir)
        os.close(descriptor)
        output = Path(temporary_name)
        output.unlink()
        command = [
            codex,
            "exec",
            "-m",
            "gpt-5.6-luna",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "-C",
            str(run_dir),
            "--output-schema",
            str(schema),
            "-o",
            str(output),
            "-",
        ]
        try:
            try:
                completed = subprocess.run(
                    command,
                    cwd=run_dir,
                    input=prompt,
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                last_error = f"timed out after {timeout:g}s"
                continue
            if completed.returncode != 0:
                detail = completed.stderr.strip()[-1000:]
                last_error = detail or f"codex exited {completed.returncode}"
                continue
            try:
                return validate_result(json.loads(output.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, ValueError) as error:
                last_error = str(error)
        finally:
            output.unlink(missing_ok=True)
    raise RuntimeError(f"triage failed after retry: {last_error}")


def frontmatter_with_status(text: str, disposition: str) -> str:
    if not text.startswith("---\n") or (end := text.find("\n---\n", 4)) < 0:
        raise ValueError("agent note has invalid frontmatter")
    lines = text[4:end].splitlines()
    lines = [line for line in lines if not re.match(r"^status\s*:", line)]
    if disposition == "test":
        lines.append("status: trash_candidate")
    return "---\n" + "\n".join(lines) + text[end:]


def note_target(note: Path, title: str) -> Path:
    date_match = re.match(r"^(\d{4}-\d{2}-\d{2})_", note.stem)
    prefix = f"{date_match.group(1)}_" if date_match else ""
    stem = prefix + obsidian_publish.slug_title(title)
    first = note.with_name(stem + ".md")
    if first == note or not first.exists():
        return first
    for number in range(2, 10_000):
        candidate = note.with_name(f"{stem}-{number}.md")
        if candidate == note or not candidate.exists():
            return candidate
    raise RuntimeError("could not allocate triaged note filename")


def link_names(note: Path, meetings: Path) -> set[str]:
    relative = note.relative_to(meetings).with_suffix("").as_posix()
    return {note.stem, relative, f"Meetings/{relative}"}


def rewrite_wikilinks(text: str, old_names: set[str], old_note: Path, new_note: Path, meetings: Path) -> str:
    new_relative = new_note.relative_to(meetings).with_suffix("").as_posix()
    replacements = {
        old_note.stem: new_note.stem,
        old_note.relative_to(meetings).with_suffix("").as_posix(): new_relative,
        f"Meetings/{old_note.relative_to(meetings).with_suffix('').as_posix()}": f"Meetings/{new_relative}",
    }

    def replace(match: re.Match[str]) -> str:
        target = match.group(1)
        if target not in old_names:
            return match.group(0)
        return f"[[{replacements[target]}{match.group(2) or ''}{match.group(3) or ''}]]"

    return WIKILINK_RE.sub(replace, text)


def reconcile_note(vault: Path | None, run_id: str, result: dict[str, Any]) -> str | None:
    if vault is None:
        return None
    meetings = vault / "Meetings"
    note = obsidian_publish.find_existing(meetings, run_id)
    if note is None:
        return None
    original = note.read_text(encoding="utf-8")
    updated = frontmatter_with_status(original, result["disposition"])
    if updated != original:
        obsidian_publish.atomic_text(note, updated)
    target = note_target(note, result["title"])
    if target != note:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(note, target)
        obsidian_publish.fsync_directory(target.parent)
        old_names = link_names(note, meetings)
        for candidate in sorted(meetings.glob("**/*.md")):
            if not candidate.is_file() or candidate.is_symlink() or obsidian_publish.created_by(candidate) != "agent":
                continue
            text = candidate.read_text(encoding="utf-8")
            rewritten = rewrite_wikilinks(text, old_names, note, target, meetings)
            if rewritten != text:
                obsidian_publish.atomic_text(candidate, rewritten)
        note = target
    return note.relative_to(vault).as_posix()


def triage(base_dir: Path, *, vault: Path | None = None, codex: str = "codex", schema: Path | None = None) -> dict[str, Any]:
    schema_path = schema or Path(__file__).with_name("triage_result.schema.json")
    validate_schema(schema_path)
    if vault is not None and not vault.is_dir():
        raise ValueError(f"vault directory does not exist: {vault}")
    outcomes: dict[str, dict[str, Any]] = {}
    for run_dir in mtg_index.iter_run_directories(base_dir):
        if run_dir.is_symlink():
            continue
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            continue
        chunks = run_dir / "chunks"
        if chunks.is_symlink():
            outcomes[run_dir.name] = {"status": "failed", "error": "chunks directory is a symlink"}
            continue
        chunks.mkdir(exist_ok=True)
        lock_handle = (chunks / "postprocess.lock").open("a+b")
        try:
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                outcomes[run_dir.name] = {"status": "skipped_locked"}
                continue
            manifest = run_storage.read_manifest(run_dir)
            if not manifest or not isinstance(manifest.get("artifacts"), dict):
                raise ValueError("invalid manifest JSON")
            result = existing_result(manifest)
            reused = result is not None
            if result is None:
                result = classify(run_dir, transcript_segments(run_dir, manifest), codex=codex, schema=schema_path)
            run_id = str(manifest.get("run_id") or run_dir.name)
            relative = reconcile_note(vault, run_id, result)
            run_storage.apply_triage(manifest, result, preserve_override=reused)
            manifest["manifest_schema_version"] = run_storage.MANIFEST_SCHEMA_VERSION
            if relative is not None:
                manifest["vault_note"] = relative
            if vault is not None:
                manifest["vault_root"] = str(vault)
            run_storage.atomic_json(manifest_path, manifest)
            outcomes[run_dir.name] = {"status": "unchanged" if reused else "triaged", **result, "vault_note": relative}
        except Exception as error:
            outcomes[run_dir.name] = {"status": "failed", "error": str(error)}
        finally:
            lock_handle.close()
    index = mtg_index.rebuild_index(base_dir)
    return {"runs": outcomes, "index": index}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=Path.home() / "Movies/meeting-recordings")
    parser.add_argument("--vault-dir", type=Path)
    parser.add_argument("--codex", default="codex", help=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    vault = args.vault_dir or (Path(value) if (value := os.environ.get("MEETING_VAULT_DIR")) else None)
    try:
        result = triage(
            args.base_dir.expanduser().resolve(),
            vault=vault.expanduser().resolve() if vault is not None else None,
            codex=args.codex,
        )
    except Exception as error:
        print(f"triage failed: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        for run_id, outcome in result["runs"].items():
            print(f"{run_id}\t{outcome['status']}")
        print(f"index\t{result['index']['indexed']} indexed, {result['index']['excluded_active']} active excluded")
    return 1 if any(value["status"] == "failed" for value in result["runs"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
