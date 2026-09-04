from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--episode", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--privacy", choices=("private", "unlisted", "public"), default="public")
    args = parser.parse_args()
    if args.state.is_file():
        state = json.loads(args.state.read_text(encoding="utf-8"))
        if state.get("video_id"):
            print(f"YouTube already recorded: {state['video_id']}")
            return 0

    required = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("Missing YouTube secrets: " + ", ".join(missing))
    episode = json.loads(args.episode.read_text(encoding="utf-8"))
    episode_date = episode["episode_date"]
    credentials = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        scopes=[SCOPE],
    )
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    body = {
        "snippet": {
            "title": f"Journey Talk — {episode_date}",
            "description": "日本語ナビで世界のニュースと5言語を学ぶ、10〜15分のデイリーラジオ。",
            "tags": ["語学", "ニュース", "ドイツ語", "スペイン語", "ロシア語", "中国語", "韓国語"],
            "categoryId": "27",
        },
        "status": {"privacyStatus": args.privacy, "selfDeclaredMadeForKids": False},
    }
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(str(args.video), mimetype="video/mp4", resumable=True),
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    video_id = response["id"]
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(
        json.dumps({"episode_date": episode_date, "video_id": video_id, "url": f"https://youtu.be/{video_id}"}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"https://youtu.be/{video_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
