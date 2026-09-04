from __future__ import annotations

import argparse
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow


SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def main() -> int:
    parser = argparse.ArgumentParser(description="One-time helper to obtain the YouTube refresh token")
    parser.add_argument("client_secrets", type=Path)
    args = parser.parse_args()
    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secrets), [SCOPE])
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    client = json.loads(args.client_secrets.read_text(encoding="utf-8"))
    client_data = client.get("installed") or client.get("web") or {}
    print("Set these GitHub Actions secrets:")
    print(f"YOUTUBE_CLIENT_ID={client_data.get('client_id', '')}")
    print(f"YOUTUBE_CLIENT_SECRET={client_data.get('client_secret', '')}")
    print(f"YOUTUBE_REFRESH_TOKEN={credentials.refresh_token or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
