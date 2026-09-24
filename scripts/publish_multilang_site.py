from __future__ import annotations

import argparse
import email.utils
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)


def text_node(parent: ET.Element, tag: str, value: str, **attrs: str) -> ET.Element:
    node = ET.SubElement(parent, tag, attrs)
    node.text = value
    return node


def load_history(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("docs/episodes.json must be an array")
    return data


def build_feed(history: list[dict], base_url: str) -> ET.ElementTree:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    text_node(channel, "title", "Journey Talk")
    text_node(channel, "link", base_url)
    text_node(channel, "language", "ja")
    text_node(channel, "description", "最新ニュースを題材にした、ドイツ語・スペイン語・ロシア語・中国語・韓国語のデイリー語学ラジオ。")
    text_node(channel, f"{{{ITUNES}}}author", "Journey Talk")
    text_node(channel, f"{{{ITUNES}}}explicit", "false")
    ET.SubElement(
        channel,
        f"{{{ATOM}}}link",
        {"href": f"{base_url}/feed.xml", "rel": "self", "type": "application/rss+xml"},
    )

    for day in history:
        for episode in day["episodes"]:
            item = ET.SubElement(channel, "item")
            text_node(item, "title", f"{episode['japanese_name']} — {day['date']} — {episode['title']}")
            text_node(item, "description", f"日本語ナビ付き {episode['japanese_name']} Journey Talk。ニュース会話、復習、豆知識を収録。")
            text_node(item, "guid", f"journey-talk:{day['date']}:{episode['slug']}", isPermaLink="false")
            local_time = datetime.fromisoformat(f"{day['date']}T07:00:00").replace(tzinfo=ZoneInfo("Asia/Tokyo"))
            text_node(item, "pubDate", email.utils.format_datetime(local_time))
            ET.SubElement(
                item,
                "enclosure",
                {"url": episode["audio_url"], "length": str(episode["bytes"]), "type": "audio/mpeg"},
            )
            text_node(item, f"{{{ITUNES}}}duration", str(round(float(episode["duration_seconds"]))))
            text_node(item, f"{{{ITUNES}}}explicit", "false")
    return ET.ElementTree(rss)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--episode-dir", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--docs-dir", type=Path, default=Path("docs"))
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", "RyoSAKu610/journey-talk-radio"))
    parser.add_argument("--base-url", default="")
    args = parser.parse_args()

    owner, repo = args.repository.split("/", 1)
    base_url = args.base_url.rstrip("/") or f"https://{owner}.github.io/{repo}"
    tag = f"multilang-{args.date}"
    release_base = f"https://github.com/{args.repository}/releases/download/{tag}"

    manifest = json.loads((args.episode_dir / "manifest.json").read_text(encoding="utf-8"))
    media = json.loads((args.media_dir / "media-manifest.json").read_text(encoding="utf-8"))
    media_by_slug = {x["slug"]: x for x in media["episodes"]}

    episodes: list[dict] = []
    for item in manifest["episodes"]:
        episode = json.loads((args.episode_dir / item["json"]).read_text(encoding="utf-8"))
        media_item = media_by_slug[item["slug"]]
        episodes.append(
            {
                "slug": item["slug"],
                "language": item["language"],
                "japanese_name": item["japanese_name"],
                "title": episode["title"],
                "audio_url": f"{release_base}/{media_item['audio']}",
                "script_url": f"{release_base}/{item['markdown']}",
                "duration_seconds": media_item["duration_seconds"],
                "bytes": media_item["bytes"],
            }
        )

    args.docs_dir.mkdir(parents=True, exist_ok=True)
    history_path = args.docs_dir / "episodes.json"
    history = [x for x in load_history(history_path) if x.get("date") != args.date]
    history.insert(0, {"date": args.date, "stories": manifest["stories"], "episodes": episodes})
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    tree = build_feed(history, base_url)
    ET.indent(tree, space="  ")
    feed_path = args.docs_dir / "feed.xml"
    tree.write(feed_path, encoding="utf-8", xml_declaration=True)
    ET.parse(feed_path)
    print(f"[site] {history_path}")
    print(f"[feed] {feed_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
