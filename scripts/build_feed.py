from __future__ import annotations

import argparse
import email.utils
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)


def _text(parent: ET.Element, tag: str, value: str, **attrs: str) -> ET.Element:
    node = ET.SubElement(parent, tag, attrs)
    node.text = value
    return node


def load_or_create(feed_path: Path, base_url: str, author: str, email: str, description: str) -> ET.ElementTree:
    if feed_path.is_file():
        return ET.parse(feed_path)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    _text(channel, "title", "Journey Talk")
    _text(channel, "link", base_url)
    _text(channel, "language", "ja")
    _text(channel, "description", description)
    _text(channel, f"{{{ITUNES}}}author", author)
    owner = ET.SubElement(channel, f"{{{ITUNES}}}owner")
    _text(owner, f"{{{ITUNES}}}name", author)
    _text(owner, f"{{{ITUNES}}}email", email)
    _text(channel, f"{{{ITUNES}}}explicit", "false")
    _text(channel, f"{{{ITUNES}}}image", "", href=f"{base_url}/cover.png")
    ET.SubElement(channel, f"{{{ATOM}}}link", {"href": f"{base_url}/feed.xml", "rel": "self", "type": "application/rss+xml"})
    return ET.ElementTree(rss)


def add_episode(
    tree: ET.ElementTree,
    episode: dict,
    manifest: dict,
    audio_url: str,
    audio_bytes: int,
) -> None:
    channel = tree.getroot().find("channel")
    if channel is None:
        raise ValueError("RSS channel is missing")
    episode_date = episode["episode_date"]
    guid = f"journey-talk:{episode_date}"
    for item in channel.findall("item"):
        if item.findtext("guid") == guid:
            return
    item = ET.Element("item")
    _text(item, "title", f"Journey Talk — {episode_date}")
    _text(item, "description", "日本語ナビで、ドイツ語・スペイン語・ロシア語・中国語・韓国語を巡るデイリー語学ニュース。")
    _text(item, "guid", guid, isPermaLink="false")
    noon = datetime.fromisoformat(f"{episode_date}T06:00:00").replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    _text(item, "pubDate", email.utils.format_datetime(noon))
    ET.SubElement(item, "enclosure", {"url": audio_url, "length": str(audio_bytes), "type": "audio/mpeg"})
    _text(item, f"{{{ITUNES}}}duration", str(round(float(manifest["duration_seconds"]))))
    _text(item, f"{{{ITUNES}}}explicit", "false")
    first_item = channel.find("item")
    channel.insert(list(channel).index(first_item) if first_item is not None else len(channel), item)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--feed", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--audio-url", required=True)
    parser.add_argument("--author", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--description", default="世界のニュースで毎日ことばを旅する、多言語学習ラジオ。")
    args = parser.parse_args()
    episode = json.loads(args.episode.read_text(encoding="utf-8"))
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    tree = load_or_create(args.feed, args.base_url.rstrip("/"), args.author, args.email, args.description)
    add_episode(tree, episode, manifest, args.audio_url, int(manifest["audio_bytes"]))
    ET.indent(tree, space="  ")
    args.feed.parent.mkdir(parents=True, exist_ok=True)
    tree.write(args.feed, encoding="utf-8", xml_declaration=True)
    ET.parse(args.feed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
