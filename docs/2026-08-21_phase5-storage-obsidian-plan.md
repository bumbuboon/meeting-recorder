# 計画 Phase 5 v2: 保存構造の再設計(保持ポリシー / Obsidian publish / 検索 CLI)

作成: 2026-08-21 / 改訂: v2(Codex レビュー条件反映)/ 状態: 承認済み・実装可 / 前提: v3.2 計画(Phase 1〜4)完了済み

## 確定要件(ユーザー裁定)

- 動画(raw.mp4)は残さない: **7日間保持して自動削除**。文字起こし全文は永続保存。
- フロントエンドは作らない。**議事録は Obsidian vault から閲覧できる Markdown として書き出す**(閲覧専用。編集は別ノートで行う運用)。
- 保存先は vault 直下に **Meetings/ を新設**。画像は Meetings 配下の images 系サブフォルダに置く。
- 録画中のリアルタイム閲覧は不要。単一マシン運用。分析機能は不要。HTML 等へのエクスポートは不要。
- **CLI での横断検索**(特に文字起こし全文)は必要。

## 設計原則(再設計の土台)

1. **ファイルが正本、索引は導出物**: run ディレクトリ群が唯一の真実。SQLite 索引はいつでも走査で再構築できる。
2. 書き手は追記専用 + atomic rename(既存の event log 設計を踏襲)。読み手(CLI)は読むだけ。
3. run の機械可読な入口として `manifest.json` を1つ置く(状態スナップショット・成果物の相対パス・schema_version)。
4. メタデータと媒体を分離: 媒体(raw.mp4 / WAV / 旧 meeting.mp4)は削除対象、メタデータは無期限。
5. **状態判定は常に event fold が正**。manifest は判定に使わないキャッシュ。

## 変更内容

### 5a. canonical minutes と publish/index の失敗分離【前提契約】

1. **canonical minutes path を固定**: postprocess は今回生成した minutes.md の正確なパスを後続(manifest / publish / retention)へ明示的に受け渡す。`find` の先頭採用(現 meeting_postprocess.sh:33-46 相当)による「古い議事録を掴む」経路を排除する。再実行時も canonical は「最新の成功 postprocess の成果物」と定義。
2. **失敗分離**: core(transcript→minutes 生成)成功後の publish / index 更新の失敗は core の完了を巻き戻さない。`publish_failed` / `index_failed` を postprocess.events に記録し、**独立に再試行**(起動時 maintenance が対象)。vault 一時障害で postprocess 全再生成や動画の無期限保持が起きないようにする。`MEETING_VAULT_DIR` 未設定時の publish はスキップ(成功扱い)。
3. 再試行順: core → manifest → publish → index。後段の失敗は前段を無効化しない。

### 5b. manifest.json

1. postprocess 完了時(および maintenance / cleanup 時)に run 直下へ atomic write(tmp→rename)。
2. **state は既存 fold 関数を再利用して導出**(chunks 配下 recorder/worker/postprocess ログ優先、無い場合のみ run 直下 events.jsonl へ fallback、という現行規則を踏襲)。独自の時系列 merge を新設しない。
3. `postprocess_completed` event を durable に追記(fsync)**した後**に manifest を再生成する。manifest 書込み失敗は起動時 maintenance が修復(completed なのに manifest 不在/旧い run を検出して再生成)。
4. 内容: `manifest_schema_version`(transcript / index の version とは別名で分離), run id, 開始/終了時刻(タイムゾーン付き ISO8601, 起点は run 開始時刻), duration, 状態, 成果物の**相対パス**(run 内 containment を検証、親参照 `..` 拒否), 媒体の有無, タイトル, `vault_note`, `retention_started_at`(移行 run 用), `media_deleted_at`。存在フラグは実ファイルから再計算。

### 5c. 保持ポリシー(retention)

1. cleanup は resume scan / maintenance に統合(起動時)。**削除判定は manifest ではなく event fold から導出**: `postprocess_completed` かつ canonical minutes.md が実在、完了 event 時刻から**7日経過**、per-run lock 取得済み、現在録画中/未 finalize の run は除外 — を全て満たす場合のみ削除。
2. 削除対象は **exact path のみ**: `<run>/raw.mp4`(移行 run では `<run>/meeting.mp4` も候補に含む)。regular file であること・symlink でないこと・run ディレクトリ配下であること(containment)を unlink 直前に検査。削除後 `media_deleted_at` を manifest に記録。途中失敗は再実行で収束(冪等)。
3. `audio-chunks/chunk_NNNN.wav` は該当 chunk の transcript の tmp→rename と success event の fsync が**完了した後**に即削除。対象は exact な chunk 命名のみ(event 内の任意パスに削除権限を与えない)。`MEETING_KEEP_CHUNK_WAV=1` で保持可。
4. 移行 run の7日起算は `retention_started_at`(移行実行時刻)を使い、`postprocess_completed_at` を捏造しない。
5. `transcript.json` / minutes / images / event logs / manifest は削除しない(永続)。

### 5d. Obsidian publish

1. postprocess の最終段(失敗分離は 5a)。vault ルートは `MEETING_VAULT_DIR`。
2. 配置:
   - ノート: `Meetings/YYYY/YYYY-MM-DD_<title>.md`(日付は run 開始時刻・ローカル TZ。title は LLM sections 由来、slug 正規化規則を実装で固定。衝突時は連番を付け、**採番結果を manifest に固定**して以後の再 publish で再利用)
   - 画像: `Meetings/images/<run-id>/frame_XXXX.jpg`
3. **vault 正本の更新を作業に含める**: 現行 vault の Properties.md は `meeting_minutes` type を定義せず、validator の対象も Projects/Resources/Inbox のみ。Phase 5 の作業として vault 側(.agents/skills/vault-sync/references/Properties.md 等)に `meeting_minutes` の許可キー(`type`, `created_by`, `date`, `source_run`, `updated`)と `Meetings/` の検証対象追加を行う(vault リポジトリへの変更は別コミット)。
4. frontmatter: `type: meeting_minutes` / `created_by: agent` / `date` / `source_run` / `updated`(再 publish 時のみ更新)。タグはここでは付与せず vault-sync に委ねる。
5. 冪等性: まず `source_run` frontmatter が一致する既存ノートを探して同一性を判断。**一致しないノートや `created_by: user` のノートは絶対に上書きしない**(その場合は連番で新規)。画像は `<run-id>` 専用 staging に書いてからディレクトリ置換 → その後ノートを atomic replace(部分 publish・stale 画像を防ぐ)。
6. 本文: minutes.md + 冒頭に生成元明記(vault 規約)+ 末尾に run バンドル(transcript 全文)への参照。ノートは chmod 444 にしない(閲覧専用は運用ルール)。
7. **DailyNote には書き込まない**(user ノート本文の編集禁止)。
8. 文字起こし全文は vault に複製しない。正本は run バンドルの `transcript.json`、検索は CLI(5e)。

### 5e. 検索 CLI + 索引

1. `Scripts/mtg`(Python stdlib のみ): `list` / `show <run>` / `search <query>` / `open <run>` / `index --rebuild`。全コマンド `--json` 対応。
2. 索引 `<base>/index.db`(SQLite): runs + FTS5 **trigram** tokenizer(transcript segments 全文 + minutes 本文)。起動時に FTS5/trigram の capability を確認。
3. **検索方式**: クエリの Unicode 文字長が3以上なら FTS5 MATCH、**1〜2文字は literal LIKE fallback**(wildcard を escape)。受入条件に両経路の日本語検索を含める。
4. 更新: postprocess 完了時に増分更新(失敗は warning、5a の分離に従い postprocess は失敗にしない)。`index --rebuild` は tmp DB を完成させてから atomic replace、走査中の録画中 run は除外。schema 不一致(`index_schema_version`)時は自動再構築。索引は導出物であり、破損時は常に再構築で回復。
5. CLI は app bundle に同梱し、`build-app.sh` / `Tests/verify.sh` の検証対象に加える。`~/.local/bin/mtg` symlink を README に記載。

### 5f. 既存 run の移行

- one-shot スクリプト: 既存 run を走査し manifest 生成・索引投入・(任意)vault publish。ファイル移動はしない。
- `retention_started_at` = 移行実行時刻。旧 `meeting.mp4` も retention 候補に含める(5c-2)。
- rebuild と同じく tmp→atomic replace、active run 除外。

## テスト / 受入条件

- retention: fold 判定で完了 run のみ・7日経過のみ削除(未完了/失敗/録画中 run 不変)。exact path / regular file / 非 symlink / containment 検査。chunk WAV の即削除(rename + event fsync 後)と KEEP フラグ。削除途中失敗からの冪等収束。
- manifest: fold 一致、completed event 追記後の生成順序、書込み失敗の maintenance 修復、相対パス containment・親参照拒否、schema version 分離。
- publish: 5c の vault 正本更新込みで frontmatter 適合、画像 staging→atomic replace、同一 run 再 publish の冪等性(採番固定)、source_run 不一致/user ノートの非上書き、`MEETING_VAULT_DIR` 未設定スキップ、publish 失敗が core を巻き戻さないこと + maintenance 再試行。
- CLI: list/show/search/open/--json、trigram MATCH(3文字以上)と LIKE fallback(1〜2文字、escape)、rebuild の atomic replace と active run 除外、capability check、索引破損からの自動再構築。CLI の bundle 同梱検証。
- 実データ: 試験録画 run で end-to-end(publish 先はテスト用仮 vault)。

## 実装順序

5a/5b(canonical + manifest)→ 5c(retention)→ 5d(publish、vault 正本更新含む)→ 5e(CLI)→ 5f(移行)。

## 非スコープ

専用フロントエンド / リアルタイム表示 / マルチマシン同期 / 分析 / HTML エクスポート。

## 変更履歴

- v2 (2026-08-21): Codex レビュー(条件付き承認)の6条件を反映 — canonical minutes path と publish/index 失敗分離、retention の fold 判定 + exact path/containment 検査 + retention_started_at、vault 正本(Properties/validator)更新の作業化 + 冪等 publish 契約(採番固定・staging・非上書き)、manifest の fold 再利用と生成順序、FTS trigram + 1〜2文字 LIKE fallback、rebuild の atomic replace / active run 除外 / CLI 同梱検証。
- v1 (2026-08-21): 初版。
