from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from voice_director import VoiceDirector, normalize_spoken_text  # noqa: E402


class VoiceDirectorV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.director = VoiceDirector(ROOT / "voice_profiles.yaml")

    def test_voice_matrix_has_twelve_unique_voices(self) -> None:
        voices = self.director.verify_voice_matrix()
        self.assertEqual(12, len(voices))
        self.assertEqual(12, len(set(voices)))

    def test_mandarin_never_pitch_shifts(self) -> None:
        line = {
            "speaker": "MC_F",
            "language": "zh-CN",
            "intent": "reaction",
            "text": "对，这个角度很有意思。",
        }
        controls = self.director.controls(line, 4, 60, episode_seed="2026-10-07:zh")
        self.assertEqual("+0Hz", controls.pitch)

    def test_controls_are_reproducible(self) -> None:
        line = {
            "speaker": "MC_M",
            "language": "de-DE",
            "intent": "question",
            "text": "Wie würdest du das im Alltag sagen?",
        }
        first = self.director.controls(line, 8, 60, episode_seed="2026-10-07:de")
        second = self.director.controls(line, 8, 60, episode_seed="2026-10-07:de")
        self.assertEqual(first, second)

    def test_languages_have_different_timing_character(self) -> None:
        de = {
            "speaker": "MC_M",
            "language": "de-DE",
            "intent": "reaction",
            "text": "Genau, das klingt im Alltag viel natürlicher.",
        }
        es = {
            "speaker": "MC_M",
            "language": "es-ES",
            "intent": "reaction",
            "text": "Claro, así suena mucho más natural en una conversación.",
        }
        de_controls = self.director.controls(de, 12, 60, episode_seed="same")
        es_controls = self.director.controls(es, 12, 60, episode_seed="same")
        self.assertNotEqual(de_controls.rate, es_controls.rate)
        self.assertNotEqual(de_controls.pause_after_ms, es_controls.pause_after_ms)

    def test_language_switch_adds_pause(self) -> None:
        current = {
            "speaker": "MC_F",
            "language": "ko-KR",
            "intent": "teaching",
            "text": "이 표현은 일상 대화에서 정말 자주 써요.",
        }
        same_language_previous = {
            "speaker": "MC_M",
            "language": "ko-KR",
            "intent": "conversation",
            "text": "맞아요.",
        }
        japanese_previous = {
            "speaker": "MC_M",
            "language": "ja-JP",
            "intent": "explanation",
            "text": "ここで意味を確認しましょう。",
        }
        same = self.director.controls(current, 20, 60, previous=same_language_previous, episode_seed="ko")
        switched = self.director.controls(current, 20, 60, previous=japanese_previous, episode_seed="ko")
        self.assertGreater(switched.pause_after_ms, same.pause_after_ms)

    def test_spoken_text_sanitizer_removes_markup_and_urls(self) -> None:
        value = normalize_spoken_text("**今日** は https://example.com を見ます。")
        self.assertNotIn("**", value)
        self.assertNotIn("http", value)
        self.assertIn("今日", value)

    def test_episode_estimate_is_positive_and_includes_pauses(self) -> None:
        lines = [
            {"speaker": "MC_F", "language": "es-ES", "intent": "question", "text": "¿Qué te parece esta noticia?"},
            {"speaker": "MC_M", "language": "es-ES", "intent": "conversation", "text": "Me parece interesante porque conecta con la vida diaria."},
            {"speaker": "MC_F", "language": "ja-JP", "intent": "teaching", "text": "今の表現は日常会話でも使えます。"},
        ]
        total = self.director.estimate_episode_seconds(lines, seed="test")
        speech_only = sum(self.director.estimate_utterance_seconds(line, i, len(lines)) for i, line in enumerate(lines))
        self.assertGreater(total, speech_only)


if __name__ == "__main__":
    unittest.main()
