from __future__ import annotations

import argparse
import html
import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "cloud_languages.yaml"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def clean(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def gemini_json(prompt: str, cfg: dict) -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    model = os.getenv("GEMINI_MODEL", "").strip() or cfg["provider"]["model"]
    endpoint = GEMINI_ENDPOINT.format(model=model)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": cfg["episode"]["temperature"],
            "maxOutputTokens": cfg["episode"]["max_output_tokens"],
            "responseMimeType": "application/json",
        },
    }
    response = requests.post(endpoint, params={"key": api_key}, json=payload, timeout=(20, 240))
    if response.status_code >= 400:
        raise RuntimeError(f"Gemini HTTP {response.status_code}: {response.text[:1000]}")
    body = response.json()
    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {json.dumps(body)[:1000]}")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(str(part.get("text", "")) for part in parts).strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response")
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def collect_news(cfg: dict) -> list[dict]:
    news_cfg = cfg["news"]
    headers = {"User-Agent": "JourneyTalk/3.0 (+https://github.com/RyoSAKu610/journey-talk-radio)"}
    out: list[dict] = []
    seen: set[str] = set()
    for source in news_cfg["sources"]:
        try:
            response = requests.get(source["url"], headers=headers, timeout=news_cfg["request_timeout_seconds"])
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
        except Exception as exc:
            print(f"[warn] RSS failed for {source['name']}: {exc}")
            continue
        for entry in parsed.entries[: news_cfg["items_per_source"]]:
            title = clean(str(entry.get("title", "")))
            summary = clean(str(entry.get("summary", entry.get("description", ""))))
            url = str(entry.get("link", "")).strip()
            key = re.sub(r"\W+", "", title.casefold())
            if not title or not url or not key or key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "id": f"N{len(out) + 1:02d}",
                    "source": source["name"],
                    "title": title[:300],
                    "summary": summary[:1200],
                    "url": url,
                }
            )
            if len(out) >= news_cfg["max_candidates"]:
                return out
    return out


def choose_shared_stories(cfg: dict, news: list[dict]) -> list[dict]:
    required = int(cfg["episode"]["stories"])
    if len(news) < required:
        raise RuntimeError(f"Need at least {required} RSS stories, got {len(news)}")
    candidates = [
        {"id": x["id"], "source": x["source"], "title": x["title"], "summary": x["summary"][:500]}
        for x in news
    ]
    prompt = f"""
Choose exactly {required} items for a casual daily language-learning radio show.
Prefer stories that are easy to discuss in everyday conversation: technology, culture, science, travel, lifestyle, business or broadly useful world news.
Avoid graphic tragedy, crime details, and partisan horse-race politics when other suitable stories are available.
If political or policy news must be used, keep it descriptive and neutral.
Do not invent facts. Return JSON only as {{"ids":["N01", ...]}}.

Candidates:
{json.dumps(candidates, ensure_ascii=False)}
""".strip()
    try:
        result = gemini_json(prompt, cfg)
        ids = [str(x) for x in result.get("ids", [])]
        valid = {x["id"] for x in news}
        if len(ids) != required or len(set(ids)) != required or any(x not in valid for x in ids):
            raise ValueError("invalid story selection")
        by_id = {x["id"]: x for x in news}
        return [by_id[x] for x in ids]
    except Exception as exc:
        print(f"[warn] Gemini story selection failed; using RSS fallback: {exc}")
        selected: list[dict] = []
        used_sources: set[str] = set()
        for item in news:
            if item["source"] in used_sources:
                continue
            selected.append(item)
            used_sources.add(item["source"])
            if len(selected) == required:
                return selected
        return news[:required]


def episode_prompt(date: str, lang: dict, stories: list[dict], cfg: dict) -> str:
    minimum = cfg["episode"]["minimum_seconds"] // 60
    maximum = cfg["episode"]["maximum_seconds"] // 60
    return f"""
Write the {lang['japanese_name']} edition of Journey Talk for {date}.

This is a relaxed daily language-learning podcast with two hosts, MC_F and MC_M.
The listener is Japanese and wants practical spoken {lang['name']} ({lang['code']}).
Use Japanese (ja-JP) only for short navigation, explanations and recap.

Hard requirements:
- Natural spoken length: {minimum} to {maximum} minutes. Do not pad with empty repetition.
- Use all supplied news stories as light conversation material. They are a starting point, not a hard-news bulletin.
- News facts may come only from the supplied source/title/summary. Never invent names, dates, numbers, quotations, causes or outcomes.
- Two hosts should sound like real people chatting: reactions, small follow-up questions, examples and short transitions.
- Alternate speakers naturally. Both hosts must speak in both Japanese navigation and the target language across the episode.
- Target-language speech should be the majority of the episode.
- Keep individual turns short enough to sound conversational, usually one to three spoken sentences.
- Explain useful vocabulary or nuance in Japanese after important target-language exchanges.
- Near the end include a review section with several useful expressions from the episode.
- Finish with a short aftertalk / trivia exchange: a culturally useful filler, reaction, everyday phrase, TV/drama-like expression or conversation habit that the listener could actually use.
- No markdown or URLs inside spoken text.
- Avoid stiff textbook dialogue. Prefer natural modern spoken language without slang that is too niche.
- Political or policy content, if present, must remain descriptive and neutral. No endorsements, rankings or persuasion.

Return JSON only:
{{
  "title": "short Japanese title",
  "utterances": [
    {{"speaker":"MC_F","language":"ja-JP","text":"..."}},
    {{"speaker":"MC_M","language":"{lang['code']}","text":"..."}}
  ]
}}

Stories:
{json.dumps(stories, ensure_ascii=False)}
""".strip()


def estimate_seconds(utterances: list[dict]) -> float:
    total = 0.0
    for utterance in utterances:
        text = utterance["text"]
        lang = utterance["language"]
        visible = len(re.sub(r"\s+", "", text))
        if lang in {"ja-JP", "zh-CN", "ko-KR"}:
            total += visible / 5.0
        else:
            total += len(text) / 13.0
        total += 0.55
    return total


def normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def validate_episode(data: dict, date: str, lang: dict, stories: list[dict], cfg: dict) -> dict:
    utterances = data.get("utterances")
    if not isinstance(utterances, list):
        raise ValueError("utterances missing")
    lo = int(cfg["episode"]["minimum_utterances"])
    hi = int(cfg["episode"]["maximum_utterances"])
    if not lo <= len(utterances) <= hi:
        raise ValueError(f"utterance count {len(utterances)} outside {lo}..{hi}")
    allowed_languages = {"ja-JP", lang["code"]}
    cleaned: list[dict] = []
    speakers: set[str] = set()
    languages: set[str] = set()
    target_chars = 0
    for index, utterance in enumerate(utterances):
        speaker = str(utterance.get("speaker", ""))
        language = str(utterance.get("language", ""))
        text = normalized_text(clean(str(utterance.get("text", ""))))
        if speaker not in {"MC_F", "MC_M"}:
            raise ValueError(f"invalid speaker at utterance {index}")
        if language not in allowed_languages:
            raise ValueError(f"invalid language at utterance {index}")
        if not text:
            raise ValueError(f"empty text at utterance {index}")
        if re.search(r"https?://|www\.", text, flags=re.I):
            raise ValueError(f"spoken URL at utterance {index}")
        cleaned.append({"speaker": speaker, "language": language, "text": text})
        speakers.add(speaker)
        languages.add(language)
        if language == lang["code"]:
            target_chars += len(text)
    if speakers != {"MC_F", "MC_M"}:
        raise ValueError("both hosts must appear")
    if languages != allowed_languages:
        raise ValueError("both Japanese and target language must appear")
    if target_chars <= sum(len(x["text"]) for x in cleaned) / 2:
        raise ValueError("target language must be the majority of spoken text")
    estimate = estimate_seconds(cleaned)
    minimum = int(cfg["episode"]["minimum_seconds"])
    maximum = int(cfg["episode"]["maximum_seconds"])
    if not minimum <= estimate <= maximum:
        raise ValueError(f"estimated duration {estimate:.1f}s outside {minimum}..{maximum}s")
    title = clean(str(data.get("title", f"Journey Talk {lang['japanese_name']}")))
    return {
        "episode_date": date,
        "language": lang["code"],
        "slug": lang["slug"],
        "language_name": lang["name"],
        "japanese_name": lang["japanese_name"],
        "title": title,
        "stories": [{"source": x["source"], "title": x["title"], "url": x["url"]} for x in stories],
        "estimated_seconds": round(estimate, 2),
        "utterances": cleaned,
    }


def write_markdown(episode: dict, path: Path) -> None:
    rows = [f"# {episode['title']}", ""]
    for utterance in episode["utterances"]:
        rows.append(f"**{utterance['speaker']} [{utterance['language']}]**  {utterance['text']}")
        rows.append("")
    path.write_text("\n".join(rows), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "languages")
    args = parser.parse_args()

    cfg = load_config()
    news = collect_news(cfg)
    selected = choose_shared_stories(cfg, news)
    day = args.output_dir / args.date
    day.mkdir(parents=True, exist_ok=True)

    manifest = {
        "episode_date": args.date,
        "model": os.getenv("GEMINI_MODEL", "").strip() or cfg["provider"]["model"],
        "stories": [{"source": x["source"], "title": x["title"], "url": x["url"]} for x in selected],
        "episodes": [],
    }
    for lang in cfg["languages"]:
        raw = gemini_json(episode_prompt(args.date, lang, selected, cfg), cfg)
        episode = validate_episode(raw, args.date, lang, selected, cfg)
        json_path = day / f"{lang['slug']}.json"
        md_path = day / f"{lang['slug']}.md"
        json_path.write_text(json.dumps(episode, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_markdown(episode, md_path)
        manifest["episodes"].append(
            {
                "slug": lang["slug"],
                "language": lang["code"],
                "japanese_name": lang["japanese_name"],
                "json": json_path.name,
                "markdown": md_path.name,
            }
        )
        print(f"[script] {lang['japanese_name']}: {json_path}")

    (day / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
