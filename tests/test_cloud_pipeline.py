from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = PROJECT_DIR / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CloudPipelineTests(unittest.TestCase):
    def test_voice_map_covers_both_hosts_and_all_languages(self):
        media = load_script("render_media")
        languages = ("ja-JP", "en-US", "de-DE", "es-ES", "ru-RU", "zh-CN", "ko-KR")
        self.assertEqual(
            set(media.VOICE_MAP),
            {(language, host) for language in languages for host in ("MC_F", "MC_M")},
        )

    def test_feed_add_is_idempotent_and_parseable(self):
        feed = load_script("build_feed")
        episode = {"episode_date": "2026-09-03"}
        manifest = {"duration_seconds": 720.0, "audio_bytes": 1234}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "feed.xml"
            tree = feed.load_or_create(path, "https://example.test", "Journey Talk", "owner@example.test", "Description")
            for _ in range(2):
                feed.add_episode(tree, episode, manifest, "https://example.test/episode.mp3", 1234)
            tree.write(path, encoding="utf-8", xml_declaration=True)
            parsed = ET.parse(path)
            items = parsed.getroot().findall("./channel/item")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].findtext("guid"), "journey-talk:2026-09-03")
            self.assertEqual(items[0].find("enclosure").attrib["type"], "audio/mpeg")

    def test_weekly_status_reports_failures(self):
        status = load_script("weekly_status")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "runs.json").write_text(json.dumps([{"conclusion": "success"}, {"conclusion": "failure"}]))
            (root / "manifest.json").write_text(json.dumps({"episode_date": "2026-09-03", "duration_seconds": 720, "utterances": 110}))
            (root / "youtube.json").write_text(json.dumps({"video_id": "abc", "url": "https://youtu.be/abc"}))
            old_argv = __import__("sys").argv
            try:
                __import__("sys").argv = ["weekly_status", "--runs", str(root / "runs.json"), "--manifest", str(root / "manifest.json"), "--youtube-state", str(root / "youtube.json"), "--output", str(root / "status.md")]
                self.assertEqual(status.main(), 0)
            finally:
                __import__("sys").argv = old_argv
            text = (root / "status.md").read_text()
            self.assertIn("成功 1件 / 失敗 1件", text)
            self.assertIn("直近実行の失敗", text)
