#!/bin/bash
# meeting_postprocess.sh <run_dir>
# Generates minutes from finalized media and the live transcript.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

notify() {
  /usr/bin/osascript -e 'on run argv
display notification (item 2 of argv) with title "Meeting Recorder" subtitle (item 1 of argv)
end run' "$1" "$2" >/dev/null 2>&1 || true
}

run_dir="${1:?run directory is required}"
raw="$run_dir/raw.mp4"
transcript="$run_dir/chunks/transcript.json"
if [ ! -s "$raw" ]; then
  notify "エラー" "録画ファイルがありません: $raw"; exit 1
fi
if [ ! -s "$transcript" ]; then
  notify "エラー" "文字起こし結果がありません: $transcript"; exit 1
fi

for dependency in ffmpeg ffprobe python3; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    notify "エラー" "必要なコマンドがありません: $dependency"
    echo "missing required command: $dependency" >&2
    exit 69
  fi
done

if md=$(python3 "$SCRIPT_DIR/video_meeting_minutes.py" "$raw" \
    --out "$run_dir/minutes" \
    --transcript "$transcript" \
    --interpret-cmd "\"$SCRIPT_DIR/interpret_codex.sh\" \"{prompt}\" \"{out}\"" \
    --interpret-planned-frames \
    2>"$run_dir/minutes.log"); then
  printf '%s\n' "$md" >>"$run_dir/minutes.log"
  if [ -z "$md" ] || [ ! -s "$md" ]; then
    notify "エラー" "議事録ファイルが生成されませんでした"
    echo "minutes.md was not generated" >&2
    exit 1
  fi
  notify "完了" "議事録ができました: ${md#$HOME/}"
  /usr/bin/open -R "$md" >/dev/null 2>&1 || true
  printf '%s\n' "$md"
else
  notify "エラー" "議事録生成に失敗。$run_dir/minutes.log を確認してください"
  exit 1
fi
