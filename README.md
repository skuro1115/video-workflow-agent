# video-workflow

長尺動画 / 配信アーカイブから盛り上がりポイントを検出し、段階的に短尺クリップへ加工するための自動化基盤。

> **Status: 複合検出（音声 + ライブチャット）動作中 (2026-05-09)**
> 検出器は `even` / `audio_rms` / `comment_density`（ライブチャット密度）/ `composite`（重み付き合成）の4種。`composite` はスコア重みを外部 JSON または対話入力で設定できる。字幕・LLM ベース検出器は未実装。

---

## ゴール

- 1時間程度の動画 → メタデータ抽出 → ホットポイント候補 → 短尺クリップ複数本、を自動で出す
- 「どの時間帯を、なぜ切り抜いたか」をすべて JSON メタデータに残す
- 検出ロジックを後から差し替えられるよう、ffmpeg 依存・解析ロジック・出力管理・設定管理を分離する
- 将来的に CLI / Web UI / バッチ / GitHub Actions / 常駐処理に拡張できる構成にする

詳細は [docs/project_overview.md](docs/project_overview.md) を参照。

---

## 必要環境

- Python 3.11+
- ffmpeg / ffprobe（PATH 上に必要）
  - macOS: `brew install ffmpeg`
  - Ubuntu: `sudo apt install ffmpeg`

現時点で Python の追加依存はありません（[requirements.txt](requirements.txt) はコメントアウト済みの将来予定リスト）。

---

## 使い方

```bash
# 0. 利用可能な検出器の一覧
python -m src.main --list-detectors

# 1. 入力動画を samples/ に置く（任意のパスでも可）
cp /path/to/video.mp4 samples/sample.mp4

# 2. プレースホルダ検出器でパイプラインを通す（JSON だけ出す）
python -m src.main --input samples/sample.mp4 --output output/

# 3. 音声 RMS で盛り上がり候補を出す（音声トラックがある動画向け）
python -m src.main --input samples/sample.mp4 --output output/ \
    --detector audio_rms --candidates 6 --window 30 --debug

# 4. ライブ配信のチャットログから盛り上がり候補を出す
python -m src.main --input live.mp4 --output output/ \
    --detector comment_density --chat-log chat.json --candidates 6 --window 30

# 5. 複数検出器を重み付きで合成（推奨・配信向け）
python -m src.main --input live.mp4 --output output/ \
    --detector composite --weights weights.example.json \
    --chat-log chat.json --candidates 6 --window 30 --debug

# 6. 重みをその場で対話入力（非エンジニア向け）
python -m src.main --input live.mp4 --output output/ \
    --interactive-weights --chat-log chat.json

# 7. プランを人間がレビュー → 編集 → そのまま export
python -m src.main --input samples/sample.mp4 --output output/ \
    --from-plan output/clip_plan.json

# 8. 検出から export まで一気に
python -m src.main --input samples/sample.mp4 --output output/ \
    --detector audio_rms --export-clips
```

### 重み設定ファイル ([weights.example.json](weights.example.json))

```json
{
  "detectors": [
    { "name": "audio_rms",       "weight": 1.0 },
    { "name": "comment_density", "weight": 2.0 }
  ],
  "bin_seconds": 1.0,
  "min_score":   0.0,
  "fusion":      "weighted_sum",
  "rrf_k":       60
}
```

- `weight = 0` でその検出器を無効化
- `bin_seconds`: 合成時の時間粒度
- `min_score`: 合成スコアの下限フィルタ
- `fusion`: `"weighted_sum"`（デフォルト, スコア値で合成）または `"rrf"`（順位ベース合成, 外れ値に強い）
- `rrf_k`: RRF の damping 定数（`fusion: "rrf"` のときのみ使用、デフォルト 60）

詳細は [docs/workflow.md](docs/workflow.md#--detector-composite複数検出器の合成) を参照。

### チャットログ形式 ([samples/chat_log.example.json](samples/chat_log.example.json))

```json
[
  { "t": 56.4, "user": "alice", "text": "うわあ" },
  { "t": 57.0, "user": "bob",   "text": "wwww" }
]
```

`t` は動画頭からの秒数。Twitch / YouTube ライブのチャット履歴を取り込むには、それぞれの形式（yt-dlp など）からこの形式に変換するスクリプトが別途必要（未実装）。

合成サンプル動画（手元に動画が無いときの動作確認用）:

```bash
ffmpeg -y -f lavfi -i testsrc=duration=120:size=320x240:rate=30 \
       -f lavfi -i sine=frequency=440:duration=120 \
       -c:v libx264 -preset ultrafast -c:a aac -shortest samples/sample.mp4
```

テスト実行:

```bash
python -m unittest discover -s tests
```

### 検出結果の評価

正解 JSON ([samples/varying.expected.json](samples/varying.expected.json) 形式) があれば、検出結果との一致度を機械的に測れます。

```bash
# 1. パイプラインを回す
python -m src.main --input samples/varying.mp4 --output output/ \
    --detector audio_rms --candidates 3 --window 10

# 2. 候補が「正解ピーク」を捉えているか採点
python -m scripts.eval \
    --hotspots output/hotspot_candidates.json \
    --expected samples/varying.expected.json \
    --out output/eval_result.json
```

`hit_rate`（期待ピークの検出率）と `precision`（候補の的中率）を出します。検出器のチューニング前後で同じ数値を比較できるので、改善が定量的に見えるようになります。

### 出力ファイル

`output/` 以下に以下が生成されます。

| ファイル | 内容 |
| --- | --- |
| `video_info.json` | ffprobe で取得したメタデータ（長さ・解像度・fps・コーデック等） |
| `hotspot_candidates.json` | 検出器が出した候補区間（start / end / score / reason） |
| `clip_plan.json` | 実際に切り出すクリップの計画（clip_id 付き） |
| `clips/clip_NNN.mp4` | `--export-clips` / `--from-plan` 指定時に出力される実クリップ |
| `clip_export_result.json` | 各 clip_id の export 成否ログ |
| `debug/audio_rms.json` | `--debug` + `--detector audio_rms` 時の秒次 RMS 系列 |

### CLI オプション

| フラグ | デフォルト | 説明 |
| --- | --- | --- |
| `--input` | (必須) | 入力動画パス |
| `--output` | (必須) | 出力ディレクトリ |
| `--detector` | `even` | 検出器名: `even` / `audio_rms` / `comment_density` / `composite` |
| `--candidates` | `6` | 候補区間の本数 |
| `--window` | `30.0` | 1候補の長さ（NMS の最小間隔を兼ねる） |
| `--min-duration` | `10.0` | クリップ最小秒数（短すぎる候補は捨てる） |
| `--max-duration` | `60.0` | クリップ最大秒数（長すぎる候補は切り詰める） |
| `--export-clips` | off | ffmpeg で実エンコードする |
| `--from-plan` | off | `clip_plan.json` を読んで export だけ実行（手編集ワークフロー用） |
| `--debug` | off | 検出器の中間データを `<output>/debug/` に書き出す |
| `--chat-log` | none | `comment_density` / `composite` 用のチャットログ JSON |
| `--weights` | none | `composite` 用の重み設定 JSON |
| `--interactive-weights` | off | 重みを stdin で対話入力（`composite` を強制） |
| `--list-detectors` | — | 登録済み検出器の一覧を出して終了 |

---

## ディレクトリ構成

```
src/
  main.py              # CLI / パイプライン実行 / --from-plan 再 export
  config.py            # PipelineConfig（dataclass）
  video_info.py        # ffprobe ラッパー
  hotspot_detector.py  # 検出器: HotspotDetector / EvenSampling / AudioRms
  clip_planner.py      # 候補 → ClipPlan
  clip_exporter.py     # ffmpeg で実切り出し
tests/
  test_hotspot_detector.py  # EvenSampling / AudioRms / build_detector
  test_clip_planner.py      # plan_clips の境界条件
docs/
  project_overview.md  # 何を作っているか
  architecture.md      # モジュール間の責務と依存
  workflow.md          # パイプラインの段階別フロー
  tasks.md             # 進捗・残タスク・設計判断ログ
output/                # パイプライン出力（gitignore）
samples/               # 入力動画置き場（gitignore）
```

---

## ロードマップ概要

1. **MVP scaffold** ✅ — ffprobe + 等間隔ホットポイント + ffmpeg 切り出し
2. **音声・シーン解析** — 音声 RMS ✅ / PySceneDetect でショット境界 ⏳
3. **コミュニティ信号** — ライブチャット密度 ✅ / リアクション分類 ⏳
4. **複数検出器の合成** — 重み付き合成 ✅ / 順位ベース統合 (RRF) ⏳
5. **字幕・LLM** — Whisper で文字起こし、LLM で盛り上がり判定
6. **段階的要約** — 1h → 10min ダイジェスト → 短尺複数本のカスケード
7. **配信** — サムネイル、字幕焼き込み、SNS 投稿、レビュー UI

詳細・優先度は [docs/tasks.md](docs/tasks.md)。
