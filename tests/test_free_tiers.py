"""Free-tier engines and script sources: Mistral / OpenRouter contestants, reviewed script bundles,
Fish Audio, CosyVoice on Kaggle and on the runner's CPU, per-edition QA and QA-gated publishing."""
from __future__ import annotations

import copy
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest import mock

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))


def load_script(name: str):
    path = PROJECT_DIR / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"free_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


build = load_script("build_language_episodes")
BASE_CFG = build.load_config()
LANGS = {x["slug"]: x for x in BASE_CFG["languages"]}
KEYS = {"GEMINI_API_KEY": "g", "MISTRAL_API_KEY": "m", "OPENROUTER_API_KEY": "o"}


class Response:
    def __init__(self, status, body=None, text="", content=b"", headers=None):
        self.status_code, self._body, self.content, self.headers = status, body, content, headers or {}
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise build.requests.HTTPError(str(self.status_code))


def chat(content, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


class ScriptProviderTests(unittest.TestCase):
    def setUp(self):
        build.EXHAUSTED.clear()
        build.FREE_MODEL_CACHE.clear()
        self.addCleanup(build.EXHAUSTED.clear)
        self.addCleanup(build.FREE_MODEL_CACHE.clear)

    def test_contestants_start_on_different_providers(self):
        cfg = copy.deepcopy(BASE_CFG)
        cfg["episode"]["parallel_models"] = 3
        with mock.patch.dict("os.environ", {**KEYS, "GEMINI_MODEL": ""}):
            orders = build.contestant_orders(cfg)
        self.assertEqual([build.provider_of(o[0]) for o in orders], ["gemini", "mistral", "openrouter"])
        self.assertTrue(all(len(o) == len(cfg["provider"]["models"]) for o in orders), "each contestant can fall back")

    def test_providers_without_a_key_are_left_out(self):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "", "MISTRAL_API_KEY": "m", "OPENROUTER_API_KEY": ""}):
            models = build.text_models(BASE_CFG)
            self.assertTrue(models)
            self.assertTrue(all(m.startswith("mistral:") for m in models))
            self.assertTrue(build.any_script_provider())
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "", "MISTRAL_API_KEY": "", "OPENROUTER_API_KEY": ""}):
            self.assertFalse(build.any_script_provider())
            self.assertEqual(build.contestant_orders(BASE_CFG), [])

    def test_mistral_answer_is_parsed_and_labelled(self):
        calls = []

        def post(url, **kwargs):
            calls.append((url, kwargs))
            return Response(200, chat('```json\n{"ids": ["N01"]}\n```'))

        with mock.patch.dict("os.environ", KEYS), mock.patch.object(build.requests, "post", side_effect=post):
            answer, model = build.gemini_json_from("prompt", BASE_CFG, ["mistral:mistral-medium-latest"])
        self.assertEqual((answer, model), ({"ids": ["N01"]}, "mistral:mistral-medium-latest"))
        url, kwargs = calls[0]
        self.assertEqual(url, "https://api.mistral.ai/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer m")
        self.assertEqual(kwargs["json"]["model"], "mistral-medium-latest")
        self.assertEqual(kwargs["json"]["response_format"], {"type": "json_object"})

    def test_openrouter_picks_todays_preferred_free_model(self):
        listing = {"data": [
            {"id": "meta/llama-4-maverick:free", "context_length": 128000},
            {"id": "acme/paid-model", "context_length": 999999},
            {"id": "tiny/model:free", "context_length": 8000},
            {"id": "deepseek/deepseek-chat-v3.1:free", "context_length": 64000, "top_provider": {"max_completion_tokens": 16000}},
            {"id": "img/maker:free", "context_length": 64000, "architecture": {"output_modalities": ["image"]}},
        ]}
        posts = []

        def post(url, **kwargs):
            posts.append(kwargs["json"])
            return Response(200, chat('{"ok": true}'))

        with mock.patch.dict("os.environ", KEYS), \
                mock.patch.object(build.requests, "get", return_value=Response(200, listing)) as get, \
                mock.patch.object(build.requests, "post", side_effect=post):
            for _ in range(2):
                answer, model = build.gemini_json_from("prompt", BASE_CFG, ["openrouter:free"])
        self.assertEqual(model, "openrouter:deepseek/deepseek-chat-v3.1:free")
        self.assertEqual(posts[0]["max_tokens"], 16000)
        self.assertEqual(get.call_count, 1, "the free model is looked up once per run")

    def test_daily_quota_moves_on_to_the_next_provider(self):
        gemini_ok = {"candidates": [{"content": {"parts": [{"text": '{"ok": 1}'}]}}]}
        responses = [Response(429, text='{"error": "Rate limit exceeded: free-models-per-day"}'), Response(200, gemini_ok)]

        with mock.patch.dict("os.environ", KEYS), mock.patch.object(build.time, "sleep"), \
                mock.patch.object(build.requests, "post", side_effect=lambda url, **k: responses.pop(0)), \
                mock.patch.object(build, "openrouter_free_model", return_value={"id": "x/y:free", "max_tokens": 0}):
            answer, model = build.gemini_json_from("prompt", BASE_CFG, ["openrouter:free", "gemini-3.8-flash"])
        self.assertEqual((answer, model), ({"ok": 1}, "gemini-3.8-flash"))
        self.assertIn("openrouter:free", build.EXHAUSTED)

    def test_truncated_answer_is_a_content_error(self):
        with mock.patch.dict("os.environ", KEYS), \
                mock.patch.object(build.requests, "post", return_value=Response(200, chat('{"a"', finish="length"))):
            with self.assertRaisesRegex(ValueError, "output token limit"):
                build.gemini_json_from("prompt", BASE_CFG, ["mistral:mistral-medium-latest"])


class BundleTests(unittest.TestCase):
    def test_reviewed_bundles_load_without_any_voice_director(self):
        for path in sorted((PROJECT_DIR / "incoming").glob("*.json")):
            bundle = build.load_bundle(path, path.stem)
            for slug, lang in LANGS.items():
                episode = build.bundle_episode(bundle[slug], path.stem, lang, BASE_CFG)
                self.assertEqual(episode["source"], "bundle")
                self.assertTrue(any(u.get("slow") for u in episode["utterances"]), f"{path.stem} {slug}: review_slow becomes slow")
                self.assertTrue(all(u["language"] in {"ja-JP", lang["code"]} for u in episode["utterances"]))
        self.assertFalse((PROJECT_DIR / "scripts" / "voice_director.py").exists())

    def test_bundle_rejects_wrong_language_and_urls(self):
        bundle = build.load_bundle(PROJECT_DIR / "incoming" / "2026-10-08.json", "2026-10-08")
        with self.assertRaisesRegex(ValueError, "language must be"):
            build.bundle_episode(bundle["de"], "2026-10-08", LANGS["es"], BASE_CFG)
        broken = copy.deepcopy(bundle["es"])
        broken["utterances"][3]["text"] = "Mira https://example.com ahora"
        with self.assertRaisesRegex(ValueError, "URL"):
            build.bundle_episode(broken, "2026-10-08", LANGS["es"], BASE_CFG)
        with self.assertRaisesRegex(ValueError, "does not match"):
            build.load_bundle(PROJECT_DIR / "incoming" / "2026-10-08.json", "2026-10-09")

    def test_study_materials_are_added_and_failure_keeps_the_script(self):
        bundle = build.load_bundle(PROJECT_DIR / "incoming" / "2026-10-08.json", "2026-10-08")
        episode = build.bundle_episode(bundle["es"], "2026-10-08", LANGS["es"], BASE_CFG)
        target = [i for i, u in enumerate(episode["utterances"]) if u["language"] == "es-ES"]
        materials = {
            "title_ja": "今日のニュース会話",
            "summary_ja": "ニュースで会話します。",
            "translations": {str(i): f"訳{i}" for i in target},
            "vocabulary": [{"term": f"palabra{i}", "reading": "", "meaning_ja": "語", "example": "", "example_ja": ""} for i in range(6)],
            "quiz": [{"question_ja": f"質問{i}", "choices": ["あ", "い", "う"], "answer": 0, "explanation_ja": ""} for i in range(4)],
        }
        with mock.patch.dict("os.environ", KEYS), \
                mock.patch.object(build, "gemini_json_from", return_value=(materials, "mistral:m")):
            enriched = build.enrich_with_materials(copy.deepcopy(episode), LANGS["es"], BASE_CFG)
        self.assertEqual(enriched["utterances"][target[0]]["ja"], f"訳{target[0]}")
        self.assertEqual((len(enriched["vocabulary"]), len(enriched["quiz"]), enriched["title_ja"]), (6, 4, "今日のニュース会話"))
        with mock.patch.dict("os.environ", KEYS), \
                mock.patch.object(build, "gemini_json_from", side_effect=RuntimeError("no script model answered")):
            plain = build.enrich_with_materials(copy.deepcopy(episode), LANGS["es"], BASE_CFG)
        self.assertEqual((plain["vocabulary"], plain["quiz"]), ([], []))
        self.assertEqual(len(plain["utterances"]), len(episode["utterances"]))

    def test_main_builds_all_languages_from_a_bundle_without_keys(self):
        none = {"GEMINI_API_KEY": "", "MISTRAL_API_KEY": "", "OPENROUTER_API_KEY": ""}
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict("os.environ", none), \
                mock.patch.object(sys, "argv", ["build", "--date", "2026-10-07", "--output-dir", temporary,
                                                "--bundle", str(PROJECT_DIR / "incoming" / "2026-10-07.json")]):
            self.assertEqual(build.main(), 0)
            manifest = json.loads((Path(temporary) / "2026-10-07" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"], "bundle")
        self.assertEqual([x["slug"] for x in manifest["episodes"]], ["de", "es", "ru", "zh", "ko"])
        self.assertTrue(manifest["stories"])


class CosyVoiceCoreTests(unittest.TestCase):
    def test_inference_requirements_drop_training_and_gpu_extras(self):
        core = load_script("cosyvoice_core")
        text = (
            "--extra-index-url https://download.pytorch.org/whl/cu121\n"
            "conformer==0.3.2\ndeepspeed==0.15.1; sys_platform == 'linux'\ngradio==5.4.0\n"
            "onnxruntime-gpu==1.18.0; sys_platform == 'linux'\ntensorrt-cu12==10.13.3.9\ntorch==2.3.1\n"
            "transformers==4.51.3 # tokenizer\n"
        )
        self.assertEqual(core.inference_requirements(text, skip={"torch"}), ["conformer==0.3.2", "transformers==4.51.3", "onnxruntime==1.18.0"])


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        try:
            self.tts = load_script("tts_engines")
            from pydub import AudioSegment
            from pydub.generators import Sine
        except ImportError as exc:  # audio dependencies are installed in CI
            self.skipTest(f"audio dependencies unavailable: {exc}")
        self.AudioSegment, self.Sine = AudioSegment, Sine
        self.utterances = [
            {"speaker": "MC_F", "language": "ja-JP", "text": "それでは次の話題です。"},
            {"speaker": "MC_M", "language": "es-ES", "text": "¿Has visto la noticia de hoy?"},
            {"speaker": "MC_F", "language": "es-ES", "text": "Vale la pena.", "slow": True},
        ]

    def wav(self, ms=800):
        buffer = io.BytesIO()
        self.Sine(300).to_audio_segment(duration=ms).apply_gain(-6).export(buffer, format="wav")
        return buffer.getvalue()

    def write_clips(self, directory: Path, count: int, done=True):
        directory.mkdir(parents=True, exist_ok=True)
        for index in range(count):
            self.Sine(300).to_audio_segment(duration=700).apply_gain(-6).export(directory / f"{index:03d}.flac", format="flac")
        if done:
            (directory / "DONE").write_text("ok\n")


class FishAudioTests(EngineTestCase):
    def engine(self, responses, calls, today=date(2026, 10, 9)):
        class Session:
            def post(_self, url, **kwargs):
                calls.append((url, kwargs))
                return responses.pop(0)

        return self.tts.FishAudioTTS(BASE_CFG, api_key="fk", session=Session(), sleep=lambda s: None, today=today)

    def test_each_line_is_one_cloned_request_at_the_learner_pace(self):
        import msgpack

        calls = []
        engine = self.engine([Response(200, content=self.wav()) for _ in self.utterances], calls)
        clips = engine.render(self.utterances)
        self.assertEqual(len(clips), 3)
        self.assertEqual(engine.model_in_use, "s2.1-pro-free")
        url, kwargs = calls[0]
        self.assertEqual(url, "https://api.fish.audio/v1/tts")
        self.assertEqual(kwargs["headers"]["model"], "s2.1-pro-free")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/msgpack")
        bodies = sorted((msgpack.unpackb(c[1]["data"]) for c in calls), key=lambda b: b["text"])
        speeds = {b["text"]: b["prosody"]["speed"] for b in bodies}
        self.assertEqual(speeds["それでは次の話題です。"], 1.0)
        self.assertEqual(speeds["¿Has visto la noticia de hoy?"], 0.85)
        self.assertEqual(speeds["Vale la pena."], 0.75)
        reference = bodies[0]["references"][0]
        self.assertTrue(reference["audio"].startswith(b"RIFF"))
        self.assertTrue(reference["text"])

    def test_payment_required_disables_the_engine_and_the_offer_ends(self):
        calls = []
        engine = self.engine([Response(402, text="insufficient balance")], calls)
        with self.assertRaisesRegex(self.tts.TTSError, "402"):
            engine.render(self.utterances)
        with self.assertRaisesRegex(self.tts.TTSError, "402"):
            engine.render(self.utterances)
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(self.tts.TTSError, "2026-11-30"):
            self.engine([], [], today=date(2026, 12, 1))
        with mock.patch.dict("os.environ", {"FISH_API_KEY": ""}):
            with self.assertRaisesRegex(self.tts.TTSError, "FISH_API_KEY"):
                self.tts.FishAudioTTS(BASE_CFG)

    def test_rate_limits_are_retried(self):
        calls = []
        responses = [Response(429, headers={"Retry-After": "2"}), *[Response(200, content=self.wav()) for _ in self.utterances]]
        engine = self.engine(responses, calls)
        engine.parallel = 1
        self.assertEqual(len(engine.render(self.utterances)), 3)
        self.assertEqual(len(calls), 4)


class KaggleTests(EngineTestCase):
    def test_one_kernel_run_renders_every_pending_episode(self):
        commands = []
        episodes = {"es": self.utterances, "de": self.utterances[:2]}
        test = self

        def runner(args, **kwargs):
            commands.append(args[1:3])
            if args[1:3] == ["kernels", "push"]:
                folder = Path(args[4])
                metadata = json.loads((folder / "kernel-metadata.json").read_text())
                test.assertEqual(metadata["id"], "mina/journey-talk-tts")
                test.assertEqual((metadata["enable_gpu"], metadata["enable_internet"], metadata["is_private"]), ("true", "true", "true"))
                test.assertEqual(metadata["machine_shape"], "NvidiaTeslaT4")
                source = (folder / "kernel.py").read_text()
                compile(source, "kernel.py", "exec")
                payload = source.split('PAYLOAD = "', 1)[1].split('"', 1)[0]
                import base64
                with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload))) as archive:
                    job = json.loads(archive.read("job.json"))
                    test.assertIn("cosyvoice_worker.py", archive.namelist())
                test.assertEqual([e["slug"] for e in job["episodes"]], ["es", "de"])
                test.assertEqual(job["episodes"][0]["lines"][2]["speed"], 0.75)
                return subprocess.CompletedProcess(args, 0, "pushed", "")
            if args[1:3] == ["kernels", "status"]:
                return subprocess.CompletedProcess(args, 0, 'mina/journey-talk-tts has status "KernelWorkerStatus.COMPLETE"', "")
            if args[1:3] == ["kernels", "output"]:
                target = Path(args[args.index("-p") + 1])
                with tempfile.TemporaryDirectory() as staging:
                    test.write_clips(Path(staging) / "es", 3)
                    test.write_clips(Path(staging) / "de", 2, done=False)  # cut off by the deadline
                    with zipfile.ZipFile(target / "tts.zip", "w") as archive:
                        for path in Path(staging).rglob("*"):
                            if path.is_file():
                                archive.write(path, path.relative_to(staging))
                return subprocess.CompletedProcess(args, 0, "", "")
            raise AssertionError(args)

        env = {"KAGGLE_USERNAME": "mina", "KAGGLE_API_TOKEN": "t"}
        with mock.patch.dict("os.environ", env), mock.patch.object(self.tts.shutil, "which", return_value="/usr/bin/kaggle"):
            engine = self.tts.KaggleCosyVoiceTTS(BASE_CFG, runner=runner, sleep=lambda s: None)
            results = engine.render_many(episodes)
        self.assertEqual(list(results), ["es"])
        self.assertEqual(len(results["es"]), 3)
        self.assertEqual(commands, [["kernels", "push"], ["kernels", "status"], ["kernels", "output"]])

    def test_failed_kernel_is_a_tts_error_and_needs_credentials(self):
        def runner(args, **kwargs):
            if args[1:3] == ["kernels", "status"]:
                return subprocess.CompletedProcess(args, 0, 'has status "KernelWorkerStatus.ERROR"', "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.dict("os.environ", {"KAGGLE_USERNAME": "mina", "KAGGLE_API_TOKEN": "t"}), \
                mock.patch.object(self.tts.shutil, "which", return_value="/usr/bin/kaggle"):
            engine = self.tts.KaggleCosyVoiceTTS(BASE_CFG, runner=runner, sleep=lambda s: None)
            with self.assertRaisesRegex(self.tts.TTSError, "error"):
                engine.render_many({"es": self.utterances})
        with mock.patch.dict("os.environ", {"KAGGLE_USERNAME": "", "KAGGLE_API_TOKEN": "", "KAGGLE_KEY": ""}):
            with self.assertRaisesRegex(self.tts.TTSError, "KAGGLE_USERNAME"):
                self.tts.KaggleCosyVoiceTTS(BASE_CFG)


class LocalCosyVoiceTests(EngineTestCase):
    def test_runs_only_on_the_actions_runner_and_returns_finished_episodes(self):
        with mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "", "COSYVOICE_LOCAL": ""}):
            with self.assertRaisesRegex(self.tts.TTSError, "GitHub Actions"):
                self.tts.LocalCosyVoiceTTS(BASE_CFG)
        commands = []
        test = self

        def runner(args, **kwargs):
            commands.append(args)
            if "--job" in args:
                job = json.loads(Path(args[args.index("--job") + 1]).read_text())
                out = Path(args[args.index("--out") + 1])
                test.assertGreater(float(args[args.index("--deadline") + 1]), 0)
                test.write_clips(out / job["episodes"][0]["slug"], len(job["episodes"][0]["lines"]))
            return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "COSYVOICE_PYTHON": sys.executable}):
            cfg = copy.deepcopy(BASE_CFG)
            cfg["tts"]["cosyvoice_local"]["cache_dir"] = temporary
            engine = self.tts.LocalCosyVoiceTTS(cfg, runner=runner)
            results = engine.render_many({"es": self.utterances, "ko": self.utterances[:1]})
        self.assertEqual(list(results), ["es"])
        self.assertIn("--setup", commands[1])
        self.assertIn("--torch-index", commands[1])


class RenderLoopTests(EngineTestCase):
    def test_batch_engines_get_all_pending_episodes_and_others_one_by_one(self):
        render = load_script("render_language_episodes")

        class Batch:
            name, model_in_use, batch = "kaggle", "x", True

            def __init__(self):
                self.calls = []

            def render_many(self, pending):
                self.calls.append(sorted(pending))
                return {"es": ["clips"]}

        class Single:
            name, model_in_use = "fish", "x"

            def render(self, utterances):
                if utterances == "bad":
                    raise render.TTSError("quota")
                return ["clip"]

        batch = Batch()
        self.assertEqual(render.engine_clips(batch, {"es": [], "de": []}), {"es": ["clips"]})
        self.assertEqual(batch.calls, [["de", "es"]])
        self.assertEqual(render.engine_clips(Single(), {"es": "ok", "de": "bad"}), {"es": ["clip"]})

    def test_bundle_editions_have_their_own_audio_window(self):
        render = load_script("render_language_episodes")
        self.assertEqual(render.audio_window(BASE_CFG, {"source": "bundle"}), (300.0, 1080.0))
        self.assertEqual(render.audio_window(BASE_CFG, {}), (540.0, 1080.0))


class QualityGateTests(unittest.TestCase):
    def test_editions_pass_or_fail_on_their_own_and_only_passes_are_published(self):
        try:
            qa = load_script("qa_language_episodes")
            publish = load_script("publish_multilang_site")
        except ImportError as exc:
            self.skipTest(f"audio dependencies unavailable: {exc}")
        bundle = build.load_bundle(PROJECT_DIR / "incoming" / "2026-10-08.json", "2026-10-08")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            out, media, qa_dir, docs = root / "out", root / "media", root / "qa", root / "docs"
            out.mkdir()
            media.mkdir()
            items = []
            for slug in ("es", "de"):
                episode = build.bundle_episode(bundle[slug], "2026-10-08", LANGS[slug], BASE_CFG)
                (out / f"{slug}.json").write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")
                items.append({"slug": slug, "language": episode["language"], "japanese_name": slug, "json": f"{slug}.json", "markdown": f"{slug}.md"})
                (media / f"{slug}.mp3").write_bytes(b"")
            (out / "manifest.json").write_text(json.dumps({"episode_date": "2026-10-08", "stories": [], "episodes": items}), encoding="utf-8")
            (media / "media-manifest.json").write_text(json.dumps({"episodes": [
                {"slug": s, "audio": f"{s}.mp3", "duration_seconds": 600, "bytes": 1, "tts": "fish:s2.1-pro-free"} for s in ("es", "de")
            ]}), encoding="utf-8")

            def check(slug, audio, episode, whisper, cfg):
                return ({"transcript_ratio": 0.9}, [] if slug == "es" else ["long silence(s): [4.1]"], "text")

            argv = ["qa", "--episode-dir", str(out), "--media-dir", str(media), "--output-dir", str(qa_dir)]
            with mock.patch.object(qa, "WhisperModel"), mock.patch.object(qa, "check_episode", side_effect=check), \
                    mock.patch.object(sys, "argv", argv):
                self.assertEqual(qa.main(), 0)
            report = json.loads((qa_dir / "qa-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "PARTIAL")

            docs.mkdir()
            (docs / "episodes.json").write_text(json.dumps([{"date": "2026-10-09", "stories": [], "episodes": []}]), encoding="utf-8")
            argv = ["publish", "--date", "2026-10-08", "--episode-dir", str(out), "--media-dir", str(media),
                    "--qa-report", str(qa_dir / "qa-report.json"), "--docs-dir", str(docs), "--email", "a@example.test"]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(publish.main(), 0)
            history = json.loads((docs / "episodes.json").read_text(encoding="utf-8"))
            health = json.loads((docs / "health.json").read_text(encoding="utf-8"))
            feed = (docs / "feed.xml").read_text(encoding="utf-8")
        self.assertEqual([d["date"] for d in history], ["2026-10-09", "2026-10-08"], "a late day is filed by date")
        self.assertEqual([e["slug"] for e in history[1]["episodes"]], ["es"])
        self.assertEqual(health["engines"], {"es": "fish:s2.1-pro-free"})
        self.assertIn("a@example.test", feed)


if __name__ == "__main__":
    unittest.main()
