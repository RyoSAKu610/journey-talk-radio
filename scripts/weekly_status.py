from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--youtube-state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runs = json.loads(args.runs.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    successes = sum(run.get("conclusion") == "success" for run in runs)
    failures = sum(run.get("conclusion") == "failure" for run in runs)
    youtube = json.loads(args.youtube_state.read_text(encoding="utf-8")) if args.youtube_state.is_file() else {}
    blockers = []
    if failures:
        blockers.append(f"直近実行の失敗: {failures}件（Actionsログ確認）")
    if not youtube.get("video_id"):
        blockers.append("YouTube公開状態が未記録")
    if not blockers:
        blockers.append("確認できる重大ブロッカーなし")
    body = [
        "## Journey Talk 週次ステータス",
        "",
        "### 進捗",
        f"- 直近Actions: 成功 {successes}件 / 失敗 {failures}件",
        f"- 最新エピソード: {manifest['episode_date']} / {manifest['duration_seconds']}秒 / {manifest['utterances']}発話",
        f"- YouTube: {youtube.get('url', '未記録')}",
        "- Spotify: RSS更新後に自動取得される構成",
        "",
        "### ブロッカー",
        *[f"- {item}" for item in blockers],
        "",
        "### 次のマイルストーン",
        "- 次回日次実行の成功を確認",
        "- RSSの到達性と最新enclosureを確認",
        "- YouTubeの処理完了と公開状態を確認",
        "",
    ]
    args.output.write_text("\n".join(body), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
