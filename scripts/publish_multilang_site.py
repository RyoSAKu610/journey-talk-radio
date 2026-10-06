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

LABELS = {
    "de": "ドイツ語",
    "es": "スペイン語",
    "ru": "ロシア語",
    "zh": "中国語",
    "ko": "韓国語",
}


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


def iter_episodes(history: list[dict], slug: str | None = None):
    emitted = 0
    for day in history:
        for episode in day.get("episodes", []):
            if slug is not None and episode.get("slug") != slug:
                continue
            yield day, episode
            emitted += 1
            if emitted >= 100:
                return


def build_feed(
    history: list[dict],
    base_url: str,
    *,
    slug: str | None,
    author: str,
    email: str,
) -> ET.ElementTree:
    label = LABELS.get(slug, "5言語") if slug else "5言語"
    filename = f"feed-{slug}.xml" if slug else "feed.xml"
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    text_node(channel, "title", f"Journey Talk — {label}")
    text_node(channel, "link", base_url)
    text_node(channel, "language", "ja")
    text_node(
        channel,
        "description",
        f"最新ニュースを題材に、日本語ナビ付きで学ぶJourney Talk {label}版。自然な男女MC会話、復習、豆知識を収録。",
    )
    text_node(channel, f"{{{ITUNES}}}author", author)
    text_node(channel, f"{{{ITUNES}}}explicit", "false")
    if email:
        owner = ET.SubElement(channel, f"{{{ITUNES}}}owner")
        text_node(owner, f"{{{ITUNES}}}name", author)
        text_node(owner, f"{{{ITUNES}}}email", email)
    ET.SubElement(
        channel,
        f"{{{ATOM}}}link",
        {"href": f"{base_url}/{filename}", "rel": "self", "type": "application/rss+xml"},
    )

    for day, episode in iter_episodes(history, slug):
        item = ET.SubElement(channel, "item")
        label_for_item = LABELS.get(episode["slug"], episode["language"])
        text_node(item, "title", f"{label_for_item} — {day['date']} — {episode['title']}")
        text_node(
            item,
            "description",
            f"日本語ナビ付き{label_for_item} Journey Talk。ニュース会話、復習、豆知識を収録。音声QA合格済み。",
        )
        text_node(item, "guid", f"journey-talk:{day['date']}:{episode['slug']}", isPermaLink="false")
        local_time = datetime.fromisoformat(f"{day['date']}T07:10:00").replace(tzinfo=ZoneInfo("Asia/Tokyo"))
        text_node(item, "pubDate", email.utils.format_datetime(local_time))
        ET.SubElement(
            item,
            "enclosure",
            {"url": episode["audio_url"], "length": str(episode["bytes"]), "type": "audio/mpeg"},
        )
        text_node(item, f"{{{ITUNES}}}duration", str(round(float(episode["duration_seconds"]))))
        text_node(item, f"{{{ITUNES}}}explicit", "false")
    return ET.ElementTree(rss)


def write_feed(tree: ET.ElementTree, path: Path) -> None:
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    ET.parse(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--episode-dir", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--qa-report", type=Path, required=True)
    parser.add_argument("--docs-dir", type=Path, default=Path("docs"))
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", "RyoSAKu610/journey-talk-radio"))
    parser.add_argument("--base-url", default="")
    parser.add_argument("--author", default=os.getenv("PODCAST_AUTHOR", "Journey Talk"))
    parser.add_argument("--email", default=os.getenv("PODCAST_EMAIL", ""))
    args = parser.parse_args()

    owner, repo = args.repository.split("/", 1)
    base_url = args.base_url.rstrip("/") or f"https://{owner}.github.io/{repo}"
    tag = f"multilang-{args.date}"
    release_base = f"https://github.com/{args.repository}/releases/download/{tag}"

    manifest = json.loads((args.episode_dir / "manifest.json").read_text(encoding="utf-8"))
    media = json.loads((args.media_dir / "media-manifest.json").read_text(encoding="utf-8"))
    qa = json.loads(args.qa_report.read_text(encoding="utf-8"))
    if qa.get("status") != "PASS":
        raise RuntimeError("QA report is not PASS; refusing to publish")
    qa_by_slug = {item["slug"]: item for item in qa.get("episodes", [])}
    media_by_slug = {item["slug"]: item for item in media["episodes"]}

    episodes: list[dict] = []
    stories: list[dict] = []
    seen_story_keys: set[str] = set()
    for item in manifest["episodes"]:
        slug = item["slug"]
        if qa_by_slug.get(slug, {}).get("status") != "PASS":
            raise RuntimeError(f"{slug}: QA is not PASS; refusing to publish")
        episode = json.loads((args.episode_dir / item["json"]).read_text(encoding="utf-8"))
        media_item = media_by_slug[slug]
        for story in episode.get("stories", []):
            key = story.get("url") or story.get("title")
            if key and key not in seen_story_keys:
                seen_story_keys.add(key)
                stories.append(story)
        episodes.append(
            {
                "slug": slug,
                "language": item["language"],
                "japanese_name": LABELS[slug],
                "title": episode["title"],
                "audio_url": f"{release_base}/{media_item['audio']}",
                "script_url": f"{release_base}/{item['markdown']}",
                "qa_url": f"{release_base}/{slug}-qa.json",
                "duration_seconds": media_item["duration_seconds"],
                "bytes": media_item["bytes"],
                "asr_average_similarity": qa_by_slug[slug]["asr_average_similarity"],
            }
        )

    args.docs_dir.mkdir(parents=True, exist_ok=True)
    history_path = args.docs_dir / "episodes.json"
    history = [item for item in load_history(history_path) if item.get("date") != args.date]
    history.insert(
        0,
        {
            "date": args.date,
            "voice_profile_version": media.get("voice_profile_version"),
            "qa_status": "PASS",
            "stories": stories,
            "episodes": episodes,
        },
    )
    history = history[:400]
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    write_feed(build_feed(history, base_url, slug=None, author=args.author, email=args.email), args.docs_dir / "feed.xml")
    for slug in LABELS:
        write_feed(build_feed(history, base_url, slug=slug, author=args.author, email=args.email), args.docs_dir / f"feed-{slug}.xml")

    health = {
        "status": "PASS",
        "last_published_date": args.date,
        "voice_profile_version": media.get("voice_profile_version"),
        "episode_count": len(episodes),
        "feeds": {"all": f"{base_url}/feed.xml", **{slug: f"{base_url}/feed-{slug}.xml" for slug in LABELS}},
    }
    (args.docs_dir / "health.json").write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[site] {history_path}")
    print(f"[feed] aggregate + {len(LABELS)} language feeds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
