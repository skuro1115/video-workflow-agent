# JSON 出力スキーマ

パイプラインが書く JSON ファイルの完全リファレンス。各ステージはこの契約を守ることで「途中だけ再実行」「手で編集して `--from-plan` で続きから」が成立する。

スキーマが変わる PR では **このドキュメントを必ず同時更新する**（[CLAUDE.md](../CLAUDE.md) の運用ルール）。

源泉となる dataclass:
- `VideoInfo` → [src/video_info.py](../src/video_info.py)
- `HotspotCandidate` → [src/hotspot_detector.py](../src/hotspot_detector.py)
- `ClipPlan` → [src/clip_planner.py](../src/clip_planner.py)

---

## `output/video_info.json`

ステージ1（メタデータ抽出）の出力。`ffprobe` の結果を必要分だけ抜き出した dataclass のシリアライズ。

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

| フィールド | 型 | null 可 | 意味 |
| --- | --- | --- | --- |
| `path` | string | × | 入力動画への絶対 or 相対パス |
| `duration` | float | × | 秒。ffprobe の `format.duration` |
| `width` / `height` | int | ○ | 映像ストリームの解像度。映像なしなら null |
| `fps` | float | ○ | `avg_frame_rate` を分子分母に割って算出 |
| `video_codec` | string | ○ | 映像コーデック名（h264 等） |
| `audio_codec` | string | ○ | 音声コーデック名（aac 等）。音声トラックがなければ null |
| `bit_rate` | int | ○ | 全体のビットレート bps |
| `container` | string | ○ | コンテナ識別子（`mov,mp4,m4a,...`） |

---

## `output/hotspot_candidates.json`

ステージ2（検出器）の出力。`HotspotCandidate` のリスト。検出器が違えば `reason` の表現は変わるが、フィールドは共通。

```json
[
  {"start": 4.5,    "end": 19.5,   "score": 1.0,  "reason": "audio peak: -8.2 dBFS"},
  {"start": 716.4,  "end": 746.4,  "score": 0.42, "reason": "audio peak: -19.5 dBFS"}
]
```

| フィールド | 型 | 意味 |
| --- | --- | --- |
| `start` / `end` | float | 動画頭からの秒数。`start < end` を保証 |
| `score` | float | [0, 1] の検出器スコア。検出器内で min-max 正規化済み |
| `reason` | string | 人間レビュー用の理由文字列。検出器ごとに表現が違う（後述） |

**reason の例:**

| 検出器 | 例 |
| --- | --- |
| `even` | `"temporary placeholder: evenly sampled segment"` |
| `audio_rms` | `"audio peak: -8.2 dBFS"` |
| `comment_density` | `"comment density: 7 unique users in 10s"` |
| `composite` | `"composite: comment_density=2.00, audio_rms=0.36"` |

---

## `output/clip_plan.json`

ステージ3（クリップ計画）の出力。`ClipPlan` のリスト。**人間が手で編集してよい**唯一のファイル — 編集後に `--from-plan` で export だけ走らせる運用が想定されている。

```json
[
  {
    "clip_id": "clip_001",
    "source_start": 4.5,
    "source_end": 19.5,
    "duration": 15.0,
    "purpose": "short clip candidate",
    "status": "planned",
    "score": 1.0,
    "reason": "audio peak: -8.2 dBFS"
  }
]
```

| フィールド | 型 | 意味 |
| --- | --- | --- |
| `clip_id` | string | `clip_001`, `clip_002`, ... 連番 |
| `source_start` / `source_end` | float | 入力動画上の切り出し範囲（秒） |
| `duration` | float | `source_end - source_start`。冗長だがレビュー時に便利 |
| `purpose` | string | 出力の使途タグ（現状は `"short clip candidate"` 固定）。将来 `"digest"` `"shorts"` 等へ拡張予定 |
| `status` | string | `"planned"`（候補）/ 編集者がスキップしたい時は手で `"skipped"` 等を入れる運用想定 |
| `score` | float | 候補時点のスコアをそのまま継承 |
| `reason` | string | 候補時点の理由をそのまま継承 |

**手編集時の注意:**
- `clip_id` の重複は避ける（exporter が同名ファイルを上書きしてしまう）
- `source_end > source_start` を保つ
- `duration` を後から自動再計算する処理は今は無い（必要なら手で合わせる）

---

## `output/run_timing.json`

パイプライン実行の段階別経過時間。エラー時にも書き出される（部分的な計測でも debug に有用）。

```json
{
  "stages": [
    {"name": "probe",  "elapsed_seconds": 0.42},
    {"name": "detect", "elapsed_seconds": 12.31, "detector": "audio_rms"},
    {"name": "plan",   "elapsed_seconds": 0.001},
    {"name": "export", "elapsed_seconds": 8.42, "clips": 6}
  ],
  "total_seconds": 21.151
}
```

| フィールド | 型 | 意味 |
| --- | --- | --- |
| `stages[].name` | string | `probe` / `detect` / `plan` / `export` / `load_plan` のいずれか |
| `stages[].elapsed_seconds` | float | その段階の経過時間（秒） |
| `stages[].*` | varies | 段階特有のメタ情報（`detect` なら `detector`、`export` なら `clips` 数） |
| `total_seconds` | float | 全段階の合計（オーバーヘッドは含まない） |

検出器のチューニングや並列化判断の指標として使う。連続実行で `detect` 時間が伸びていないか、`export` がボトルネックになっていないか等を見る。

---

## `output/clip_export_result.json`

ステージ4（書き出し）の出力。`--export-clips` 指定時のみ生成。失敗してもパイプライン全体は継続するため、**個別の失敗もここに残す**。

```json
[
  {"clip_id": "clip_001", "status": "exported", "path": "output/clips/clip_001.mp4"},
  {"clip_id": "clip_002", "status": "failed",   "error": "Conversion failed: ..."}
]
```

| フィールド | 型 | 意味 |
| --- | --- | --- |
| `clip_id` | string | 対応する `ClipPlan.clip_id` |
| `status` | string | `"exported"` or `"failed"` |
| `path` | string | 成功時のみ。出力ファイルへの相対パス |
| `error` | string | 失敗時のみ。ffmpeg stderr の最終行 |

---

## `output/debug/` （`--debug` 指定時のみ）

検出器ごとに中間特徴量を dump。チューニング用。スキーマは検出器内部の都合に合わせて自由に変えてよい（外部契約ではない）。

| ファイル | 検出器 | 中身 |
| --- | --- | --- |
| `audio_rms.json` | `audio_rms` | `[{t, rms_db}, ...]` 秒単位 RMS 系列 |
| `comment_density.json` | `comment_density` | `[{t, unique_users, messages}, ...]` 10秒 bin |
| `composite_combined.json` | `composite` | `{fusion, rrf_k, bins: [{t, score}, ...]}` bin 別合成スコア（`fusion` は `"weighted_sum"` か `"rrf"`、`rrf_k` は RRF 使用時のみ） |
| `composite_subdetectors.json` | `composite` | `{detector_name: [HotspotCandidate, ...]}` 各サブ検出器の素出力 |

---

## 入力 JSON

### chat-log（`--chat-log <path>` で指定、`comment_density` 検出器の入力）

```json
[
  {"t": 56.4, "user": "alice", "text": "うわあ"},
  {"t": 57.0, "user": "bob",   "text": "wwww"}
]
```

`{"messages": [...]}` で wrap した形式も受理（`CommentDensityDetector.detect` の分岐参照）。`t` は動画頭からの秒数。

### weights（`--weights <path>` で指定、`composite` 検出器の入力）

[weights.example.json](../weights.example.json) と [src/score_weights.py](../src/score_weights.py) を参照。
