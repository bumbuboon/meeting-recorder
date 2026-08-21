# Phase 1 受入記録: Kanary CLI transcription adapter

実施日: 2026-08-21

## 結論

Phase 1 の受入条件を満たした。fixture 5種と既存検証スイートは全通過し、過去 run の先頭10分を使った Kanary 経路の end-to-end 処理も成功した。既存 whisper transcript と比べ、Kanary は不自然な反復と長い脱落が少なく、会議内容の被覆が改善した。固有名詞と医療・機械学習用語の誤認は残るが、Phase 1 の既定経路として受入可能と判断した。

受入コミットでは `transcribe_mlx_whisper.sh` を残し、本記録と実装に対する claude-main の承認後、後続の削除コミットで既定経路と同スクリプトから whisper 依存を除去した。品質比較のため、以下の旧出力に関する記録は履歴として残す。

## Fixture・回帰テスト

実行コマンド:

```bash
python3 Tests/test_kanary_transcribe.py
./Tests/verify.sh
```

結果:

- 正常: `schema_version == 3` の Kanary JSON を whisper 互換 `segments` に変換
- segments 空: 正常終了し、`{"segments": []}` を生成
- JSON 破損: 非0終了し、最終出力を生成しない
- Kanary 非0終了: 非0終了し、最終出力を生成しない
- hang: fake Kanary に対して timeout が発火し、終了コード124で終了
- `Tests/verify.sh`: app build、署名、bundle 同梱、既存検証を含め全通過

## 実データ end-to-end

入力:

- `~/Movies/meeting-recordings/<run>/meeting.mp4`(実会議の録画、非公開)
- 全長: 4710.478秒
- 比較範囲: 先頭600秒

元 run ディレクトリは変更せず、先頭10分を `/tmp/meeting-recorder-kanary-phase1.zs9b2J/meeting-first-10m.mp4` に remux した。まず `video_meeting_minutes.py` に `--transcribe-script Resources/Scripts/kanary_transcribe.py` を指定し、次に同じ動画を `/tmp` の複製 run の `raw.mp4` として `meeting_postprocess.sh` から実行した。いずれも音声抽出、Kanary CLI、互換JSON変換、minutes・画像・SQLite生成まで完走した。

結果:

- 終了コード: 0
- `video_meeting_minutes.py` 直接経路の処理時間: 36.98秒
- `meeting_postprocess.sh` 経路の処理時間: 20.57秒、終了コード0
- Kanary transcript: 37 segments、1,983文字、時刻範囲 12.900〜599.979秒
- 既存 whisper transcript（同じ先頭10分）: 210 segments（空を含む）、1,673文字、時刻範囲 0.000〜600.680秒
- `/tmp` の実データ生成物はコミット対象外

## 品質比較所見

Kanary は会議本文の被覆と読みやすさで既存 whisper を上回った。

- 既存 whisper の冒頭には、後続文脈とつながらない「ご視聴ありがとうございました」が約29秒の segment として存在する。Kanary には同種の定型句出力がない。
- 既存 whisper は約124〜170秒で「はい」を細切れに反復する。Kanary は同区間の参加者確認と発表者調整を文章として保持している。
- 既存 whisper は約272〜302秒がほぼ空だが、Kanary は同区間の発表内容の説明を文章として保持している。
- Kanary は長めの segment と句読点により流れを追いやすい。一方で話者分離はなく、複数話者の発言を同じ segment に結合する箇所がある。
- 両経路とも人名とドメイン固有の略語・専門用語に誤認がある(短い略語ほど脱落・置換が起きやすい)。

正解 transcript がないため WER/CER は算出していない。今回の判断は同一音声範囲に対する時刻付き出力の目視比較である。固有名詞辞書や用語補正は今後の課題だが、whisper 削除判断を妨げる品質退行は認めなかった。
