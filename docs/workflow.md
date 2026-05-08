# Workflow

このドキュメントは「実際に動画を流したときに、各段階で何が起きるか」を時系列で書いたもの。コードの読み始めに使う。

## 段階0: 入力受付

- 入力: ローカルパスの動画ファイル（mp4 / mkv / mov など ffmpeg が読めるもの）
- 想定サイズ: 数百 MB 〜 数 GB
- バリデーション: ファイル存在チェックのみ。形式チェックは ffprobe に委譲

## 段階1: メタデータ抽出（`video_info.py`）

- `ffprobe -show_format -show_streams -print_format json` で全ストリームを取得
- 必要なものだけ拾って `VideoInfo` dataclass に詰める
  - `duration` / `width` / `height` / `fps` / `video_codec` / `audio_codec` / `bit_rate` / `container`
- 出力: `output/video_info.json`

**例:**
```json
{
  "path": "samples/sample.mp4",
  "duration": 3612.34,
  "width": 1920,
  "height": 1080,
  "fps": 29.97,
  "video_codec": "h264",
  "audio_codec": "aac",
  "bit_rate": 5234567,
  "container": "mov,mp4,m4a,3gp,3g2,mj2"
}
```

## 段階2: ホットポイント候補抽出（`hotspot_detector.py`）

検出器は CLI フラグ `--detector <name>` で切り替える。共通契約は

```python
detect(*, input_path: Path, duration: float, debug_dir: Path | None = None)
    -> list[HotspotCandidate]
```

### `--detector even`（プレースホルダ）

`EvenSamplingDetector`。動画長を `--candidates` 等分し、各点から `--window` 秒の窓を切り出す。

- score は全候補 0.5 固定（情報が無いので）
- reason は `"temporary placeholder: evenly sampled segment"`

実信号は使わないので、検証用 / 検出器が使えない環境用のフォールバック。

### `--detector audio_rms`（音声ベース）

`AudioRmsDetector`。音声の瞬時 RMS が大きい区間を候補にする。

1. ffmpeg で `-ac 1 -ar 4000 -f s16le` の生 PCM を stdout 経由で取得（mono 4kHz）
2. 1 秒ごとに RMS を計算して dBFS に変換、`(time, rms_db)` 系列を作る
3. RMS 降順にソート → 既に選ばれた bin から `--window` 秒以内なら飛ばす（NMS）
4. `--candidates` 個取れるか系列が尽きるまで繰り返す
5. 各 pick の bin 中心を中心とした `--window` 秒窓を作る（端で切られたらスライド）
6. score は系列全体の min-max 正規化値 [0..1]、reason は `"audio peak: -X.X dBFS"`

`--debug` で `output/debug/audio_rms.json` に秒次 RMS 系列が dump され、調整に使える。

### 将来想定

| 検出器 | 信号 | 実装案 |
| --- | --- | --- |
| `scenedetect` | フレーム差分 | PySceneDetect の `ContentDetector` でショット境界を取り、長いショットや切替の多い区間を候補に |
| `transcript` | 字幕中の盛り上がり語 | Whisper 文字起こし → 笑い / 感嘆 / キーワードでスコアリング |
| `llm` | 字幕の意味理解 | Whisper の出力を窓ごとに分割し、LLM に「面白さ」を 0..1 で採点させる |
| `composite` | 複数検出器の合成 | weighted average / RRF |

実装する順序は [docs/tasks.md](tasks.md) 参照。

### 出力例（audio_rms）

```json
[
  {"start": 4.5,    "end": 19.5,   "score": 1.0,   "reason": "audio peak: -8.2 dBFS"},
  {"start": 716.4,  "end": 746.4,  "score": 0.42,  "reason": "audio peak: -19.5 dBFS"}
]
```

## 段階3: クリップ計画（`clip_planner.py`）

- `--min-duration` 未満の候補は捨てる
- `--max-duration` を超える候補は末尾を切り詰める
- 連番で `clip_001`, `clip_002`, ... を振る
- `score` と `reason` は候補からそのまま引き継ぐ（人間レビュー時に必須）

将来追加予定のロジック:
- 候補同士のオーバーラップ統合
- score 降順での優先度付け
- 「ダイジェスト用 10分版」「ショート用 30秒版」のような複数 purpose の同時発行

## 段階4: クリップ書き出し（`clip_exporter.py`、`--export-clips` 指定時のみ）

- 各 ClipPlan に対して `ffmpeg -ss <start> -i <input> -t <dur> -c:v libx264 -c:a aac` を実行
- 出力: `output/clips/clip_001.mp4` など
- 失敗した clip は `clip_export_result.json` に `status: "failed"` と error 行で残す（次のクリップは継続処理）

### 注意

- 現状は再エンコード（精度優先）。速度優先にしたければ `-c copy` に切り替えるが、I-frame に揃わないと冒頭が黒くなる
- 並列化はまだしていない。CPU を埋めるなら `concurrent.futures` で 2〜4 並列に上げる余地あり

## 中断・再開のしかた

各段階が JSON を残すので、

- メタデータだけ更新したい → `video_info.json` を消して再実行
- 検出器を変えたい → `--detector` を変えてもう一度回すだけ
- プランを手で編集してから書き出したい → `clip_plan.json` を編集 → `--from-plan output/clip_plan.json` で export だけ実行

## エラー時の挙動

| 失敗箇所 | 戻り値 | メッセージ |
| --- | --- | --- |
| 入力ファイル無し | exit 2 | `Input video not found: ...` |
| ffprobe 無し | exit 3 | `ffprobe not found on PATH. Install ffmpeg ...` |
| ffprobe 実行失敗 | exit 4 | `ffprobe failed for ...: <stderr>` |
| ffmpeg 無し（exportのみ） | exit 5 | `ffmpeg not found on PATH ...` |
| 個別クリップの ffmpeg 失敗 | 全体は exit 0 | `clip_export_result.json` に `status: "failed"` |
| 音声抽出失敗（audio_rms） | exit 6 | `ffmpeg audio extraction failed: ...` |
| `--from-plan` のファイル無し | exit 7 | `plan file not found: ...` |
| `--from-plan` の JSON 不正 | exit 8 | `failed to parse plan ...` |
