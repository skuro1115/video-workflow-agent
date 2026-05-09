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

## 未完了 / 次にやること

### 優先度: 高（次のセッションで触る想定）

- [ ] 実動画での end-to-end 動作確認（合成2分動画でしか試していない。実配信アーカイブで挙動を見る）
- [ ] `audio_rms` のチューニング: NMS gap、bin 秒数、score 正規化のレンジを実動画でキャリブレーション
- [ ] `--config <path>` で YAML/TOML 読み込み（CLI フラグが増えてきたら）

### 優先度: 中（検出器の本実装）

- [ ] `scenedetect` 検出器: PySceneDetect 導入。`requirements.txt` の `scenedetect` を有効化
- [ ] 複合検出器: 音声 RMS + コメント密度 + シーン検出をスコア合成（重み付き平均 or RRF）
- [ ] スコアの正規化方針を確定（検出器ごとに [0, 1] に揃える、min-max vs z-score）
- [ ] チャットログ変換アダプタ: Twitch chat replay / YouTube yt-dlp 出力 → 標準形式 JSON

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
