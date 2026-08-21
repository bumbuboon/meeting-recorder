#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/meeting-recorder-test.XXXXXX")"
trap 'rm -rf "$TEST_ROOT"' EXIT
TEST_APP="$TEST_ROOT/Meeting Recorder.test.app"
TEST_RUN="$TEST_ROOT/diagnostics-test"

MEETING_RECORDER_APP="$TEST_APP" "$ROOT/build-app.sh"

test -x "$TEST_APP/Contents/MacOS/MeetingRecorder"
test -x "$TEST_APP/Contents/Resources/Scripts/collect_diagnostics.sh"
test -x "$TEST_APP/Contents/Resources/Scripts/kanary_transcribe.py"
test -f "$TEST_APP/Contents/Resources/Scripts/transcriber_worker.py"
test -f "$TEST_APP/Contents/Resources/Scripts/rolling_minutes_worker.py"
test -f "$TEST_APP/Contents/Resources/Scripts/postprocess_runner.py"
test -f "$TEST_APP/Contents/Resources/Scripts/postprocess_followups.py"
test -f "$TEST_APP/Contents/Resources/Scripts/resume_scan.py"
test -f "$TEST_APP/Contents/Resources/Scripts/run_storage.py"
test -f "$TEST_APP/Contents/Resources/Scripts/obsidian_publish.py"
test -f "$TEST_APP/Contents/Resources/Scripts/video_meeting_minutes.py"
test -x "$TEST_APP/Contents/Resources/Scripts/interpret_codex.sh"
test -f "$TEST_APP/Contents/Resources/Scripts/interpret_sections.schema.json"
test ! -e "$TEST_APP/Contents/Resources/Scripts/transcribe_mlx_whisper.sh"
test ! -e "$ROOT/Resources/Scripts/transcribe_mlx_whisper.sh"
codesign --verify --deep --strict "$TEST_APP"
plutil -lint "$TEST_APP/Contents/Info.plist" >/dev/null

if grep -R -n 'personal-skills/skills/video-meeting-minutes' "$ROOT/Sources" "$ROOT/Resources"; then
  echo "runtime still depends on the old skill directory" >&2
  exit 1
fi

if grep -R -n -E 'MEETING_WHISPER_MODEL|\buvx\b' "$ROOT/Sources" "$ROOT/Resources"; then
  echo "runtime still depends on the removed whisper path" >&2
  exit 1
fi

mkdir -p "$TEST_RUN"
"$TEST_APP/Contents/Resources/Scripts/collect_diagnostics.sh" "$TEST_RUN" "verification"
python3 -m json.tool "$TEST_RUN/diagnostics/manifest.json" >/dev/null
test -s "$TEST_RUN/diagnostics/system.txt"
test -f "$TEST_RUN/diagnostics/system.log"

if "$TEST_APP/Contents/Resources/Scripts/collect_diagnostics.sh" /dev/null/diagnostics-test "must-fail" >/dev/null 2>&1; then
  echo "diagnostics failure was reported as success" >&2
  exit 1
fi

python3 - "$ROOT/Resources/Scripts/video_meeting_minutes.py" <<'PY'
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("minutes", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
segment = module.normalize_segment({"start": "00:01:02", "end": "00:01:03.5", "text": "ok"}, 0)
assert segment["start"] == 62.0
assert segment["end"] == 63.5
PY

python3 "$ROOT/Tests/test_kanary_transcribe.py"
python3 "$ROOT/Tests/test_transcription_worker.py"
python3 "$ROOT/Tests/test_rolling_minutes_worker.py"
python3 "$ROOT/Tests/test_phase2_contract.py"
python3 "$ROOT/Tests/test_resume_scan.py"
python3 "$ROOT/Tests/test_run_storage.py"
python3 "$ROOT/Tests/test_obsidian_publish.py"
python3 "$ROOT/Tests/test_postprocess_followups.py"
python3 "$ROOT/Tests/test_video_meeting_minutes.py"
python3 "$ROOT/Tests/test_interpret_codex.py"

swiftc -warnings-as-errors -module-cache-path "$TEST_ROOT/swift-module-cache" \
  "$ROOT/Sources/MeetingRecorder/AudioChunker.swift" \
  "$ROOT/Tests/test_audio_chunker_watermark.swift" \
  -o "$TEST_ROOT/test-audio-chunker-watermark"
"$TEST_ROOT/test-audio-chunker-watermark"

test "$(/usr/libexec/PlistBuddy -c 'Print :LSMinimumSystemVersion' "$TEST_APP/Contents/Info.plist")" = "26.0"
test ! -e "$ROOT/Package.swift"
test "$(grep -c 'PYTHONDONTWRITEBYTECODE.*=.*\"1\"' "$ROOT/Sources/MeetingRecorder/main.swift")" -eq 4

echo "verification passed"
