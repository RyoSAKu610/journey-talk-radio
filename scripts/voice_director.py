from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT / "voice_profiles.yaml"
ALLOWED_INTENTS = {
    "intro",
    "story_intro",
    "conversation",
    "question",
    "reaction",
    "explanation",
    "teaching",
    "example",
    "review",
    "review_slow",
    "trivia",
    "closing",
}


@dataclass(frozen=True)
class SpeechControls:
    voice: str
    rate: str
    volume: str
    pitch: str
    pause_after_ms: int
    intent: str
    tts_text: str


def _signed_percent(value: int) -> str:
    return f"{value:+d}%"


def _signed_hz(value: int) -> str:
    return f"{value:+d}Hz"


def normalize_spoken_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"[`*_#]+", "", text)
    text = re.sub(r"https?://\S+|www\.\S+", "", text, flags=re.I)
    text = text.replace("…", "...")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def infer_intent(text: str, language: str, index: int, total: int) -> str:
    value = normalize_spoken_text(text)
    lower = value.casefold()
    if index <= 1:
        return "intro"
    if index >= max(0, total - 2):
        return "closing"
    if value.endswith(("?", "？")):
        return "question"
    if any(token in lower for token in ("復習", "review", "wiederholen", "repaso", "повтор", "复习", "복습")):
        return "review"
    if any(token in lower for token in ("豆知識", "trivia", "culture tip", "kultur", "curiosidad", "факт", "小知识", "상식")):
        return "trivia"
    if language == "ja-JP" and any(token in value for token in ("ポイント", "表現", "意味", "使い方", "発音")):
        return "teaching"
    if len(value) <= 28:
        return "reaction"
    return "conversation"


class VoiceDirector:
    def __init__(self, profile_path: Path | str = DEFAULT_PROFILE):
        self.profile_path = Path(profile_path)
        self.config: dict[str, Any] = yaml.safe_load(self.profile_path.read_text(encoding="utf-8"))
        self.version = int(self.config.get("version", 0))
        self.languages: dict[str, dict[str, Any]] = self.config["languages"]
        self.intents: dict[str, dict[str, Any]] = self.config["intents"]
        self.hosts: dict[str, dict[str, Any]] = self.config["hosts"]

    def language(self, code: str) -> dict[str, Any]:
        if code not in self.languages:
            raise ValueError(f"Unsupported language: {code}")
        return self.languages[code]

    def intent(self, utterance: dict[str, Any], index: int, total: int) -> str:
        explicit = str(utterance.get("intent", "")).strip()
        if explicit:
            if explicit not in ALLOWED_INTENTS:
                raise ValueError(f"Unsupported intent {explicit!r} at utterance {index}")
            return explicit
        return infer_intent(str(utterance.get("text", "")), str(utterance.get("language", "")), index, total)

    def controls(
        self,
        utterance: dict[str, Any],
        index: int,
        total: int,
        *,
        previous: dict[str, Any] | None = None,
        episode_seed: str = "",
    ) -> SpeechControls:
        language_code = str(utterance["language"])
        speaker = str(utterance["speaker"])
        if speaker not in self.hosts:
            raise ValueError(f"Unsupported speaker: {speaker}")
        profile = self.language(language_code)
        intent = self.intent(utterance, index, total)
        text = normalize_spoken_text(str(utterance["text"]))
        if not text:
            raise ValueError(f"Empty TTS text at utterance {index}")

        base_rate = int(profile.get("base_rate_percent", 0))
        host_delta = int(self.hosts[speaker].get("rate_delta_percent", 0))
        intent_delta = int(profile.get("intent_rate_delta", {}).get(intent, 0))
        rate_value = max(-18, min(12, base_rate + host_delta + intent_delta))

        pitch_value = int(profile.get("pitch_hz", 0))
        if profile.get("pitch_adjustment_allowed") is False:
            pitch_value = 0
        volume_value = int(profile.get("volume_percent", 0))

        base_pause = int(self.intents.get(intent, {}).get("pause_ms", 420))
        pause = round(base_pause * float(profile.get("pause_multiplier", 1.0)))
        if previous is not None:
            if str(previous.get("speaker")) != speaker:
                pause += int(profile.get("speaker_switch_bonus_ms", 0))
            if str(previous.get("language")) != language_code:
                pause += int(profile.get("language_switch_bonus_ms", 0))

        # Deterministic micro-variation avoids metronomic timing without making reruns nondeterministic.
        digest = hashlib.sha256(f"{episode_seed}|{index}|{language_code}|{speaker}|{intent}".encode()).digest()
        jitter = int(digest[0] % 81) - 40
        pause = max(180, min(1300, pause + jitter))

        voice = profile["voice_f"] if speaker == "MC_F" else profile["voice_m"]
        return SpeechControls(
            voice=voice,
            rate=_signed_percent(rate_value),
            volume=_signed_percent(volume_value),
            pitch=_signed_hz(pitch_value),
            pause_after_ms=pause,
            intent=intent,
            tts_text=text,
        )

    def estimate_utterance_seconds(self, utterance: dict[str, Any], index: int, total: int) -> float:
        language_code = str(utterance["language"])
        profile = self.language(language_code)
        intent = self.intent(utterance, index, total)
        text = normalize_spoken_text(str(utterance["text"]))
        if not text:
            return 0.0
        if "words_per_second" in profile:
            units = max(1, len(re.findall(r"\b[^\W_]+\b", text, flags=re.UNICODE)))
            seconds = units / float(profile["words_per_second"])
        else:
            units = len(re.sub(r"\s+", "", text))
            seconds = units / float(profile.get("chars_per_second", 4.0))
        rate = int(profile.get("base_rate_percent", 0)) + int(profile.get("intent_rate_delta", {}).get(intent, 0))
        speed_factor = max(0.82, min(1.12, 1.0 + rate / 100.0))
        return seconds / speed_factor

    def estimate_episode_seconds(self, utterances: list[dict[str, Any]], seed: str = "") -> float:
        total_seconds = 0.0
        previous: dict[str, Any] | None = None
        count = len(utterances)
        for index, utterance in enumerate(utterances):
            total_seconds += self.estimate_utterance_seconds(utterance, index, count)
            controls = self.controls(utterance, index, count, previous=previous, episode_seed=seed)
            total_seconds += controls.pause_after_ms / 1000.0
            previous = utterance
        return total_seconds

    def verify_voice_matrix(self) -> list[str]:
        voices: list[str] = []
        for code, profile in self.languages.items():
            for key in ("voice_f", "voice_m"):
                value = str(profile.get(key, "")).strip()
                if not value:
                    raise ValueError(f"Missing {key} for {code}")
                voices.append(value)
        if len(set(voices)) != len(voices):
            raise ValueError("Voice profile contains duplicate voice IDs")
        return voices
