# Phase 2 チャンカー試作の計測記録

実施日: 2026-08-21

正本: `docs/2026-08-21_kanary-live-transcription-plan.md` v3.1 手順6

## 結論

計画どおりの `hop=60s`、前方 `2s` overlap、`global end <= i*60` を捨てる結合方式のまま Swift 本実装へ進むべきではない。

Kanary は境界をまたぐ長い segment を返すため、後続チャンクの境界 segment は `global end > i*60` となり、重複部分を含んだまま採用される。

74分の代替比較では dedup による drop が0件で、欠落proxyは7.88%、重複・余剰proxyは9.68%だった。

現行のsegment単位dedupを変えない場合の推奨値は `hop=120s`、overlap `0s` とする。

この設定は74分比較で欠落proxy 5.15%、重複・余剰proxy 3.18%となり、試した5設定の中で両値が最小だった。

逐次結果を60秒ごとに得る要件を優先する場合は `hop=60s`、overlap `0s` が次点だが、境界で一部の文字が欠ける例が残った。

前方2秒overlapを維持するなら、境界をまたぐsegmentから採用側を選ぶ規則を別途設計し、このプロトタイプで再計測してから Swift を変更する必要がある。

## 実装範囲

独立プロトタイプは `prototype/chunker_prototype.py` に実装した。

元runは読み取り専用とし、WAV、Kanary JSON、結合結果、計測結果はすべて `/tmp` 以下に作成した。

プロトタイプは次の処理を行う。

- 音声開始PTSの0.024秒を先頭無音として保持し、raw session原点に揃えた16 kHz mono PCM WAVを作る。
- chunk 0を `[0, 60)`、chunk iを `[i*60-2, (i+1)*60)` として分割し、最終partialを閉じる。
- 最大3並列で `kanary transcribe` を実行し、schema version 3を検査する。
- `global = start_abs + rel` で時刻を変換し、chunk iでは `global end <= i*60` のsegmentだけを捨てる。
- NFKC正規化後の文字列をone-shot結果と整列し、欠落proxyと重複・余剰proxyを算出する。
- 各境界の前後5秒を別に比較し、one-shotとchunkedのsegmentをJSONへ残す。

`prototype/tests/test_chunker_prototype.py` は、最終partial、125 msのPTSずれ、workdir制約、`<=` 境界、文字整列、境界観察の6件を検査する。

## 素材と実行条件

素材は `~/Movies/meeting-recordings/20260821-110011/meeting.mp4` である。

元動画の尺は4,710.478秒、音声trackの開始PTSは0.024秒、音声尺は4,710.399秒だった。

session原点に揃えたcanonical WAVの尺は4,710.423秒となった。

Kanaryは3並列で実行した。

全尺の成果物は `/tmp/meeting-recorder-phase2-real/`、74分比較は `/tmp/meeting-recorder-phase2-74m/`、境界fixtureは `/tmp/meeting-recorder-phase2-fixtures/` と `/tmp/meeting-recorder-phase2-case-*/` に保存した。

主要な再現コマンドは次のとおりである。

```bash
python3 prototype/chunker_prototype.py run \
  --input ~/Movies/meeting-recordings/20260821-110011/meeting.mp4 \
  --workdir /tmp/meeting-recorder-phase2-real \
  --jobs 3

python3 prototype/chunker_prototype.py make-fixtures \
  --input ~/Movies/meeting-recordings/20260821-110011/meeting.mp4 \
  --workdir /tmp/meeting-recorder-phase2-fixtures

python3 -m unittest discover -s prototype/tests -v
```

## 全尺78分30秒の結果

チャンク経路は79本すべてを処理し、結合まで完了した。

3並列のwall timeは26.83秒、1チャンクの平均実行時間は1.01秒、最大は1.40秒だった。

最終partialは `[4678.0, 4710.423)` の32.423秒だった。

結合結果は306 segments、正規化後22,754文字、時刻範囲7.62秒から4,704.88秒で、79個すべてのchunk idが結果に含まれた。

一方、全尺one-shotは開始直後に失敗した。

Kanary CLIは4,710秒の入力に対し、75分を超えるCLI文字起こしにはKanary Proが必要であるとしてcode `-32010` を返した。

このため、78分30秒全体についてone-shotを基準にした欠落率と重複率は算出できていない。

これは推測していた75分制限が、少なくとも現在の非Pro環境では1回の入力尺に適用されることを示す。

## 74分のone-shot代替比較

全尺比較の代わりに、同じ録画の先頭4,440秒（元録画の94.3%）を切り出し、one-shot結果を固定して5設定を比較した。

one-shotは28.04秒で完了し、242 segments、正規化後21,069文字だった。

ここでの欠落率と重複率は正解transcriptに対する誤り率ではない。

one-shot文字列とchunked文字列の `SequenceMatcher` 整列で対応しなかった文字を、欠落proxyおよび重複・余剰proxyとして数えているため、認識語の置換も両値へ混ざる。

| hop / overlap | chunks | chunk wall | merged segments | dedup drop | 欠落proxy | 重複・余剰proxy |
|---|---:|---:|---:|---:|---:|---:|
| 60s / 0s | 75 | 26.84s | 265 | 0 | 7.04% | 4.40% |
| 60s / 2s | 75 | 26.62s | 289 | 0 | 7.88% | 9.68% |
| 60s / 5s | 75 | 31.78s | 296 | 0 | 6.79% | 14.47% |
| 120s / 0s | 38 | 20.39s | 252 | 0 | 5.15% | 3.18% |
| 120s / 2s | 38 | 31.63s | 266 | 0 | 7.84% | 8.14% |

60秒境界は74箇所あり、one-shotでは74箇所すべてで一つのsegmentが境界をまたいだ。

`60s / 2s` では73境界の前後5秒に両側のchunk idが存在し、境界窓の平均欠落proxyは13.88%、平均重複・余剰proxyは33.99%だった。

overlapを2秒から5秒へ増やすと全体の重複・余剰proxyは14.47%へ悪化した。

時間窓を増やしても、segment全体のendだけを見る現在のdedupでは重複を落とせない。

## 境界fixture

実録画の300秒付近から同じ6秒の発話を取り、59秒、60秒、61秒に置いたfixtureを作成した。

最終partialは73秒、PTSずれはchunk 1以降の `start_abs` を125 ms後方へずらした。

片側無音fixtureはstereo WAVとし、片方を最大 -91.0 dB、他方を最大 -4.2 dBにした。

| case | chunk範囲 | 欠落proxy | 重複・余剰proxy | 観察 |
|---|---|---:|---:|---|
| 発話中心59s | `[0,60)`, `[58,67)` | 0.00% | 15.38% | 「報告から」が重複した |
| 発話中心60s | `[0,60)`, `[58,68)` | 0.00% | 40.74% | 境界をまたぐ同一発話断片が重複した |
| 発話中心61s | `[0,60)`, `[58,69)` | 3.70% | 40.74% | 前chunkが「症」で切れ、後chunkが発話全体を再出力した |
| 最終partial 73s | `[0,60)`, `[58,73)` | 0.00% | 0.00% | 最終partialは処理できた |
| PTS +125ms | `[0,60)`, `[58.125,120.125)`, `[118.125,125)` | 11.11% | 51.85% | `start_abs + rel` は維持したが、無音末尾で「あ」の追加出力があった |
| 左片側無音 | `[0,12)` | 0.00% | 0.00% | one-shotとchunkedが一致した |
| 右片側無音 | `[0,12)` | 0.00% | 0.00% | one-shotとchunkedが一致した |

発話中心60秒のone-shotは、境界をまたぐ発話を一つのsegment(発話断片A+B)として返した。

chunkedでは、chunk 0が断片A、chunk 1が断片A+Bを返し、境界部分が両chunkに重複して現れた。

chunk 1のsegmentは58.0秒から65.38秒まで続くため、endはdedup境界の60秒を超え、現規約ではdropされない。

overlapを0秒にした同じ3 fixtureでは、重複・余剰proxyは0.00%、0.00%、3.70%へ下がったが、欠落proxyは3.85%、3.70%、11.11%となった。

この比較から、2秒overlapは境界発話の後半を保持する一方、現行dedupでは前半の重複を除けないことが分かる。

## Swift 本実装へ進む条件

WAV分割、最終partial、片側無音、PTSに基づく `start_abs`、3並列Kanaryの処理速度には、本実装を妨げる問題は見つからなかった。

しかし、計画の結合規約は実データで重複を残したため、同じ規約をSwiftへ固定する段階にはない。

次のいずれかを正本へ反映し、プロトタイプで再検証した後に本実装へ進む。

1. segment単位dedupを維持し、`hop=120s`、overlap `0s` を採用する。
2. 60秒の更新間隔を維持し、overlap `0s` を採用して境界欠落を許容する。
3. 前方2秒overlapを維持し、境界をまたぐsegmentの採用規則を追加する。

選択肢3ではtoken alignmentを導入する必要まではないが、少なくとも前後chunkの境界segmentを対にして、どちらを残すか決める規則が必要である。

その規則はv3.1の `global end <= i*60` だけでは表現できないため、正本の変更が先になる。

## 検証結果

次の検証はすべて通過した。

```text
python3 -m unittest discover -s prototype/tests -v
Ran 6 tests in 0.000s
OK

./Tests/verify.sh
Ran 5 tests in 0.887s
OK
verification passed
```

`./Tests/verify.sh` は作業Aの変更後に再実行し、Swift app build、codesign検査、Kanary adapterのfixture 5件を含めて通過した。
