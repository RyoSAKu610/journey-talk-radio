# Journey Talk production episode bundle

A reviewed script for a day can be committed as one JSON bundle:

`incoming/YYYY-MM-DD.json`

When it exists, `daily-multilang.yml` uses these scripts instead of having the script models write new ones
(`build_language_episodes.py --bundle`). The study materials (Japanese translations, vocabulary, quiz) are added
by the script models when a key is configured; the voices come from the normal TTS order (Fish Audio, Gemini,
CosyVoice on Kaggle / Modal / the Actions CPU, Google Cloud, Edge as the last resort). Past days can be built
from their bundles with the workflow's `dates` input.

## Required top-level shape

```json
{
  "date": "2026-10-07",
  "generator": "chatgpt-scheduled-generator-v1",
  "episodes": [
    {
      "slug": "de",
      "language": "de-DE",
      "title": "...",
      "stories": [
        {"source": "BBC World", "title": "...", "url": "https://..."}
      ],
      "utterances": [
        {
          "speaker": "MC_F",
          "language": "ja-JP",
          "intent": "intro",
          "text": "...",
          "optional": false
        }
      ]
    }
  ]
}
```

Exactly five episodes are required: `de`, `es`, `ru`, `zh`, `ko`.

## Voice / host rules

- `MC_F` is Mina and `MC_M` is Ren.
- Gender never determines who teaches, asks, or explains. Both hosts must take active roles.
- Keep each host between roughly one third and two thirds of turns.
- The target language must be the majority of spoken content; Japanese is navigation and explanation.
- Target-language lines must use that language's native script.
- Korean co-host dialogue uses friendly `해요체` consistently.
- Mandarin lines use neutral Putonghua; do not write pronunciation hacks that depend on pitch shifting.
- German uses friendly `du`; Spain Spanish uses `tú`; Russian co-hosts use friendly `ты`.

## Intents

Each utterance should declare one of the intents below (kept as metadata). `review_slow` target-language lines
are read slowly (0.75×) with a pause after them for shadowing.

`intro`, `story_intro`, `conversation`, `question`, `reaction`, `explanation`, `teaching`, `example`, `review`, `review_slow`, `trivia`, `closing`.

Every episode should include at least: `intro`, `story_intro`, `question`, `reaction`, `teaching`, `review`, `trivia`, `closing`.

`review` and `review_slow` may intentionally repeat a useful expression; avoid other exact duplicates.

## Duration and writing style

- Aim for 10–15 minutes of audio (about 12) and 60–75 conversational turns. Target-language lines are read at a
  learner pace (0.85×), so a script needs roughly 8,000 characters for German/Spanish/Russian, 2,600 for Chinese
  and 3,800 for Korean (the 2026-10-07 bundle, at about half of that, renders to only 6–7 minutes).
- Individual turns should usually be 1–3 short spoken sentences.
- Use natural reactions and follow-up questions; avoid textbook A/B dialogue.
- Do not put URLs or Markdown in spoken text.
- News facts must be supported by the listed source/title/summary used by the producer. If detail is uncertain, teach vocabulary or nuance instead of inventing facts.
- End with a compact review and one useful cultural/conversation trivia section.

## Publishing gate

The structure is checked when the bundle is loaded (speakers, languages, no spoken URLs, both hosts and both
languages present). Length is judged on the rendered audio: 5 to 18 minutes for a bundle edition
(`episode.bundle_audio_seconds`), aiming for about 12. Each edition then passes audio QA on its own (full decode,
long silences, loudness and peak, Whisper comparison); only editions that pass are published, the others are
reported in the run summary and `qa-report.json`.
