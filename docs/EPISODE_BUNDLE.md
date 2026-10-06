# Journey Talk production episode bundle

The production renderer does **not** call Gemini or any paid LLM API. A daily content producer writes one reviewed JSON bundle to:

`incoming/YYYY-MM-DD.json`

The GitHub Actions pipeline validates that file, renders five language editions, runs audio QA, and publishes only when every edition passes.

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

Every utterance must declare one of:

`intro`, `story_intro`, `conversation`, `question`, `reaction`, `explanation`, `teaching`, `example`, `review`, `review_slow`, `trivia`, `closing`.

Every episode must include at least: `intro`, `story_intro`, `question`, `reaction`, `teaching`, `review`, `trivia`, `closing`.

`review` and `review_slow` may intentionally repeat a useful expression. Other exact duplicate lines are rejected.

## Duration and writing style

- Production audio must land between 10 and 15 minutes **without time stretching**.
- Aim for approximately 12 minutes and 50–75 conversational turns.
- Individual turns should usually be 1–3 short spoken sentences.
- Use natural reactions and follow-up questions; avoid textbook A/B dialogue.
- Do not put URLs or Markdown in spoken text.
- News facts must be supported by the listed source/title/summary used by the producer. If detail is uncertain, teach vocabulary or nuance instead of inventing facts.
- End with a compact review and one useful cultural/conversation trivia section.

## Publishing gate

A bundle is not publishable until all of these pass:

1. JSON/content contract
2. configured voice availability
3. complete TTS rendering with per-utterance retry
4. 10–15 minute duration without speed-forcing
5. two-pass loudness normalization
6. full MP3 decode
7. long-silence and clipping/volume checks
8. sampled language-aware Whisper verification
9. feed generation and final QA status `PASS`

If any gate fails, the day's MP3s are not published.
