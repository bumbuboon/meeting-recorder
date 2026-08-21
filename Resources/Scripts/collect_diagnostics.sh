#!/bin/bash
set -euo pipefail
umask 077

run_dir="${1:?run directory is required}"
reason="${2:-unknown}"
diag="$run_dir/diagnostics"
mkdir -p "$diag"
chmod 700 "$diag"

timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
app_version="${MEETING_RECORDER_VERSION:-unknown}"
python3 - "$diag/manifest.json" "$timestamp" "$reason" "$app_version" <<'PY'
import json
import sys

path, timestamp, reason, version = sys.argv[1:]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({
        "collected_at": timestamp,
        "reason": reason,
        "app_version": version,
        "bundle_id": "local.meeting-recorder",
    }, handle, ensure_ascii=False)
    handle.write("\n")
PY

/usr/bin/sw_vers >"$diag/system.txt" 2>&1 || true
/usr/bin/df -h "$run_dir" >>"$diag/system.txt" 2>&1 || true
/usr/bin/log show --style ndjson --timezone UTC --last 15m \
  --predicate 'subsystem == "local.meeting-recorder" OR process == "MeetingRecorder"' \
  >"$diag/system.log" 2>&1 || true
chmod 600 "$diag"/* 2>/dev/null || true

if [ -s "$run_dir/raw.mp4" ] && command -v ffprobe >/dev/null 2>&1; then
  ffprobe -v error -show_format -show_streams -of json "$run_dir/raw.mp4" \
    >"$diag/raw-media.json" 2>"$diag/ffprobe-error.log" || true
fi

python3 -m json.tool "$diag/manifest.json" >/dev/null
test -s "$diag/system.txt"
test -f "$diag/system.log"
