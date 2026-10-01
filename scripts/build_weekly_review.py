"""Weekend review editions: one extra episode per language built only from the week's expressions.

Runs after build_language_episodes.py on the same day. For every language it gathers the
vocabulary published during the last week (docs/episodes/<date>/<slug>.json, plus today's
fresh edition in the output directory), asks Gemini for a review show that recycles those
expressions in new everyday situations, validates it with the daily contracts and appends it
to the day's manifest as "<slug>-weekly" so render, QA and publishing pick it up unchanged.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date as Date
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_language_episodes as daily  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def week_material(day: str, slug: str, docs_dir: Path, output_dir: Path, lookback_days: int) -> tuple[list[dict], list[dict]]:
    """Return (episodes, vocabulary) for one language over the lookback window, oldest first."""
    end = Date.fromisoformat(day)
    episodes: list[dict] = []
    vocabulary: list[dict] = []
    seen: set[str] = set()
    for offset in range(lookback_days - 1, -1, -1):
        current = (end - timedelta(days=offset)).isoformat()
        # Today's edition is not published to docs/ yet; earlier days come from the site history.
        data = read_json(output_dir / current / f"{slug}.json") if current == day else None
        data = data or read_json(docs_dir / "episodes" / current / f"{slug}.json")
        if not data:
            continue
        episodes.append({"date": current, "title": str(data.get("title", ""))})
        for item in data.get("vocabulary") or []:
            if not isinstance(item, dict) or not item.get("term") or not item.get("meaning_ja"):
                continue
            key = str(item["term"]).casefold()
            if key in seen:
                continue
            seen.add(key)
            vocabulary.append({**{k: item.get(k, "") for k in ("term", "reading", "meaning_ja", "example", "example_ja")}, "date": current})
    return episodes, vocabulary


def weekly_prompt(vocabulary: list[dict]):
    """Build a prompt function with the same signature generate_episode() expects."""

    def build(day: str, lang: dict, stories: list[dict], cfg: dict, feedback: str = "") -> str:
        minimum = cfg["episode"]["minimum_seconds"] // 60
        maximum = cfg["episode"]["maximum_seconds"] // 60
        level = cfg["episode"].get("learner_level", "CEFR B1")
        hosts = cfg.get("hosts", {})
        max_slow = cfg["learning"]["max_slow_utterances"]
        return f"""
Write the weekend review edition of Journey Talk in {lang['japanese_name']} for {day}.

Two regular hosts: MC_F is {hosts.get('MC_F', 'Mina')} (female) and MC_M is {hosts.get('MC_M', 'Ren')} (male).
The listener is Japanese, around {level} in {lang['name']}, and listened to this week's daily episodes.
This episode is a review: its only goal is to make this week's expressions stick. Use Japanese (ja-JP) for navigation and explanations.

Hard requirements:
- Natural spoken length: {minimum} to {maximum} minutes. Do not pad with empty repetition.
- Open with a short Japanese recap of the week's topics (titles below), then work through the expressions.
- Focus on the week's expressions listed below. Revisit as many as fit naturally, favouring the most useful ones.
- For each expression: use it in a NEW short everyday situation (not the news story it came from), explain nuance or
  common mistakes in Japanese, and add a quick challenge where one host asks in Japanese "how would you say ...?"
  and the other answers in {lang['name']}.
- Other words should be simple, high-frequency vocabulary so the reviewed expressions stand out.
- Do not restate news facts; you may mention which day's topic an expression came from.
- Two hosts chatting naturally, short turns, both speak Japanese and {lang['name']}; target-language speech is the majority.
- End with a rapid review: for the best expressions, say the {lang['code']} sentence at natural speed, then repeat it once
  more as a separate utterance with "slow": true so the listener can shadow it. At most {max_slow} slow utterances, only
  target-language ones. Then a one-line encouraging sign-off for next week.
- No markdown or URLs inside spoken text.

{daily.materials_contract(lang, cfg)}
The "vocabulary" list must be taken from this week's expressions below (you may correct a reading or meaning).

This week's episodes:
{json.dumps(stories, ensure_ascii=False)}

This week's expressions:
{json.dumps(vocabulary, ensure_ascii=False)}{daily.retry_note(feedback)}
""".strip()

    return build


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "output" / "languages")
    parser.add_argument("--docs-dir", type=Path, default=ROOT / "docs")
    args = parser.parse_args()

    cfg = daily.load_config()
    weekly_cfg = cfg.get("weekly_review", {})
    lookback = int(weekly_cfg.get("lookback_days", 7))
    max_terms = int(weekly_cfg.get("max_expressions", 30))
    day_dir = args.output_dir / args.date
    manifest_path = day_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    present = {x["slug"] for x in manifest["episodes"]}

    for lang in cfg["languages"]:
        slug = f"{lang['slug']}-weekly"
        if slug in present:
            print(f"[weekly] {slug}: already built")
            continue
        episodes, vocabulary = week_material(args.date, lang["slug"], args.docs_dir, args.output_dir, lookback)
        if len(vocabulary) < int(cfg["learning"]["vocabulary_min"]):
            print(f"[weekly] {lang['slug']}: only {len(vocabulary)} expressions this week; skipped")
            continue
        vocabulary = vocabulary[-max_terms:]  # keep the most recent ones if the week was rich
        stories = [{"source": f"Journey Talk {x['date']}", "title": x["title"], "url": ""} for x in episodes]
        try:
            episode = daily.generate_episode(args.date, lang, stories, cfg, prompt_builder=weekly_prompt(vocabulary))
        except Exception as exc:
            # A review edition is a bonus; never let it cost the day's regular editions.
            print(f"::warning::{slug} weekly review skipped: {exc}")
            manifest.setdefault("failed", []).append({"slug": slug, "error": str(exc)[:500]})
            continue
        episode["slug"] = slug
        episode["kind"] = "weekly"
        if not episode["title"].startswith("週末まとめ"):
            episode["title"] = f"週末まとめ：{episode['title']}"
        json_path = day_dir / f"{slug}.json"
        md_path = day_dir / f"{slug}.md"
        json_path.write_text(json.dumps(episode, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        daily.write_markdown(episode, md_path)
        manifest["episodes"].append(
            {
                "slug": slug,
                "language": lang["code"],
                "japanese_name": lang["japanese_name"],
                "json": json_path.name,
                "markdown": md_path.name,
                "kind": "weekly",
            }
        )
        print(f"[weekly] {lang['japanese_name']}: {json_path} ({len(vocabulary)} expressions)")

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
