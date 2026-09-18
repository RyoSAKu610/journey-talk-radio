from __future__ import annotations

import argparse
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "cloud_languages.yaml"


def clean(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def load_config():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def provider(cfg):
    for name in cfg["provider"]["order"]:
        item = cfg["provider"][name]
        key = os.getenv(item["api_key_env"], "").strip()
        if not key:
            continue
        model = os.getenv(item.get("model_env", ""), "").strip() if item.get("model_env") else ""
        model = model or item.get("model", "")
        if model:
            return name, item["base_url"].rstrip("/"), key, model
    raise RuntimeError("Set VENICE_API_KEY, FEATHERLESS_API_KEY, or ABLITERATION_API_KEY")


def call_llm(p, messages, temperature, max_tokens):
    name, base_url, key, model = p
    r = requests.post(
        base_url + "/chat/completions",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/RyoSAKu610/journey-talk-radio",
            "X-Title": "Journey Talk",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=(20, 240),
    )
    if r.status_code >= 400:
        raise RuntimeError(name + " API HTTP " + str(r.status_code) + ": " + r.text[:1000])
    text = r.json()["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^" + chr(96) * 3 + r"(?:json)?\s*", "", text)
    text = re.sub(r"\s*" + chr(96) * 3 + r"$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def collect_news(cfg):
    out, seen = [], set()
    nc = cfg["news"]
    headers = {"User-Agent": "JourneyTalk/2.0"}
    for source in nc["sources"]:
        try:
            r = requests.get(source["url"], headers=headers, timeout=nc["request_timeout_seconds"])
            r.raise_for_status()
            feed = feedparser.parse(r.content)
        except Exception as exc:
            print("[warn]", source["name"], exc)
            continue
        for entry in feed.entries[: nc["items_per_source"]]:
            title = clean(str(entry.get("title", "")))
            summary = clean(str(entry.get("summary", entry.get("description", ""))))
            url = str(entry.get("link", "")).strip()
            key = re.sub(r"\W+", "", title.casefold())
            if not title or not url or key in seen:
                continue
            seen.add(key)
            out.append({
                "id": "N%02d" % (len(out) + 1),
                "source": source["name"],
                "title": title[:300],
                "summary": summary[:1200],
                "url": url,
            })
            if len(out) >= nc["max_candidates"]:
                return out
    return out


def choose_assignments(p, cfg, news):
    per = cfg["episode"]["stories_per_language"]
    languages = cfg["languages"]
    needed = len(languages) * per
    fallback = {}
    offset = 0
    for lang in languages:
        fallback[lang["code"]] = [x["id"] for x in news[offset:offset + per]]
        offset += per

    compact = [
        {
            "id": x["id"],
            "source": x["source"],
            "title": x["title"],
            "summary": x["summary"][:350],
        }
        for x in news
    ]
    prompt = (
        "Assign exactly %d news IDs to each language. No ID may be reused. "
        "More importantly, do not assign two articles about the same underlying event "
        "to different language editions. Prefer a broad mix of topics. "
        "Return JSON only as {\"assignments\": {\"en-US\": [\"N01\", ...]}}.\n\n"
        "Languages: %s\n\nCandidates: %s"
        % (
            per,
            json.dumps([{"code": x["code"], "name": x["name"]} for x in languages], ensure_ascii=False),
            json.dumps(compact, ensure_ascii=False),
        )
    )
    try:
        data = call_llm(
            p,
            [
                {"role": "system", "content": "You are a careful multilingual news editor. Stay neutral and factual."},
                {"role": "user", "content": prompt},
            ],
            0.1,
            1800,
        )
        raw = data["assignments"]
        valid = {x["id"] for x in news}
        used = set()
        checked = {}
        for lang in languages:
            ids = [str(x) for x in raw[lang["code"]]]
            if len(ids) != per or any(x not in valid for x in ids) or any(x in used for x in ids):
                raise ValueError("invalid assignment")
            used.update(ids)
            checked[lang["code"]] = ids
        if len(used) != needed:
            raise ValueError("assignment does not cover enough unique stories")
        return checked
    except Exception as exc:
        print("[warn] semantic news assignment failed; using unique-record fallback:", exc)
        return fallback


def prompt_for(date, lang, stories, cfg):
    ec = cfg["episode"]
    return f"""
Create one bilingual daily language-learning podcast episode for {date}.

Target language: {lang["name"]} ({lang["code"]})
Explanation language: Japanese (ja-JP)

Requirements:
- Natural spoken duration about 10 minutes.
- Use all three supplied news stories.
- This language edition must be independent from the other editions.
- Current-event facts may come ONLY from the supplied title, summary, source and URL.
- Never invent names, dates, numbers, quotes, causes or outcomes.
- If the news detail is thin, spend time on vocabulary, grammar, pronunciation, nuance and recap instead of adding facts.
- Political material must be descriptive and neutral, with no endorsement, ranking or persuasion.
- Two conversational hosts: MC_F and MC_M.
- {ec["min_utterances"]} to {ec["max_utterances"]} utterances.
- Roughly 60 percent target language and 40 percent Japanese.
- Start with a short Japanese intro, then alternate target-language listening with Japanese explanation.
- End with a Japanese recap.
- Each utterance should normally be 1 to 4 spoken sentences.
- No URLs or markdown inside spoken text.

Stories:
{json.dumps(stories, ensure_ascii=False)}

Return JSON only:
{{
  "title": "short title",
  "stories": [
    {{"id": "N01", "source": "...", "title": "...", "url": "..."}}
  ],
  "utterances": [
    {{"speaker": "MC_F", "language": "ja-JP", "text": "..."}},
    {{"speaker": "MC_M", "language": "{lang["code"]}", "text": "..."}}
  ]
}}
""".strip()


def validate(data, date, lang, stories, cfg):
    utterances = data.get("utterances")
    if not isinstance(utterances, list):
        raise ValueError("utterances missing")
    lo, hi = cfg["episode"]["min_utterances"], cfg["episode"]["max_utterances"]
    if not lo <= len(utterances) <= hi:
        raise ValueError("utterance count outside contract")
    allowed = {"ja-JP", lang["code"]}
    total = 0
    cleaned = []
    for i, u in enumerate(utterances):
        speaker = str(u.get("speaker", ""))
        language = str(u.get("language", ""))
        text = clean(str(u.get("text", "")))
        if speaker not in {"MC_F", "MC_M"} or language not in allowed or not text:
            raise ValueError("invalid utterance %d" % i)
        if re.search(r"https?://|www\.", text, flags=re.I):
            raise ValueError("spoken URL in utterance %d" % i)
        total += len(text)
        cleaned.append({"speaker": speaker, "language": language, "text": text})
    if total < 5000:
        raise ValueError("script too short for about 10 minutes")
    return {
        "episode_date": date,
        "language": lang["code"],
        "language_name": lang["name"],
        "title": str(data.get("title", lang["name"] + " Journey Talk")),
        "target_seconds": cfg["episode"]["target_seconds"],
        "stories": [
            {"id": s["id"], "source": s["source"], "title": s["title"], "url": s["url"]}
            for s in stories
        ],
        "utterances": cleaned,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--output-dir", type=Path, default=ROOT / "output" / "languages")
    args = ap.parse_args()

    cfg = load_config()
    p = provider(cfg)
    print("[provider]", p[0], p[3])
    news = collect_news(cfg)
    per = cfg["episode"]["stories_per_language"]
    needed = len(cfg["languages"]) * per
    if len(news) < needed:
        raise RuntimeError("Need %d unique stories; got %d" % (needed, len(news)))

    day = args.output_dir / args.date
    day.mkdir(parents=True, exist_ok=True)
    manifest = {"episode_date": args.date, "provider": p[0], "model": p[3], "episodes": []}

    assignments = choose_assignments(p, cfg, news)
    by_id = {x["id"]: x for x in news}

    for lang in cfg["languages"]:
        stories = [by_id[x] for x in assignments[lang["code"]]]
        data = call_llm(
            p,
            [
                {"role": "system", "content": "You are a careful multilingual radio writer and language teacher."},
                {"role": "user", "content": prompt_for(args.date, lang, stories, cfg)},
            ],
            cfg["episode"]["temperature"],
            cfg["episode"]["max_tokens"],
        )
        episode = validate(data, args.date, lang, stories, cfg)
        episode["provider"] = p[0]
        episode["model"] = p[3]
        path = day / (lang["slug"] + ".json")
        path.write_text(json.dumps(episode, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest["episodes"].append({
            "slug": lang["slug"],
            "language": lang["code"],
            "json": path.name,
            "story_ids": [s["id"] for s in stories],
        })
        print("[done]", lang["name"], path)

    (day / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
