あなたはこのリポジトリの開発エージェントです。

対象リポジトリ:
https://github.com/skuro1115/video-workflow-agent.git

目的:
長尺動画・配信アーカイブから、面白い箇所や使えそうな場面を検出し、段階的に短尺動画へ加工するための自動化基盤を作りたいです。

想定する最終ゴール:
- 1時間程度の動画を入力する
- 動画全体を解析する
- 盛り上がりポイント / ホットポイント候補を抽出する
- 1時間 → 10分要約 → 10秒〜60秒程度の短尺クリップ複数本、のように段階的に切り抜く
- どの時間帯をなぜ切り抜いたかをメタデータとして保存する
- 将来的には字幕生成、SNS投稿、サムネイル生成、自動レビューにも拡張できる構成にする

今回の作業時間:
約60分を想定してください。
60分で完璧に完成させる必要はありません。
ただし、終わった時点で「次に人間が見て判断・継続できる状態」にしてください。

今回やってほしいこと:
1. まずリポジトリ全体を確認してください
2. 現在の構成・不足しているもの・技術的な課題を整理してください
3. このプロジェクトのREADMEを整備してください
4. docs/ 以下に、最低限以下のドキュメントを作成してください
   - docs/project_overview.md
   - docs/architecture.md
   - docs/workflow.md
   - docs/tasks.md
5. 実装できそうであれば、最小構成の処理パイプラインを作ってください
   - input video pathを受け取る
   - ffmpegで動画情報を取得する
   - 仮のホットポイント候補を生成する
   - clips/ または output/ に切り抜き予定情報をJSONで保存する
6. 可能なら、実際の動画切り抜き処理の雛形まで作ってください
7. 最後に、今回やったこと・未完了・次にやるべきことをまとめてください

重要な設計方針:
- いきなり複雑なAI解析を実装しない
- まずは「長尺動画 → メタデータ抽出 → ホットポイント候補 → クリップ生成」のパイプラインを通す
- AI解析部分は後から差し替えられるようにする
- ffmpeg依存部分、解析ロジック、出力管理、設定管理を分離する
- 小さく動くものを優先する
- 将来的にCLI、Web UI、バッチ処理、GitHub Actions、ローカル常駐処理に拡張できるようにする

技術スタックの判断:
まだ未確定の場合は、Python中心で進めてください。
理由:
- ffmpeg連携がしやすい
- OpenAI / Whisper / moviepy / scenedetect などと相性が良い
- 自動化パイプラインを作りやすい

推奨構成:
- src/
  - main.py
  - config.py
  - video_info.py
  - hotspot_detector.py
  - clip_planner.py
  - clip_exporter.py
- docs/
  - project_overview.md
  - architecture.md
  - workflow.md
  - tasks.md
- output/
  - .gitkeep
- samples/
  - .gitkeep
- README.md
- requirements.txt

今回のMVP仕様:
CLIで以下のように実行できる状態を目指してください。

python -m src.main --input samples/sample.mp4 --output output/

最低限の出力:
- video_info.json
- hotspot_candidates.json
- clip_plan.json

hotspot_candidates.json の例:
[
  {
    "start": 120,
    "end": 180,
    "score": 0.72,
    "reason": "temporary placeholder: evenly sampled segment"
  }
]

clip_plan.json の例:
[
  {
    "clip_id": "clip_001",
    "source_start": 120,
    "source_end": 180,
    "duration": 60,
    "purpose": "short clip candidate",
    "status": "planned"
  }
]

注意:
- 実動画がない場合でも、コード構造とJSON出力仕様を整備してください
- ffmpegがない環境でも、エラーメッセージが分かりやすく出るようにしてください
- 既存ファイルがある場合は、壊さずに改善してください
- 大きな設計変更をする場合は、docs/tasks.mdに理由を残してください
- 不明点で止まらず、合理的な仮定を置いて進めてください

作業の進め方:
- 最初にリポジトリを調査する
- その後、実装方針を短く決める
- 小さい単位でファイルを作成・修正する
- 可能なら最後に簡単な動作確認をする
- 最後に必ずサマリーを出す

最終出力で必ず書いてほしいこと:
1. 変更したファイル一覧
2. 実装したこと
3. 実行方法
4. まだ仮実装の部分
5. 次に人間が判断すべきこと
6. 次にClaudeへ依頼するならどんなプロンプトがよいか

この60分では「完成」よりも「継続開発できる土台」を優先してください。

