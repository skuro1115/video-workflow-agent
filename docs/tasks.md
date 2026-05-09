# Tasks

進捗・残タスク・設計判断のログ。

## 完了（2026-05-08 時点）

### 第1セッション（MVP scaffold）
- [x] リポジトリ構成決定（src / docs / output / samples）
- [x] CLI エントリ `python -m src.main --input ... --output ...`
- [x] ffprobe ラッパー `video_info.py`（型付き例外つき）
- [x] 検出器インタフェース + プレースホルダ `EvenSamplingDetector`
- [x] クリップ計画 `clip_planner.py`（min/max duration クリップ）
- [x] ffmpeg による書き出し `clip_exporter.py`（`--export-clips` で有効化）
- [x] JSON 出力仕様（`video_info.json` / `hotspot_candidates.json` / `clip_plan.json` / `clip_export_result.json`）
- [x] README + docs（overview / architecture / workflow / tasks）

### 第2セッション（音声ベース検出 + 運用フロー強化）
- [x] 検出器契約を拡張: `detect(*, input_path, duration, debug_dir)` キーワード引数化
- [x] `AudioRmsDetector` 実装: ffmpeg で PCM 抽出 → 秒単位 RMS → top-K NMS で候補生成
- [x] `--detector audio_rms` で CLI から呼び出せる
- [x] `--from-plan <path>` モード: 既存の `clip_plan.json` を読んで export だけ走らせる（手編集ワークフロー）
- [x] `tests/` ディレクトリ + `unittest` ベースのテスト（追加依存ゼロで `python -m unittest` で走る）
- [x] `--debug` フラグ: 検出器が `audio_rms.json` などの中間データを `output/debug/` に書く

### 第3セッション-a（ライブチャット信号）
- [x] `CommentDensityDetector` 実装: チャットログ JSON → bin 別ユニークユーザ数 → NMS top-K
- [x] `--chat-log <path>` フラグ + 標準チャットログ JSON 形式の定義
- [x] `AVAILABLE_DETECTORS` を公開定数化（後続の `--list-detectors` 等で利用予定）
- [x] `samples/chat_log.example.json` を整備（`.gitignore` の例外指定で commit 可能に）
- [x] `CommentDensityDetector` の unittest（密度検出 / ユニークユーザ vs スパマー / 空ログ / ファイル無し）
- [x] `audio_rms`: ffmpeg「音声ストリーム無し」判定の marker を拡充

### 第3セッション-b（複合検出器 + 重み外部設定）
- [x] `CompositeDetector` 実装: per-bin 加重合成 + 寄与検出器の reason への明記
- [x] `src/score_weights.py`: 重み設定の dataclass / JSON load・save / 対話入力 UI
- [x] `--weights <path>` / `--interactive-weights` フラグ
- [x] `--list-detectors` フラグ
- [x] [weights.example.json](../weights.example.json) の整備
- [x] `CompositeDetector` / `score_weights` の unittest（合意ブースト / 重み 0 除外 / 閾値 / 部分失敗）
- [x] 既知バグ修正: 単一候補時の min-max 正規化が 0 になる件、寄与 0 bin が NMS に拾われる件

### 第3セッション-c（メタワーク: AI 駆動開発のための土台整備）
- [x] GitHub Actions CI ([.github/workflows/test.yml](../.github/workflows/test.yml)): Python 3.11/3.12 + ffmpeg で `unittest discover` を push/PR で自動実行
- [x] [docs/schemas.md](schemas.md): JSON 出力契約のフィールド単位リファレンス（dataclass 変更時の同期ルールを CLAUDE.md に追記）
- [x] CLAUDE.md の remote/CI note を更新（git remote 確定、`main` の upstream 設定、CI 場所を明記）
- [x] `.gitignore` 整理（dead な `!output/.gitkeep` 行を削除、`output/` 全体を ignore に統一）
- [x] `output/.gitkeep` 削除（パイプラインが実行時に作成するので tracking 不要）

## 未完了 / 次にやること

### 優先度: 高（次のセッションで触る想定）

- [ ] 実動画での end-to-end 動作確認（合成2分動画でしか試していない。実配信アーカイブで挙動を見る）
- [ ] `audio_rms` のチューニング: NMS gap、bin 秒数、score 正規化のレンジを実動画でキャリブレーション
- [ ] `--config <path>` で YAML/TOML 読み込み（CLI フラグが増えてきたら）

### 優先度: 中（検出器の本実装）

- [ ] `scenedetect` 検出器: PySceneDetect 導入。`requirements.txt` の `scenedetect` を有効化
- [ ] スコア合成方式の高度化: 現状は per-bin 重み付き和（min-max 正規化 + 線形合成）。RRF（Reciprocal Rank Fusion）を選べるよう `weights.fusion: "weighted_sum" | "rrf"` を追加
- [ ] チャットログ変換アダプタ: Twitch chat replay / YouTube yt-dlp 出力 → 標準形式 JSON
- [ ] チャット詳細スコア: ユニークユーザ数だけでなく「草 / lol / w連投」など特定リアクションの数を信号に

### 優先度: 中（段階的要約）

- [ ] Whisper 連携モジュール `transcribe.py`（`faster-whisper` ベース、字幕 SRT を `output/transcript.srt` に出す）
- [ ] 字幕を入力にした LLM スコアリング検出器
- [ ] 「1h → 10min ダイジェスト」をまず作り、その中から短尺をさらに切り出すカスケード構成

### 優先度: 低（運用・配信）

- [ ] サムネイル自動生成（候補区間中央のフレーム抜き）
- [ ] 字幕焼き込み（`drawtext` または `subtitles` フィルタ）
- [ ] GitHub Actions ワークフロー（PR で changed sample に対し自動でプランだけ生成）
- [ ] バッチランナー（`samples/*.mp4` を全部回す）
- [ ] Web UI（`clip_plan.json` を読んで人間がレビュー / 編集 / approve）
- [ ] SNS 投稿アダプタ（YouTube Shorts / X / TikTok）

## 設計判断ログ

### 2026-05-09（第3セッション-b）

**スコア正規化: min-max を採用（z-score / RRF は将来オプション）**
- 候補:
  1. min-max（各検出器のスコアを `(x-min)/(max-min)` で [0,1] に揃える）
  2. z-score（平均 0 / σ 1 に正規化）
  3. RRF（順位ベース合成、値そのものは捨てる）
- 採用: (1)
- 理由:
  - 非エンジニアが重みを直感で設定できる（重み 1.0 なら最大寄与 1.0、重み 2.0 なら最大寄与 2.0）
  - スコアが [0,1] に乗っているので `min_score` 閾値が直感的（「合成スコア 0.5 以上を採用」が自然）
  - z-score は「σの何倍」になり閾値が解釈しにくい
  - RRF は値情報を捨てる代わりに分布に頑健だが、小規模データでは順位差を過大評価しやすい
- 既知の弱点:
  - 外れ値1個で他のスコアが潰れる（優先度中タスクに RRF オプション追加で対処予定）
  - 動画間でスコアを比較できない（同じ閾値でも動画ごとに意味が変わる）
- トレードオフ: 単純さ優先。複数動画にまたがる絶対比較が必要になったら z-score を併設

**重み設定の外部化: JSON ファイル + stdin 対話入力の両対応**
- 候補:
  1. CLI フラグだけ（`--weight-audio-rms 1.0 --weight-comment-density 2.0`）
  2. JSON 設定ファイル（`--weights weights.json`）
  3. 対話入力（`--interactive-weights`）
  4. 上記の組み合わせ
- 採用: (2) + (3) の併用
- 理由:
  - (1) は検出器が増えるとフラグが爆発する
  - (2) は再現性が高い（CI 等で使える）。コミット可能
  - (3) は非エンジニアが触りやすい。実行直前にチューニング可能。任意で (2) に保存できる
- 単一検出器の場合は重み設定不要 → `--weights` は `--detector composite` のときだけ意味を持つ

**`CompositeDetector` で空寄与 bin を NMS から除外（既知バグ修正）**
- 初版では `combined[bin] >= min_score` で sort しており、`min_score=0`（デフォルト）のときに **どの検出器も寄与していない bin** が NMS で拾われる事故があった
- 修正: `combined[bin] > 0 AND combined[bin] >= min_score` に変更。寄与ゼロ bin は無条件で除外

**単一候補時の min-max 正規化を 1.0 に（既知バグ修正）**
- 初版では候補が1つだけのとき `s_max == s_min` → 正規化値 0 → その検出器が寄与しない問題
- 修正: `s_max <= s_min` のとき norm = 1.0（全候補を最大寄与として扱う）

### 2026-05-08（第2セッション）

**音声 RMS 抽出は ffmpeg → PCM → Python で計算（astats parsing は採用せず）**
- 候補:
  1. `ffmpeg -af astats=metadata=1:reset=N,ametadata=mode=print` で RMS 値を ffmpeg 側で計算 → テキスト出力をパース
  2. `ffmpeg -f s16le -` で生 PCM を取り出し、Python で RMS を計算
- 採用: (2)
- 理由:
  - astats のテキスト出力フォーマットは ffmpeg のバージョンで微妙に揺れる（`pts_time:` のフォーマット、`-inf` の扱い等）。パースが脆い
  - 生 PCM は `array.array('h')` で確定的にパースでき、テストもしやすい
  - ダウンサンプリング（4kHz / mono）すれば 1時間動画でも処理時間は数秒
  - 将来 Python 側で別の特徴量（ZCR、スペクトル重心、無音率）を足したくなった時にも同じパイプで取れる
- トレードオフ: ffmpeg 内蔵 RMS よりほんの少し遅い（1時間で +数秒）。許容

**検出器契約に `input_path` と `debug_dir` を追加（破壊的変更）**
- 既存 `EvenSamplingDetector` も新シグネチャに合わせたが、duration 以外は無視
- キーワード引数化（`*,`）したので呼び出し側は明示的になり、誤呼び出しは即 TypeError で検出される
- `debug_dir` はオプション。検出器が中間特徴量（RMS 系列など）をデバッグ用に dump できる

**`--from-plan` を実装、設定ファイル対応は後回し**
- 「プランを手で編集してから export だけ走らせる」運用が現実的に必要
- `--config` のような設定ファイル対応は CLI フラグが増えてから（YAGNI）

### 2026-05-08（第1セッション）

**Python + subprocess 方式を採用、Python ビデオライブラリは導入しない**
- 候補: `ffmpeg-python`, `av`, `moviepy`
- 採用理由: 依存ゼロで動く・ffmpeg 自体が安定 API・エラーメッセージが ffmpeg のものそのままで debug しやすい
- 将来見直し条件: in-process でのフレーム解析が必要になったら `av` を追加

**検出器の引数を `duration: float` だけにした**
- プレースホルダ用には十分。実検出器（audio_rms など）が出てきた時点で `input_path` などを足す
- 早期一般化を避け、最初の実検出器が決まってから契約を見直す

**`--export-clips` をデフォルト off**
- 1時間動画から 6 本切り出すと、再エンコードで数分単位の時間がかかる
- 最初の運用は「プランだけ出して人間がレビュー → OK なら export」を想定

**dataclass + JSON、pydantic / DB は入れない**
- スキーマが固まる前に重い依存を入れたくない
- 将来 schema が安定し、外部に公開する API が出てきたら pydantic に上げる

### 既知の懸念

- リポジトリ名のずれ: GitHub URL は `video-workflow-agent` だがローカルは `video-workflow`。リモート push 時に判断必要
- ffmpeg `-ss` を `-i` の前に置いているので高速だが精度はキーフレーム単位。フレーム正確に切りたい場合は `-i` の後ろに移すこと
- `clip_exporter` で並列化していない。1時間動画から 6 本切るくらいなら問題ないが、本数が増えたら `concurrent.futures` 化すべき
