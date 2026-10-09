from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "cloud_languages.yaml"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
HTTP_ATTEMPTS = 3


def clean(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def provider_of(model: str) -> str:
    """'mistral:mistral-medium-latest' -> 'mistral'; bare names are Gemini models."""
    return model.split(":", 1)[0] if ":" in model and model.split(":", 1)[0] in PROVIDER_KEYS else "gemini"


# Environment variable holding each script provider's key. A provider without a key is simply not asked.
PROVIDER_KEYS = {"gemini": "GEMINI_API_KEY", "mistral": "MISTRAL_API_KEY", "openrouter": "OPENROUTER_API_KEY"}


def has_key(provider: str) -> bool:
    return bool(os.getenv(PROVIDER_KEYS[provider], "").strip())


def text_models(cfg: dict) -> list[str]:
    """Script-writing models in contest order; GEMINI_MODEL overrides the first Gemini model.

    Mixes providers (Gemini, Mistral, OpenRouter free models): each has its own free quota, so a contest
    round with one model from each costs nothing extra. Models out of quota or without a key are skipped.
    """
    override = os.getenv("GEMINI_MODEL", "").strip()
    models = [override, *cfg["provider"]["models"]]
    seen: set[str] = set()
    out: list[str] = []
    for model in models:
        if not model or model in seen or model in EXHAUSTED or not has_key(provider_of(model)):
            continue
        seen.add(model)
        out.append(model)
    return out


def last_resort_models(cfg: dict) -> list[str]:
    """Weaker models used only when no regular model can answer (they write much shorter scripts)."""
    return [m for m in cfg["provider"].get("last_resort_models", []) if m not in EXHAUSTED and has_key(provider_of(m))]


def any_script_provider() -> bool:
    return any(has_key(p) for p in PROVIDER_KEYS)


# Models whose free daily quota ran out during this run; asking again today only wastes time.
EXHAUSTED: set[str] = set()


MODEL_USED: dict[str, str] = {}


def post_gemini(payload: dict, cfg: dict, models: list[str] | None = None):
    """POST to the first model that answers. Retries brief overloads, then moves on to the next
    model when one is retired (404), out of daily quota, or stays overloaded.

    Returns (response, model)."""
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    problems: list[str] = []
    for model in models or text_models(cfg):
        response = None
        for attempt in range(1, HTTP_ATTEMPTS + 1):
            try:
                response = requests.post(GEMINI_ENDPOINT.format(model=model), params={"key": api_key}, json=payload, timeout=(20, 240))
            except requests.RequestException as exc:
                response = None
                problems.append(f"{model}: {exc}")
                time.sleep(15 * attempt)
                continue
            daily_quota = response.status_code == 429 and "PerDay" in response.text
            if daily_quota:
                EXHAUSTED.add(model)
                print(f"[warn] {model}: free-tier daily quota used up; skipping it for the rest of this run")
            if response.status_code in RETRYABLE_STATUS and not daily_quota and attempt < HTTP_ATTEMPTS:
                print(f"[warn] {model} HTTP {response.status_code}; retrying in {15 * attempt}s")
                time.sleep(15 * attempt)
                continue
            break
        if response is None:
            continue
        if response.status_code == 404 or response.status_code in RETRYABLE_STATUS:
            problems.append(f"{model}: HTTP {response.status_code}")
            print(f"[warn] {model} unavailable (HTTP {response.status_code}); trying the next model")
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"Gemini HTTP {response.status_code}: {response.text[:1000]}")
        MODEL_USED["name"] = model
        return response, model
    raise RuntimeError("no Gemini model answered: " + "; ".join(problems[-4:]))


def gemini_json(prompt: str, cfg: dict) -> dict:
    """Ask the first available script model (any provider) for a JSON object."""
    return gemini_json_from(prompt, cfg)[0]


def parse_json_text(text: str, who: str) -> dict:
    text = text.strip()
    if not text:
        raise ValueError(f"{who} returned an empty response")
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # Some models wrap the JSON in prose or append text after it ("Extra data"); read the first object.
        start = text.find("{")
        if start < 0:
            raise ValueError(f"{who} did not return JSON")
        try:
            result, _ = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError as exc:
            raise ValueError(f"{who} returned malformed JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{who} response must be a JSON object")
    return result


def gemini_json_from(prompt: str, cfg: dict, models: list[str] | None = None) -> tuple[dict, str]:
    """Ask the first available model in `models` (any provider) for a JSON answer; returns (answer, model).

    Models are tried in order, grouped by provider so each provider's own fallback logic applies.
    """
    models = list(models) if models else text_models(cfg)
    problems: list[str] = []
    index = 0
    while index < len(models):
        provider = provider_of(models[index])
        group = [models[index]]
        index += 1
        while index < len(models) and provider_of(models[index]) == provider:
            group.append(models[index])
            index += 1
        group = [m for m in group if m not in EXHAUSTED]
        if not group or not has_key(provider):
            continue
        try:
            if provider == "gemini":
                return gemini_answer(prompt, cfg, group)
            return compatible_answer(provider, prompt, cfg, group)
        except RuntimeError as exc:  # this provider is unavailable right now; try the next one
            problems.append(str(exc)[:300])
            print(f"[warn] {exc}"[:400])
    raise RuntimeError("no script model answered: " + "; ".join(problems[-4:]))


def gemini_answer(prompt: str, cfg: dict, models: list[str]) -> tuple[dict, str]:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": cfg["episode"]["temperature"],
            "maxOutputTokens": cfg["episode"]["max_output_tokens"],
            "responseMimeType": "application/json",
        },
    }
    response, model = post_gemini(payload, cfg, models)
    body = response.json()
    candidates = body.get("candidates") or []
    # Content problems are ValueErrors so generate_episode() retries them with feedback.
    if not candidates:
        raise ValueError(f"Gemini returned no candidates: {json.dumps(body)[:1000]}")
    if candidates[0].get("finishReason") == "MAX_TOKENS":
        raise ValueError("response was cut off at the output token limit; keep turns and study notes more concise")
    parts = candidates[0].get("content", {}).get("parts", [])
    # Thinking models may return their reasoning as separate "thought" parts; keep the answer only.
    text = "".join(str(part.get("text", "")) for part in parts if not part.get("thought"))
    return parse_json_text(text, model), model


# --------------------------------------------------------------------------- OpenAI-compatible providers

COMPATIBLE_ENDPOINTS = {
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}
OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
# Resolved once per run: "openrouter:free" -> the best free model OpenRouter lists today.
FREE_MODEL_CACHE: dict[str, dict] = {}


def openrouter_free_model(cfg: dict) -> dict:
    """Pick today's best ':free' model: free models rotate, so the id is looked up instead of hard-coded."""
    if "pick" in FREE_MODEL_CACHE:
        return FREE_MODEL_CACHE["pick"]
    settings = cfg["provider"].get("openrouter", {})
    preferred = [str(x).lower() for x in settings.get("preferred", [])]
    minimum_context = int(settings.get("min_context", 32000))
    response = requests.get(OPENROUTER_MODELS, timeout=(10, 60))
    response.raise_for_status()
    candidates = []
    for item in response.json().get("data", []):
        model_id = str(item.get("id", ""))
        modalities = (item.get("architecture") or {}).get("output_modalities") or ["text"]
        if not model_id.endswith(":free") or "text" not in modalities:
            continue
        if int(item.get("context_length") or 0) < minimum_context:
            continue
        rank = next((i for i, word in enumerate(preferred) if word in model_id.lower()), len(preferred))
        limit = int((item.get("top_provider") or {}).get("max_completion_tokens") or 0)
        candidates.append((rank, -int(item.get("context_length") or 0), model_id, limit))
    if not candidates:
        raise RuntimeError("OpenRouter lists no suitable free model today")
    candidates.sort()
    _, _, model_id, limit = candidates[0]
    FREE_MODEL_CACHE["pick"] = {"id": model_id, "max_tokens": limit}
    print(f"[script] OpenRouter free model today: {model_id}")
    return FREE_MODEL_CACHE["pick"]


def compatible_answer(provider: str, prompt: str, cfg: dict, models: list[str]) -> tuple[dict, str]:
    """Chat-completions call for Mistral / OpenRouter. Returns (answer, 'provider:model')."""
    api_key = os.getenv(PROVIDER_KEYS[provider], "").strip()
    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "openrouter":
        headers.update({"HTTP-Referer": "https://github.com/RyoSAKu610/journey-talk-radio", "X-Title": "Journey Talk"})
    problems: list[str] = []
    for model in models:
        name = model.split(":", 1)[1]
        max_tokens = int(cfg["episode"]["max_output_tokens"])
        if provider == "openrouter" and name == "free":
            try:
                pick = openrouter_free_model(cfg)
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                problems.append(f"{model}: {exc}")
                continue
            name = pick["id"]
            if pick["max_tokens"]:
                max_tokens = min(max_tokens, pick["max_tokens"])
        body = {
            "model": name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": cfg["episode"]["temperature"],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        response = None
        for attempt in range(1, HTTP_ATTEMPTS + 1):
            try:
                response = requests.post(COMPATIBLE_ENDPOINTS[provider], headers=headers, json=body, timeout=(20, 300))
            except requests.RequestException as exc:
                response = None
                problems.append(f"{model}: {exc}")
                time.sleep(10 * attempt)
                continue
            text = response.text
            daily = response.status_code == 429 and re.search(r"per[- _]?day|daily|free-models-per-day|quota", text, re.I)
            if daily or response.status_code == 402:
                EXHAUSTED.add(model)
                print(f"[warn] {model}: free quota used up (HTTP {response.status_code}); skipping it for the rest of this run")
                break
            if response.status_code in RETRYABLE_STATUS and attempt < HTTP_ATTEMPTS:
                print(f"[warn] {model} HTTP {response.status_code}; retrying in {10 * attempt}s")
                time.sleep(10 * attempt)
                continue
            break
        if response is None or model in EXHAUSTED:
            continue
        if response.status_code in (401, 403):
            problems.append(f"{model}: key rejected (HTTP {response.status_code})")
            EXHAUSTED.update(m for m in models)
            break
        if response.status_code >= 400:
            problems.append(f"{model}: HTTP {response.status_code} {response.text[:200]}")
            continue
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise ValueError(f"{model} returned no choices: {json.dumps(payload)[:300]}")
        if choices[0].get("finish_reason") == "length":
            raise ValueError("response was cut off at the output token limit; keep turns and study notes more concise")
        content = (choices[0].get("message") or {}).get("content") or ""
        if isinstance(content, list):  # some providers return content parts
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        label = f"{provider}:{name}"
        MODEL_USED["name"] = label
        return parse_json_text(str(content), label), label
    raise RuntimeError(f"no {provider} model answered: " + "; ".join(problems[-3:]))


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
        print(f"[warn] model story selection failed; using RSS fallback: {exc}")
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


def retry_note(feedback: str) -> str:
    if not feedback:
        return ""
    return f"""

The previous attempt was rejected by the validator: {feedback}
Write a corrected, complete episode that satisfies every requirement."""


def length_plan(lang: dict, cfg: dict) -> str:
    """Concrete volume targets derived from the measured speaking rates.

    Models badly under-estimate how much text fills 10+ minutes of audio, and the right amount differs
    a lot by language (a Chinese line needs far fewer characters than a Spanish one).
    """
    rates = {**DEFAULT_RATES, **(cfg["episode"].get("speech_rates") or {})}
    # Models consistently deliver 15-25% less text than asked for, so the plan aims high in the window.
    low_end, high_end = cfg["episode"]["minimum_seconds"], cfg["episode"]["maximum_seconds"]
    target = low_end + 0.75 * (high_end - low_end)
    turns, japanese_turns, target_turns = 66, 26, 40
    japanese_seconds = japanese_turns * 55 / rates["ja-JP"]
    per_turn = max(3.0, (target - 0.55 * turns - japanese_seconds - 20) / target_turns)
    middle = per_turn * float(rates.get(lang["code"], LATIN_RATE))
    low, high = int(round(middle * 0.7, -1)), int(round(middle * 1.3, -1))
    unit = "characters (not counting spaces)" if lang["code"] in NO_SPACE_COUNT else "characters"
    return f"""Length plan (the finished audio must run {cfg['episode']['minimum_seconds'] // 60} to {cfg['episode']['maximum_seconds'] // 60} minutes, so this volume is required):
- Write 60 to 74 utterances in total, about {target_turns} in {lang['name']} and about {japanese_turns} in Japanese.
- {lang['name']} turns: {low} to {high} {unit} each (two to four full sentences). Japanese turns: 40 to 75 characters each.
  Avoid one-word or one-phrase turns.
- Structure: opening chat (about 6 turns), each news story (about 15 turns each), review with slow repeats
  (about 10 turns), aftertalk (about 6 turns)."""


def materials_contract(lang: dict, cfg: dict) -> str:
    """Prompt section shared by every edition type: on-screen study materials and the JSON shape."""
    learning = cfg["learning"]
    return f"""Learning materials for the companion web player (these are shown on screen, not spoken):
- Every {lang['code']} utterance must include "ja": a natural Japanese translation of that utterance. Japanese utterances have no "ja".
- "summary_ja": two or three Japanese sentences telling the listener what this episode covers and what they will be able to say.
- "vocabulary": {learning['vocabulary_min']} to {learning['vocabulary_max']} useful words or phrases that actually appear in the {lang['code']} utterances.
  Each item has "term", "reading" (pronunciation help for a Japanese learner: pinyin with tone marks for Chinese,
  Revised Romanization for Korean, the word with a stress mark for Russian, otherwise an empty string), "meaning_ja",
  "example" (a {lang['code']} sentence from the episode that contains the term) and "example_ja".
- "quiz": {learning['quiz_min']} to {learning['quiz_max']} listening-comprehension questions written in Japanese about what the hosts said.
  Each item has "question_ja", "choices" (three or four short Japanese options), "answer" (zero-based index of the correct
  choice) and "explanation_ja".

Return JSON only:
{{
  "title": "short Japanese title",
  "summary_ja": "...",
  "utterances": [
    {{"speaker":"MC_F","language":"ja-JP","text":"..."}},
    {{"speaker":"MC_M","language":"{lang['code']}","text":"...","ja":"..."}},
    {{"speaker":"MC_F","language":"{lang['code']}","text":"...","ja":"...","slow":true}}
  ],
  "vocabulary": [
    {{"term":"...","reading":"...","meaning_ja":"...","example":"...","example_ja":"..."}}
  ],
  "quiz": [
    {{"question_ja":"...","choices":["...","...","..."],"answer":0,"explanation_ja":"..."}}
  ]
}}""".strip()


def episode_prompt(date: str, lang: dict, stories: list[dict], cfg: dict, feedback: str = "") -> str:
    minimum = cfg["episode"]["minimum_seconds"] // 60
    maximum = cfg["episode"]["maximum_seconds"] // 60
    level = cfg["episode"].get("learner_level", "CEFR B1")
    hosts = cfg.get("hosts", {})
    learning = cfg["learning"]
    return f"""
Write the {lang['japanese_name']} edition of Journey Talk for {date}.

This is a relaxed daily language-learning podcast with two regular hosts:
MC_F is {hosts.get('MC_F', 'Mina')} (female) and MC_M is {hosts.get('MC_M', 'Ren')} (male). They may call each other by name.
The listener is Japanese, around {level} in {lang['name']}, and wants practical spoken {lang['name']} ({lang['code']}).
Use Japanese (ja-JP) only for short navigation, explanations and recap.

Hard requirements:
- Natural spoken length: {minimum} to {maximum} minutes. Do not pad with empty repetition.
- Use all supplied news stories as light conversation material. They are a starting point, not a hard-news bulletin.
- News facts may come only from the supplied source/title/summary. Never invent names, dates, numbers, quotations, causes or outcomes.
- Two hosts should sound like real people chatting: reactions, small follow-up questions, examples and short transitions.
- Alternate speakers naturally. Both hosts must speak in both Japanese navigation and the target language across the episode.
- Target-language speech should be the majority of the episode.
- Keep individual turns short enough to sound conversational, usually one to three spoken sentences.
- Match the listener level: mostly high-frequency words and clear sentence structure, with a few stretch expressions explained in Japanese.
- Explain useful vocabulary or nuance in Japanese after important target-language exchanges.
- Near the end include a review section with several useful expressions from the episode. For each key expression, say the
  target-language sentence at natural speed, then repeat the same sentence once more as a separate utterance with "slow": true
  so the listener can shadow it. Mark at most {learning['max_slow_utterances']} utterances as slow, and only target-language ones.
- Finish with a short aftertalk / trivia exchange: a culturally useful filler, reaction, everyday phrase, TV/drama-like expression or conversation habit that the listener could actually use.
- No markdown or URLs inside spoken text.
- Avoid stiff textbook dialogue. Prefer natural modern spoken language without slang that is too niche.
- Political or policy content, if present, must remain descriptive and neutral. No endorsements, rankings or persuasion.

{length_plan(lang, cfg)}

{materials_contract(lang, cfg)}

Stories:
{json.dumps(stories, ensure_ascii=False)}{retry_note(feedback)}
""".strip()


SLOW_FACTOR = 1.33
SHADOWING_SECONDS = 1.6


DEFAULT_RATES = {"ja-JP": 7.5, "zh-CN": 4.6, "ko-KR": 5.2}
LATIN_RATE = 15.0
NO_SPACE_COUNT = {"ja-JP", "zh-CN", "ko-KR"}


def spoken_units(text: str, language: str) -> int:
    return len(re.sub(r"\s+", "", text)) if language in NO_SPACE_COUNT else len(text)


def estimate_seconds(utterances: list[dict], rates: dict | None = None) -> float:
    """Expected audio length: characters at the measured per-language speaking rate, plus pauses."""
    rates = {**DEFAULT_RATES, **(rates or {})}
    total = 0.0
    for utterance in utterances:
        language = utterance["language"]
        spoken = spoken_units(utterance["text"], language) / float(rates.get(language, LATIN_RATE))
        if utterance.get("slow"):
            spoken = spoken * SLOW_FACTOR + SHADOWING_SECONDS
        total += spoken + 0.55
    return total


def normalized_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def clean_field(value: object, limit: int = 400) -> str:
    if not isinstance(value, str):
        return ""
    text = normalized_text(clean(value))
    if re.search(r"https?://|www\.", text, flags=re.I):
        return ""
    return text[:limit]


def validate_episode(data: dict, date: str, lang: dict, stories: list[dict], cfg: dict) -> dict:
    utterances = data.get("utterances")
    if not isinstance(utterances, list):
        raise ValueError("utterances missing")
    lo = int(cfg["episode"]["minimum_utterances"])
    hi = int(cfg["episode"]["maximum_utterances"])
    if not lo <= len(utterances) <= hi:
        raise ValueError(
            f"utterance count {len(utterances)} outside {lo}..{hi}; "
            + ("write more turns following the length plan" if len(utterances) < lo else "merge or cut turns")
        )
    max_slow = int(cfg["learning"]["max_slow_utterances"])
    allowed_languages = {"ja-JP", lang["code"]}
    cleaned: list[dict] = []
    speakers: set[str] = set()
    languages: set[str] = set()
    target_chars = 0
    slow_count = 0
    for index, utterance in enumerate(utterances):
        if not isinstance(utterance, dict):
            raise ValueError(f"utterance {index} is not an object")
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
        item = {"speaker": speaker, "language": language, "text": text}
        if language == lang["code"]:
            translation = clean_field(utterance.get("ja"))
            if translation:
                item["ja"] = translation
            if utterance.get("slow") is True and slow_count < max_slow:
                item["slow"] = True
                slow_count += 1
        cleaned.append(item)
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
    estimate = estimate_seconds(cleaned, cfg["episode"].get("speech_rates"))
    minimum = int(cfg["episode"]["minimum_seconds"])
    maximum = int(cfg["episode"]["maximum_seconds"])
    if not minimum <= estimate <= maximum:
        target = (minimum + maximum) / 2
        if estimate < minimum:
            advice = f"the script is about {round((1 - estimate / target) * 100)}% too short: add turns and make each turn longer, following the length plan"
        else:
            advice = f"the script is about {round((estimate / target - 1) * 100)}% too long: shorten turns"
        raise ValueError(f"estimated duration {estimate:.0f}s outside {minimum}..{maximum}s; {advice}")
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


def empty_learning() -> dict:
    return {"summary_ja": "", "vocabulary": [], "quiz": []}


def validate_learning(data: dict, episode: dict, cfg: dict) -> dict:
    """Validate the on-screen study materials that accompany an already-valid spoken episode."""
    learning = cfg["learning"]
    target = [x for x in episode["utterances"] if x["language"] == episode["language"]]
    missing = sum(1 for x in target if not x.get("ja"))
    if missing > len(target) // 10:
        raise ValueError(f"Japanese translation missing for {missing} of {len(target)} target-language utterances")

    vocabulary: list[dict] = []
    seen_terms: set[str] = set()
    for raw in data.get("vocabulary") or []:
        if not isinstance(raw, dict):
            continue
        term = clean_field(raw.get("term"), 120)
        meaning = clean_field(raw.get("meaning_ja"), 200)
        key = term.casefold()
        if not term or not meaning or key in seen_terms:
            continue
        seen_terms.add(key)
        vocabulary.append(
            {
                "term": term,
                "reading": clean_field(raw.get("reading"), 160),
                "meaning_ja": meaning,
                "example": clean_field(raw.get("example"), 300),
                "example_ja": clean_field(raw.get("example_ja"), 300),
            }
        )
    if len(vocabulary) < int(learning["vocabulary_min"]):
        raise ValueError(f"vocabulary has {len(vocabulary)} usable items, need {learning['vocabulary_min']}")

    quiz: list[dict] = []
    for index, raw in enumerate(data.get("quiz") or []):
        if not isinstance(raw, dict):
            continue
        question = clean_field(raw.get("question_ja"), 300)
        choices = [clean_field(x, 120) for x in raw.get("choices") or []]
        answer = raw.get("answer")
        if isinstance(answer, str) and answer.strip().isdigit():
            answer = int(answer.strip())
        if (
            not question
            or not 3 <= len(choices) <= 4
            or not all(choices)
            or len(set(choices)) != len(choices)
            or isinstance(answer, bool)
            or not isinstance(answer, int)
            or not 0 <= answer < len(choices)
        ):
            continue
        # Models tend to put the right answer first; shuffle deterministically so the position carries no hint.
        correct = choices[answer]
        random.Random(f"{episode['episode_date']}:{episode['slug']}:{index}").shuffle(choices)
        quiz.append(
            {
                "question_ja": question,
                "choices": choices,
                "answer": choices.index(correct),
                "explanation_ja": clean_field(raw.get("explanation_ja"), 300),
            }
        )
    if len(quiz) < int(learning["quiz_min"]):
        raise ValueError(f"quiz has {len(quiz)} usable questions, need {learning['quiz_min']}")

    return {
        "summary_ja": clean_field(data.get("summary_ja"), 400),
        "vocabulary": vocabulary[: int(learning["vocabulary_max"])],
        "quiz": quiz[: int(learning["quiz_max"])],
    }


def contestant_orders(cfg: dict) -> list[list[str]]:
    """Model order for each parallel contestant: each starts on a different model, then falls back
    through the other regular models. When every regular model is out of quota, a single contestant
    uses the last-resort models."""
    models = text_models(cfg)
    if not models:
        return [last_resort_models(cfg)] if last_resort_models(cfg) else []
    count = max(1, min(int(cfg["episode"].get("parallel_models", 1)), len(models)))
    # Contestants start on different providers first (Gemini, Mistral, OpenRouter...), so one provider's
    # outage or style never decides a whole round; extra contestants take the next models in the list.
    starts: list[str] = []
    for model in models:
        if provider_of(model) not in {provider_of(x) for x in starts}:
            starts.append(model)
    starts += [m for m in models if m not in starts]
    return [[start, *[m for m in models if m != start]] for start in starts[:count]]


def run_contestant(prompt: str, order: list[str], date: str, lang: dict, stories: list[dict], cfg: dict) -> dict:
    """One model's attempt, validated. Never raises: the outcome is described in the returned dict."""
    try:
        raw, model = gemini_json_from(prompt, cfg, order)
    except ValueError as exc:
        return {"model": order[0], "error": str(exc)}
    except RuntimeError as exc:
        return {"model": order[0], "error": str(exc), "unavailable": True}
    try:
        episode = validate_episode(raw, date, lang, stories, cfg)
    except ValueError as exc:
        return {"model": model, "error": str(exc)}
    episode["script_model"] = model
    try:
        episode.update(validate_learning(raw, episode, cfg))
    except ValueError as exc:
        return {"model": model, "episode": episode, "complete": False, "error": str(exc)}
    return {"model": model, "episode": episode, "complete": True}


def generate_episode(date: str, lang: dict, stories: list[dict], cfg: dict, prompt_builder=None) -> dict:
    """Have several models write the edition in parallel each round and keep the best valid script.

    Every model has its own free-tier quota, so a round of N contestants costs nothing extra; it
    turns "this model is overloaded" or "this model wrote too little" into a non-event. A round
    with no valid script feeds the most useful validator error back into the next round. A spoken
    script that passes every audio contract is never thrown away just because the study materials
    were incomplete: after the last round it is published without them.
    """
    attempts = max(1, int(cfg["episode"].get("generation_attempts", 3)))
    low, high = cfg["episode"]["minimum_seconds"], cfg["episode"]["maximum_seconds"]
    ideal = low + 0.6 * (high - low)
    feedback = ""
    spoken_only: dict | None = None
    for attempt in range(1, attempts + 1):
        orders = contestant_orders(cfg)  # re-read each round: quotas may have run out meanwhile
        if not orders:
            raise RuntimeError(f"{lang['slug']}: every script model is out of free-tier quota today")
        prompt = (prompt_builder or episode_prompt)(date, lang, stories, cfg, feedback)
        if len(orders) == 1:
            results = [run_contestant(prompt, orders[0], date, lang, stories, cfg)]
        else:
            with ThreadPoolExecutor(max_workers=len(orders)) as pool:
                results = list(pool.map(lambda order: run_contestant(prompt, order, date, lang, stories, cfg), orders))
        for result in results:
            status = "ok" if result.get("complete") else result.get("error", "")[:160]
            print(f"[{lang['slug']}] round {attempt}/{attempts} {result['model']}: {status}")
        complete = [r["episode"] for r in results if r.get("complete")]
        if complete:
            best = min(complete, key=lambda e: abs(e["estimated_seconds"] - ideal))
            print(f"[{lang['slug']}] picked {best['script_model']} ({best['estimated_seconds']:.0f}s)")
            return best
        partial = [r["episode"] for r in results if r.get("episode")]
        if partial and spoken_only is None:
            spoken_only = min(partial, key=lambda e: abs(e["estimated_seconds"] - ideal))
        content_errors = [r["error"] for r in results if r.get("error") and not r.get("unavailable")]
        if not content_errors:
            if contestant_orders(cfg) not in ([], orders):
                continue  # quotas ran out during this round; the next round uses the models still available
            raise RuntimeError(f"{lang['slug']}: no script model answered ({results[0].get('error', '')[:300]})")
        # Length problems are the most common and the most actionable, so prefer them as feedback.
        feedback = next((e for e in content_errors if "duration" in e or "utterance count" in e), content_errors[0])
    if spoken_only is not None:
        print(f"[warn] {lang['slug']}: publishing audio script without study materials")
        spoken_only.update(empty_learning())
        return spoken_only
    raise RuntimeError(f"{lang['slug']}: no valid episode after {attempts} rounds (last error: {feedback})")


def write_markdown(episode: dict, path: Path) -> None:
    rows = [f"# {episode['title']}", ""]
    if episode.get("summary_ja"):
        rows += [episode["summary_ja"], ""]
    rows += ["## Script", ""]
    for utterance in episode["utterances"]:
        slow = " 🐢" if utterance.get("slow") else ""
        rows.append(f"**{utterance['speaker']} [{utterance['language']}]{slow}**  {utterance['text']}")
        if utterance.get("ja"):
            rows.append(f"> {utterance['ja']}")
        rows.append("")
    if episode.get("vocabulary"):
        rows += ["## Vocabulary", "", "| 表現 | 読み | 意味 | 例文 |", "| --- | --- | --- | --- |"]
        for item in episode["vocabulary"]:
            cells = [item["term"], item["reading"], item["meaning_ja"], f"{item['example']} / {item['example_ja']}"]
            rows.append("| " + " | ".join(x.replace("|", "／") for x in cells) + " |")
        rows.append("")
    if episode.get("quiz"):
        rows += ["## Quiz", ""]
        for number, item in enumerate(episode["quiz"], start=1):
            rows.append(f"{number}. {item['question_ja']}")
            for index, choice in enumerate(item["choices"]):
                mark = "✅" if index == item["answer"] else "・"
                rows.append(f"   - {mark} {choice}")
            if item["explanation_ja"]:
                rows.append(f"   - 解説: {item['explanation_ja']}")
        rows.append("")
    path.write_text("\n".join(rows), encoding="utf-8")


# --------------------------------------------------------------------------- reviewed script bundles


def bundle_episode(raw: dict, date: str, lang: dict, cfg: dict) -> dict:
    """A reviewed script from incoming/DATE.json (docs/EPISODE_BUNDLE.md) in this pipeline's episode format.

    The script is fixed, so only its structure is checked here; its length is judged on the rendered audio
    (tts.bundle_audio_seconds) instead of a text estimate.
    """
    if str(raw.get("language", "")) != lang["code"]:
        raise ValueError(f"language must be {lang['code']}")
    utterances = raw.get("utterances")
    if not isinstance(utterances, list) or len(utterances) < 20:
        raise ValueError("utterances missing or too few")
    allowed = {"ja-JP", lang["code"]}
    cleaned: list[dict] = []
    for index, line in enumerate(utterances):
        if not isinstance(line, dict):
            raise ValueError(f"utterance {index} is not an object")
        speaker, language = str(line.get("speaker", "")), str(line.get("language", ""))
        text = normalized_text(clean(str(line.get("text", ""))))
        text = re.sub(r"[`*_#]+", "", text).strip()
        if speaker not in {"MC_F", "MC_M"} or language not in allowed or not text:
            raise ValueError(f"invalid speaker, language or text at utterance {index}")
        if re.search(r"https?://|www\.", text, flags=re.I):
            raise ValueError(f"spoken URL at utterance {index}")
        item = {"speaker": speaker, "language": language, "text": text}
        intent = str(line.get("intent", "")).strip()
        if intent:
            item["intent"] = intent
        if language == lang["code"] and (line.get("slow") is True or intent == "review_slow"):
            item["slow"] = True
        cleaned.append(item)
    if {u["speaker"] for u in cleaned} != {"MC_F", "MC_M"} or {u["language"] for u in cleaned} != allowed:
        raise ValueError("both hosts and both languages must appear")
    stories = [
        {"source": clean_field(s.get("source"), 120), "title": clean_field(s.get("title"), 500), "url": str(s.get("url", ""))[:2000]}
        for s in raw.get("stories") or []
        if isinstance(s, dict) and s.get("title")
    ]
    title = clean_field(raw.get("title"), 160) or f"Journey Talk {lang['japanese_name']}"
    return {
        "episode_date": date,
        "language": lang["code"],
        "slug": lang["slug"],
        "language_name": lang["name"],
        "japanese_name": lang["japanese_name"],
        "title": title,
        "stories": stories,
        "estimated_seconds": round(estimate_seconds(cleaned, cfg["episode"].get("speech_rates")), 2),
        "utterances": cleaned,
        "script_model": "bundle",
        "source": "bundle",
    }


def enrichment_prompt(episode: dict, lang: dict, cfg: dict) -> str:
    learning = cfg["learning"]
    lines = [
        {"i": index, "language": u["language"], "text": u["text"]}
        for index, u in enumerate(episode["utterances"])
    ]
    return f"""
These are the lines of a finished Journey Talk episode: a Japanese-navigated {lang['name']} ({lang['code']}) language-learning
radio show for a Japanese listener around {cfg['episode'].get('learner_level', 'CEFR B1')}. Do not change the lines.
Write the study materials shown next to the audio in the web player.

- "translations": an object mapping the index (as a string) of EVERY {lang['code']} line to a natural Japanese translation.
- "title_ja": a short Japanese title for the episode.
- "summary_ja": two or three Japanese sentences on what the episode covers and what the listener will be able to say.
- "vocabulary": {learning['vocabulary_min']} to {learning['vocabulary_max']} useful words or phrases that appear in the {lang['code']} lines.
  Each item has "term", "reading" (pinyin with tone marks for Chinese, Revised Romanization for Korean, the word with a stress
  mark for Russian, otherwise ""), "meaning_ja", "example" (a {lang['code']} line containing the term) and "example_ja".
- "quiz": {learning['quiz_min']} to {learning['quiz_max']} listening-comprehension questions in Japanese about what the hosts said.
  Each item has "question_ja", "choices" (three or four short Japanese options), "answer" (zero-based index) and "explanation_ja".

Return JSON only: {{"title_ja":"...","summary_ja":"...","translations":{{"1":"..."}},"vocabulary":[...],"quiz":[...]}}

Lines:
{json.dumps(lines, ensure_ascii=False)}
""".strip()


def enrich_with_materials(episode: dict, lang: dict, cfg: dict) -> dict:
    """Add translations, vocabulary and a quiz to a bundle script. Best effort: the audio never waits on it."""
    if not text_models(cfg):
        episode.update(empty_learning())
        return episode
    try:
        raw, model = gemini_json_from(enrichment_prompt(episode, lang, cfg), cfg)
        translations = raw.get("translations") if isinstance(raw.get("translations"), dict) else {}
        for index, utterance in enumerate(episode["utterances"]):
            if utterance["language"] == lang["code"]:
                text = clean_field(translations.get(str(index)))
                if text:
                    utterance["ja"] = text
        episode.update(validate_learning(raw, episode, cfg))
        title_ja = clean_field(raw.get("title_ja"), 120)
        if title_ja:
            episode["title_ja"] = title_ja
        episode["materials_model"] = model
        print(f"[{lang['slug']}] study materials by {model}")
    except Exception as exc:  # noqa: BLE001 - materials are optional for a reviewed script
        # Translations that did arrive are kept; vocabulary and quiz need the full validation.
        print(f"::warning::{lang['slug']} study materials skipped: {str(exc)[:300]}")
        for key, value in empty_learning().items():
            episode.setdefault(key, value)
    return episode


def load_bundle(path: Path, date: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if str(data.get("date", "")) != date:
        raise ValueError(f"bundle date {data.get('date')!r} does not match {date}")
    episodes = data.get("episodes")
    if not isinstance(episodes, list):
        raise ValueError("bundle has no episodes list")
    return {str(item.get("slug", "")): item for item in episodes if isinstance(item, dict)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "languages")
    parser.add_argument("--bundle", type=Path, help="reviewed scripts (incoming/DATE.json) to use instead of writing new ones")
    args = parser.parse_args()

    cfg = load_config()
    bundle = load_bundle(args.bundle, args.date) if args.bundle else None
    if bundle is None and not any_script_provider():
        raise SystemExit("No script source: add incoming/DATE.json or one of GEMINI_API_KEY / MISTRAL_API_KEY / OPENROUTER_API_KEY")
    if bundle is None:
        news = collect_news(cfg)
        selected = choose_shared_stories(cfg, news)
    else:
        selected = []
    day = args.output_dir / args.date
    day.mkdir(parents=True, exist_ok=True)

    manifest = {
        "episode_date": args.date,
        "model": None,
        "source": "bundle" if bundle is not None else "generated",
        "stories": [{"source": x["source"], "title": x["title"], "url": x["url"]} for x in selected],
        "episodes": [],
        "failed": [],
    }
    for lang in cfg["languages"]:
        try:
            if bundle is not None:
                if lang["slug"] not in bundle:
                    raise ValueError("not in the bundle")
                episode = enrich_with_materials(bundle_episode(bundle[lang["slug"]], args.date, lang, cfg), lang, cfg)
                for story in episode["stories"]:
                    if story not in manifest["stories"]:
                        manifest["stories"].append(story)
            else:
                episode = generate_episode(args.date, lang, selected, cfg)
        except Exception as exc:
            # One language failing should not cost learners of the other four their daily episode.
            print(f"::warning::{lang['slug']} edition skipped: {exc}")
            manifest["failed"].append({"slug": lang["slug"], "error": str(exc)[:500]})
            continue
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
                "script_model": episode.get("script_model"),
                "source": episode.get("source", "generated"),
            }
        )
        print(f"[script] {lang['japanese_name']}: {json_path}")

    manifest["model"] = MODEL_USED.get("name")
    (day / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not manifest["episodes"]:
        print("[error] no language edition could be generated")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
