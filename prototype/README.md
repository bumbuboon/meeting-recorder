# Phase 2 chunker prototype

This directory is an independent experiment for plan v3.1 step 6. It does not
write to the source run. Every generated WAV/JSON must be placed below `/tmp`.

## Full recording measurement

```sh
python3 prototype/chunker_prototype.py run \
  --input ~/Movies/meeting-recordings/20260821-110011/meeting.mp4 \
  --workdir /tmp/meeting-recorder-phase2-real \
  --jobs 3
```

The command makes session-origin-aligned 16 kHz mono PCM, cuts chunk 0 as
`[0, 60)` and chunk i as `[i*60-2, (i+1)*60)`, runs `kanary transcribe` in
parallel, and writes:

- `chunks.json`: actual `start_abs` values and dedup boundaries
- `merged-transcript.json`: deterministic chunk-id-order merge
- `dedup-decisions.json`: every keep/drop decision
- `transcription-timings.json`: per-chunk elapsed time and parallel wall time
- `measurements.json`: whole-file character alignment and ±5 s boundary windows

Use `--resume` after an interruption. If a schema-v3 whole-file Kanary JSON is
already available, pass `--whole-transcript FILE`. `--skip-whole` exercises only
chunking/transcription/merge. In the measured non-Pro environment, Kanary rejects
the 78.5-minute source because it exceeds the 75-minute per-invocation limit.
That failure must be reported rather than silently replacing the reference.

`duplicate_or_extra_rate` is a conservative normalized-character alignment
proxy: recognition substitutions can contribute to it as well as true overlap
duplication. Inspect `boundary_windows` and `dedup-decisions.json` to distinguish
the two.

## Reproducible fixtures

```sh
python3 prototype/chunker_prototype.py make-fixtures \
  --input ~/Movies/meeting-recordings/20260821-110011/meeting.mp4 \
  --workdir /tmp/meeting-recorder-phase2-fixtures

python3 prototype/chunker_prototype.py run \
  --input /tmp/meeting-recorder-phase2-fixtures/final_partial.wav \
  --workdir /tmp/meeting-recorder-phase2-final-partial --jobs 3

python3 prototype/chunker_prototype.py run \
  --input /tmp/meeting-recorder-phase2-fixtures/pts_drift.wav \
  --workdir /tmp/meeting-recorder-phase2-pts-drift --pts-drift-ms 125 --jobs 3
```

`fixtures.json` records provenance. The fixture suite copies actual meeting
audio onto silent timelines crossing 59, 60, or 61 seconds, creates a 73-second
final-partial case, creates left/right-silent stereo cases, and records a
125-ms `start_abs` drift invocation. The drift is applied to both the physical
cut and its manifest time; dedup remains fixed at `i*60` as required.

## Tests

```sh
python3 -m unittest discover -s prototype/tests -v
python3 prototype/chunker_prototype.py --help
```
