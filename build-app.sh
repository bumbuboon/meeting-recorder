#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${MEETING_RECORDER_APP:-$HOME/Applications/Meeting Recorder.app}"
BUILD_DIR="$ROOT/.build/release"
MODULE_CACHE="$ROOT/.build/module-cache"
STAGE="$ROOT/.build/Meeting Recorder.stage.app"

mkdir -p "$BUILD_DIR" "$MODULE_CACHE"
swiftc -O -warnings-as-errors -module-cache-path "$MODULE_CACHE" \
  "$ROOT/Sources/MeetingRecorder/AudioChunker.swift" \
  "$ROOT/Sources/MeetingRecorder/main.swift" \
  -o "$BUILD_DIR/MeetingRecorder"

rm -rf "$STAGE"
mkdir -p "$STAGE/Contents/MacOS" "$STAGE/Contents/Resources/Scripts"
cp "$BUILD_DIR/MeetingRecorder" "$STAGE/Contents/MacOS/MeetingRecorder"
find "$ROOT/Resources/Scripts" -maxdepth 1 -type f -exec cp {} "$STAGE/Contents/Resources/Scripts/" \;
chmod +x "$STAGE/Contents/MacOS/MeetingRecorder" "$STAGE/Contents/Resources/Scripts"/*.sh
cp "$ROOT/Resources/Info.plist" "$STAGE/Contents/Info.plist"

codesign --force --deep --sign - "$STAGE"
codesign --verify --deep --strict "$STAGE"
rm -rf "$APP"
mkdir -p "$(dirname "$APP")"
mv "$STAGE" "$APP"
echo "built: $APP"
