# Meeting Recorder

A standalone macOS menu-bar meeting recorder with **live incremental transcription** and **LLM-generated minutes** — fully local capture, no cloud upload of your audio.

macOS の独立メニューバー録画アプリです。画面・システム音声・マイクを録画しながら、**録画中に120秒チャンク単位でローカル文字起こし**を進め、停止後に LLM がスクリーンショット付きの議事録(Markdown)を生成します。

## How it works

```
ScreenCaptureKit ──► raw.mp4 (screen + system audio + mic, finalized on stop)
        │
        └─► AudioChunker: 120s mono WAV chunks (bounded queue, isolated from the main writer)
                 │
                 ▼
        transcriber worker: Kanary CLI per chunk → whisper-compatible transcript.json
                 │                     (runs live, during the recording)
                 ▼
        post-process (caffeinate-wrapped): LLM interprets the transcript,
        picks capture timestamps, extracts frames from raw.mp4,
        writes minutes.md + images/ + SQLite
```

- **Transcription**: [Kanary](https://kanary.download) CLI (`kanary transcribe`), on-device. Chunking keeps every call short.
- **Minutes**: `codex exec` (one-shot, read-only sandbox, JSON-schema-validated output). Falls back to rule-based sections if the LLM is unavailable.
- **Durability**: append-only per-writer event logs (`recorder.events.jsonl`, `worker.events.jsonl`, `postprocess.events.jsonl`), atomic tmp→rename writes, an explicit stop handshake (`END` → `WORKER_DONE`), and a resume scan that retries interrupted post-processing on next launch.

## Requirements

- macOS 26+ (Apple Silicon), Screen & System Audio Recording permission
- `kanary` CLI on PATH (bundled with the Kanary app; symlink `Kanary.app/Contents/Helpers/kanary` into `~/.local/bin`)
- `ffmpeg`, `ffprobe`, `python3`
- `codex` CLI (optional — minutes fall back to rule-based sections without it)

## Build & install

```bash
./build-app.sh
```

Installs `Meeting Recorder.app` into `~/Applications`. Launching the app starts a recording; launching a second instance (`open -n`) stops it. Note: rebuilding re-signs the binary ad hoc, so macOS revokes the Screen Recording permission each time — toggle it off/on again in System Settings.

## Output layout

Each run is written under `~/Movies/meeting-recordings/<timestamp>/`:

- `raw.mp4` — original three-track recording (never post-processed in place; deleted after 7 days once minutes are durably complete)
- `audio-chunks/` — 120 s WAV chunks produced during recording and removed after each durable transcript success (`MEETING_KEEP_CHUNK_WAV=1` keeps them)
- `chunks/` — per-chunk transcripts, merged `transcript.json`, per-worker event logs, handshake markers
- `minutes/<generation-id>/` — generated `minutes.md`, screenshots, imported transcript, interpretation artifacts, and SQLite data
- `manifest.json` — atomic run summary with canonical artifact paths and retention state
- `minutes-draft.md` — disposable rolling draft written during recording when interpretation succeeds
- `recorder.log`, `transcriber-worker.log`, `rolling-minutes-worker.log`, `minutes.log` — recorder, worker, and minutes-generation logs
- `events.jsonl` — run lifecycle events; `chunks/*.events.jsonl` stores recorder, transcription, rolling-minutes, and post-processing events

The run bundles are the source of truth. `<base>/index.db` is a derived SQLite/FTS5 index and can always be rebuilt. Set `MEETING_RECORD_DIR` to override the default base (`~/Movies/meeting-recordings`). Set `MEETING_VAULT_DIR` to publish read-only-by-convention copies under `Meetings/YYYY/` in an Obsidian vault; the transcript remains only in the run bundle.

The Luna minutes pass also assigns a meeting title and a `keep`/`test` disposition. Test runs are published with `status: trash_candidate`. Startup maintenance deletes the complete run bundle, its vault note and images, and its index entry seven days after `disposition_flagged_at`. Removing or changing that status in Obsidian before deletion cancels cleanup and changes the manifest disposition to `keep`.

## Search CLI

The app bundles `mtg`, a Python-stdlib-only history CLI. Install a convenient symlink after building:

```bash
mkdir -p "$HOME/.local/bin"
ln -sfn "$HOME/Applications/Meeting Recorder.app/Contents/Resources/Scripts/mtg" "$HOME/.local/bin/mtg"
mtg index --rebuild
mtg list
mtg show 20260821-154836
mtg search 日本語
mtg open 20260821-154836
```

Every command accepts `--json`. Queries of three or more Unicode characters use FTS5 trigram search; one- and two-character queries use escaped literal `LIKE` matching.

Existing runs can be prepared with the bundled `migrate_runs.py`, which adds manifests and rebuilds the index without moving or deleting run files. Add `--publish` only when `MEETING_VAULT_DIR` points to the intended vault. Migration is intentionally not run by the build.

## Tests

```bash
./Tests/verify.sh
```

Builds and signs an isolated test app bundle, validates its resources and plist, then runs the Python contract suite and the Swift `AudioChunker` watermark test. Coverage includes transcription-adapter failures, worker restart/idempotency, stop handshake, resume and retention flows, run indexing/migration, Obsidian publishing, triage, and minutes interpretation.

## Design notes

Design notes and phase acceptance records are maintained outside this public repository.

## License

MIT
