#!/bin/bash
# interpret_codex.sh <prompt.json> <output.json>
# Runs one ephemeral, read-only Codex interpretation with one retry.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
prompt_path="${1:?prompt JSON is required}"
output_path="${2:?output JSON path is required}"
schema_path="$SCRIPT_DIR/interpret_sections.schema.json"
timeout_seconds="${MEETING_CODEX_TIMEOUT_SECONDS:-300}"

if [ ! -s "$prompt_path" ]; then
  echo "prompt JSON is missing or empty: $prompt_path" >&2
  exit 64
fi
if [ ! -s "$schema_path" ]; then
  echo "sections schema is missing: $schema_path" >&2
  exit 66
fi
if ! command -v codex >/dev/null 2>&1; then
  echo "codex CLI is unavailable" >&2
  exit 69
fi

prompt_path="$(cd "$(dirname "$prompt_path")" && pwd)/$(basename "$prompt_path")"
output_dir="$(cd "$(dirname "$output_path")" && pwd)"
output_path="$output_dir/$(basename "$output_path")"
run_dir="$(dirname "$prompt_path")"

python3 - "$prompt_path" "$output_path" "$schema_path" "$run_dir" "$timeout_seconds" <<'PY'
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(sys.argv[3]).parent))
import meeting_triage

prompt_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
schema_path = Path(sys.argv[3])
run_dir = Path(sys.argv[4])
try:
    timeout_seconds = float(sys.argv[5])
except ValueError:
    print("MEETING_CODEX_TIMEOUT_SECONDS must be numeric", file=sys.stderr)
    raise SystemExit(64)
if timeout_seconds <= 0:
    print("MEETING_CODEX_TIMEOUT_SECONDS must be positive", file=sys.stderr)
    raise SystemExit(64)

try:
    prompt_data = json.loads(prompt_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    print(f"invalid prompt JSON: {error}", file=sys.stderr)
    raise SystemExit(65)

instruction = """Create concise Japanese meeting minutes using only the supplied transcript JSON.
Return only JSON matching the provided schema, with top-level meeting_title, disposition, confidence, reason, and sections fields.
Each section must contain title, timestamp, capture_timestamp, summary, and bullets.
timestamp is the topic start in numeric seconds. capture_timestamp is a useful exact video second inferred only from transcript cues; do not assume OCR, images, or vision input.
Choose screenshots near topic or screen/slide changes, demonstrations, important claims, decisions, and action items.
Split at real topic boundaries and scale section count to meeting duration: roughly one section per 5 to 10 minutes (for 60 minutes, about 6 to 12), with more for rapid topic changes and fewer for one sustained topic.
Do not invent facts absent from the transcript.
""" + meeting_triage.CLASSIFICATION_INSTRUCTION + "\nInput JSON follows:\n"
request = instruction + json.dumps(prompt_data, ensure_ascii=False)

def valid_sections(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = {"meeting_title", "disposition", "confidence", "reason", "sections"}
    if not isinstance(data, dict) or set(data) != expected:
        return False
    try:
        meeting_triage.from_interpretation(data)
    except ValueError:
        return False
    sections = data["sections"]
    if not isinstance(sections, list) or not sections:
        return False
    required = {"title", "timestamp", "capture_timestamp", "summary", "bullets"}
    for section in sections:
        if not isinstance(section, dict) or set(section) != required:
            return False
        if not isinstance(section["title"], str) or not isinstance(section["summary"], str):
            return False
        if isinstance(section["timestamp"], bool) or not isinstance(section["timestamp"], (int, float)) or section["timestamp"] < 0:
            return False
        if isinstance(section["capture_timestamp"], bool) or not isinstance(section["capture_timestamp"], (int, float)) or section["capture_timestamp"] < 0:
            return False
        if not isinstance(section["bullets"], list) or not all(isinstance(item, str) for item in section["bullets"]):
            return False
    return True

output_path.parent.mkdir(parents=True, exist_ok=True)
last_error = "unknown failure"
for attempt in (1, 2):
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{output_path.name}.attempt-{attempt}.", dir=output_path.parent)
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        command = [
            "codex", "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox", "read-only",
            "-m", "gpt-5.6-luna",
            "-C", str(run_dir),
            "--output-schema", str(schema_path),
            "-o", str(temporary_path),
            "-",
        ]
        result = subprocess.run(
            command,
            cwd=run_dir,
            input=request,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        if result.returncode == 0 and valid_sections(temporary_path):
            os.replace(temporary_path, output_path)
            raise SystemExit(0)
        detail = result.stderr.strip()[-1000:]
        last_error = f"exit={result.returncode}" + (f" stderr={detail}" if detail else " invalid sections JSON")
    except subprocess.TimeoutExpired:
        last_error = f"timed out after {timeout_seconds:g}s"
    finally:
        temporary_path.unlink(missing_ok=True)
    print(f"codex interpretation attempt {attempt} failed: {last_error}", file=sys.stderr)

print(f"codex interpretation failed after retry: {last_error}", file=sys.stderr)
raise SystemExit(1)
PY
