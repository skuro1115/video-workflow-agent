# video-workflow

長尺動画 / 配信アーカイブから盛り上がりポイントを検出し、段階的に短尺クリップへ加工するための自動化基盤。

> **Status: 音声ベース検出 (audio_rms) 動作中 (2026-05-08)**
> パイプライン骨格、JSON 出力仕様、`--from-plan` 再 export、`unittest` ベースのテストまで通っている。検出器は `even`（等間隔プレースホルダ）と `audio_rms`（音声 RMS のピーク + NMS）の2種。字幕・LLM ベースの検出器は未実装。

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
# 1. 入力動画を samples/ に置く（任意のパスでも可）
cp /path/to/video.mp4 samples/sample.mp4

# 2. プレースホルダ検出器でパイプラインを通す（JSON だけ出す）
python -m src.main --input samples/sample.mp4 --output output/

# 3. 音声 RMS で盛り上がり候補を出す（推奨）
python -m src.main --input samples/sample.mp4 --output output/ \
    --detector audio_rms --candidates 6 --window 30 --debug

# 4. プランを人間がレビュー → 編集 → そのまま export
python -m src.main --input samples/sample.mp4 --output output/ \
    --from-plan output/clip_plan.json

# 5. 検出から export まで一気に
python -m src.main --input samples/sample.mp4 --output output/ \
    --detector audio_rms --export-clips
```

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
| `--detector` | `even` | ホットポイント検出器名: `even` / `audio_rms` |
| `--candidates` | `6` | 候補区間の本数 |
| `--window` | `30.0` | 1候補あたりの秒数（audio_rms では NMS の最小間隔を兼ねる） |
| `--min-duration` | `10.0` | クリップ最小秒数（短すぎる候補は捨てる） |
| `--max-duration` | `60.0` | クリップ最大秒数（長すぎる候補は切り詰める） |
| `--export-clips` | off | ffmpeg で実エンコードする |
| `--from-plan` | off | `clip_plan.json` を読んで export だけ実行（手編集ワークフロー用） |
| `--debug` | off | 検出器の中間データを `<output>/debug/` に書き出す |

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
3. **字幕・LLM** — Whisper で文字起こし、LLM で盛り上がり判定
4. **段階的要約** — 1h → 10min ダイジェスト → 短尺複数本のカスケード
5. **配信** — サムネイル、字幕焼き込み、SNS 投稿、レビュー UI

詳細・優先度は [docs/tasks.md](docs/tasks.md)。
