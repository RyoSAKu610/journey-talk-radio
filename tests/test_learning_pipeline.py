from __future__ import annotations

import base64
import copy
import importlib.util
import io
import json
import re
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
    cfg["episode"]["parallel_models"] = 1
    return cfg


def as_contestant(fake):
    """Adapt a fake(prompt, cfg) -> answer to gemini_json_from(prompt, cfg, models) -> (answer, model)."""
    def call(prompt, cfg, models=None):
        return fake(prompt, cfg), (models or ["test-model"])[0]
    return call


LINES = 56


def raw_episode(slow_lines: int = 2, translate: bool = True) -> dict:
    utterances = []
    for index in range(LINES):
        speaker = "MC_F" if index % 2 == 0 else "MC_M"
        if index % 4 == 0:
            utterances.append({"speaker": speaker, "language": "ja-JP", "text": "ポイントを確認しましょう。", "ja": "ignored"})
        else:
            item = {"speaker": speaker, "language": "es-ES", "text": f"Me encanta viajar en tren número {index}.", "ja": "電車の旅が大好き。"}
            if not translate:
                item.pop("ja")
            if index >= LINES - slow_lines * 2 and index % 2:
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


class ModelContestTests(unittest.TestCase):
    def setUp(self):
        build.EXHAUSTED.clear()
        self.addCleanup(build.EXHAUSTED.clear)

    def test_models_out_of_quota_are_skipped_and_flash_lite_is_last_resort(self):
        cfg = test_cfg()
        cfg["episode"]["parallel_models"] = 3
        regular = cfg["provider"]["models"]
        self.assertNotIn("gemini-3.1-flash-lite", regular)
        build.EXHAUSTED.update(regular[1:])
        self.assertEqual(build.contestant_orders(cfg), [[regular[0]]])
        build.EXHAUSTED.add(regular[0])
        self.assertEqual(build.contestant_orders(cfg), [cfg["provider"]["last_resort_models"]])
        build.EXHAUSTED.update(cfg["provider"]["last_resort_models"])
        with self.assertRaisesRegex(RuntimeError, "out of free-tier quota"):
            build.generate_episode(DATE, LANG, STORIES, cfg)

    def test_quota_running_out_mid_round_moves_to_the_last_resort(self):
        cfg = test_cfg()
        cfg["episode"]["parallel_models"] = 2
        used = []

        def fake(prompt, _cfg, models):
            used.append(models[0])
            if models[0] in cfg["provider"]["models"]:
                build.EXHAUSTED.update(cfg["provider"]["models"])  # every regular model hits its daily quota
                raise RuntimeError("no Gemini model answered: HTTP 429")
            return raw_episode(), models[0]

        with mock.patch.object(build, "gemini_json_from", side_effect=fake):
            episode = build.generate_episode(DATE, LANG, STORIES, cfg)
        self.assertEqual(episode["script_model"], cfg["provider"]["last_resort_models"][0])

    def test_json_followed_by_extra_text_is_read(self):
        body = {"candidates": [{"content": {"parts": [{"text": '{"ids": ["N02"]}\n\nHope this helps! {"x": 1}'}]}}]}

        class Response:
            status_code, text = 200, ""

            def json(self):
                return body

        with mock.patch.object(build, "post_gemini", return_value=(Response(), "m")):
            self.assertEqual(build.gemini_json_from("prompt", test_cfg()), ({"ids": ["N02"]}, "m"))

    def test_parallel_models_compete_and_the_best_valid_script_wins(self):
        cfg = test_cfg()
        cfg["episode"]["parallel_models"] = 3
        short, terse_study, good = raw_episode(), raw_episode(), raw_episode()
        short["utterances"] = short["utterances"][:20]
        terse_study["vocabulary"] = []
        answers = {cfg["provider"]["models"][0]: short, cfg["provider"]["models"][1]: terse_study, cfg["provider"]["models"][2]: good}
        orders = []

        def fake(prompt, _cfg, models):
            orders.append(models)
            return answers[models[0]], models[0]

        with mock.patch.object(build, "gemini_json_from", side_effect=fake):
            episode = build.generate_episode(DATE, LANG, STORIES, cfg)
        self.assertEqual(sorted(o[0] for o in orders), sorted(cfg["provider"]["models"][:3]))
        self.assertTrue(all(len(o) == len(cfg["provider"]["models"]) for o in orders), "each contestant can fall back")
        self.assertEqual(episode["script_model"], cfg["provider"]["models"][2])
        self.assertEqual(len(episode["vocabulary"]), 5)

    def test_round_with_only_failures_feeds_the_length_error_back(self):
        cfg = test_cfg()
        cfg["episode"]["parallel_models"] = 2
        short = raw_episode()
        short["utterances"] = short["utterances"][:20]
        prompts = []

        def fake(prompt, _cfg, models):
            prompts.append(prompt)
            if len(prompts) <= 2:
                if models[0] == cfg["provider"]["models"][0]:
                    raise ValueError("Gemini returned an empty response")
                return short, models[0]
            return raw_episode(), models[0]

        with mock.patch.object(build, "gemini_json_from", side_effect=fake):
            episode = build.generate_episode(DATE, LANG, STORIES, cfg)
        self.assertIn("utterance count 20", prompts[2])
        self.assertEqual(len(episode["utterances"]), LINES)

    def test_every_model_unavailable_fails_the_language(self):
        cfg = test_cfg()
        cfg["episode"]["parallel_models"] = 2

        def fake(prompt, _cfg, models):
            raise RuntimeError("no Gemini model answered: HTTP 503")

        with mock.patch.object(build, "gemini_json_from", side_effect=fake):
            with self.assertRaisesRegex(RuntimeError, "no Gemini model answered"):
                build.generate_episode(DATE, LANG, STORIES, cfg)


class TextModelTests(unittest.TestCase):
    def setUp(self):
        build.EXHAUSTED.clear()
        self.addCleanup(build.EXHAUSTED.clear)

    class Response:
        def __init__(self, status, body=None, text=""):
            self.status_code, self._body, self.text = status, body or {}, text

        def json(self):
            return self._body

    def test_retired_or_exhausted_models_fall_through_to_the_next(self):
        answer = {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}, {"text": '{"ids": ["N01"]}'}]}}]}
        responses = [self.Response(404, text="no longer available"), self.Response(429, text="GenerateRequestsPerDayPerProjectPerModel"), self.Response(200, answer)]
        urls = []

        def fake_post(url, **kwargs):
            urls.append(url)
            return responses.pop(0)

        cfg = test_cfg()
        with mock.patch.object(build.requests, "post", side_effect=fake_post), mock.patch.object(build.time, "sleep"), \
                mock.patch.dict("os.environ", {"GEMINI_API_KEY": "test", "GEMINI_MODEL": ""}):
            self.assertEqual(build.gemini_json("prompt", cfg), {"ids": ["N01"]})
        self.assertEqual([u.split("/models/")[1].split(":")[0] for u in urls], cfg["provider"]["models"][:3])
        self.assertEqual(build.MODEL_USED["name"], cfg["provider"]["models"][2])

    def test_short_scripts_get_actionable_feedback(self):
        raw = raw_episode()
        raw["utterances"] = raw["utterances"][:52]
        with self.assertRaisesRegex(ValueError, r"too short: add turns"):
            build.validate_episode(raw, DATE, LANG, STORIES, BASE_CFG)
        spanish = build.length_plan(LANG, BASE_CFG)
        chinese = build.length_plan(next(x for x in BASE_CFG["languages"] if x["slug"] == "zh"), BASE_CFG)
        self.assertIn("Write 60 to 74 utterances", spanish)
        turn_chars = lambda plan: int(re.search(r"turns: (\d+) to", plan).group(1))
        # Turn sizes follow the measured speaking rate: a Chinese turn needs far fewer characters.
        self.assertGreater(turn_chars(spanish), 3 * turn_chars(chinese))


class GenerationRetryTests(unittest.TestCase):
    def test_validator_feedback_is_sent_with_the_retry(self):
        cfg = test_cfg()
        prompts: list[str] = []
        responses = [{"utterances": []}, raw_episode()]

        def fake(prompt, _cfg):
            prompts.append(prompt)
            return responses.pop(0)

        with mock.patch.object(build, "gemini_json_from", side_effect=as_contestant(fake)):
            episode = build.generate_episode(DATE, LANG, STORIES, cfg)
        self.assertEqual(len(prompts), 2)
        self.assertNotIn("rejected by the validator", prompts[0])
        self.assertIn("utterance count 0", prompts[1])
        self.assertEqual(len(episode["quiz"]), 4)

    def test_spoken_script_survives_when_study_materials_keep_failing(self):
        cfg = test_cfg()
        raw = raw_episode()
        raw["vocabulary"] = []
        with mock.patch.object(build, "gemini_json_from", side_effect=as_contestant(lambda p, c: raw)) as fake:
            episode = build.generate_episode(DATE, LANG, STORIES, cfg)
        self.assertEqual(fake.call_count, cfg["episode"]["generation_attempts"])
        self.assertEqual(episode["vocabulary"], [])
        self.assertEqual(len(episode["utterances"]), LINES)

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
                mock.patch.object(build, "gemini_json_from", side_effect=as_contestant(fake)), \
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
    def test_editions_without_audio_are_not_published(self):
        cfg = test_cfg()
        raw = raw_episode()
        episode = build.validate_episode(raw, DATE, LANG, STORIES, cfg)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "out").mkdir()
            (root / "media").mkdir()
            for slug in ("es", "de"):
                (root / "out" / f"{slug}.json").write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")
            (root / "out" / "manifest.json").write_text(json.dumps({"episode_date": DATE, "stories": [], "episodes": [
                {"slug": slug, "language": "es-ES", "japanese_name": "x", "json": f"{slug}.json", "markdown": f"{slug}.md"}
                for slug in ("es", "de")]}), encoding="utf-8")
            (root / "media" / "media-manifest.json").write_text(json.dumps({"episodes": [
                {"slug": "es", "audio": "a.mp3", "duration_seconds": 700, "bytes": 1}], "failed": [{"slug": "de", "error": "tts"}]}), encoding="utf-8")
            argv = ["publish", "--date", DATE, "--episode-dir", str(root / "out"), "--media-dir", str(root / "media"), "--docs-dir", str(root / "docs")]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(publish.main(), 0)
            history = json.loads((root / "docs" / "episodes.json").read_text(encoding="utf-8"))
        self.assertEqual([x["slug"] for x in history[0]["episodes"]], ["es"])

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
                "offline_audio": f"journey-talk-{DATE}-es.offline.mp3", "offline_bytes": 400,
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
            self.assertEqual(entry["offline_url"], f"offline/{DATE}/journey-talk-{DATE}-es.offline.mp3")
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


class WeeklyReviewTests(unittest.TestCase):
    def setUp(self):
        self.weekly = load_script("build_weekly_review")
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.docs, self.output = root / "docs", root / "output"
        for day, terms in (("2026-09-26", ["viejo"]), ("2026-09-28", ["viajar", "tren"]), ("2026-10-01", ["Tren", "sin prisa"])):
            path = self.docs / "episodes" / day / "es.json"
            path.parent.mkdir(parents=True)
            vocab = [{"term": t, "reading": "", "meaning_ja": f"意味{t}", "example": "", "example_ja": ""} for t in terms]
            path.write_text(json.dumps({"title": f"回{day}", "vocabulary": vocab}, ensure_ascii=False), encoding="utf-8")
        today = self.output / "2026-10-04"
        today.mkdir(parents=True)
        (today / "es.json").write_text(json.dumps({"title": "今日", "vocabulary": [
            {"term": "vale la pena", "meaning_ja": "価値がある"}, {"term": "de moda", "meaning_ja": "流行の"},
            {"term": "Viajar", "meaning_ja": "dup"},
        ]}, ensure_ascii=False), encoding="utf-8")
        (today / "manifest.json").write_text(json.dumps({"episode_date": "2026-10-04", "stories": [], "episodes": [
            {"slug": "es", "language": "es-ES", "japanese_name": "スペイン語", "json": "es.json", "markdown": "es.md"}]}), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_week_material_spans_lookback_and_dedupes(self):
        episodes, vocab = self.weekly.week_material("2026-10-04", "es", self.docs, self.output, 7)
        self.assertEqual([x["date"] for x in episodes], ["2026-09-28", "2026-10-01", "2026-10-04"])
        self.assertEqual([x["term"] for x in vocab], ["viajar", "tren", "sin prisa", "vale la pena", "de moda"])

    def run_weekly(self, cfg, fake):
        argv = ["weekly", "--date", "2026-10-04", "--output-dir", str(self.output), "--docs-dir", str(self.docs)]
        with mock.patch.object(self.weekly.daily, "load_config", return_value=cfg), \
                mock.patch.object(self.weekly.daily, "gemini_json_from", side_effect=as_contestant(fake)), \
                mock.patch.object(sys, "argv", argv):
            self.assertEqual(self.weekly.main(), 0)
        return json.loads((self.output / "2026-10-04" / "manifest.json").read_text(encoding="utf-8"))

    def test_review_edition_is_appended_with_weekly_slug(self):
        cfg = test_cfg()
        cfg["languages"] = [LANG]
        prompts = []

        def fake(prompt, _cfg):
            prompts.append(prompt)
            return raw_episode()

        manifest = self.run_weekly(cfg, fake)
        self.assertEqual([x["slug"] for x in manifest["episodes"]], ["es", "es-weekly"])
        self.assertIn("weekend review edition", prompts[0])
        self.assertIn("sin prisa", prompts[0])
        self.assertNotIn("viejo", prompts[0], "expressions older than the lookback window must be left out")
        episode = json.loads((self.output / "2026-10-04" / "es-weekly.json").read_text(encoding="utf-8"))
        self.assertEqual(episode["kind"], "weekly")
        self.assertTrue(episode["title"].startswith("週末まとめ"))
        # Re-running the step is a no-op.
        manifest = self.run_weekly(cfg, fake)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(len(manifest["episodes"]), 2)

    def test_failures_and_thin_weeks_never_fail_the_run(self):
        cfg = test_cfg()
        cfg["languages"] = [LANG, next(x for x in BASE_CFG["languages"] if x["slug"] == "de")]
        manifest = self.run_weekly(cfg, lambda prompt, _cfg: {"utterances": []})
        self.assertEqual([x["slug"] for x in manifest["episodes"]], ["es"])
        self.assertEqual([x["slug"] for x in manifest["failed"]], ["es-weekly"])

    def test_language_feed_includes_weekly_edition(self):
        history = [{"date": "2026-10-04", "episodes": [
            {"slug": slug, "kind": kind, "language": "es-ES", "japanese_name": "スペイン語", "title": slug,
             "audio_url": "https://example.test/a.mp3", "bytes": 1, "duration_seconds": 700}
            for slug, kind in (("es", "daily"), ("es-weekly", "weekly"), ("de", "daily"))]}]
        items = publish.build_feed(history, "https://example.test", slug="es").getroot().findall("./channel/item")
        self.assertEqual([x.findtext("guid") for x in items], ["journey-talk:2026-10-04:es", "journey-talk:2026-10-04:es-weekly"])
        self.assertIn("07:05:00", items[1].findtext("pubDate"))


class OfflineAndArtworkTests(unittest.TestCase):
    def test_feeds_carry_cover_art(self):
        history = [{"date": DATE, "episodes": []}]
        combined = publish.build_feed(history, "https://example.test").getroot().find("channel")
        self.assertEqual(combined.find(f"{{{publish.ITUNES}}}image").attrib["href"], "https://example.test/covers/journey-talk.png")
        spanish = publish.build_feed(history, "https://example.test", slug="es", image="covers/es.png").getroot().find("channel")
        self.assertEqual(spanish.findtext("image/url"), "https://example.test/covers/es.png")

    def test_committed_artwork_exists_for_every_language(self):
        for lang in BASE_CFG["languages"]:
            self.assertTrue((PROJECT_DIR / "docs" / "covers" / f"{lang['slug']}.png").is_file(), lang["slug"])
        manifest = json.loads((PROJECT_DIR / "docs" / "manifest.webmanifest").read_text(encoding="utf-8"))
        for icon in manifest["icons"]:
            self.assertTrue((PROJECT_DIR / "docs" / icon["src"]).is_file(), icon["src"])

    def test_render_writes_small_offline_copy_with_same_length(self):
        try:
            render = load_script("render_language_episodes")
            from pydub import AudioSegment
        except ImportError as exc:  # audio dependencies are installed in CI
            self.skipTest(f"audio dependencies unavailable: {exc}")
        self.assertEqual(render.offline_name("journey-talk-2026-10-01-es-weekly.mp3"), "journey-talk-2026-10-01-es-weekly.offline.mp3")
        tone = AudioSegment.silent(duration=3000)
        with tempfile.TemporaryDirectory() as temporary:
            master, offline = Path(temporary) / "a.mp3", Path(temporary) / "a.offline.mp3"
            render.export_normalized(tone, master, offline)
            self.assertTrue(master.is_file() and offline.is_file())
            self.assertLess(offline.stat().st_size, master.stat().st_size)
            self.assertAlmostEqual(render.probe_duration(master), render.probe_duration(offline), delta=0.1)


class SpeechEngineTests(unittest.TestCase):
    """Gemini (one take per episode, split into lines), OpenAI (per line) and the engine order."""

    def setUp(self):
        try:
            self.tts = load_script("tts_engines")
            from pydub import AudioSegment
            from pydub.generators import Sine
        except ImportError as exc:  # audio dependencies are installed in CI
            self.skipTest(f"audio dependencies unavailable: {exc}")
        self.AudioSegment, self.Sine = AudioSegment, Sine
        self.sleeps = []
        self.utterances = [
            {"speaker": "MC_F", "language": "ja-JP", "text": "それでは次の話題です。夜行列車が人気だそうです。"},
            {"speaker": "MC_M", "language": "es-ES", "text": "¿Has visto la noticia de hoy sobre los trenes nocturnos?"},
            {"speaker": "MC_F", "language": "es-ES", "text": "Sí, dicen que vuelven a estar de moda en Europa."},
            {"speaker": "MC_M", "language": "es-ES", "text": "Vale la pena, aunque tarde más.", "slow": True},
        ]

    def take(self, lengths, gap_ms=900, inner_gap_ms=None):
        """A synthetic multi-line take: one tone per line, separated by hand-over pauses."""
        audio = self.AudioSegment.silent(duration=200, frame_rate=24000)
        for index, ms in enumerate(lengths):
            if inner_gap_ms:  # a breath inside the line, shorter than the hand-over pause
                half = ms // 2
                audio += self.Sine(220 + 60 * index).to_audio_segment(duration=half).apply_gain(-6)
                audio += self.AudioSegment.silent(duration=inner_gap_ms, frame_rate=24000)
                audio += self.Sine(220 + 60 * index).to_audio_segment(duration=ms - half).apply_gain(-6)
            else:
                audio += self.Sine(220 + 60 * index).to_audio_segment(duration=ms).apply_gain(-6)
            audio += self.AudioSegment.silent(duration=gap_ms, frame_rate=24000)
        return audio.set_frame_rate(24000).set_channels(1).set_sample_width(2)

    class Response:
        def __init__(self, status, body=None, text="", content=b"", headers=None):
            self.status_code, self._body, self.content, self.headers = status, body or {}, content, headers or {}
            self.text = text or (json.dumps(body) if body else "")

        def json(self):
            return self._body

    def gemini_body(self, audio):
        buffer = io.BytesIO()
        audio.export(buffer, format="wav")
        return {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(buffer.getvalue()).decode()}}]}}]}

    def session(self, responses, calls):
        class Session:
            def post(_self, url, **kwargs):
                calls.append((url, kwargs))
                return responses.pop(0)
        return Session()

    def test_gemini_sends_one_tagged_multi_speaker_request_per_episode(self):
        expected = [self.tts.expected_seconds(u["text"], u["language"]) for u in self.utterances]
        take = self.take([int(e * 1000) for e in expected])
        calls = []
        engine = self.tts.GeminiEpisodeTTS(BASE_CFG, api_key="k", session=self.session([self.Response(200, self.gemini_body(take))], calls), sleep=self.sleeps.append)
        clips = engine.render(self.utterances)
        self.assertEqual(len(calls), 1)
        body = calls[0][1]["json"]
        parts = body["contents"][0]["parts"]
        self.assertEqual([p["speechMetadata"]["speaker"] for p in parts], ["Mina", "Ren", "Mina", "Ren"])
        self.assertTrue(all(p["text"].endswith("[long pause]") for p in parts))
        voices = body["generationConfig"]["speechConfig"]["multiSpeakerVoiceConfig"]["speakerVoiceConfigs"]
        self.assertEqual({v["speaker"] for v in voices}, {"Mina", "Ren"})
        for clip, want in zip(clips, expected):
            self.assertAlmostEqual(len(clip) / 1000, want, delta=0.25)

    def test_split_prefers_hand_over_pauses_to_breaths_inside_a_line(self):
        lengths = [3000, 4200, 2600, 3600, 2200, 4000]
        take = self.take(lengths, gap_ms=1100, inner_gap_ms=450)
        spans = self.tts.align_lines(take, [x / 1000 for x in lengths])
        self.assertEqual(len(spans), 6)
        for (a, b), want in zip(spans, lengths):
            # Each clip holds exactly its own line (including the breath inside it) once edge silence is trimmed.
            self.assertAlmostEqual(len(self.tts.trim_silence(take[a:b])), want + 450, delta=150)

    def test_take_that_does_not_match_the_script_is_rejected(self):
        with self.assertRaises(ValueError):
            self.tts.align_lines(self.take([3000, 3000]), [1.0, 1.0, 1.0, 1.0, 1.0, 8.0])

    def test_gemini_moves_on_after_daily_quota_and_remembers_rejected_keys(self):
        expected = [self.tts.expected_seconds(u["text"], u["language"]) for u in self.utterances]
        take = self.take([int(e * 1000) for e in expected])
        quota = self.Response(429, text='{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}')
        calls = []
        engine = self.tts.GeminiEpisodeTTS(BASE_CFG, api_key="k", session=self.session([quota, self.Response(200, self.gemini_body(take))], calls), sleep=self.sleeps.append)
        engine.render(self.utterances)
        models = [url.split("/models/")[1].split(":")[0] for url, _ in calls]
        self.assertEqual(models, BASE_CFG["tts"]["gemini"]["models"][:2])
        self.assertEqual(self.sleeps, [])
        calls.clear()
        engine = self.tts.GeminiEpisodeTTS(BASE_CFG, api_key="k", session=self.session([self.Response(400, text="API_KEY_INVALID")], calls), sleep=self.sleeps.append)
        for _ in range(2):
            with self.assertRaises(self.tts.TTSError):
                engine.render(self.utterances)
        self.assertEqual(len(calls), 1)

    def test_rate_limits_wait_for_the_server_retry_delay(self):
        expected = [self.tts.expected_seconds(u["text"], u["language"]) for u in self.utterances]
        take = self.take([int(e * 1000) for e in expected])
        busy = self.Response(429, text='{"details": [{"retryDelay": "7s"}]}')
        engine = self.tts.GeminiEpisodeTTS(BASE_CFG, api_key="k", session=self.session([busy, self.Response(200, self.gemini_body(take))], []), sleep=self.sleeps.append)
        engine.render(self.utterances)
        self.assertEqual(self.sleeps, [7.0])

    def test_openai_renders_each_line_with_language_instructions(self):
        pcm = self.Sine(300).to_audio_segment(duration=900).set_frame_rate(24000).set_channels(1).set_sample_width(2).raw_data
        calls = []
        responses = [self.Response(200, content=pcm) for _ in self.utterances]
        engine = self.tts.OpenAITTS(BASE_CFG, api_key="k", session=self.session(responses, calls), sleep=self.sleeps.append)
        clips = engine.render(self.utterances)
        self.assertEqual(len(calls), len(self.utterances))
        first = calls[0][1]
        self.assertEqual(first["headers"]["Authorization"], "Bearer k")
        self.assertEqual(first["json"]["voice"], BASE_CFG["tts"]["openai"]["voices"]["MC_F"])
        self.assertEqual(first["json"]["response_format"], "pcm")
        self.assertIn("Japanese", first["json"]["instructions"])
        self.assertIn("Spanish", calls[1][1]["json"]["instructions"])
        self.assertTrue(all(800 <= len(c) <= 1000 for c in clips))

    def test_openai_without_quota_fails_fast(self):
        engine = self.tts.OpenAITTS(BASE_CFG, api_key="k", session=self.session([self.Response(429, text="insufficient_quota")], []), sleep=self.sleeps.append)
        with self.assertRaisesRegex(self.tts.TTSError, "no remaining quota"):
            engine.render(self.utterances[:1])

    def cloud(self, responses, calls, used=0, budget=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        usage = Path(temporary.name) / "usage.json"
        if used:
            usage.write_text(json.dumps({"2026-10": used}))
        cfg = copy.deepcopy(BASE_CFG)
        if budget is not None:
            cfg["tts"]["google_cloud"]["monthly_character_budget"] = budget
        engine = self.tts.GoogleCloudTTS(cfg, api_key="k", session=self.session(responses, calls), sleep=self.sleeps.append, usage_path=usage, month="2026-10")
        return engine, usage

    def wav_body(self, ms=900):
        buffer = io.BytesIO()
        self.Sine(300).to_audio_segment(duration=ms).set_frame_rate(24000).set_channels(1).export(buffer, format="wav")
        return {"audioContent": base64.b64encode(buffer.getvalue()).decode()}

    def test_cloud_tts_uses_chirp3_hd_voices_and_native_pace(self):
        calls = []
        engine, usage = self.cloud([self.Response(200, self.wav_body()) for _ in self.utterances], calls)
        zh = {"speaker": "MC_M", "language": "zh-CN", "text": "我们走吧。"}
        clips = engine.render(self.utterances)
        engine.session = self.session([self.Response(200, self.wav_body())], calls)
        engine.render([zh])
        bodies = [c[1]["json"] for c in calls]
        self.assertEqual(bodies[0]["voice"], {"languageCode": "ja-JP", "name": "ja-JP-Chirp3-HD-Aoede"})
        self.assertEqual(bodies[1]["voice"]["name"], "es-ES-Chirp3-HD-Puck")
        self.assertEqual(bodies[-1]["voice"], {"languageCode": "cmn-CN", "name": "cmn-CN-Chirp3-HD-Puck"})
        self.assertEqual([b["audioConfig"]["speakingRate"] for b in bodies[:4]], [1.0, 0.85, 0.85, 0.75])
        self.assertTrue(all(800 <= len(c) <= 1000 for c in clips))
        self.assertTrue(engine.native_pace)
        recorded = json.loads(usage.read_text())["2026-10"]
        self.assertEqual(recorded, sum(len(u["text"]) for u in self.utterances) + len(zh["text"]))

    def test_cloud_tts_never_exceeds_the_monthly_free_budget(self):
        calls = []
        engine, usage = self.cloud([], calls, used=949_990, budget=950_000)
        with self.assertRaisesRegex(self.tts.TTSError, "budget would be exceeded"):
            engine.render(self.utterances)
        self.assertEqual(calls, [], "no request may be sent once the budget is reached")

    def test_cloud_tts_disabled_api_is_reported_once(self):
        calls = []
        refused = self.Response(403, text="Cloud Text-to-Speech API has not been used in project 1 before or it is disabled")
        engine, _ = self.cloud([refused], calls)
        for _ in range(2):
            with self.assertRaisesRegex(self.tts.TTSError, "HTTP 403"):
                engine.render(self.utterances)
        self.assertEqual(len(calls), 1)

    def test_slow_rate_and_time_stretch(self):
        self.assertEqual(self.tts.tempo_from_rate("-25%"), 0.75)
        self.assertEqual(self.tts.tempo_from_rate("+0%"), 1.0)
        tone = self.Sine(300).to_audio_segment(duration=2000)
        self.assertAlmostEqual(len(self.tts.slow_down(tone, 0.75)) / 1000, 2.0 / 0.75, delta=0.1)

    def test_estimated_word_timings_follow_text_order(self):
        words = self.tts.estimate_word_timings("¿Has visto la noticia?", 2.0)
        self.assertEqual([w[2] for w in words], ["Has", "visto", "la", "noticia"])
        self.assertTrue(all(a[1] <= b[0] for a, b in zip(words, words[1:])))
        self.assertEqual([w[2] for w in self.tts.estimate_word_timings("我喜欢 AI", 1.0)], ["我", "喜", "欢", "AI"])
        self.assertEqual(len(publish.word_ranges("¿Has visto la noticia?", words)), 4)

    def test_engines_are_tried_in_order_down_to_edge(self):
        try:
            render = load_script("render_language_episodes")
        except ImportError as exc:
            self.skipTest(f"audio dependencies unavailable: {exc}")
        self.assertEqual(render.tts_order(BASE_CFG), ["gemini", "google_cloud", "openai", "edge"])
        with mock.patch.dict("os.environ", {"TTS_ORDER": "openai"}):
            self.assertEqual(render.tts_order(BASE_CFG), ["openai", "edge"])

        class Failing:
            def __init__(self, name):
                self.name, self.model_in_use, self.calls = name, None, 0

            def render(self, utterances):
                self.calls += 1
                raise render.TTSError("quota exhausted")

        async def fake_edge(cfg, utterances, work):
            return ["edge.mp3"], [[]]

        gemini, openai = Failing("gemini"), Failing("openai")
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(render, "synthesize", fake_edge):
            paths, _, engine = render.render_audio(BASE_CFG, self.utterances[:1], Path(temporary), [gemini, openai])
        self.assertEqual((paths, engine, gemini.calls, openai.calls), (["edge.mp3"], "edge", 1, 1))
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "", "GOOGLE_TTS_API_KEY": "", "OPENAI_API_KEY": ""}):
            self.assertEqual(render.open_engines(BASE_CFG), [])


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
