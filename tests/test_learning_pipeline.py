from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[1]
DATE = "2026-10-01"


def load_script(name: str):
    path = PROJECT_DIR / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"learning_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


build = load_script("build_language_episodes")
publish = load_script("publish_multilang_site")
BASE_CFG = build.load_config()
LANG = next(x for x in BASE_CFG["languages"] if x["slug"] == "es")
STORIES = [
    {"id": "N01", "source": "BBC World", "title": "Night trains return", "summary": "s", "url": "https://example.test/a"},
    {"id": "N02", "source": "NHK", "title": "観光ルート", "summary": "s", "url": "https://example.test/b"},
    {"id": "N03", "source": "Reuters", "title": "Rail routes", "summary": "s", "url": "https://example.test/c"},
]


def test_cfg() -> dict:
    cfg = copy.deepcopy(BASE_CFG)
    cfg["episode"]["minimum_seconds"] = 30
    return cfg


def raw_episode(slow_lines: int = 2, translate: bool = True) -> dict:
    utterances = []
    for index in range(48):
        speaker = "MC_F" if index % 2 == 0 else "MC_M"
        if index % 4 == 0:
            utterances.append({"speaker": speaker, "language": "ja-JP", "text": "ポイントを確認しましょう。", "ja": "ignored"})
        else:
            item = {"speaker": speaker, "language": "es-ES", "text": f"Me encanta viajar en tren número {index}.", "ja": "電車の旅が大好き。"}
            if not translate:
                item.pop("ja")
            if index >= 48 - slow_lines * 2 and index % 2:
                item["slow"] = True
            utterances.append(item)
    return {
        "title": "夜行列車の旅",
        "summary_ja": "夜行列車を題材に、旅の好みを話す表現を学びます。",
        "utterances": utterances,
        "vocabulary": [
            {"term": "viajar", "reading": "", "meaning_ja": "旅行する", "example": "Me encanta viajar.", "example_ja": "旅が大好き。"},
            {"term": "Viajar", "reading": "", "meaning_ja": "duplicate", "example": "", "example_ja": ""},
            {"term": "tren", "reading": "", "meaning_ja": "電車", "example": "", "example_ja": ""},
            {"term": "sin prisa", "reading": "", "meaning_ja": "急がずに", "example": "", "example_ja": ""},
            {"term": "vale la pena", "reading": "", "meaning_ja": "価値がある", "example": "", "example_ja": ""},
            {"term": "de moda", "reading": "", "meaning_ja": "流行の", "example": "", "example_ja": ""},
            {"term": "", "meaning_ja": "missing term"},
            "not an object",
        ],
        "quiz": [
            {"question_ja": f"質問{i}", "choices": ["正解", "誤り1", "誤り2"], "answer": 0, "explanation_ja": "解説"}
            for i in range(4)
        ]
        + [
            {"question_ja": "壊れた質問", "choices": ["a", "a", "b"], "answer": 0},
            {"question_ja": "範囲外", "choices": ["a", "b", "c"], "answer": 5},
            {"question_ja": "真偽値", "choices": ["a", "b", "c"], "answer": True},
        ],
    }


class EpisodeValidationTests(unittest.TestCase):
    def test_translations_only_on_target_lines_and_slow_is_capped(self):
        cfg = test_cfg()
        cfg["learning"]["max_slow_utterances"] = 1
        episode = build.validate_episode(raw_episode(slow_lines=3), DATE, LANG, STORIES, cfg)
        japanese = [x for x in episode["utterances"] if x["language"] == "ja-JP"]
        target = [x for x in episode["utterances"] if x["language"] == "es-ES"]
        self.assertTrue(all("ja" not in x and "slow" not in x for x in japanese))
        self.assertTrue(all(x["ja"] == "電車の旅が大好き。" for x in target))
        self.assertEqual(sum(1 for x in episode["utterances"] if x.get("slow")), 1)

    def test_slow_lines_count_toward_estimated_duration(self):
        plain = [{"speaker": "MC_F", "language": "es-ES", "text": "Hola a todos."}]
        slow = [dict(plain[0], slow=True)]
        self.assertGreater(build.estimate_seconds(slow), build.estimate_seconds(plain) + build.SHADOWING_SECONDS)

    def test_learning_materials_are_cleaned_and_quiz_answers_shuffled(self):
        cfg = test_cfg()
        raw = raw_episode()
        episode = build.validate_episode(raw, DATE, LANG, STORIES, cfg)
        learning = build.validate_learning(raw, episode, cfg)
        terms = [x["term"] for x in learning["vocabulary"]]
        self.assertEqual(terms, ["viajar", "tren", "sin prisa", "vale la pena", "de moda"])
        self.assertEqual(len(learning["quiz"]), 4)
        for item in learning["quiz"]:
            self.assertEqual(item["choices"][item["answer"]], "正解")
        self.assertGreater(len({x["answer"] for x in learning["quiz"]}), 1, "answers should not all sit in one position")
        again = build.validate_learning(raw, episode, cfg)
        self.assertEqual(learning, again, "shuffle must be deterministic")

    def test_missing_translations_or_too_few_items_are_rejected(self):
        cfg = test_cfg()
        untranslated = raw_episode(translate=False)
        episode = build.validate_episode(untranslated, DATE, LANG, STORIES, cfg)
        with self.assertRaisesRegex(ValueError, "translation missing"):
            build.validate_learning(untranslated, episode, cfg)
        sparse = raw_episode()
        sparse["quiz"] = sparse["quiz"][:1]
        episode = build.validate_episode(sparse, DATE, LANG, STORIES, cfg)
        with self.assertRaisesRegex(ValueError, "quiz has 1"):
            build.validate_learning(sparse, episode, cfg)


class GenerationRetryTests(unittest.TestCase):
    def test_validator_feedback_is_sent_with_the_retry(self):
        cfg = test_cfg()
        prompts: list[str] = []
        responses = [{"utterances": []}, raw_episode()]

        def fake(prompt, _cfg):
            prompts.append(prompt)
            return responses.pop(0)

        with mock.patch.object(build, "gemini_json", side_effect=fake):
            episode = build.generate_episode(DATE, LANG, STORIES, cfg)
        self.assertEqual(len(prompts), 2)
        self.assertNotIn("rejected by the validator", prompts[0])
        self.assertIn("utterance count 0", prompts[1])
        self.assertEqual(len(episode["quiz"]), 4)

    def test_spoken_script_survives_when_study_materials_keep_failing(self):
        cfg = test_cfg()
        raw = raw_episode()
        raw["vocabulary"] = []
        with mock.patch.object(build, "gemini_json", return_value=raw) as fake:
            episode = build.generate_episode(DATE, LANG, STORIES, cfg)
        self.assertEqual(fake.call_count, cfg["episode"]["generation_attempts"])
        self.assertEqual(episode["vocabulary"], [])
        self.assertEqual(len(episode["utterances"]), 48)

    def test_one_failed_language_does_not_block_the_others(self):
        cfg = test_cfg()
        cfg["languages"] = [x for x in cfg["languages"] if x["slug"] in {"es", "de"}]

        def fake(prompt, _cfg):
            if "German" in prompt:
                return {"utterances": []}
            return raw_episode()

        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(build, "load_config", return_value=cfg), \
                mock.patch.object(build, "collect_news", return_value=STORIES), \
                mock.patch.object(build, "choose_shared_stories", return_value=STORIES), \
                mock.patch.object(build, "gemini_json", side_effect=fake), \
                mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test"}), \
                mock.patch.object(sys, "argv", ["build", "--date", DATE, "--output-dir", temporary]):
            self.assertEqual(build.main(), 0)
            manifest = json.loads((Path(temporary) / DATE / "manifest.json").read_text(encoding="utf-8"))
            markdown = (Path(temporary) / DATE / "es.md").read_text(encoding="utf-8")
        self.assertEqual([x["slug"] for x in manifest["episodes"]], ["es"])
        self.assertEqual([x["slug"] for x in manifest["failed"]], ["de"])
        self.assertIn("## Vocabulary", markdown)
        self.assertIn("✅", markdown)


class PublishTests(unittest.TestCase):
    def test_vtt_timestamps_and_escaping(self):
        lines = [{"speaker": "MC_F", "language": "es-ES", "text": "A <b> & B", "start": 0.0, "end": 3725.5}]
        vtt = publish.build_vtt(lines, {"MC_F": "ミナ"})
        self.assertTrue(vtt.startswith("WEBVTT\n"))
        self.assertIn("00:00:00.000 --> 01:02:05.500", vtt)
        self.assertIn("<v ミナ>A &lt;b&gt; &amp; B", vtt)

    def test_timeline_length_must_match(self):
        with self.assertRaises(ValueError):
            publish.timed_lines([{"speaker": "MC_F", "language": "ja-JP", "text": "x"}], [[0, 1], [1, 2]])

    def test_publish_writes_study_page_data_transcripts_and_language_feeds(self):
        cfg = test_cfg()
        raw = raw_episode()
        episode = build.validate_episode(raw, DATE, LANG, STORIES, cfg)
        episode.update(build.validate_learning(raw, episode, cfg))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            episode_dir, media_dir, docs = root / "out", root / "media", root / "docs"
            episode_dir.mkdir()
            media_dir.mkdir()
            (episode_dir / "es.json").write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")
            (episode_dir / "manifest.json").write_text(json.dumps({
                "episode_date": DATE,
                "stories": episode["stories"],
                "episodes": [{"slug": "es", "language": "es-ES", "japanese_name": "スペイン語", "json": "es.json", "markdown": "es.md"}],
            }), encoding="utf-8")
            timeline = [[i * 2.0, i * 2.0 + 1.5] for i in range(len(episode["utterances"]))]
            (media_dir / "media-manifest.json").write_text(json.dumps({"episodes": [{
                "slug": "es", "audio": f"journey-talk-{DATE}-es.mp3", "duration_seconds": 700.0, "bytes": 1234, "timeline": timeline,
            }]}), encoding="utf-8")
            # A day published before the study player existed must keep working.
            docs.mkdir()
            (docs / "episodes.json").write_text(json.dumps([{"date": "2026-09-30", "stories": [], "episodes": [{
                "slug": "es", "language": "es-ES", "japanese_name": "スペイン語", "title": "old",
                "audio_url": "https://example.test/old.mp3", "script_url": "https://example.test/old.md",
                "duration_seconds": 650, "bytes": 99,
            }]}]), encoding="utf-8")
            argv = ["publish", "--date", DATE, "--episode-dir", str(episode_dir), "--media-dir", str(media_dir),
                    "--docs-dir", str(docs), "--repository", "Owner/repo"]
            for _ in range(2):
                with mock.patch.object(sys, "argv", argv):
                    self.assertEqual(publish.main(), 0)

            history = json.loads((docs / "episodes.json").read_text(encoding="utf-8"))
            self.assertEqual([x["date"] for x in history], [DATE, "2026-09-30"])
            entry = history[0]["episodes"][0]
            self.assertEqual(entry["detail_url"], f"episodes/{DATE}/es.json")
            self.assertEqual(entry["transcript_url"], f"episodes/{DATE}/es.vtt")
            self.assertEqual(entry["highlights"][0], "viajar — 旅行する")

            detail = json.loads((docs / entry["detail_url"]).read_text(encoding="utf-8"))
            self.assertEqual(len(detail["lines"]), len(episode["utterances"]))
            self.assertEqual(detail["lines"][1]["start"], 2.0)
            self.assertEqual(detail["lines"][1]["ja"], "電車の旅が大好き。")
            self.assertEqual(detail["hosts"]["MC_F"], "ミナ")
            self.assertEqual(len(detail["quiz"]), 4)
            self.assertTrue((docs / entry["transcript_url"]).read_text(encoding="utf-8").startswith("WEBVTT"))

            combined = ET.parse(docs / "feed.xml").getroot().findall("./channel/item")
            self.assertEqual(len(combined), 2)
            spanish = ET.parse(docs / "feeds" / "es.xml").getroot()
            items = spanish.findall("./channel/item")
            self.assertEqual(len(items), 2)
            transcript = items[0].find(f"{{{publish.PODCAST}}}transcript")
            self.assertEqual(transcript.attrib["url"], f"https://owner.github.io/repo/episodes/{DATE}/es.vtt")
            self.assertIn("今日の表現", items[0].findtext("description"))
            self.assertIsNone(items[1].find(f"{{{publish.PODCAST}}}transcript"))
            german = ET.parse(docs / "feeds" / "de.xml").getroot()
            self.assertEqual(german.findall("./channel/item"), [])


class KaraokeTests(unittest.TestCase):
    def test_word_ranges_use_utf16_offsets_and_skip_unmatched_words(self):
        ranges = publish.word_ranges("😀 ¿Has visto la Noticia?", [
            [0.0, 0.3, "Has"], [0.3, 0.6, "visto"], [0.6, 0.7, "missing"], [0.7, 0.8, "la"], [0.8, 1.2, "noticia"],
        ])
        self.assertEqual(ranges, [[0.0, 0.3, 4, 7], [0.3, 0.6, 8, 13], [0.7, 0.8, 14, 16], [0.8, 1.2, 17, 24]])

    def test_word_ranges_follow_order_for_repeated_words(self):
        ranges = publish.word_ranges("我们我们", [[0, 1, "我们"], [1, 2, "我们"]])
        self.assertEqual([r[2:] for r in ranges], [[0, 2], [2, 4]])

    def test_lines_carry_word_ranges(self):
        utterances = [{"speaker": "MC_F", "language": "es-ES", "text": "Hola amigos"}]
        lines = publish.timed_lines(utterances, [[1.0, 2.0]], [[[1.0, 1.4, "Hola"], [1.5, 2.0, "amigos"]]])
        self.assertEqual(lines[0]["w"], [[1.0, 1.4, 0, 4], [1.5, 2.0, 5, 11]])
        with self.assertRaises(ValueError):
            publish.timed_lines(utterances, [[1.0, 2.0]], [[], []])


class RenderTimingTests(unittest.TestCase):
    def test_word_boundaries_are_collected_and_offset_by_line_start(self):
        try:
            render = load_script("render_language_episodes")
            from pydub import AudioSegment
        except ImportError as exc:  # audio dependencies are installed in CI
            self.skipTest(f"audio dependencies unavailable: {exc}")

        class FakeCommunicate:
            def __init__(self, text, voice, rate, boundary):
                self.boundary = boundary

            async def stream(self):
                yield {"type": "audio", "data": b"ID3"}
                yield {"type": "WordBoundary", "offset": 1_000_000, "duration": 4_000_000, "text": "Hola"}
                yield {"type": "WordBoundary", "offset": 6_000_000, "duration": 3_000_000, "text": "amigos"}

        import asyncio
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(render.edge_tts, "Communicate", FakeCommunicate):
            words = asyncio.run(render.synthesize_one("Hola amigos", "voice", "+0%", Path(temporary) / "a.mp3"))
            self.assertEqual(words, [[0.1, 0.5, "Hola"], [0.6, 0.9, "amigos"]])
            paths = []
            for index in range(2):
                path = Path(temporary) / f"{index}.mp3"
                AudioSegment.silent(duration=1000).export(path, format="mp3")
                paths.append(path)
            utterances = [{"language": "es-ES"}, {"language": "es-ES"}]
            _, timeline, absolute = render.assemble(paths, utterances, BASE_CFG, [[], words])
        second_start = timeline[1][0]
        self.assertGreater(second_start, 1.0)
        self.assertEqual(absolute[0], [])
        self.assertAlmostEqual(absolute[1][0][0], round(second_start + 0.1, 2), places=2)


    def test_shadowing_pause_stays_below_qa_silence_limit(self):
        try:
            render = load_script("render_language_episodes")
        except ImportError as exc:  # audio dependencies are installed in CI
            self.skipTest(f"audio dependencies unavailable: {exc}")
        cfg = BASE_CFG
        low, high = cfg["learning"]["shadowing_pause_ms"]
        self.assertLess(high, cfg["qa"]["max_silence_seconds"] * 1000 - 500)
        slow = {"language": "es-ES", "slow": True}
        self.assertEqual(render.pause_after(slow, "es-ES", 100, cfg), low)
        self.assertEqual(render.pause_after(slow, "es-ES", 60_000, cfg), high)
        self.assertEqual(render.pause_after({"language": "es-ES"}, "es-ES", 1000, cfg), 480)
        self.assertEqual(render.pause_after({"language": "es-ES"}, "ja-JP", 1000, cfg), 650)


if __name__ == "__main__":
    unittest.main()
