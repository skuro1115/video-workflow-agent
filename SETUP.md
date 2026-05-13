# SETUP — 環境構築と必要バージョン

このリポジトリを動かすために必要なソフトウェア・バージョン・設定をすべてここにまとめる。**バージョン情報の単一の真実源（Single Source of Truth）**。README やその他のドキュメントは、環境周りの記述についてはこのファイルに誘導する。

---

## 1. 必要なもの一覧

| 種別 | 名前 | 必須バージョン | 推奨バージョン | 用途 |
| --- | --- | --- | --- | --- |
| ランタイム | **Python** | 3.11 以上 | 3.12 | パイプライン実行 |
| 外部コマンド | **ffmpeg** | 4.0 以上 | 6.0 以上（手元は 8.0 で動作確認済） | 動画切り出し・音声抽出 |
| 外部コマンド | **ffprobe** | ffmpeg と同梱 | 同上 | 動画メタデータ取得 |
| OS | **macOS / Linux** | macOS 12+ / Ubuntu 22.04+ | — | 開発・CI で動作確認済 |
| OS | **Windows** | 10/11 | WSL2 経由を推奨 | ネイティブは未検証 |
| Python パッケージ（コア） | （なし） | — | — | コアパイプラインは標準ライブラリのみで動作 |
| Python パッケージ（任意） | `yt-dlp` / `chat-downloader` | [requirements.txt](requirements.txt) を参照 | 最新 | 後述の「URL から動画 + チャットを取得する」場合のみ。コアパイプラインには不要 |

| 任意 | 用途 |
| --- | --- |
| **Docker** 24+ + Docker Compose v2+ | コンテナで動かす場合（後述） |
| **git** 2.30+ | リポジトリのクローン |
| **GitHub Actions** | CI で自動テスト（[.github/workflows/test.yml](.github/workflows/test.yml)） |

> **将来追加予定の依存（未実装、参考情報）**: `scenedetect`、`faster-whisper`、`openai`。詳細は [requirements.txt](requirements.txt) のコメント。

---

## 2. ネイティブ環境のセットアップ

### macOS

```bash
# 1. Homebrew で Python 3.12 と ffmpeg を入れる
brew install python@3.12 ffmpeg

# 2. リポジトリを取得
git clone <repository-url> video-workflow
cd video-workflow

# 3. 動作確認
python3 --version           # → 3.11 以上が出ればOK
ffmpeg -version | head -1   # → ffmpeg version ... が出ればOK
ffprobe -version | head -1  # → ffprobe version ... が出ればOK

# 4. テスト実行（ここまで通れば環境OK）
python3 -m unittest discover -s tests

# 5.（任意）URL から動画 + チャットを取得する scripts/fetch.py を使うなら
pip install -r requirements.txt
```

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y python3.12 python3-pip ffmpeg git
git clone <repository-url> video-workflow
cd video-workflow
python3 -m unittest discover -s tests

# （任意）URL から動画 + チャットを取得する scripts/fetch.py を使うなら
pip install -r requirements.txt
```

### Windows（WSL2 推奨）

1. PowerShell（管理者）で `wsl --install` → Ubuntu 22.04 を選ぶ
2. 起動した Ubuntu で上記の Ubuntu 手順をそのまま実行
3. Windows 側のファイルは `/mnt/c/Users/<name>/...` でアクセス可

> ネイティブ Windows でも理論上動くが、ffmpeg のパス通しなど追加作業が必要で、未検証のため WSL2 を推奨。

---

## 3. Docker でセットアップ（ネイティブが嫌な人向け）

ローカルに Python・ffmpeg を入れたくない場合の代替手段。Docker と Docker Compose があれば動く。

```bash
# 1. ビルド（初回 + Dockerfile / requirements.txt 更新時のみ）
docker compose build

# 2. パイプライン実行（samples/ と output/ がコンテナの /app/samples, /app/output にマウントされる）
docker compose run --rm app \
    --input samples/sample.mp4 --output output/ \
    --detector audio_rms --candidates 4 --window 20 --debug

# 3. 検出器一覧
docker compose run --rm app --list-detectors

# 4. テスト実行
docker compose run --rm tests
```

ファイル: [Dockerfile](Dockerfile) / [docker-compose.yml](docker-compose.yml) / [.dockerignore](.dockerignore)

| 何が嬉しいか | 何に注意 |
| --- | --- |
| Python・ffmpeg バージョンを完全固定できる | コンテナ起動オーバーヘッドあり（数秒） |
| ホストを汚さない | GPU パススルーは未設定（CPU のみ） |
| CI と同じ環境で再現できる | macOS では Docker Desktop が必要（有償条件あり） |

---

## 4. 非エンジニア向け：これだけ触ればよいファイル

| ファイル | 何のため | 触り方 |
| --- | --- | --- |
| [weights.example.json](weights.example.json) | 検出器の重み・合成方法 | コピーして `weights.json` を作り、`weight` の数字を変える |
| [samples/chat_log.example.json](samples/chat_log.example.json) | ライブ配信のチャットログ形式 | 自分のデータをこの形式に変換する |
| [samples/](samples/) | 入力動画を置く場所 | 動画ファイルをここに置く |

エンジニア向けの設定（CI、Docker、テスト）は触る必要なし。

### URL から動画 + チャットを自動取得する

YouTube ライブアーカイブ / Twitch VOD なら、URL 1本で動画とチャットを `samples/` に落としてパイプラインまで一気に流せる（要 `pip install -r requirements.txt`）。

```bash
# 取得 + パイプライン実行を1コマンドで
python3 -m src.main --url https://www.youtube.com/watch?v=XXXXXXXXXXX \
    --output output/ --detector composite --weights weights.example.json

# 取得だけ（後でレビュー / 再利用したい場合）
python3 -m scripts.fetch --url https://www.twitch.tv/videos/123456789 \
    --output samples/ --name vodA
```

出力は `samples/<name>.mp4`（動画）と `samples/<name>.chat.json`（アプリの形式に変換済み）の2点。`--fetch-name` 未指定なら URL 由来のIDが入る。

### よくある設定変更例

```json
// weights.json — 「チャットを音声の3倍重視」に変えたい
{
  "detectors": [
    { "name": "audio_rms",       "weight": 1.0 },
    { "name": "comment_density", "weight": 3.0 }
  ],
  "fusion": "weighted_sum",
  "bin_seconds": 1.0,
  "min_score": 0.0
}
```

```bash
# クリップを 6本ではなく 3本だけ作りたい
python3 -m src.main --input video.mp4 --output output/ --candidates 3

# 30秒ではなく 60秒のクリップにしたい
python3 -m src.main --input video.mp4 --output output/ --window 60
```

CLI オプション一覧は [README.md](README.md#cli-オプション) 参照。

---

## 5. 動作確認（環境が壊れたときのチェックリスト）

```bash
# (1) Python が呼べる
python3 --version
# → "Python 3.11.x" 以上

# (2) ffmpeg / ffprobe が呼べる
ffmpeg -version | head -1
ffprobe -version | head -1

# (3) パッケージ依存なし（何も install されていなくてOK）
python3 -c "import sys; print(sys.path)"

# (4) テストが全部通る
python3 -m unittest discover -s tests
# → "OK" で終わればOK

# (5) 合成サンプルでパイプラインが流れる
ffmpeg -y -f lavfi -i "testsrc=duration=10:size=320x240:rate=30" \
       -f lavfi -i "sine=frequency=440:duration=10" \
       -c:v libx264 -preset ultrafast -c:a aac -shortest samples/_smoke.mp4

python3 -m src.main --input samples/_smoke.mp4 --output output/ \
    --detector audio_rms --candidates 2 --window 3 --min-duration 2

ls output/
# → video_info.json hotspot_candidates.json clip_plan.json が出ればOK
```

(1)〜(5) のどこで失敗したかで原因を切り分けられる。

---

## 6. トラブルシューティング

| 症状 | 原因 | 対処 |
| --- | --- | --- |
| `command not found: ffmpeg` | ffmpeg 未インストールまたは PATH 不通 | `brew install ffmpeg` / `apt install ffmpeg` を再実行 |
| `ffprobe failed for ...` | 入力動画が壊れている / コーデック非対応 | `ffprobe <file>` を直接叩いてエラー内容を確認 |
| `Output file does not contain any stream` | 入力動画に音声トラックが無い | 既知の挙動: `audio_rms` は 0 候補で正常終了。問題なし |
| `Python 3.11` 以上が必要 | 古い Python | `brew install python@3.12` / `apt install python3.12` で更新 |
| Docker でビルドが遅い | キャッシュ未利用 | 2回目以降は `docker compose build` の差分ビルドで高速化 |
| `weights file not found` | `--weights <path>` のパスが間違っている | 絶対パスで指定するか、カレントディレクトリを確認 |
| `required command not found on PATH: 'yt-dlp'` | `--url` を使うのに依存未インストール | `pip install -r requirements.txt` |
| `required command not found on PATH: 'chat-downloader'` | Twitch URL のチャット取得に必須 | `pip install -r requirements.txt`（`chat-downloader` も入る） |
| `yt-dlp produced no live_chat.json` | YouTube 動画にチャットリプレイが無い（通常動画 / 配信前削除等） | `--skip-chat`（`scripts/fetch.py`）を使うか、`--detector audio_rms` に切り替える |
| `yt-dlp failed (exit ...)` | URL 制限 / age-restricted / リージョン制限 / 動画削除 | yt-dlp 単体で再現確認 → 必要なら `yt-dlp -U` で更新 |

---

## 7. バージョンを上げるとき

- **Python のバージョンを変える**: [.github/workflows/test.yml](.github/workflows/test.yml) の `python-version` matrix を更新 → CI で動作確認 → このファイルの表を更新
- **ffmpeg メジャーバージョンを上げる**: 検出器の挙動（特に `astats`、`aevalsrc`）が微妙に変わることがある。`tests/` を流して回帰がないか確認
- **Docker ベースイメージを変える**: [Dockerfile](Dockerfile) の `FROM python:...` 行を変える → このファイルの表を更新

このファイルを更新したら、必ず [docs/tasks.md](docs/tasks.md) の設計判断ログにも追記する（なぜそのバージョンに上げたか）。
