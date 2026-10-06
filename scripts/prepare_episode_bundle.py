from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from voice_director import ALLOWED_INTENTS, VoiceDirector, normalize_spoken_text

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "de": "de-DE",
    "es": "es-ES",
    "ru": "ru-RU",
    "zh": "zh-CN",
    "ko": "ko-KR",
}
REQUIRED_INTENTS = {"intro", "story_intro", "question", "reaction", "teaching", "review", "trivia", "closing"}


def _letters(text: str) -> list[str]:
    return [ch for ch in text if unicodedata.category(ch).startswith("L")]


def _script_ratio(text: str, language: str) -> float:
    letters = _letters(text)
    if not letters:
        return 1.0

    def is_latin(ch: str) -> bool:
        name = unicodedata.name(ch, "")
        return "LATIN" in name

    def is_cyrillic(ch: str) -> bool:
        name = unicodedata.name(ch, "")
        return "CYRILLIC" in name

    def is_han(ch: str) -> bool:
        return "CJK UNIFIED IDEOGRAPH" in unicodedata.name(ch, "") or "CJK COMPATIBILITY IDEOGRAPH" in unicodedata.name(ch, "")

    def is_hangul(ch: str) -> bool:
        return "HANGUL" in unicodedata.name(ch, "")

    if language in {"de-DE", "es-ES"}:
        good = sum(is_latin(ch) for ch in letters)
    elif language == "ru-RU":
        good = sum(is_cyrillic(ch) for ch in letters)
    elif language == "zh-CN":
        good = sum(is_han(ch) for ch in letters)
    elif language == "ko-KR":
        good = sum(is_hangul(ch) for ch in letters)
    else:
        return 1.0
    return good / len(letters)


def _validate_story(story: dict[str, Any], index: int) -> dict[str, str]:
    source = str(story.get("source", "")).strip()
    title = str(story.get("title", "")).strip()
    url = str(story.get("url", "")).strip()
    if not source or not title:
        raise ValueError(f"story {index} requires source and title")
    if url and not re.match(r"^https://", url):
        raise ValueError(f"story {index} URL must be https:// when present")
    return {"source": source[:120], "title": title[:500], "url": url[:2000]}


def _validate_episode(raw: dict[str, Any], date: str, director: VoiceDirector) -> dict[str, Any]:
    slug = str(raw.get("slug", "")).strip()
    if slug not in EXPECTED:
        raise ValueError(f"unsupported episode slug: {slug!r}")
    language = str(raw.get("language", "")).strip()
    if language != EXPECTED[slug]:
        raise ValueError(f"{slug}: language must be {EXPECTED[slug]}, got {language!r}")
    title = str(raw.get("title", "")).strip()
    if not title or len(title) > 160:
        raise ValueError(f"{slug}: title is missing or too long")

    stories_raw = raw.get("stories", [])
    if not isinstance(stories_raw, list) or not 2 <= len(stories_raw) <= 5:
        raise ValueError(f"{slug}: stories must contain 2 to 5 items")
    stories = [_validate_story(story, i) for i, story in enumerate(stories_raw)]
    if len({story["url"] or story["title"] for story in stories}) != len(stories):
        raise ValueError(f"{slug}: duplicate stories are not allowed")

    utterances_raw = raw.get("utterances")
    if not isinstance(utterances_raw, list) or not 42 <= len(utterances_raw) <= 90:
        raise ValueError(f"{slug}: utterances must contain 42 to 90 turns")

    utterances: list[dict[str, Any]] = []
    speakers: Counter[str] = Counter()
    intents: Counter[str] = Counter()
    target_seconds = 0.0
    japanese_seconds = 0.0
    target_line_ratios: list[float] = []
    seen_non_review: set[tuple[str, str]] = set()

    for index, item in enumerate(utterances_raw):
        if not isinstance(item, dict):
            raise ValueError(f"{slug}: utterance {index} is not an object")
        speaker = str(item.get("speaker", "")).strip()
        if speaker not in {"MC_F", "MC_M"}:
            raise ValueError(f"{slug}: invalid speaker at utterance {index}")
        line_language = str(item.get("language", "")).strip()
        if line_language not in {"ja-JP", language}:
            raise ValueError(f"{slug}: invalid language {line_language!r} at utterance {index}")
        text = normalize_spoken_text(str(item.get("text", "")))
        if not text or len(text) > 500:
            raise ValueError(f"{slug}: utterance {index} is empty or too long")
        if re.search(r"https?://|www\.", text, flags=re.I):
            raise ValueError(f"{slug}: spoken URL at utterance {index}")
        intent = str(item.get("intent", "")).strip()
        if intent not in ALLOWED_INTENTS:
            raise ValueError(f"{slug}: explicit valid intent is required at utterance {index}")
        optional = bool(item.get("optional", False))

        canonical = re.sub(r"\W+", "", unicodedata.normalize("NFKC", text).casefold())
        duplicate_key = (line_language, canonical)
        if intent not in {"review", "review_slow"} and duplicate_key in seen_non_review:
            raise ValueError(f"{slug}: duplicate non-review utterance at {index}")
        if intent not in {"review", "review_slow"}:
            seen_non_review.add(duplicate_key)

        clean_item = {
            "speaker": speaker,
            "language": line_language,
            "intent": intent,
            "text": text,
            "optional": optional,
        }
        utterances.append(clean_item)
        speakers[speaker] += 1
        intents[intent] += 1
        seconds = director.estimate_utterance_seconds(clean_item, index, len(utterances_raw))
        if line_language == language:
            target_seconds += seconds
            target_line_ratios.append(_script_ratio(text, language))
        else:
            japanese_seconds += seconds

    total_turns = len(utterances)
    for speaker, count in speakers.items():
        ratio = count / total_turns
        if not 0.32 <= ratio <= 0.68:
            raise ValueError(f"{slug}: {speaker} turn share {ratio:.2%} is outside 32-68%")
    if set(speakers) != {"MC_F", "MC_M"}:
        raise ValueError(f"{slug}: both hosts are required")

    missing_intents = REQUIRED_INTENTS - set(intents)
    if missing_intents:
        raise ValueError(f"{slug}: missing required intents: {sorted(missing_intents)}")

    speech_total = target_seconds + japanese_seconds
    if speech_total <= 0 or target_seconds / speech_total < 0.54:
        raise ValueError(f"{slug}: target language must be at least 54% of estimated spoken content")

    if target_line_ratios:
        avg_script_ratio = sum(target_line_ratios) / len(target_line_ratios)
        min_ratio = 0.70 if language in {"de-DE", "es-ES", "ru-RU"} else 0.58
        if avg_script_ratio < min_ratio:
            raise ValueError(f"{slug}: target-script ratio {avg_script_ratio:.2f} is below {min_ratio:.2f}")

    estimated = director.estimate_episode_seconds(utterances, seed=f"{date}:{slug}")
    if not 555 <= estimated <= 945:
        raise ValueError(f"{slug}: estimated duration {estimated:.1f}s is outside preflight 555-945s")

    return {
        "episode_date": date,
        "slug": slug,
        "language": language,
        "title": title,
        "stories": stories,
        "estimated_seconds": round(estimated, 2),
        "voice_profile_version": director.version,
        "utterances": utterances,
    }


def write_markdown(episode: dict[str, Any], path: Path) -> None:
    rows = [f"# {episode['title']}", "", f"Date: {episode['episode_date']}", ""]
    for story in episode["stories"]:
        rows.append(f"- {story['source']}: {story['title']}")
    rows.append("")
    for utterance in episode["utterances"]:
        rows.append(f"**{utterance['speaker']} [{utterance['language']} / {utterance['intent']}]**  {utterance['text']}")
        rows.append("")
    path.write_text("\n".join(rows), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "languages")
    parser.add_argument("--profile", type=Path, default=ROOT / "voice_profiles.yaml")
    args = parser.parse_args()

    if not args.input.is_file():
        raise FileNotFoundError(f"Daily episode bundle not found: {args.input}")
    raw = json.loads(args.input.read_text(encoding="utf-8"))
    if str(raw.get("date", "")) != args.date:
        raise ValueError(f"bundle date {raw.get('date')!r} does not match requested date {args.date}")
    episodes_raw = raw.get("episodes")
    if not isinstance(episodes_raw, list) or len(episodes_raw) != 5:
        raise ValueError("bundle must contain exactly five language episodes")

    director = VoiceDirector(args.profile)
    director.verify_voice_matrix()
    episodes = [_validate_episode(item, args.date, director) for item in episodes_raw]
    if {episode["slug"] for episode in episodes} != set(EXPECTED):
        raise ValueError("bundle must contain exactly de, es, ru, zh, ko")

    out = args.output_dir / args.date
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "episode_date": args.date,
        "generator": str(raw.get("generator", "unspecified"))[:120],
        "voice_profile_version": director.version,
        "episodes": [],
    }
    for episode in sorted(episodes, key=lambda item: list(EXPECTED).index(item["slug"])):
        json_path = out / f"{episode['slug']}.json"
        md_path = out / f"{episode['slug']}.md"
        json_path.write_text(json.dumps(episode, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_markdown(episode, md_path)
        manifest["episodes"].append(
            {
                "slug": episode["slug"],
                "language": episode["language"],
                "title": episode["title"],
                "json": json_path.name,
                "markdown": md_path.name,
                "estimated_seconds": episode["estimated_seconds"],
            }
        )
        print(f"[contract] {episode['slug']}: PASS {episode['estimated_seconds']:.1f}s estimated")

    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
