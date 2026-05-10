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

### `--detector comment_density`（ライブチャット）

`CommentDensityDetector`。`--chat-log <path>` で渡したチャットログ JSON を入力にする。

1. メッセージを 10 秒 bin で集計、bin ごとの **ユニークユーザ数** を計算（1人がスパムで張り付いても1票扱い）
2. 上位 N bin を NMS で間引き
3. score は min-max 正規化値、reason は `"comment density: 7 unique users in 10s"`

`--debug` で `output/debug/comment_density.json` に bin 別の `(t, unique_users, messages)` を吐く。

チャットログ JSON 形式:

```json
[
  { "t": 56.4, "user": "alice", "text": "うわあ" },
  { "t": 57.0, "user": "bob",   "text": "wwww" }
]
```

`t` は動画頭からの秒数。プラットフォーム固有形式（Twitch chat replay / YouTube `live_chat.json`）からの変換は [scripts/fetch.py](../scripts/fetch.py) が担当する。`python -m scripts.fetch --url <URL>` で動画もチャットも取得 → アプリ形式に変換まで一気に走る。`src.main --url <URL>` でも同等のフローが走り、結果がそのままパイプラインに流れる。

### `--detector composite`（複数検出器の合成）

`CompositeDetector`。`--weights <path>` または `--interactive-weights` で重み設定を渡す。`weights.fusion` で2種類の合成方式から選べる:

#### `fusion: "weighted_sum"`（デフォルト）

1. 各サブ検出器を独立に走らせる（個別失敗は WARNING で握りつぶす）
2. 各検出器のスコアを **その動画内で min-max 正規化** して [0, 1] に揃える
3. `bin_seconds` 粒度の per-bin スコア配列を作り、各候補 (start, end, norm_score) を `weight × norm_score` で該当 bin に加算
4. `sum(weight)` で割って合成スコア [0, 1] に正規化
5. `min_score` 以上の bin から NMS で上位 K 個を選ぶ
6. reason は寄与した検出器名 + 寄与量を `"composite: comment_density=2.00, audio_rms=0.36"` のように残す

#### `fusion: "rrf"`（Reciprocal Rank Fusion）

1. 各検出器の候補を score 降順に並べて rank 1, 2, ... を振る（**スコアの大きさは捨てる**）
2. 候補が覆う bin にその検出器の rank を割り当てる（同じ bin を複数候補が覆うなら最良 rank を使う）
3. bin スコア = `sum_i (weight_i / (rrf_k + rank_i)) / 最大可能値`（最大可能値 = 全検出器が rank 1 のとき）
4. 以降は weighted_sum と同じ NMS / `min_score` フィルタ
5. reason は `"composite (rrf): audio_rms@rank=1, comment_density@rank=2"` のように rank を残す

`rrf_k` のデフォルトは 60（IR 文献の標準値）。小さくすると上位 rank の差を強調、大きくすると平準化。

#### モードの選び分け

- スコアの大きさが信頼できる（外れ値が少ない）→ `weighted_sum`
- 1つの検出器が偶発的に巨大スコアを出す可能性がある、または検出器のスコア分布が異なる（chat 密度 vs 音声 dB 等）→ `rrf`

`--debug` で `composite_combined.json`（fusion 種別 + bin 別合成スコア）と `composite_subdetectors.json`（各検出器の素の出力）を吐く。

### 将来想定

| 検出器 | 信号 | 実装案 |
| --- | --- | --- |
| `scenedetect` | フレーム差分 | PySceneDetect の `ContentDetector` |
| `transcript` | 字幕中の盛り上がり語 | Whisper 文字起こし → 笑い / 感嘆 / キーワード |
| `llm` | 字幕の意味理解 | Whisper 出力を窓ごとに LLM 採点 |
| `chat_reaction` | 特定リアクション | 草 / 100 / `:Kappa:` / 絵文字カウント |

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
| 検出器ビルド失敗 / weights 不正 | exit 9 | `Unknown detector ...` / `weights file ...` |
| chat-log ファイル無し | exit 10 | `Chat log not found: ...` |
