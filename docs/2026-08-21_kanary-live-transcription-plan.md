# 計画 v3.2: Kanary CLI 移行 + 録画中逐次文字起こし + LLM 議事録

作成: 2026-08-21 / 改訂: v3.2(試作計測反映)/ 状態: **承認済み・実装中(Phase 1 + whisper 削除 + Phase 2 試作まで完了)**

## ゴール

1. 文字起こしエンジンを mlx-whisper から **Kanary CLI(`kanary transcribe`)に全面移行**し、whisper 経路は削除する(ユーザー決定)。
2. 文字起こしを**録画中に逐次実行**する(録画停止後の重い処理をなくし、蓋閉じ・中断リスクを最小化)。
3. 議事録生成を **codex CLI(モデル: gpt-5.6-luna)** による LLM 解釈に切り替える(rolling 生成 + 停止後の最終統合)。

## 検証済みの事実(2026-08-21 実測)

- Kanary v3.1.3 / CLI 3.1.3 (67)。`kanary transcribe <path> [--out <file.json>] [--lang <code>]`。
- 単発: 60秒音声を 7.7 秒で処理。**並列3本同時: 合計 3.6 秒で全完走**(並列可)。
- **Kanary.app が起動していなくても transcribe は成功する**(CLI 単体で完結)。
- 出力 JSON: `{schema_version: 3, duration, transcript: {tracks, segments[{track, start_seconds, end_seconds, confidence, text}], diagnostics[]}}`。互換性は `schema_version` で検査。
- ローカル完結(ユーザー確認済み)。**75分制限は「1回の transcribe あたり」と実証済み**(78.5分の一括実行が code -32010 で拒否、チャンク79本は全成功)。チャンク方式なら無料枠で運用可能。
- **チャンカー試作の実測(2026-08-21、78.5分実会議)**: 120秒チャンク・オーバーラップなしが最良(欠落 proxy 5.15% / 重複 3.18%)。60秒+2秒オーバーラップは境界セグメントの語句重複が実際に発生し劣後(7.88% / 9.68%)。79チャンク3並列で wall 26.8秒。
- 議事録プロンプト(planned_frames)はセクション数を会議長比例に修正済み。フレーム画像は LLM に読ませない(コスト、ユーザー決定)。
- **この macOS 環境には `timeout` / `gtimeout` / CLI 版 `flock` が存在しない**。タイムアウトは Python subprocess、ロックは Swift `flock(2)` / Python `fcntl` で実装する。

## レビュー対応方針(意図的な簡略化)

個人用ツールという規模感に合わせ、以下は意図的に簡略化する(Codex レビューで妥当と評価済み):

- 重複除去は**時間窓ベース**(グローバル時刻で判定、後述)。token alignment はやらない。
- exactly-once は狙わず、**at-least-once + 冪等な再構築**で担保。
- codex 呼び出しは **`codex exec` 単発方式**(常駐 app-server は使わない)。

## アーキテクチャ

```
[MeetingRecorder.app (Swift)]
  ├─ raw.mp4 (従来どおり非fragmented、停止時 finalize。writer 構成には一切触れない)
  ├─ audio-chunks/chunk_NNNN.wav      ← PCM モノラルミックス 120秒・オーバーラップなし
  │     (*.tmp → rename。専用 bounded queue 経由、本体録画から隔離)
  ├─ chunks/recorder.events.jsonl     ← 書き手: Swift のみ(chunk ready / drop gap / END 前提イベント)
  └─ chunks/END                        ← 停止ハンドシェイクの sentinel(後述の順序でのみ作成)
        │
        ▼
[transcriber worker (アプリが録画開始時に spawn、per-run lock 保持)]
  recorder.events.jsonl を単一シリアルで処理 → kanary transcribe (Python subprocess timeout)
  → chunk_NNNN.transcript.json.tmp → rename
  → chunks/worker.events.jsonl に attempt/success/failed を追記(書き手: worker のみ)
  → 全完了 + END 検知で chunks/WORKER_DONE を atomic 作成(ACK)
  transcript は両イベントログ + chunk JSON 群から常に決定的に再構築
        │
        ▼
[minutes worker]
  録画中: 5〜10分ごとに累積 transcript → codex exec (gpt-5.6-luna) → minutes-draft.md (atomic replace)
  停止後: 最終統合パス → capture_timestamp で raw.mp4 から直接フレーム抽出
          → minutes.md + images/ + SQLite

[状態機械] = イベントログの fold で導出(単一の可変 state.json は持たない)
```

### 書込所有権(単一 writer 原則)

- `recorder.events.jsonl`: **Swift のみ**が追記。1 event 1 行 1 write + fsync。
- `worker.events.jsonl`: **transcriber worker のみ**が追記。同上。
- `postprocess.events.jsonl`: **postprocess ラッパーのみ**が追記(started / completed / failed)。
- 可変共有ファイルへの read-modify-write は行わない。**run の状態は3つのログを時系列 fold して導出**する。fold 規則: 末尾の壊れた行(JSON parse 不能)は無視して打ち切り、既知イベントの最新値を採用。重複イベント(同一 chunk id の ready 二重追記等)は冪等に扱う。
- worker の多重起動防止: run ディレクトリ内 lock ファイルに `flock(2)`(Swift)/ `fcntl.flock`(Python)。postprocess も同様に per-run lock。

### 状態機械

```
recording_started
  → recording_finalized            (raw finalize 成功)
  → finalization_failed | finalized_media_invalid | capture_empty   (既存 terminal、main.swift:402-425 のイベント名に一致させる)
recording_finalized
  → transcription_drained          (全 chunk success + WORKER_DONE ACK)
  → postprocess_failed             (chunk 3回失敗 / worker crash / drain deadline 超過)
transcription_drained
  → postprocess_started → postprocess_completed | postprocess_failed
```

- **postprocess 開始の前提条件**: raw が正常 finalize 済み **かつ** 全 chunk success **かつ** WORKER_DONE ACK 済み、のすべて。
- **再試行分類**: `postprocess_failed` のみ再開スキャンの自動再試行対象。`finalization_failed` / `finalized_media_invalid` / `capture_empty` は raw 正常 finalize の前提を満たせず再実行では回復しないため、**通知のみで自動再試行から分離**する。再開時は fold 結果から未完了 chunk のみ再処理(chunk id で冪等)。

### 停止ハンドシェイク(この順序でのみ実行)

1. 停止要求 → チャンカーへの音声投入を停止
2. chunk queue を drain → 最終 partial chunk を close → `*.tmp` → rename
3. `recorder.events.jsonl` に最終 chunk の ready を追記 + fsync
4. `chunks/END` を atomic 作成(**1〜3 完了後のみ**)
5. worker: END 検知 → 残 chunk + 最終 partial を処理 → `WORKER_DONE` を atomic 作成(ACK)
6. アプリ / postprocess は **WORKER_DONE を待ってから**次工程へ。drain deadline(既定5分)超過は postprocess_failed に落として通知
- 注意: 現行 finish 処理は raw 側 queue の sync/close のみ(main.swift:367-401)。チャンカー側の flush/join を独立に追加し、raw finalize と直列化する。

### 時刻規約(chunk → グローバル時刻)

- グローバル原点 = **raw.mp4 の AVAssetWriter session start PTS**(最初の usable buffer、main.swift:333-336)に統一する。チャンカーの音声先頭 PTS が session start と異なる場合は、その offset を recorder.events に記録し結合時に加算する(raw writer は不変のまま capture_timestamp と映像を一致させる)。**hop = 120s、オーバーラップなし**(試作計測により v3.1 の 60s+2s から変更。境界の語句分断は計測上 120/0 が最小)。chunk i の収録範囲は `[i*120, (i+1)*120)`。
- 各 chunk の `start_abs` は **PTS から計算した実際の値**を recorder.events の ready イベントに記録する(理論値との突合で drift 検出)。
- Kanary 出力の chunk 相対秒 → `global = start_abs + rel`。
- dedup: オーバーラップ廃止により不要。チャンクは互いに素で、各セグメントは自チャンク帰属。結合は chunk id 昇順で決定的。境界での文分断は許容(計測済みの許容範囲)。
- bounded queue で drop が発生しても**時間を詰めない**: 欠損区間は silence として PCM を埋め、PTS gap を保持し、gap 量を recorder.events に記録する(capture_timestamp と raw.mp4 の時刻一致を保証)。

## 変更内容

### Phase 1: エンジン差し替え(録画後処理のまま)【先行着手可】

1. `Scripts/kanary_transcribe.py` 新規(**Python 実装**。macOS に timeout CLI がないため): `kanary transcribe {audio} --out {tmp}` を `subprocess.run(timeout=max(音声実時間*2, 120))` で実行 → `schema_version == 3` を検査 → whisper 互換 JSON(`{segments:[{start,end,text}]}`)へ変換して `{out}` に rename。失敗・timeout は非0終了。
2. `meeting_postprocess.sh`: `--transcribe-script` を kanary_transcribe.py へ差し替え。依存チェックは `uvx` → `kanary` + `python3`(app 起動チェックは不要と実証済み)。
3. **whisper 削除範囲**: `transcribe_mlx_whisper.sh` 削除 / `MEETING_WHISPER_MODEL` 参照削除 / Swift の依存チェック(main.swift:702-706)から uvx を除去し kanary を追加 / Python の mlx 既定値・whisper-1 fallback は CLI 引数として残すが既定経路から外す / README・ドキュメントの whisper 記述を更新。
4. **削除前の受入条件**: (a) fixture テスト(正常 / segments 空 / JSON 破損 / 非0終了 / hang→timeout 発火)、(b) 過去 run(20260821-110011)で kanary 経路 end-to-end + whisper 出力との品質目視比較、(c) 通過後に whisper 削除。
5. 失敗時: whisper フォールバックなし。postprocess.events に failed を記録して通知、再開スキャン対象。

### Phase 2: 録画中の逐次文字起こし

**前提: PCM チャンカーのプロトタイプを先に作り、方式を確定してから Swift 本実装に入る。**

6. **チャンカー試作(独立プロトタイプ)**: 既存録画音声で 59/60/61秒境界・最終 partial・片側無音・PTS ずれを再現し、WAV 分割 → kanary → 時刻規約どおりの dedup → 結合 transcript の欠落/重複率を計測。
7. **Swift 実装**: mic / system 音声サンプルを**専用 bounded queue**(上限超過は drop + 上記 silence 埋め規約)へ複製投入。専用スレッドで PTS 整列・モノラルミックス・欠損側 silence 埋めを行い WAV chunk を書く(AAC priming 回避)。チャンカーの失敗は本体録画に波及させない(隔離・イベント記録のみ)。raw.mp4 writer は非fragmented のまま不変。
8. **worker 耐久性契約**: 単一シリアル / per-chunk timeout / 失敗は attempts 記録 + 指数 backoff 最大3回 / イベントログは上記所有権規約に従う / 停止は上記ハンドシェイク / crash 時は再開スキャンが fold から未完了 chunk のみ再処理。
9. 録画停止時: WORKER_DONE 確認後、fold + chunk JSON 群から whisper 互換 JSON を構築し、Phase 1 経路(`--transcript` 指定)に接続。

### Phase 3: LLM 議事録(codex exec / gpt-5.6-luna)

10. **Python 変更**: `--interpret-planned-frames` フラグを追加し、外部 `--interpret-cmd` でも planned-frame 経路(LLM が capture_timestamp を返し後からフレーム抽出)を使えるよう共通化。あわせて **interpret 呼び出し(interpret_sections / interpret_openai)を try/except で包み、失敗時は `default_sections` にフォールバック**する(現行は無 catch、video_meeting_minutes.py:378-384)。
11. **`Scripts/interpret_codex.sh` 新規**: `codex exec` 単発呼び出し。契約: `--skip-git-repo-check` / cwd を run ディレクトリに固定 / `--ephemeral` / `--sandbox read-only` / `-m gpt-5.6-luna` / `--output-schema`(sections スキーマ)/ `-o` で出力ファイル指定 / timeout / 失敗時1回リトライ → 非0終了(Python 側 fallback が受ける)。
12. **rolling worker**: `video_meeting_minutes.py` は使わず(毎回新 run_id を作るため)、「前回ドラフト + 差分 transcript」を luna に渡して `minutes-draft.md` を atomic replace する軽量スクリプト。最終統合のみ video_meeting_minutes.py(--transcript + --interpret-cmd + --interpret-planned-frames)。

### Phase 4: 堅牢化・停止後処理の軽量化

13. 再開スキャン: アプリ起動時にイベントログ fold で未完了 run を検出(postprocess_failed / terminal 状態 / 中断)し、per-run lock を取って postprocess を再実行。`caffeinate -i` でラップ。
14. **停止後の重い処理を削減**: transcript 指定時は全尺 audio.wav 抽出をスキップ(Python 小改修)。フレームは raw.mp4 から直接抽出し、**meeting.mp4 全尺 remux は廃止**(閲覧用が欲しい場合のみオプション)。
15. 正本は `~/workspace/dev/meeting-recorder`。`build-app.sh` でアプリへ反映。

## 実装順序

Phase 1(受入条件込み)→ whisper 削除 → Phase 2 試作(手順6)→ Phase 2 本実装 → Phase 4(13,14 は Phase 2 と並行可)→ Phase 3。

## リスク / 未解決

- Kanary 認識品質が whisper 比で劣る場面(固有名詞等)→ Phase 1 受入条件(b)で判断。不許容なら中断してユーザーに再相談。
- 75分制限が累計だった場合 → 初の長会議で観察、抵触時はユーザー判断。
- schema_version 変化 → 変換スクリプト非0終了 + 通知で検知。
- gpt-5.6-luna の可用性・レート → リトライ + default_sections フォールバック。

## 検証計画

- **Phase 1**: fixture(正常/空/破損/非0/hang)、20260821-110011 end-to-end + whisper 品質比較。
- **Phase 2 試作**: 境界(59/60/61秒)・最終 partial・片側無音・PTS ずれの欠落/重複率計測。
- **Phase 2 本実装**:
  - 5分試験録画 → 逐次 transcript の遅延・欠落確認
  - **停止競合**: 停止直後の END/最終 partial の race(最終 chunk が必ず処理されること)
  - worker kill -9 → 再開で冪等復旧 / **イベントログ末尾破損・重複 event の fold 耐性**
  - **状態機械**: 不正遷移が起きないこと、全 chunk 成功ゲート(1 chunk 恒久失敗 → postprocess_failed)、worker drain deadline 超過 → postprocess_failed
  - **drop 後の時刻一致**: queue drop を注入し、capture_timestamp と raw.mp4 の映像が一致すること
  - disk full 相当(書込先 read-only 化)/ 2時間 synthetic soak(backlog 非増加)
- **Phase 3**: luna 出力の schema 適合・セクション数スケール・capture_timestamp 妥当性、interpret 失敗注入 → default_sections フォールバック動作。
- **実会議**: 77分級で全体通し、75分制限・品質・遅延を最終確認。

## 変更履歴

- v3.2 (2026-08-21): チャンカー試作の実測結果を反映。hop=60s+2s オーバーラップ → **120s・オーバーラップなし**に変更、dedup 規則を撤廃(チャンク互いに素)。75分制限が1回あたりであることを実証として記載。
- v3.1 (2026-08-21): Codex レビュー第3回(条件付き承認)の条件を反映して確定。時刻原点を raw writer session start PTS に統一(offset 記録方式)、dedup 境界を `<=` に修正、イベント名 finalized_media_invalid に統一、terminal 状態を自動再試行対象から分離。
- v3 (2026-08-21): Codex レビュー第2回反映。停止ハンドシェイクの厳密な順序 + WORKER_DONE ACK、書込所有権(writer 毎の追記専用イベントログ + fold、可変 state.json 廃止)、状態機械に既存 terminal(finalization_failed / media_invalid / capture_empty)と postprocess 前提条件を追加、時刻規約(start_abs / overlap のグローバル変換 / drop 時 silence 埋め)を定義、timeout/flock の macOS 実装方式明記(Python subprocess / flock(2)/fcntl)、interpret の例外 catch + fallback、codex exec の安全フラグ契約、検証計画に競合・破損・状態遷移系を追加。
- v2 (2026-08-21): Codex レビュー第1回反映(チャンカー隔離・manifest・planned-frame 改修・whisper 削除受入条件ほか)。
- v1 (2026-08-21): 初版。
