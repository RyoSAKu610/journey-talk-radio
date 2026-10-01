"""Gemini text-to-speech for Journey Talk.

Each utterance is synthesised on its own (single-speaker request) so the episode keeps an exact
per-line timeline for the synced transcript. Each host keeps one multilingual Gemini voice across
Japanese and the target language, and the direction prefix tells the model the language and pace.

Gemini returns raw 16-bit PCM (audio/L16, 24 kHz mono) as base64 in
candidates[0].content.parts[*].inlineData. It does not return word timings, so karaoke timings are
estimated inside each line (see estimate_word_timings).
"""
from __future__ import annotations

import base64
import os
import re
import time

import requests
from pydub import AudioSegment
from pydub.silence import detect_leading_silence

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRYABLE = {429, 500, 502, 503, 504}

# Spoken-language names used in the direction prefix. Naming the language matters for short lines
# that are ambiguous on their own, e.g. Chinese written only in hanzi that could be read as Japanese.
LANGUAGE_NAMES = {
    "ja-JP": "Japanese",
    "de-DE": "German",
    "es-ES": "Spanish (Spain)",
    "ru-RU": "Russian",
    "zh-CN": "Mandarin Chinese",
    "ko-KR": "Korean",
    "en-US": "American English",
}
CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]")


class GeminiTTSError(RuntimeError):
    pass


class GeminiTTS:
    def __init__(self, cfg: dict, api_key: str | None = None, session=None, sleep=time.sleep):
        settings = cfg["tts"]["gemini"]
        override = os.getenv("GEMINI_TTS_MODEL", "").strip()
        self.models = [m for m in [override, *settings["models"]] if m]
        self.voices = settings["voices"]
        self.normal_direction = settings["normal_direction"]
        self.slow_direction = settings["slow_direction"]
        self.max_attempts = int(settings.get("max_attempts", 6))
        self.min_interval = float(settings.get("min_interval_seconds", 0))
        self.api_key = (api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")).strip()
        if not self.api_key:
            raise GeminiTTSError("GEMINI_API_KEY is not configured")
        self.session = session or requests.Session()
        self.sleep = sleep
        self._last_call = 0.0
        self.model_in_use: str | None = None
        self.disabled = ""  # set when the key itself is rejected; no point retrying for later episodes

    def prompt(self, text: str, language: str, slow: bool) -> str:
        direction = self.slow_direction if slow else self.normal_direction
        name = LANGUAGE_NAMES.get(language, language)
        return f"{direction.format(language=name)}: {text}"

    def body(self, text: str, speaker: str, language: str, slow: bool) -> dict:
        return {
            "contents": [{"parts": [{"text": self.prompt(text, language, slow)}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voices[speaker]}}},
            },
        }

    def synthesize(self, text: str, speaker: str, language: str, slow: bool = False) -> AudioSegment:
        if self.disabled:
            raise GeminiTTSError(self.disabled)
        payload = self.body(text, speaker, language, slow)
        # Once a model has worked, stay on it; otherwise walk the list (newest first).
        candidates = [self.model_in_use] if self.model_in_use else list(self.models)
        last_error = ""
        for model in candidates:
            try:
                audio = self._request(model, payload)
            except _ModelUnavailable as exc:
                last_error = str(exc)
                print(f"[tts] {model} unavailable ({exc}); trying the next model")
                continue
            self.model_in_use = model
            return trim_silence(audio)
        raise GeminiTTSError(f"no Gemini TTS model accepted the request: {last_error}")

    def _request(self, model: str, payload: dict) -> AudioSegment:
        for attempt in range(1, self.max_attempts + 1):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                self.sleep(wait)
            self._last_call = time.monotonic()
            try:
                response = self.session.post(
                    ENDPOINT.format(model=model), params={"key": self.api_key}, json=payload, timeout=(20, 180)
                )
            except requests.RequestException as exc:
                if attempt == self.max_attempts:
                    raise GeminiTTSError(f"Gemini TTS request failed: {exc}") from exc
                self.sleep(min(60, 5 * attempt))
                continue
            if response.status_code == 404 or (
                response.status_code == 400 and re.search(r"not (found|supported)|unknown model", response.text, re.I)
            ):
                raise _ModelUnavailable(f"HTTP {response.status_code}")
            if response.status_code in RETRYABLE and attempt < self.max_attempts:
                delay = retry_delay(response) or min(60, 5 * 2 ** (attempt - 1))
                print(f"[tts] HTTP {response.status_code}; retrying in {delay:.0f}s")
                self.sleep(delay)
                continue
            if response.status_code in (401, 403) or (response.status_code == 400 and "API_KEY" in response.text):
                self.disabled = f"Gemini rejected the API key (HTTP {response.status_code})"
                raise GeminiTTSError(self.disabled)
            if response.status_code >= 400:
                raise GeminiTTSError(f"Gemini TTS HTTP {response.status_code}: {response.text[:500]}")
            audio = decode_audio(response.json())
            if audio is not None:
                return audio
            # A response without audio (e.g. the model answered in text) is retried.
            print(f"[tts] {model} returned no audio; retrying")
        raise GeminiTTSError(f"{model}: no audio after {self.max_attempts} attempts")


class _ModelUnavailable(Exception):
    pass


def retry_delay(response) -> float | None:
    """Seconds from Retry-After or the google.rpc.RetryInfo detail ("retryDelay": "31s")."""
    header = response.headers.get("Retry-After") if hasattr(response, "headers") else None
    if header and header.strip().isdigit():
        return float(header)
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', getattr(response, "text", "") or "")
    return float(match.group(1)) if match else None


def decode_audio(body: dict) -> AudioSegment | None:
    for candidate in body.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if not inline or not inline.get("data"):
                continue
            mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
            data = base64.b64decode(inline["data"])
            if "wav" in mime:
                import io

                return AudioSegment.from_file(io.BytesIO(data), format="wav")
            rate = re.search(r"rate=(\d+)", mime)
            return AudioSegment(data=data, sample_width=2, frame_rate=int(rate.group(1)) if rate else 24000, channels=1)
    return None


def trim_silence(audio: AudioSegment, keep_ms: int = 60, threshold_db: float = -45.0) -> AudioSegment:
    """Cut leading/trailing silence so the episode's own pauses set the pace."""
    if len(audio) == 0:
        return audio
    head = detect_leading_silence(audio, silence_threshold=threshold_db, chunk_size=10)
    tail = detect_leading_silence(audio.reverse(), silence_threshold=threshold_db, chunk_size=10)
    if head + tail >= len(audio):
        return audio
    return audio[max(0, head - keep_ms): len(audio) - max(0, tail - keep_ms)]


def speech_tokens(text: str) -> list[str]:
    """Units to highlight: words for spaced scripts, single characters for Chinese/Japanese."""
    return re.findall(r"[぀-ヿ㐀-鿿豈-﫿]|[^\W぀-ヿ㐀-鿿豈-﫿]+", text)


def estimate_word_timings(text: str, duration_s: float) -> list[list]:
    """Spread a line's duration over its words in proportion to their length.

    Gemini gives no word boundaries; within a single short line this tracks speech closely enough
    for the karaoke highlight, while the line boundaries themselves stay exact.
    """
    tokens = speech_tokens(text)
    if not tokens or duration_s <= 0:
        return []
    # Count each CJK character as about two Latin letters' worth of speaking time.
    weights = [2.0 if CJK.match(t) else len(t) + 1.0 for t in tokens]
    total = sum(weights)
    out: list[list] = []
    cursor = 0.0
    for token, weight in zip(tokens, weights):
        span = duration_s * weight / total
        out.append([round(cursor, 3), round(cursor + span * 0.92, 3), token])
        cursor += span
    return out
