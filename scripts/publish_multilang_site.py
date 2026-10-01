from __future__ import annotations

import argparse
import email.utils
import json
import os
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "cloud_languages.yaml"
ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM = "http://www.w3.org/2005/Atom"
PODCAST = "https://podcastindex.org/namespace/1.0"
ET.register_namespace("itunes", ITUNES)
ET.register_namespace("atom", ATOM)
ET.register_namespace("podcast", PODCAST)

CHANNEL_DESCRIPTION = "最新ニュースを題材にした、ドイツ語・スペイン語・ロシア語・中国語・韓国語のデイリー語学ラジオ。"


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


def vtt_timestamp(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def vtt_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_vtt(lines: list[dict], hosts: dict) -> str:
    """WebVTT transcript for podcast apps (podcast:transcript) and the web player."""
    cues = ["WEBVTT", ""]
    for number, line in enumerate(lines, start=1):
        speaker = vtt_escape(hosts.get(line["speaker"], line["speaker"]))
        cues += [
            str(number),
            f"{vtt_timestamp(line['start'])} --> {vtt_timestamp(line['end'])}",
            f"<v {speaker}>{vtt_escape(line['text'])}",
            "",
        ]
    return "\n".join(cues)


def utf16_index(text: str, index: int) -> int:
    """Convert a Python code-point index to the UTF-16 index the browser uses for String.slice()."""
    return len(text[:index].encode("utf-16-le")) // 2


def word_ranges(text: str, words: list) -> list[list]:
    """Locate each spoken word in the line: [start_s, end_s, from, to] with UTF-16 offsets.

    Words are matched left to right; a word the TTS engine spelled differently is skipped
    rather than highlighted in the wrong place.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    if len(folded) != len(text):
        folded = text.casefold() if len(text.casefold()) == len(text) else text
    ranges: list[list] = []
    cursor = 0
    for start, end, word in words:
        token = unicodedata.normalize("NFKC", str(word)).strip().casefold()
        if not token:
            continue
        found = folded.find(token, cursor)
        if found < 0:
            continue
        cursor = found + len(token)
        ranges.append([float(start), float(end), utf16_index(text, found), utf16_index(text, cursor)])
    return ranges


def timed_lines(utterances: list[dict], timeline: list | None, words: list | None = None) -> list[dict]:
    if timeline is not None and len(timeline) != len(utterances):
        raise ValueError(f"timeline has {len(timeline)} entries for {len(utterances)} utterances")
    if words is not None and len(words) != len(utterances):
        raise ValueError(f"word timings have {len(words)} entries for {len(utterances)} utterances")
    lines: list[dict] = []
    for index, utterance in enumerate(utterances):
        line = {key: utterance[key] for key in ("speaker", "language", "text", "ja", "slow") if key in utterance}
        if timeline is not None:
            line["start"], line["end"] = float(timeline[index][0]), float(timeline[index][1])
        if words:
            ranges = word_ranges(utterance["text"], words[index])
            if ranges:
                line["w"] = ranges
        lines.append(line)
    return lines


def show_notes(episode: dict, page_url: str) -> str:
    parts: list[str] = [f"日本語ナビ付き {episode['japanese_name']} Journey Talk。ニュース会話、復習、豆知識を収録。"]
    if episode.get("summary_ja"):
        parts.append(episode["summary_ja"])
    if episode.get("highlights"):
        parts.append("今日の表現:\n" + "\n".join(f"・{x}" for x in episode["highlights"]))
    parts.append(f"スクリプト・単語・クイズ: {page_url}")
    return "\n\n".join(parts)


def build_feed(
    history: list[dict],
    base_url: str,
    slug: str | None = None,
    title: str = "Journey Talk",
    description: str = CHANNEL_DESCRIPTION,
    image: str = "covers/journey-talk.png",
) -> ET.ElementTree:
    feed_path = f"feeds/{slug}.xml" if slug else "feed.xml"
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    text_node(channel, "title", title)
    text_node(channel, "link", base_url)
    text_node(channel, "language", "ja")
    text_node(channel, "description", description)
    text_node(channel, f"{{{ITUNES}}}author", "Journey Talk")
    text_node(channel, f"{{{ITUNES}}}explicit", "false")
    ET.SubElement(channel, f"{{{ITUNES}}}image", {"href": f"{base_url}/{image}"})
    artwork = ET.SubElement(channel, "image")
    text_node(artwork, "url", f"{base_url}/{image}")
    text_node(artwork, "title", title)
    text_node(artwork, "link", base_url)
    ET.SubElement(
        channel,
        f"{{{ATOM}}}link",
        {"href": f"{base_url}/{feed_path}", "rel": "self", "type": "application/rss+xml"},
    )

    for day in history:
        for episode in day["episodes"]:
            if slug and base_slug(episode["slug"]) != slug:
                continue
            page_url = f"{base_url}/#/{day['date']}/{episode['slug']}"
            item = ET.SubElement(channel, "item")
            text_node(item, "title", f"{episode['japanese_name']} — {day['date']} — {episode['title']}")
            text_node(item, "description", show_notes(episode, page_url))
            text_node(item, "link", page_url)
            text_node(item, "guid", f"journey-talk:{day['date']}:{episode['slug']}", isPermaLink="false")
            # The weekend review goes out a few minutes later so podcast apps list it above that day's edition.
            clock = "07:05:00" if episode.get("kind") == "weekly" else "07:00:00"
            local_time = datetime.fromisoformat(f"{day['date']}T{clock}").replace(tzinfo=ZoneInfo("Asia/Tokyo"))
            text_node(item, "pubDate", email.utils.format_datetime(local_time))
            ET.SubElement(
                item,
                "enclosure",
                {"url": episode["audio_url"], "length": str(episode["bytes"]), "type": "audio/mpeg"},
            )
            text_node(item, f"{{{ITUNES}}}duration", str(round(float(episode["duration_seconds"]))))
            text_node(item, f"{{{ITUNES}}}explicit", "false")
            if episode.get("transcript_url"):
                ET.SubElement(
                    item,
                    f"{{{PODCAST}}}transcript",
                    {"url": f"{base_url}/{episode['transcript_url']}", "type": "text/vtt", "language": episode["language"][:2]},
                )
    return ET.ElementTree(rss)


def base_slug(slug: str) -> str:
    """Language slug of an edition: "es" for both "es" and the weekend review "es-weekly"."""
    return slug.split("-", 1)[0]


def write_feed(tree: ET.ElementTree, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    ET.parse(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--episode-dir", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--docs-dir", type=Path, default=Path("docs"))
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", "RyoSAKu610/journey-talk-radio"))
    parser.add_argument("--base-url", default="")
    args = parser.parse_args()

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    hosts = cfg.get("hosts", {})
    owner, repo = args.repository.split("/", 1)
    base_url = args.base_url.rstrip("/") or f"https://{owner.lower()}.github.io/{repo}"
    tag = f"multilang-{args.date}"
    release_base = f"https://github.com/{args.repository}/releases/download/{tag}"

    manifest = json.loads((args.episode_dir / "manifest.json").read_text(encoding="utf-8"))
    media = json.loads((args.media_dir / "media-manifest.json").read_text(encoding="utf-8"))
    media_by_slug = {x["slug"]: x for x in media["episodes"]}
    detail_dir = args.docs_dir / "episodes" / args.date
    detail_dir.mkdir(parents=True, exist_ok=True)

    episodes: list[dict] = []
    for item in manifest["episodes"]:
        episode = json.loads((args.episode_dir / item["json"]).read_text(encoding="utf-8"))
        media_item = media_by_slug[item["slug"]]
        audio_url = f"{release_base}/{media_item['audio']}"
        # Same-origin low-bitrate copy, staged onto GitHub Pages for the last few days only.
        offline_url = f"offline/{args.date}/{media_item['offline_audio']}" if media_item.get("offline_audio") else ""
        lines = timed_lines(episode["utterances"], media_item.get("timeline"), media_item.get("words"))
        detail = {
            "date": args.date,
            "slug": item["slug"],
            "kind": episode.get("kind", "daily"),
            "language": item["language"],
            "japanese_name": item["japanese_name"],
            "title": episode["title"],
            "summary_ja": episode.get("summary_ja", ""),
            "audio_url": audio_url,
            "offline_url": offline_url,
            "duration_seconds": media_item["duration_seconds"],
            "hosts": hosts,
            "stories": episode.get("stories", manifest["stories"]),
            "lines": lines,
            "vocabulary": episode.get("vocabulary", []),
            "quiz": episode.get("quiz", []),
        }
        detail_path = detail_dir / f"{item['slug']}.json"
        detail_path.write_text(json.dumps(detail, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        entry = {
            "slug": item["slug"],
            "kind": episode.get("kind", "daily"),
            "language": item["language"],
            "japanese_name": item["japanese_name"],
            "title": episode["title"],
            "summary_ja": detail["summary_ja"],
            "highlights": [f"{x['term']} — {x['meaning_ja']}" for x in detail["vocabulary"][:5]],
            "audio_url": audio_url,
            "offline_url": offline_url,
            "offline_bytes": media_item.get("offline_bytes", 0),
            "script_url": f"{release_base}/{item['markdown']}",
            "detail_url": f"episodes/{args.date}/{detail_path.name}",
            "duration_seconds": media_item["duration_seconds"],
            "bytes": media_item["bytes"],
        }
        if all("start" in x for x in lines):
            vtt_path = detail_dir / f"{item['slug']}.vtt"
            vtt_path.write_text(build_vtt(lines, hosts), encoding="utf-8")
            entry["transcript_url"] = f"episodes/{args.date}/{vtt_path.name}"
        episodes.append(entry)

    args.docs_dir.mkdir(parents=True, exist_ok=True)
    history_path = args.docs_dir / "episodes.json"
    history = [x for x in load_history(history_path) if x.get("date") != args.date]
    history.insert(0, {"date": args.date, "stories": manifest["stories"], "episodes": episodes})
    history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    write_feed(build_feed(history, base_url), args.docs_dir / "feed.xml")
    for lang in cfg["languages"]:
        write_feed(
            build_feed(
                history,
                base_url,
                slug=lang["slug"],
                title=f"Journey Talk — {lang['japanese_name']}",
                description=f"最新ニュースを題材にした、日本語ナビ付き{lang['japanese_name']}のデイリー語学ラジオ。",
                image=f"covers/{lang['slug']}.png",
            ),
            args.docs_dir / "feeds" / f"{lang['slug']}.xml",
        )
    print(f"[site] {history_path}")
    print(f"[site] {detail_dir}")
    print(f"[feed] {args.docs_dir / 'feed.xml'} + {len(cfg['languages'])} language feeds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
