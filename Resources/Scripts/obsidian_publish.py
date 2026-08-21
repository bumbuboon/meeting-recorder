#!/usr/bin/env python3
"""Publish one completed Meeting Recorder run into an Obsidian vault."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sys
import unicodedata
import uuid
from typing import Any


SOURCE_RUN_RE = re.compile(r"(?m)^source_run:\s*['\"]?([^'\"\n]+)['\"]?\s*$")
CREATED_BY_RE = re.compile(r"(?m)^created_by:\s*['\"]?([^'\"\n]+)['\"]?\s*$")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            payload = text.encode("utf-8")
            if os.write(descriptor, payload) != len(payload):
                raise OSError("short publish write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def frontmatter_value(text: str, pattern: re.Pattern[str]) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    match = pattern.search(text[4:end])
    return match.group(1).strip() if match else None


def source_run(path: Path) -> str | None:
    try:
        return frontmatter_value(path.read_text(encoding="utf-8"), SOURCE_RUN_RE)
    except OSError:
        return None


def created_by(path: Path) -> str | None:
    try:
        return frontmatter_value(path.read_text(encoding="utf-8"), CREATED_BY_RE)
    except OSError:
        return None


def safe_relative(root: Path, candidate: Path) -> Path:
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"unsafe relative path: {candidate}")
    root_resolved = root.resolve()
    resolved = (root / candidate).resolve(strict=False)
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError(f"path escapes root: {candidate}")
    return resolved


def slug_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "-", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip(" .-")
    return normalized[:80].rstrip(" .-") or "Meeting"


def title_from_minutes(minutes_path: Path) -> str:
    interpreted = minutes_path.with_name("interpret_output.json")
    try:
        value = json.loads(interpreted.read_text(encoding="utf-8"))
        meeting_title = value.get("meeting_title") if isinstance(value, dict) else None
        if isinstance(meeting_title, str) and meeting_title.strip():
            return slug_title(meeting_title)
        sections = value.get("sections") if isinstance(value, dict) else value
        if isinstance(sections, list):
            for section in sections:
                title = section.get("title") if isinstance(section, dict) else None
                if isinstance(title, str) and title.strip():
                    return slug_title(title)
    except (OSError, json.JSONDecodeError):
        pass
    try:
        for line in minutes_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## ") and line[3:].strip():
                return slug_title(line[3:])
    except OSError:
        pass
    return "Meeting"


def run_start(run_dir: Path, manifest: dict[str, Any]) -> dt.datetime:
    raw = manifest.get("started_at") or manifest.get("start_time")
    if isinstance(raw, str):
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.astimezone() if parsed.tzinfo else parsed.astimezone()
        except ValueError:
            pass
    try:
        parsed = dt.datetime.strptime(run_dir.name[:15], "%Y%m%d-%H%M%S")
        return parsed.astimezone()
    except ValueError as error:
        raise ValueError(f"run start time is unavailable for {run_dir.name}") from error


def find_existing(meetings: Path, run_id: str) -> Path | None:
    if not meetings.is_dir():
        return None
    for note in meetings.glob("**/*.md"):
        if (
            note.is_file()
            and not note.is_symlink()
            and source_run(note) == run_id
            and created_by(note) == "agent"
        ):
            return note
    return None


def choose_note(
    vault: Path,
    run_id: str,
    date: dt.date,
    title: str,
    fixed_relative: str | None,
) -> tuple[Path, bool]:
    meetings = safe_relative(vault, Path("Meetings"))
    existing = find_existing(meetings, run_id)
    if existing is not None:
        return existing, True
    if fixed_relative:
        fixed = safe_relative(vault, Path(fixed_relative))
        if fixed.suffix != ".md" or Path(fixed_relative).parts[:1] != ("Meetings",):
            raise ValueError("vault_note must be a Markdown path under Meetings/")
        if not fixed.exists():
            return fixed, False
        if source_run(fixed) == run_id and created_by(fixed) == "agent":
            return fixed, True
    directory = safe_relative(vault, Path("Meetings") / f"{date:%Y}")
    stem = f"{date:%Y-%m-%d}_{slug_title(title)}"
    for number in range(1, 10_000):
        suffix = "" if number == 1 else f"-{number}"
        candidate = directory / f"{stem}{suffix}.md"
        if not candidate.exists():
            return candidate, False
        if source_run(candidate) == run_id and created_by(candidate) == "agent":
            return candidate, True
    raise RuntimeError("could not allocate a meeting note filename")


def replace_images(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.{uuid.uuid4().hex}.staging"
    backup = target.parent / f".{target.name}.{uuid.uuid4().hex}.backup"
    try:
        staging.mkdir(mode=0o700)
        if source.is_dir():
            for image in sorted(source.iterdir()):
                if image.is_file() and not image.is_symlink() and re.fullmatch(r"frame_\d{4}\.jpg", image.name):
                    shutil.copy2(image, staging / image.name)
        fsync_directory(staging)
        had_target = target.exists()
        if had_target:
            if target.is_symlink() or not target.is_dir():
                raise ValueError(f"unsafe existing image target: {target}")
            os.replace(target, backup)
        try:
            os.replace(staging, target)
            fsync_directory(target.parent)
        except BaseException:
            if had_target and backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup.exists():
            shutil.rmtree(backup)


def render_note(
    minutes: str,
    *,
    run_id: str,
    date: dt.date,
    transcript: Path,
    now: dt.datetime,
    disposition: str | None = None,
) -> str:
    rewritten_minutes = re.sub(
        r"\]\(images/(frame_\d{4}\.jpg)\)",
        rf"](../images/{run_id}/\1)",
        minutes.rstrip(),
    )
    fields = [
        "---",
        "type: meeting_minutes",
        "created_by: agent",
        f"date: {date.isoformat()}",
        f"source_run: {run_id}",
        f"updated: {now.astimezone().isoformat(timespec='seconds')}",
    ]
    if disposition == "test":
        fields.append("status: trash_candidate")
    fields.extend([
        "---",
        "",
        f"> Meeting Recorder の run `{run_id}` から自動生成した閲覧用議事録です。",
        "",
        rewritten_minutes,
        "",
        "---",
        "",
        f"文字起こし全文: [{transcript.name}]({transcript.as_uri()})",
        "",
    ])
    return "\n".join(fields)


def publish(
    run_dir: Path,
    vault: Path | None,
    manifest: dict[str, Any],
    canonical_minutes: Path,
    *,
    now: dt.datetime | None = None,
) -> str | None:
    if vault is None:
        return None
    run_dir = run_dir.resolve()
    vault = vault.expanduser().resolve()
    if not vault.is_dir():
        raise FileNotFoundError(f"vault directory does not exist: {vault}")
    try:
        canonical_minutes = canonical_minutes.resolve(strict=True)
        canonical_minutes.relative_to(run_dir)
    except (OSError, ValueError) as error:
        raise ValueError("canonical minutes must be an existing file inside the run") from error
    if not canonical_minutes.is_file() or canonical_minutes.is_symlink():
        raise ValueError("canonical minutes must be a regular non-symlink file")
    transcript = (run_dir / "chunks/transcript.json").resolve(strict=True)
    transcript.relative_to(run_dir)
    if not transcript.is_file() or transcript.is_symlink():
        raise ValueError("transcript must be a regular non-symlink file")
    run_id = str(manifest.get("run_id") or run_dir.name)
    started = run_start(run_dir, manifest)
    note, republish = choose_note(
        vault,
        run_id,
        started.date(),
        str(manifest.get("title") or title_from_minutes(canonical_minutes)),
        manifest.get("vault_note") if isinstance(manifest.get("vault_note"), str) else None,
    )
    if note.exists() and (source_run(note) != run_id or created_by(note) != "agent"):
        raise ValueError(f"refusing to overwrite unrelated or user note: {note}")
    images_target = safe_relative(vault, Path("Meetings/images") / run_id)
    replace_images(canonical_minutes.parent / "images", images_target)
    timestamp = now or dt.datetime.now().astimezone()
    atomic_text(
        note,
        render_note(
            canonical_minutes.read_text(encoding="utf-8"),
            run_id=run_id,
            date=started.date(),
            transcript=transcript,
            now=timestamp,
            disposition=manifest.get("disposition") if isinstance(manifest.get("disposition"), str) else None,
        ),
    )
    return note.relative_to(vault).as_posix()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--minutes", required=True, type=Path)
    parser.add_argument("--vault-dir", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    vault = args.vault_dir or (Path(value) if (value := os.environ.get("MEETING_VAULT_DIR")) else None)
    manifest_path = args.run_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        relative = publish(args.run_dir, vault, manifest, args.minutes)
        print(json.dumps({"status": "skipped" if relative is None else "published", "vault_note": relative}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"publish failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
