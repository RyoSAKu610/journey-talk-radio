from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

import edge_tts
import feedparser
from dotenv import load_dotenv
from google import genai

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output-v01"
SEGMENTS = OUT / "segments"

FEEDS = [
    ("NHK", "https://www3.nhk.or.jp/rss/news/cat0.xml"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Reuters via Google News", "https://news.google.com/rss/search?q=when:1d+Reuters&hl=en-US&gl=US&ceid=US:en"),
]

VOICES = {
    "ja": {"F": "ja-JP-NanamiNeural", "M": "ja-JP-KeitaNeural"},
    "de": {"F": "de-DE-KatjaNeural", "M": "de-DE-ConradNeural"},
    "es": {"F": "es-ES-ElviraNeural", "M": "es-ES-AlvaroNeural"},
    "ru": {"F": "ru-RU-SvetlanaNeural", "M": "ru-RU-DmitryNeural"},
    "zh": {"F": "zh-CN-XiaoxiaoNeural", "M": "zh-CN-YunxiNeural"},
    "ko": {"F": "ko-KR-SunHiNeural", "M": "ko-KR-InJoonNeural"},
}


def strip_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def fetch_news() -> list[dict]:
    items: list[dict] = []
    for source, url in FEEDS:
        feed = feedparser.parse(url)
        if not feed.entries:
            continue
        entry = feed.entries[0]
        items.append(
            {
                "source": source,
                "title": strip_html(getattr(entry, "title", "")),
                "summary": strip_html(getattr(entry, "summary", ""))[:1000],
                "link": getattr(entry, "link", ""),
            }
        )
    if not items:
        raise RuntimeError("No RSS items were retrieved.")
    return items


def build_prompt(news: list[dict]) -> str:
    news_json = json.dumps(news, ensure_ascii=False, indent=2)
    return f"""
Create today's episode of a casual language-learning podcast called Journey Talk.

Goal:
- About 10 to 15 minutes when spoken at a natural pace.
- Two hosts: F and M, alternating naturally.
- Japanese navigation plus German, Spanish, Russian, Mandarin Chinese, and Korean.
- Use today's news only as light conversation material. Do not turn this into a hard-news bulletin.
- Explain difficult points in simple Japanese.
- Make the foreign-language conversations practical and natural, suitable for learners.
- Reuse the same basic conversational idea across languages without making every line a literal translation.
- Near the end, include a short review section.
- Finish with a trivia / culture / useful-expression section for EACH of the five target languages. The trivia should be useful in real conversation, like a small cultural nuance, common TV/drama/anime-like phrase, filler, reaction, or everyday expression.
- Do not quote long passages from the articles. Paraphrase news facts.

Return JSON only with this schema:
{{
  "title": "...",
  "news_sources": [{{"source":"...","title":"...","link":"..."}}],
  "segments": [
    {{"speaker":"F|M","language":"ja|de|es|ru|zh|ko","text":"..."}}
  ]
}}

The episode must include all six language codes at least once, and both speakers.
Keep individual segments short enough to sound like real back-and-forth conversation.

Today's RSS items:
{news_json}
""".strip()


def parse_json_response(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data = json.loads(text)
    if not isinstance(data.get("segments"), list) or not data["segments"]:
        raise ValueError("Gemini response has no segments.")
    allowed_langs = set(VOICES)
    speakers = set()
    langs = set()
    for seg in data["segments"]:
        if seg.get("speaker") not in {"F", "M"}:
            raise ValueError("Invalid speaker in Gemini response.")
        if seg.get("language") not in allowed_langs:
            raise ValueError("Invalid language in Gemini response.")
        if not isinstance(seg.get("text"), str) or not seg["text"].strip():
            raise ValueError("Empty segment text in Gemini response.")
        speakers.add(seg["speaker"])
        langs.add(seg["language"])
    if speakers != {"F", "M"}:
        raise ValueError("Both speakers are required.")
    missing = allowed_langs - langs
    if missing:
        raise ValueError(f"Missing languages: {sorted(missing)}")
    return data


def generate_script(news: list[dict]) -> dict:
    load_dotenv(ROOT / ".env")
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is missing. Copy .env.example to .env and add your Google AI Studio key.")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client(api_key=key)
    response = client.models.generate_content(model=model, contents=build_prompt(news))
    if not response.text:
        raise RuntimeError("Gemini returned no text.")
    return parse_json_response(response.text)


async def render_segment(index: int, seg: dict) -> Path:
    lang = seg["language"]
    speaker = seg["speaker"]
    voice = VOICES[lang][speaker]
    path = SEGMENTS / f"{index:04d}.mp3"
    communicate = edge_tts.Communicate(seg["text"], voice)
    await communicate.save(str(path))
    return path


async def render_all(script: dict) -> list[Path]:
    SEGMENTS.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, seg in enumerate(script["segments"], 1):
        print(f"TTS {i}/{len(script['segments'])}: {seg['language']} {seg['speaker']}")
        paths.append(await render_segment(i, seg))
    return paths


def concat_mp3(paths: list[Path], destination: Path) -> None:
    concat_file = OUT / "concat.txt"
    lines = []
    for path in paths:
        escaped = str(path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    concat_file.write_text("\n".join(lines), encoding="utf-8")
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(destination),
    ]
    subprocess.run(cmd, check=True)


def write_markdown(script: dict, path: Path) -> None:
    rows = [f"# {script.get('title', 'Journey Talk')}\n"]
    for seg in script["segments"]:
        rows.append(f"**{seg['speaker']} [{seg['language']}]**  {seg['text']}\n")
    path.write_text("\n".join(rows), encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    news = fetch_news()
    print("RSS: OK")
    script = generate_script(news)
    print("Gemini script: OK")

    stamp = datetime.now().strftime("%Y-%m-%d")
    json_path = OUT / f"{stamp}_script.json"
    md_path = OUT / f"{stamp}_script.md"
    mp3_path = OUT / f"{stamp}_journey-talk.mp3"

    json_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(script, md_path)
    paths = asyncio.run(render_all(script))
    concat_mp3(paths, mp3_path)

    print("DONE")
    print(json_path)
    print(md_path)
    print(mp3_path)


if __name__ == "__main__":
    main()
