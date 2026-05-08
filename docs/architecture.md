# Architecture

## モジュール構成と依存関係

```
                   ┌─────────────┐
                   │  main.py    │  CLI エントリ
                   │  (CLI args) │
                   └──────┬──────┘
                          │
                          ▼
                   ┌─────────────┐
                   │  config.py  │  PipelineConfig (dataclass)
                   └──────┬──────┘
                          │
        ┌─────────────────┼─────────────────┬────────────────┐
        ▼                 ▼                 ▼                ▼
 ┌─────────────┐  ┌─────────────────┐  ┌─────────────┐  ┌──────────────┐
 │video_info.py│  │hotspot_detector │  │clip_planner │  │clip_exporter │
 │  (ffprobe)  │  │  (interface +   │  │ (candidate  │  │  (ffmpeg)    │
 │             │  │   stub impl.)   │  │   → plan)   │  │              │
 └─────────────┘  └─────────────────┘  └─────────────┘  └──────────────┘
```

依存方向は常に **上 → 下** 一方向。下位モジュールは上位を import しない。

## 各モジュールの責務

### `config.py`
- `PipelineConfig` dataclass のみを定義
- ファイルや環境変数の読み込みは持たない（CLI が唯一の供給元）
- 将来 YAML / TOML 設定ファイル対応する場合もここに集約する

### `video_info.py`
- 入力動画 → `VideoInfo` への一方向変換
- `ffprobe` を subprocess 経由で叩く。Python ラッパー (av, ffmpeg-python) は意図的に避けて依存を最小化
- ffprobe が無い・失敗したケースは型付き例外（`FFprobeNotFoundError` / `FFprobeFailedError`）にして main で人に分かるメッセージへ変換する

### `hotspot_detector.py`
- 抽象基底 `HotspotDetector` と、具象 `EvenSamplingDetector` / `AudioRmsDetector`
- 契約は `detect(*, input_path, duration, debug_dir) -> list[HotspotCandidate]`（キーワード引数）
- 新しい検出器は同じ契約を実装し、`build_detector()` のディスパッチに 1 行追加すれば CLI から使える
- `debug_dir` がセットされていれば、検出器は中間特徴量（RMS 系列など）を JSON で吐ける（強制ではない）

### `clip_planner.py`
- `HotspotCandidate` のリスト → `ClipPlan` のリスト
- ID 付与・min/max duration の適用・順序保持
- ここに「同じ時間帯が重複しているなら統合する」「優先度で並び替える」などのポリシーが入る予定

### `clip_exporter.py`
- `ClipPlan` のリスト → 実 mp4 ファイル
- ffmpeg を subprocess 経由で起動。1 クリップずつ逐次（並列化は MVP では避ける）
- 失敗しても全体を止めず、失敗ログだけ残して次のクリップへ進む

## データフロー

```
input.mp4
   │
   ▼  video_info.probe()
VideoInfo  ──→  output/video_info.json
   │
   ▼  detector.detect(info.duration)
list[HotspotCandidate]  ──→  output/hotspot_candidates.json
   │
   ▼  plan_clips(candidates, min, max)
list[ClipPlan]  ──→  output/clip_plan.json
   │
   ▼  export_clips(plans)        [optional, --export-clips]
mp4 files in output/clips/  ──→  output/clip_export_result.json
```

各段の中間生成物が JSON で残るので、

- 検出器を差し替える → 段階2から再実行できる
- プランだけ手で編集して export → 段階3だけ走らせられる

という運用ができる。

## 設計上の決断

- **dataclass + dict、ORM や pydantic は入れない（今は）**: 入出力が JSON ファイルだけなので。スキーマが安定したら pydantic に上げてもよい
- **subprocess で ffmpeg を呼ぶ、Python バインディングは使わない**: 依存を増やしたくないのと、ffmpeg コマンド自体がドキュメント化された安定 API なので
- **検出器の契約は `(input_path, duration, debug_dir)` のキーワード引数**: 第一段階では `duration` だけだったが、`AudioRmsDetector` で実ファイルが要るようになったため拡張。フレーム単位の解析が来たら `VideoInfo` 全体を渡す形に再拡張する
- **`--export-clips` をデフォルト off**: エンコードは時間がかかる。最初は計画だけ出して人間がレビューしてから走らせる運用を前提にする
- **`--from-plan` で再 export を独立コマンド化**: プランを手で編集してから encoded clips だけ作り直す運用が想定されるため

## 拡張予定の差し込み点

- 新しい検出器 → `HotspotDetector` を継承して `build_detector()` に登録
- 字幕生成 → 段階間に新モジュール `transcribe.py` を挟み、`hotspot_detector` がその出力を読む
- バッチ実行 → `main.py` の上に薄いランナー `runner.py` を載せる（main 自体は単発実行のまま）
- Web UI / API → `main.run(cfg)` を関数として再利用、HTTP ハンドラから呼ぶ
