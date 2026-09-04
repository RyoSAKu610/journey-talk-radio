from __future__ import annotations

import copy
import json
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

# The real project installs feedparser from requirements.txt.  Keep the offline
# regression suite runnable in restricted CI/sandboxes where that one optional
# import is unavailable; network-fetch behavior is mocked in these tests.
try:
    import feedparser as _feedparser  # noqa: F401
except ModuleNotFoundError:
    sys.modules["feedparser"] = types.SimpleNamespace(
        parse=lambda *_args, **_kwargs: types.SimpleNamespace(entries=[])
    )

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import safe_pipeline as sp  # noqa: E402


class SafePipelineRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = yaml.safe_load((PROJECT_DIR / "config.yaml").read_text(encoding="utf-8"))
        cls.catalog = sp.load_catalog(PROJECT_DIR / "safe_catalog.json")
        cls.episode_date = date(2026, 8, 15)
        cls.generated_at = datetime(2026, 8, 15, 18, 0, tzinfo=timezone(timedelta(hours=9)))
        cls.articles = [
            sp.SafeArticle(
                "S01",
                "NHK NEWS WEB",
                "ja-JP",
                "政府は新しい防災計画を発表しました",
                "https://example.test/nhk",
                "2026-08-15",
                "政府は地域の避難訓練と情報共有を強化する新しい防災計画を発表しました。",
            ),
            sp.SafeArticle(
                "S02",
                "BBC News World",
                "en-US",
                "Global leaders discuss climate policy",
                "https://example.test/bbc",
                "2026-08-15",
                "Global leaders met to discuss climate policy and future cooperation across several regions.",
            ),
            sp.SafeArticle(
                "S03",
                "Reuters via Google News",
                "en-US",
                "Markets react to updated economic outlook",
                "https://example.test/reuters",
                "2026-08-15",
                "Financial markets reacted after officials released an updated economic outlook for the coming period.",
            ),
        ]

    def _episode(self, *, run_id: str = "0123456789abcdef0123456789abcdef", generated_at=None):
        digest = sp.source_digest(self.articles)
        contract = sp.contract_hash(self.catalog)
        plan = sp.fallback_plan(self.episode_date, digest, contract, self.catalog, "fallback")
        return sp.build_episode(
            self.config,
            self.articles,
            plan,
            sp.selector_runtime(self.config),
            self.episode_date,
            generated_at or self.generated_at,
            self.catalog,
            run_id=run_id,
        )

    def test_catalog_is_exact_reviewed_catalog(self):
        self.assertEqual(
            self.catalog.sha256,
            "ced2da2c79186cbf202fa761bf9900d2380a98289b2df734d8ae5a5a4842abbb",
        )

    def test_config_matches_safe_contract(self):
        sp.validate_safe_config(self.config)

    def test_url_detection_catches_scheme_joined_to_japanese(self):
        for value in (
            "日本語http:x",
            "日本語mailto:a",
            "日本語data:text/plain,x",
            "日本語tel:123",
            "日本語javascript:alert(1)",
        ):
            with self.subTest(value=value):
                self.assertTrue(sp._has_url(value))
        self.assertFalse(sp._has_url("これは普通の学習文です"))
        self.assertFalse(sp._has_url("Topic: ordinary explanation"))

    def test_source_script_rejects_cross_script_spoofing(self):
        with self.assertRaises(sp.SafePipelineError):
            sp._validate_source_text_script("en-US", "English sentence にほんご", quote=True)
        with self.assertRaises(sp.SafePipelineError):
            sp._validate_source_text_script("ja-JP", "A mostly English sentence with かな", quote=True)

    def test_fallback_plan_is_deterministic(self):
        digest = sp.source_digest(self.articles)
        contract = sp.contract_hash(self.catalog)
        first = sp.fallback_plan(self.episode_date, digest, contract, self.catalog)
        second = sp.fallback_plan(self.episode_date, digest, contract, self.catalog)
        self.assertEqual(first, second)

    def test_build_episode_has_110_slots_and_valid_duration(self):
        episode = self._episode()
        sp.validate_episode(episode, self.config, self.catalog)
        self.assertEqual(len(episode["utterances"]), 110)
        duration = sp.estimate_duration_seconds(episode["utterances"])
        self.assertGreaterEqual(duration, sp.EPISODE_MINIMUM_SECONDS)
        self.assertLessEqual(duration, sp.EPISODE_MAXIMUM_SECONDS)

    def test_speaker_schedule_tamper_is_rejected(self):
        episode = self._episode()
        episode["utterances"][1]["speaker"] = episode["utterances"][0]["speaker"]
        episode["episode_sha256"] = sp.episode_hash(episode)
        with self.assertRaises(sp.SafePipelineError):
            sp.validate_episode(episode, self.config, self.catalog)

    def test_catalog_text_tamper_is_rejected(self):
        episode = self._episode()
        target = next(item for item in episode["utterances"] if item["content_kind"] == "catalog")
        target["text"] += " 改変"
        episode["episode_sha256"] = sp.episode_hash(episode)
        with self.assertRaises(sp.SafePipelineError):
            sp.validate_episode(episode, self.config, self.catalog)

    def test_quote_tamper_is_rejected(self):
        episode = self._episode()
        episode["source_quotes"][0]["text"] += "改変"
        episode["episode_sha256"] = sp.episode_hash(episode)
        with self.assertRaises(sp.SafePipelineError):
            sp.validate_episode(episode, self.config, self.catalog)

    def test_duplicate_json_keys_and_nan_are_rejected(self):
        with self.assertRaises(ValueError):
            sp.strict_json_loads(b'{"a":1,"a":2}', 1024)
        with self.assertRaises(ValueError):
            sp.strict_json_loads(b'{"a":NaN}', 1024)

    def test_pause_packer_rejects_impossible_duration(self):
        with self.assertRaises(sp.SafePipelineError):
            sp.pack_pause_options(1000.0, [(0,)], minimum_seconds=600, maximum_seconds=900)

    def test_publish_and_done_roundtrip(self):
        episode = self._episode()
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "output"
            published = sp.publish_episode(episode, self.config, out, self.catalog)
            verified = sp.verify_done_or_none(self.config, self.episode_date, out, self.catalog)
            self.assertIsNotNone(verified)
            self.assertEqual(published.done, verified.done)
            self.assertTrue(published.fixed_json.is_file())
            self.assertTrue(published.fixed_markdown.is_file())

    def test_fixed_copy_is_repaired_from_committed_run(self):
        episode = self._episode()
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "output"
            published = sp.publish_episode(episode, self.config, out, self.catalog)
            published.fixed_markdown.write_text("broken", encoding="utf-8")
            sp.verify_done_or_none(self.config, self.episode_date, out, self.catalog)
            self.assertEqual(
                published.fixed_markdown.read_bytes(),
                published.run_markdown.read_bytes(),
            )

    def test_done_tamper_is_rejected(self):
        episode = self._episode()
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "output"
            published = sp.publish_episode(episode, self.config, out, self.catalog)
            done = json.loads(published.done.read_text(encoding="utf-8"))
            done["json_sha256"] = "0" * 64
            published.done.write_text(
                json.dumps(done, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.assertRaises(sp.SafePipelineError):
                sp.verify_done_or_none(self.config, self.episode_date, out, self.catalog)

    def test_immutable_run_id_cannot_be_reused_for_different_bytes(self):
        run_id = "fedcba9876543210fedcba9876543210"
        first = self._episode(run_id=run_id)
        second = self._episode(run_id=run_id, generated_at=self.generated_at + timedelta(seconds=1))
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "output"
            sp.publish_episode(first, self.config, out, self.catalog)
            with self.assertRaises(sp.SafePipelineError):
                sp.publish_episode(second, self.config, out, self.catalog)

    def test_draft_artifact_cannot_be_published(self):
        episode = self._episode()
        episode["draft"] = True
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(sp.SafePipelineError):
                sp.publish_episode(episode, self.config, Path(temp), self.catalog)

    def test_source_manifest_roundtrip_is_network_free(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            sp.save_source_manifest(work, self.episode_date, self.config, self.catalog, self.articles)
            loaded, status = sp.load_source_manifest_or_none(
                work, self.episode_date, self.config, self.catalog
            )
            self.assertEqual(status, "valid")
            self.assertEqual(loaded, self.articles)

    def test_safe_config_rejects_language_order_change(self):
        changed = copy.deepcopy(self.config)
        changed["languages"][0], changed["languages"][1] = changed["languages"][1], changed["languages"][0]
        with self.assertRaises(sp.SafePipelineError):
            sp.validate_safe_config(changed)


if __name__ == "__main__":
    unittest.main()
