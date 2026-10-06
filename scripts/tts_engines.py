"""Speech engines for Journey Talk, tried in the order set by cloud_languages.yaml (tts.order).

1. Gemini TTS: the whole episode in ONE multi-speaker request (the free tier allows about ten
   requests per model per day), then cut into lines. Each line is sent as its own part with a
   "[long pause]" tag, which makes the hand-over between lines the longest pauses in the take;
   align_lines() then picks one pause per boundary. Verified on a 24-line take: every clip matched
   its line.
2. CosyVoice 3 (open model by Alibaba) on Modal's free monthly GPU credit, with each host's voice cloned
   from a short Gemini-voiced reference clip; one remote call per episode, exact line timing.
3. Google Cloud TTS, Chirp 3: HD voices: one request per line (exact timing) inside the 1M
   characters a month free tier, guarded by a monthly budget kept in the repository.
4. OpenAI GPT TTS (paid): one request per line, only when OPENAI_API_KEY is set.
5. Edge TTS lives in render_language_episodes.py and is the last resort.

Gemini and OpenAI return no word timings, so karaoke timings are estimated inside each line.
Shadowing ("slow") lines are time-stretched after synthesis so every engine slows them the same way.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import requests
from pydub import AudioSegment
from pydub.silence import detect_leading_silence, detect_silence

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI_ENDPOINT = "https://api.openai.com/v1/audio/speech"
RETRYABLE = {429, 500, 502, 503, 504}

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


class TTSError(RuntimeError):
    pass


class _ModelUnavailable(Exception):
    pass


# --------------------------------------------------------------------------- Gemini


class GeminiEpisodeTTS:
    name = "gemini"

    def __init__(self, cfg: dict, api_key: str | None = None, session=None, sleep=time.sleep):
        settings = cfg["tts"]["gemini"]
        override = os.getenv("GEMINI_TTS_MODEL", "").strip()
        self.models = [m for m in [override, *settings["models"]] if m]
        self.voices = settings["voices"]
        self.labels = settings["speaker_labels"]
        self.tag = settings.get("line_break_tag", "[long pause]")
        self.max_attempts = int(settings.get("max_attempts", 4))
        self.api_key = (api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")).strip()
        if not self.api_key:
            raise TTSError("GEMINI_API_KEY is not configured")
        self.session = session or requests.Session()
        self.sleep = sleep
        self.model_in_use: str | None = None
        self.disabled = ""  # set once the key itself is rejected; later episodes skip straight on

    def body(self, utterances: list[dict]) -> dict:
        parts = [
            {"text": f"{u['text']} {self.tag}".strip(), "speechMetadata": {"speaker": self.labels[u["speaker"]]}}
            for u in utterances
        ]
        speakers = [
            {"speaker": self.labels[host], "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
            for host, voice in self.voices.items()
        ]
        return {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": speakers}},
            },
        }

    def render(self, utterances: list[dict]) -> list[AudioSegment]:
        """One request for the whole episode, returned as one AudioSegment per utterance."""
        if self.disabled:
            raise TTSError(self.disabled)
        payload = self.body(utterances)
        expected = [expected_seconds(u["text"], u["language"]) for u in utterances]
        problems: list[str] = []
        for model in list(self.models):
            try:
                take = self._request(model, payload)
            except _ModelUnavailable as exc:
                problems.append(f"{model}: {exc}")
                print(f"[tts] {model} unavailable ({exc}); trying the next model")
                continue
            try:
                spans = align_lines(take, expected)
            except ValueError as exc:
                # The take does not match the script (skipped or merged lines); another model may do better.
                problems.append(f"{model}: {exc}")
                print(f"[tts] {model} take could not be split into lines ({exc})")
                continue
            self.model_in_use = model
            return [trim_silence(take[a:b]) for a, b in spans]
        raise TTSError("Gemini TTS: " + "; ".join(problems[-4:]))

    def _request(self, model: str, payload: dict) -> AudioSegment:
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.post(
                    GEMINI_ENDPOINT.format(model=model), params={"key": self.api_key}, json=payload, timeout=(20, 600)
                )
            except requests.RequestException as exc:
                if attempt == self.max_attempts:
                    raise _ModelUnavailable(f"request failed: {exc}") from exc
                self.sleep(min(60, 10 * attempt))
                continue
            status, text = response.status_code, response.text
            if status == 404 or (status == 400 and re.search(r"not (found|supported)|unknown model", text, re.I)):
                raise _ModelUnavailable(f"HTTP {status}")
            if status in (401, 403) or (status == 400 and "API_KEY" in text):
                self.disabled = f"Gemini rejected the API key (HTTP {status})"
                raise TTSError(self.disabled)
            if status == 429 and "PerDay" in text:
                raise _ModelUnavailable("daily quota exhausted")  # waiting will not help today
            if status in RETRYABLE:
                if attempt == self.max_attempts:
                    raise _ModelUnavailable(f"HTTP {status}")
                delay = retry_delay(response) or min(60, 10 * 2 ** (attempt - 1))
                print(f"[tts] {model} HTTP {status}; retrying in {delay:.0f}s")
                self.sleep(delay)
                continue
            if status >= 400:
                raise TTSError(f"Gemini TTS HTTP {status}: {text[:500]}")
            audio = decode_gemini_audio(response.json())
            if audio is not None:
                return audio
            print(f"[tts] {model} returned no audio; retrying")
        raise _ModelUnavailable(f"no audio after {self.max_attempts} attempts")


def decode_gemini_audio(body: dict) -> AudioSegment | None:
    for candidate in body.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if not inline or not inline.get("data"):
                continue
            mime = str(inline.get("mimeType") or inline.get("mime_type") or "")
            data = base64.b64decode(inline["data"])
            if "wav" in mime:
                return AudioSegment.from_file(io.BytesIO(data), format="wav")
            rate = re.search(r"rate=(\d+)", mime)
            return AudioSegment(data=data, sample_width=2, frame_rate=int(rate.group(1)) if rate else 24000, channels=1)
    return None


# --------------------------------------------------------------------------- OpenAI


class OpenAITTS:
    name = "openai"

    def __init__(self, cfg: dict, api_key: str | None = None, session=None, sleep=time.sleep):
        settings = cfg["tts"]["openai"]
        override = os.getenv("OPENAI_TTS_MODEL", "").strip()
        self.models = [m for m in [override, *settings["models"]] if m]
        self.voices = settings["voices"]
        self.instructions = settings["instructions"]
        self.max_attempts = int(settings.get("max_attempts", 5))
        self.api_key = (api_key if api_key is not None else os.getenv("OPENAI_API_KEY", "")).strip()
        if not self.api_key:
            raise TTSError("OPENAI_API_KEY is not configured")
        self.session = session or requests.Session()
        self.sleep = sleep
        self.model_in_use: str | None = None

    def body(self, model: str, utterance: dict) -> dict:
        body = {
            "model": model,
            "voice": self.voices[utterance["speaker"]],
            "input": utterance["text"],
            "response_format": "pcm",  # 24 kHz, 16-bit, mono, little-endian
        }
        if "tts-1" not in model:  # the older tts-1 models do not take instructions
            body["instructions"] = self.instructions.format(language=LANGUAGE_NAMES.get(utterance["language"], utterance["language"]))
        return body

    def render(self, utterances: list[dict]) -> list[AudioSegment]:
        return [trim_silence(self.line(u)) for u in utterances]

    def line(self, utterance: dict) -> AudioSegment:
        models = [self.model_in_use] if self.model_in_use else list(self.models)
        problems: list[str] = []
        for model in models:
            try:
                audio = self._request(self.body(model, utterance))
            except _ModelUnavailable as exc:
                problems.append(f"{model}: {exc}")
                continue
            self.model_in_use = model
            return audio
        raise TTSError("OpenAI TTS: " + "; ".join(problems))

    def _request(self, payload: dict) -> AudioSegment:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.post(OPENAI_ENDPOINT, headers=headers, json=payload, timeout=(20, 180))
            except requests.RequestException as exc:
                if attempt == self.max_attempts:
                    raise TTSError(f"OpenAI TTS request failed: {exc}") from exc
                self.sleep(min(60, 5 * attempt))
                continue
            status = response.status_code
            if status in (401, 403):
                raise TTSError(f"OpenAI rejected the API key (HTTP {status})")
            if status == 404 or (status == 400 and "model" in response.text.lower()):
                raise _ModelUnavailable(f"HTTP {status}")
            if status == 429 and "insufficient_quota" in response.text:
                raise TTSError("OpenAI account has no remaining quota")
            if status in RETRYABLE and attempt < self.max_attempts:
                self.sleep(retry_delay(response) or min(60, 5 * 2 ** (attempt - 1)))
                continue
            if status >= 400:
                raise TTSError(f"OpenAI TTS HTTP {status}: {response.text[:300]}")
            if not response.content:
                raise TTSError("OpenAI TTS returned no audio")
            return AudioSegment(data=response.content, sample_width=2, frame_rate=24000, channels=1)
        raise TTSError("OpenAI TTS: retries exhausted")


# --------------------------------------------------------------------------- CosyVoice on Modal


class CosyVoiceModalTTS:
    """CosyVoice 3 running on Modal (scripts/modal_cosyvoice.py), one remote call per episode.

    Each host's voice is cloned from assets/voices/<name>.wav (+ .txt transcript), recorded with the same
    Gemini voices, so the hosts sound the same whichever engine rendered the episode. Needs MODAL_TOKEN_ID and
    MODAL_TOKEN_SECRET; when Modal's free monthly credit is used up the call fails and the next engine runs.
    """

    name = "cosyvoice"

    def __init__(self, cfg: dict, client=None):
        settings = cfg["tts"]["cosyvoice"]
        if client is None:
            if not (os.getenv("MODAL_TOKEN_ID", "").strip() and os.getenv("MODAL_TOKEN_SECRET", "").strip()):
                raise TTSError("MODAL_TOKEN_ID / MODAL_TOKEN_SECRET are not configured")
            try:
                import modal
            except ImportError as exc:
                raise TTSError("the modal package is not installed") from exc
            client = modal.Cls.from_name(settings["app_name"], "CosyVoice")
        self.client = client
        root = Path(__file__).resolve().parents[1]
        self.voices = {}
        for speaker, voice in settings["voices"].items():
            base = root / voice["reference"]
            try:
                self.voices[speaker] = {
                    "wav": base.with_suffix(".wav").read_bytes(),
                    "text": base.with_suffix(".txt").read_text(encoding="utf-8").strip(),
                    "language": voice["language"],
                }
            except OSError as exc:
                raise TTSError(f"voice reference missing: {exc}") from exc
        self.model_in_use = "cosyvoice3"

    def render(self, utterances: list[dict]) -> list[AudioSegment]:
        lines = [{"speaker": u["speaker"], "language": u["language"], "text": u["text"]} for u in utterances]
        try:
            wavs = self.client().synthesize.remote(lines, self.voices)
        except Exception as exc:  # Modal raises its own types: auth, credit exhausted, deploy missing, CUDA errors
            raise TTSError(f"CosyVoice on Modal failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
        if len(wavs) != len(lines):
            raise TTSError(f"CosyVoice returned {len(wavs)} clips for {len(lines)} lines")
        return [trim_silence(AudioSegment.from_file(io.BytesIO(w), format="wav")) for w in wavs]


# --------------------------------------------------------------------------- Google Cloud


class GoogleCloudTTS:
    """Google Cloud Text-to-Speech, Chirp 3: HD voices (the same voice family as Gemini TTS).

    The free tier covers 1M Chirp 3: HD characters a month. Lines are synthesised one request each,
    so line timing is exact, and the pace is set natively (speakingRate) instead of time-stretching.
    A monthly character budget, stored in the repository, keeps usage inside the free tier: an episode
    that would cross it is left to the next engine.
    """

    name = "google_cloud"
    native_pace = True
    ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"
    LANGUAGE_CODES = {"zh-CN": "cmn-CN"}

    def __init__(self, cfg: dict, api_key: str | None = None, session=None, sleep=time.sleep,
                 usage_path: Path | None = None, month: str | None = None):
        settings = cfg["tts"]["google_cloud"]
        key = api_key if api_key is not None else (os.getenv("GOOGLE_TTS_API_KEY", "") or os.getenv("GEMINI_API_KEY", ""))
        self.api_key = key.strip()
        if not self.api_key:
            raise TTSError("GOOGLE_TTS_API_KEY (or GEMINI_API_KEY) is not configured")
        self.voices = settings["voices"]
        self.budget = int(settings.get("monthly_character_budget", 950_000))
        self.max_attempts = int(settings.get("max_attempts", 5))
        self.learner_tempo = float(cfg["tts"].get("target_language_tempo", 1.0))
        self.slow_tempo = tempo_from_rate(str(cfg.get("learning", {}).get("slow_rate", "-25%")))
        self.session = session or requests.Session()
        self.sleep = sleep
        self.usage_path = usage_path or Path(__file__).resolve().parents[1] / settings.get("usage_file", "state/google-tts-usage.json")
        self.month = month or time.strftime("%Y-%m")
        self.model_in_use = "chirp3-hd"
        self.disabled = ""

    # -- monthly budget -------------------------------------------------------------------------
    def used(self) -> int:
        try:
            data = json.loads(self.usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        return int(data.get(self.month, 0)) if isinstance(data, dict) else 0

    def record(self, characters: int) -> None:
        try:
            data = json.loads(self.usage_path.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        data[self.month] = int(data.get(self.month, 0)) + characters
        self.usage_path.parent.mkdir(parents=True, exist_ok=True)
        self.usage_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # -- synthesis ------------------------------------------------------------------------------
    def voice_name(self, utterance: dict) -> tuple[str, str]:
        code = self.LANGUAGE_CODES.get(utterance["language"], utterance["language"])
        return code, f"{code}-Chirp3-HD-{self.voices[utterance['speaker']]}"

    def rate(self, utterance: dict) -> float:
        if utterance.get("slow"):
            return self.slow_tempo
        return 1.0 if utterance["language"] == "ja-JP" else self.learner_tempo

    def render(self, utterances: list[dict]) -> list[AudioSegment]:
        if self.disabled:
            raise TTSError(self.disabled)
        characters = sum(len(u["text"]) for u in utterances)
        used = self.used()
        if used + characters > self.budget:
            raise TTSError(f"monthly free-tier budget would be exceeded ({used:,} + {characters:,} > {self.budget:,} characters)")
        clips = [trim_silence(self.line(u)) for u in utterances]
        self.record(characters)
        return clips

    def line(self, utterance: dict) -> AudioSegment:
        language, voice = self.voice_name(utterance)
        payload = {
            "input": {"text": utterance["text"]},
            "voice": {"languageCode": language, "name": voice},
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": 24000, "speakingRate": self.rate(utterance)},
        }
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.post(self.ENDPOINT, params={"key": self.api_key}, json=payload, timeout=(20, 120))
            except requests.RequestException as exc:
                if attempt == self.max_attempts:
                    raise TTSError(f"Cloud TTS request failed: {exc}") from exc
                self.sleep(min(30, 3 * attempt))
                continue
            status = response.status_code
            if status in (401, 403):
                # Typically "Cloud Text-to-Speech API has not been used in project ... or it is disabled".
                self.disabled = f"Cloud TTS refused the request (HTTP {status}): {response.text[:200]}"
                raise TTSError(self.disabled)
            if status in RETRYABLE and attempt < self.max_attempts:
                self.sleep(retry_delay(response) or min(30, 3 * 2 ** (attempt - 1)))
                continue
            if status >= 400:
                raise TTSError(f"Cloud TTS HTTP {status}: {response.text[:300]}")
            content = response.json().get("audioContent")
            if not content:
                raise TTSError("Cloud TTS returned no audio")
            return AudioSegment.from_file(io.BytesIO(base64.b64decode(content)), format="wav")
        raise TTSError("Cloud TTS: retries exhausted")


# --------------------------------------------------------------------------- shared helpers


def retry_delay(response) -> float | None:
    """Seconds from Retry-After or the google.rpc.RetryInfo detail ("retryDelay": "31s")."""
    headers = getattr(response, "headers", None) or {}
    header = headers.get("Retry-After") or headers.get("retry-after")
    if header and str(header).strip().replace(".", "", 1).isdigit():
        return float(header)
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', getattr(response, "text", "") or "")
    return float(match.group(1)) if match else None


def trim_silence(audio: AudioSegment, keep_ms: int = 60, threshold_db: float = -45.0) -> AudioSegment:
    """Cut leading/trailing silence so the episode's own pauses set the pace."""
    if len(audio) == 0:
        return audio
    head = detect_leading_silence(audio, silence_threshold=threshold_db, chunk_size=10)
    tail = detect_leading_silence(audio.reverse(), silence_threshold=threshold_db, chunk_size=10)
    if head + tail >= len(audio):
        return audio
    return audio[max(0, head - keep_ms): len(audio) - max(0, tail - keep_ms)]


def slow_down(audio: AudioSegment, tempo: float) -> AudioSegment:
    """Pitch-preserving time stretch (ffmpeg atempo) for shadowing lines."""
    if tempo >= 0.999:
        return audio
    with tempfile.TemporaryDirectory(prefix="journey-talk-slow-") as temp:
        source, target = Path(temp) / "in.wav", Path(temp) / "out.wav"
        audio.export(source, format="wav")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-filter:a", f"atempo={tempo:.3f}", str(target)],
            check=True,
        )
        return AudioSegment.from_file(target, format="wav")


def tempo_from_rate(rate: str) -> float:
    """Edge-style rate ("-25%") to an atempo factor (0.75)."""
    match = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)%\s*", str(rate))
    return max(0.5, min(2.0, 1 + float(match.group(1)) / 100)) if match else 1.0


def speech_tokens(text: str) -> list[str]:
    """Units to highlight: words for spaced scripts, single characters for Chinese/Japanese."""
    return re.findall(r"[぀-ヿ㐀-鿿豈-﫿]|[^\W぀-ヿ㐀-鿿豈-﫿]+", text)


def estimate_word_timings(text: str, duration_s: float) -> list[list]:
    """Spread a line's duration over its words in proportion to their length."""
    tokens = speech_tokens(text)
    if not tokens or duration_s <= 0:
        return []
    weights = [2.0 if CJK.match(t) else len(t) + 1.0 for t in tokens]
    total = sum(weights)
    out: list[list] = []
    cursor = 0.0
    for token, weight in zip(tokens, weights):
        span = duration_s * weight / total
        out.append([round(cursor, 3), round(cursor + span * 0.92, 3), token])
        cursor += span
    return out


RAW_RATES = {"ja-JP": 7.5, "es-ES": 18.6, "de-DE": 18.8, "ru-RU": 15.9, "zh-CN": 5.4, "ko-KR": 6.1}


def expected_seconds(text: str, language: str, rates: dict | None = None) -> float:
    """Rough spoken length of a line as the cloud voices say it (before any tempo change).

    Only the proportions between lines matter for splitting a take, so approximate rates are enough.
    """
    rate = (rates or RAW_RATES).get(language, 18.0)
    units = len(re.sub(r"\s+", "", text)) if language in {"ja-JP", "zh-CN", "ko-KR"} else len(text)
    return max(0.4, units / rate)


def align_lines(audio: AudioSegment, expected: list[float], min_gap_ms: int = 150) -> list[tuple[int, int]]:
    """Split one multi-line take into per-line [start_ms, end_ms] spans.

    Candidate cut points are the pauses in the take. Dynamic programming picks one pause between
    each pair of lines so that every line's length matches its expected share of the take (log-ratio
    error) while preferring longer pauses, which is where speakers hand over (and where the
    "[long pause]" tag puts its silence). Raises ValueError when no plausible split exists.
    """
    n = len(expected)
    total = len(audio)
    if n == 1:
        return [(0, total)]
    threshold = max(-50.0, audio.dBFS - 18) if audio.dBFS != float("-inf") else -50.0
    gaps = [(a, b) for a, b in detect_silence(audio, min_silence_len=min_gap_ms, silence_thresh=threshold) if 0 < a and b < total]
    if len(gaps) < n - 1:
        raise ValueError(f"only {len(gaps)} pauses for {n} lines")
    scale = total / sum(expected)
    want = [e * scale for e in expected]
    m = len(gaps)
    inf = float("inf")

    def segment_cost(k: int, start: int, end: int) -> float:
        return math.log(max(1, end - start) / want[k]) ** 2

    def gap_bonus(j: int) -> float:
        return 0.6 * min(1.2, (gaps[j][1] - gaps[j][0]) / 1000)

    # best[k][j]: lines 0..k placed, line k ends at gap j.
    best = [[inf] * m for _ in range(n - 1)]
    back = [[-1] * m for _ in range(n - 1)]
    for j in range(m):
        best[0][j] = segment_cost(0, 0, gaps[j][0]) - gap_bonus(j)
    for k in range(1, n - 1):
        prev = best[k - 1]
        for j in range(k, m):
            bonus = gap_bonus(j)
            start_j = gaps[j][0]
            row_best, row_back = inf, -1
            for i in range(k - 1, j):
                if prev[i] == inf:
                    continue
                cost = prev[i] + segment_cost(k, gaps[i][1], start_j) - bonus
                if cost < row_best:
                    row_best, row_back = cost, i
            best[k][j], back[k][j] = row_best, row_back
    final, last = inf, -1
    for j in range(n - 2, m):
        if best[n - 2][j] == inf:
            continue
        cost = best[n - 2][j] + segment_cost(n - 1, gaps[j][1], total)
        if cost < final:
            final, last = cost, j
    if last < 0:
        raise ValueError("no consistent split")
    cuts = [last]
    for k in range(n - 2, 0, -1):
        cuts.append(back[k][cuts[-1]])
    cuts.reverse()
    spans, start = [], 0
    for j in cuts:
        spans.append((start, gaps[j][0]))
        start = gaps[j][1]
    spans.append((start, total))
    for k, (a, b) in enumerate(spans):
        ratio = (b - a) / want[k]
        if not 0.3 <= ratio <= 3.0:
            raise ValueError(f"line {k} is {ratio:.2f}x its expected length; the take does not match the script")
    return spans
