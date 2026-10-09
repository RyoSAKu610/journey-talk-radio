"""Speech engines for Journey Talk, tried in the order set by cloud_languages.yaml (tts.order).

- fish: Fish Audio S2.1 Pro through its free API (announced free until 2026-11-30); one request per line,
  each host cloned from a reference clip, pace set natively.
- gemini: Gemini TTS, the whole episode in ONE multi-speaker request (the free tier allows about ten requests
  per model per day), then cut into lines at the "[long pause]" hand-overs by align_lines().
- kaggle: CosyVoice 3 on a free Kaggle GPU (about 30 hours a week), all pending episodes in one kernel run.
- cosyvoice: CosyVoice 3 on Modal's free $30/month GPU credit, one remote call per episode.
- google_cloud: Google Cloud TTS, Chirp 3: HD voices, inside the 1M characters a month free tier.
- cosyvoice_local: CosyVoice 3 on the GitHub Actions runner's own CPU: free and unlimited for this public
  repository, slow, time-boxed.
- openai: OpenAI GPT TTS (paid), only when OPENAI_API_KEY is set and named in TTS_ORDER.
- Edge TTS lives in render_language_episodes.py and is the last resort.

Engines with `batch = True` render several episodes in one go (render_many); the others one at a time.
Engines with `native_pace = True` already speak at the learner / shadowing pace; the rest are time-stretched.
Cloud engines return no word timings, so karaoke timings are estimated inside each line.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
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


ROOT = Path(__file__).resolve().parents[1]


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
    native_pace = True

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
        self.cfg = cfg
        self.voices = load_voice_references(settings["voices"])
        self.model_in_use = "cosyvoice3"

    def render(self, utterances: list[dict]) -> list[AudioSegment]:
        lines = cosyvoice_lines(self.cfg, utterances)
        try:
            wavs = self.client().synthesize.remote(lines, self.voices)
        except Exception as exc:  # Modal raises its own types: auth, credit exhausted, deploy missing, CUDA errors
            raise TTSError(f"CosyVoice on Modal failed: {type(exc).__name__}: {str(exc)[:300]}") from exc
        if len(wavs) != len(lines):
            raise TTSError(f"CosyVoice returned {len(wavs)} clips for {len(lines)} lines")
        return [trim_silence(AudioSegment.from_file(io.BytesIO(w), format="wav")) for w in wavs]


# --------------------------------------------------------------------------- shared by the cloned-voice engines


def learner_speed(cfg: dict, utterance: dict) -> float:
    """Native speaking speed for engines that set the pace themselves (1.0 = the engine's normal pace)."""
    if utterance.get("slow"):
        return tempo_from_rate(str(cfg.get("learning", {}).get("slow_rate", "-25%")))
    if utterance["language"] == "ja-JP":
        return 1.0
    return float(cfg["tts"].get("target_language_tempo", 1.0))


def load_voice_references(voices: dict, sample_rate: int = 16000) -> dict:
    """{speaker: {"reference": "assets/voices/mina", "language": "ja-JP"}} -> wav bytes + transcript.

    The clips are re-encoded as 16 kHz mono WAV: that is all the cloning models use, and it keeps requests small.
    """
    out = {}
    for speaker, voice in voices.items():
        base = ROOT / voice["reference"]
        try:
            with base.with_suffix(".wav").open("rb") as handle:
                clip = AudioSegment.from_file(handle, format="wav").set_frame_rate(sample_rate).set_channels(1)
            text = base.with_suffix(".txt").read_text(encoding="utf-8").strip()
        except (OSError, IndexError) as exc:
            raise TTSError(f"voice reference missing: {exc}") from exc
        buffer = io.BytesIO()
        clip.export(buffer, format="wav")
        out[speaker] = {"wav": buffer.getvalue(), "text": text, "language": voice.get("language", "ja-JP")}
    return out


def cosyvoice_lines(cfg: dict, utterances: list[dict]) -> list[dict]:
    return [
        {"speaker": u["speaker"], "language": u["language"], "text": u["text"], "speed": round(learner_speed(cfg, u), 3)}
        for u in utterances
    ]


def read_clip_dir(directory: Path, count: int) -> list[AudioSegment]:
    clips = []
    for index in range(count):
        path = directory / f"{index:03d}.flac"
        if not path.is_file():
            raise TTSError(f"missing clip {path.name}")
        clips.append(trim_silence(AudioSegment.from_file(path, format="flac")))
    return clips


# --------------------------------------------------------------------------- Fish Audio


class FishAudioTTS:
    """Fish Audio S2.1 Pro through the free API model ("s2.1-pro-free", announced free until 2026-11-30 under fair
    use, no SLA; requests may be kept to improve the model). One request per line, sent a few at a time.

    Each host is cloned from the same reference clips as CosyVoice, so the voices stay the same across engines,
    and the learner pace is set natively (prosody.speed). After `available_until` the engine stays out of the
    way instead of failing every day; HTTP 402 (the free model now needs credit) disables it for the run.
    """

    name = "fish"
    native_pace = True
    ENDPOINT = "https://api.fish.audio/v1/tts"

    def __init__(self, cfg: dict, api_key: str | None = None, session=None, sleep=time.sleep, today: date | None = None):
        settings = cfg["tts"]["fish"]
        self.api_key = (api_key if api_key is not None else os.getenv("FISH_API_KEY", "")).strip()
        if not self.api_key:
            raise TTSError("FISH_API_KEY is not configured")
        until = str(settings.get("available_until", "")).strip()
        if until and (today or date.today()) > date.fromisoformat(until):
            raise TTSError(f"the free Fish Audio model was announced until {until}; update tts.fish if it is still free")
        try:
            import msgpack
        except ImportError as exc:
            raise TTSError("the msgpack package is not installed") from exc
        self.pack = msgpack.packb
        self.cfg = cfg
        self.models = list(settings["models"])
        self.latency = settings.get("latency", "normal")
        self.parallel = max(1, int(settings.get("parallel_requests", 3)))
        self.max_attempts = int(settings.get("max_attempts", 5))
        self.voice_ids = {k: str(v.get("reference_id", "") or "") for k, v in settings["voices"].items()}
        self.references = load_voice_references(settings["voices"])
        self.session = session or requests.Session()
        self.sleep = sleep
        self.model_in_use: str | None = None
        self.disabled = ""

    def body(self, utterance: dict) -> dict:
        body = {
            "text": utterance["text"],
            "format": "wav",
            "sample_rate": 24000,
            "latency": self.latency,
            "normalize": True,
            "prosody": {"speed": learner_speed(self.cfg, utterance), "volume": 0},
        }
        voice_id = self.voice_ids.get(utterance["speaker"])
        if voice_id:
            body["reference_id"] = voice_id
        else:
            reference = self.references[utterance["speaker"]]
            body["references"] = [{"audio": reference["wav"], "text": reference["text"]}]
        return body

    def render(self, utterances: list[dict]) -> list[AudioSegment]:
        if self.disabled:
            raise TTSError(self.disabled)
        # Settle on a model with the first line, then send the rest a few at a time.
        clips = [self.line(utterances[0])]
        with ThreadPoolExecutor(max_workers=self.parallel) as pool:
            clips += list(pool.map(self.line, utterances[1:]))
        return [trim_silence(c) for c in clips]

    def line(self, utterance: dict) -> AudioSegment:
        if self.disabled:
            raise TTSError(self.disabled)
        models = [self.model_in_use] if self.model_in_use else list(self.models)
        problems: list[str] = []
        for model in models:
            try:
                audio = self._request(model, self.body(utterance))
            except _ModelUnavailable as exc:
                problems.append(f"{model}: {exc}")
                continue
            self.model_in_use = model
            return audio
        raise TTSError("Fish Audio: " + "; ".join(problems))

    def _request(self, model: str, payload: dict) -> AudioSegment:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/msgpack", "model": model}
        data = self.pack(payload)
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.post(self.ENDPOINT, headers=headers, data=data, timeout=(20, 180))
            except requests.RequestException as exc:
                if attempt == self.max_attempts:
                    raise TTSError(f"Fish Audio request failed: {exc}") from exc
                self.sleep(min(60, 5 * attempt))
                continue
            status = response.status_code
            if status in (401, 403):
                self.disabled = f"Fish Audio rejected the API key (HTTP {status})"
                raise TTSError(self.disabled)
            if status == 402:
                self.disabled = "Fish Audio asks for payment (HTTP 402): the free model is no longer free for this account"
                raise TTSError(self.disabled)
            if status == 404 or (status == 400 and "model" in response.text.lower()):
                raise _ModelUnavailable(f"HTTP {status}")
            if status in RETRYABLE and attempt < self.max_attempts:
                self.sleep(retry_delay(response) or min(60, 5 * 2 ** (attempt - 1)))
                continue
            if status >= 400:
                raise TTSError(f"Fish Audio HTTP {status}: {response.text[:300]}")
            if not response.content:
                raise TTSError("Fish Audio returned no audio")
            return AudioSegment.from_file(io.BytesIO(response.content), format="wav")
        raise TTSError("Fish Audio: retries exhausted")


# --------------------------------------------------------------------------- CosyVoice on the runner's CPU


class LocalCosyVoiceTTS:
    """CosyVoice 3 on the machine running this script, normally the GitHub Actions runner (4 CPUs, free and
    unlimited for a public repository). No account needed, but CPU synthesis is slow, so it is time-boxed:
    episodes not finished within `max_minutes` move on to the next engine. Dependencies go into a separate
    virtualenv the first time it is needed; the model is cached between runs by the workflow.
    """

    name = "cosyvoice_local"
    native_pace = True
    batch = True

    def __init__(self, cfg: dict, runner=subprocess.run, clock=time.time):
        settings = cfg["tts"]["cosyvoice_local"]
        if os.getenv("GITHUB_ACTIONS") != "true" and os.getenv("COSYVOICE_LOCAL") != "1":
            raise TTSError("runs on the GitHub Actions runner only (set COSYVOICE_LOCAL=1 to run it elsewhere)")
        self.cfg = cfg
        self.python = os.getenv("COSYVOICE_PYTHON", "") or settings.get("python", "python3.10")
        if not shutil.which(self.python) and not Path(self.python).is_file():
            raise TTSError(f"{self.python} is not installed")
        self.cache = ROOT / settings.get("cache_dir", ".cache/cosyvoice")
        self.max_minutes = float(os.getenv("COSYVOICE_LOCAL_MINUTES", "") or settings.get("max_minutes", 200))
        self.torch_index = settings.get("torch_index", "https://download.pytorch.org/whl/cpu")
        self.voices = load_voice_references(cfg["tts"]["cosyvoice"]["voices"])
        self.runner = runner
        self.clock = clock
        self.started = clock()
        self.model_in_use = "cosyvoice3-cpu"
        self.ready = False

    def venv_python(self) -> Path:
        return self.cache / "venv" / "bin" / "python"

    def setup(self) -> None:
        if self.ready:
            return
        if not self.venv_python().is_file():
            self.runner([self.python, "-m", "venv", str(self.cache / "venv")], check=True)
        worker = Path(__file__).resolve().parent / "cosyvoice_worker.py"
        self.runner(
            [str(self.venv_python()), str(worker), "--setup", "--source-dir", str(self.cache / "source"),
             "--model-dir", str(self.cache / "model"), "--torch-index", self.torch_index],
            check=True,
        )
        self.ready = True

    def render(self, utterances: list[dict]) -> list[AudioSegment]:
        return self.render_many({"episode": utterances})["episode"]

    def render_many(self, episodes: dict[str, list[dict]]) -> dict[str, list[AudioSegment]]:
        deadline = self.started + self.max_minutes * 60
        if self.clock() > deadline - 300:
            raise TTSError("no time left in this run for CPU synthesis")
        try:
            self.setup()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise TTSError(f"CosyVoice setup failed: {exc}") from exc
        with tempfile.TemporaryDirectory(prefix="journey-talk-cosyvoice-") as temp:
            job_path, out = Path(temp) / "job.json", Path(temp) / "out"
            job = cosyvoice_job(self.cfg, self.voices, episodes)
            job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            worker = Path(__file__).resolve().parent / "cosyvoice_worker.py"
            try:
                self.runner(
                    [str(self.venv_python()), str(worker), "--job", str(job_path), "--out", str(out),
                     "--source-dir", str(self.cache / "source"), "--model-dir", str(self.cache / "model"),
                     "--deadline", str(deadline)],
                    check=True, timeout=max(60, deadline - self.clock() + 600),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"[tts] CosyVoice CPU worker stopped: {exc}")
            return collect_batch(out, episodes)


def cosyvoice_job(cfg: dict, voices: dict, episodes: dict[str, list[dict]]) -> dict:
    return {
        "voices": {
            speaker: {"wav_b64": base64.b64encode(v["wav"]).decode("ascii"), "text": v["text"], "language": v["language"]}
            for speaker, v in voices.items()
        },
        "episodes": [{"slug": slug, "lines": cosyvoice_lines(cfg, lines)} for slug, lines in episodes.items()],
    }


def collect_batch(out: Path, episodes: dict[str, list[dict]]) -> dict[str, list[AudioSegment]]:
    """Episodes the worker finished (DONE marker and every clip present); the rest are simply absent."""
    results = {}
    for slug, lines in episodes.items():
        directory = out / slug
        if not (directory / "DONE").is_file():
            continue
        try:
            results[slug] = read_clip_dir(directory, len(lines))
        except TTSError as exc:
            print(f"[tts] {slug}: incomplete CosyVoice output ({exc})")
    return results


# --------------------------------------------------------------------------- CosyVoice on Kaggle


KAGGLE_BOOTSTRAP = """# Journey Talk: CosyVoice 3 batch synthesis (generated by tts_engines.KaggleCosyVoiceTTS; safe to delete).
import base64, io, subprocess, sys, time, zipfile
from pathlib import Path

PAYLOAD = "{payload}"
work = Path("/kaggle/temp/journey-talk")  # outside /kaggle/working so only the result is kept as output
work.mkdir(parents=True, exist_ok=True)
zipfile.ZipFile(io.BytesIO(base64.b64decode(PAYLOAD))).extractall(work)
common = ["--source-dir", "/kaggle/temp/CosyVoice", "--model-dir", "/kaggle/temp/cosyvoice3"]
subprocess.run([sys.executable, str(work / "cosyvoice_worker.py"), "--setup", "--torch-index", "{torch_index}", *common], check=True)
out = work / "out"
subprocess.run([sys.executable, str(work / "cosyvoice_worker.py"), "--job", str(work / "job.json"), "--out", str(out), "--deadline", str(time.time() + {budget_seconds}), *common], check=True)
with zipfile.ZipFile("/kaggle/working/tts.zip", "w", zipfile.ZIP_STORED) as archive:
    for path in out.rglob("*"):
        if path.is_file():
            archive.write(path, path.relative_to(out))
print("done")
"""


class KaggleCosyVoiceTTS:
    """CosyVoice 3 on a free Kaggle GPU (T4), started from the workflow with `kaggle kernels push`.

    All pending episodes go into ONE private kernel run, so the model is installed and loaded once a day.
    The kernel needs internet access, which Kaggle allows after phone verification of the account.
    Needs KAGGLE_USERNAME plus KAGGLE_API_TOKEN (or the older KAGGLE_KEY). The weekly GPU quota (about 30 h)
    covers a daily run comfortably; if it is used up the kernel fails and the next engine takes over.
    """

    name = "kaggle"
    native_pace = True
    batch = True

    def __init__(self, cfg: dict, runner=subprocess.run, sleep=time.sleep, clock=time.time):
        settings = cfg["tts"]["kaggle"]
        self.username = os.getenv("KAGGLE_USERNAME", "").strip()
        if not self.username or not (os.getenv("KAGGLE_API_TOKEN", "").strip() or os.getenv("KAGGLE_KEY", "").strip()):
            raise TTSError("KAGGLE_USERNAME and KAGGLE_API_TOKEN (or KAGGLE_KEY) are not configured")
        self.cli = shutil.which("kaggle") or ""
        if not self.cli:
            raise TTSError("the kaggle command is not installed")
        self.cfg = cfg
        self.slug = settings.get("kernel_slug", "journey-talk-tts")
        self.accelerator = settings.get("accelerator", "NvidiaTeslaT4")
        self.timeout_minutes = float(settings.get("timeout_minutes", 150))
        self.poll_seconds = float(settings.get("poll_seconds", 60))
        self.torch_index = settings.get("torch_index", "https://download.pytorch.org/whl/cu121")
        self.voices = load_voice_references(cfg["tts"]["cosyvoice"]["voices"])
        self.runner, self.sleep, self.clock = runner, sleep, clock
        self.model_in_use = "cosyvoice3-kaggle"
        self.disabled = ""

    @property
    def kernel(self) -> str:
        return f"{self.username}/{self.slug}"

    def kaggle(self, *args: str, timeout: float = 300) -> str:
        result = self.runner([self.cli, *args], capture_output=True, text=True, timeout=timeout)
        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            raise TTSError(f"kaggle {args[0]} {args[1]} failed: {output.strip()[:300]}")
        return output

    def kernel_folder(self, folder: Path, episodes: dict[str, list[dict]]) -> None:
        here = Path(__file__).resolve().parent
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(here / "cosyvoice_core.py", "cosyvoice_core.py")
            archive.write(here / "cosyvoice_worker.py", "cosyvoice_worker.py")
            archive.writestr("job.json", json.dumps(cosyvoice_job(self.cfg, self.voices, episodes), ensure_ascii=False))
        # Synthesis stops before the workflow gives up polling (setup takes ~15 min), so finished episodes come back.
        source = KAGGLE_BOOTSTRAP.format(
            payload=base64.b64encode(buffer.getvalue()).decode("ascii"),
            torch_index=self.torch_index,
            budget_seconds=round(max(10.0, self.timeout_minutes - 30) * 60),
        )
        (folder / "kernel.py").write_text(source, encoding="utf-8")
        metadata = {
            "id": self.kernel,
            "title": self.slug,
            "code_file": "kernel.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": "true",
            "enable_gpu": "true",
            "enable_tpu": "false",
            "enable_internet": "true",
            "machine_shape": self.accelerator,
            "dataset_sources": [],
            "competition_sources": [],
            "kernel_sources": [],
            "model_sources": [],
        }
        (folder / "kernel-metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    def status(self) -> str:
        text = self.kaggle("kernels", "status", self.kernel, timeout=120).lower()
        for state in ("complete", "error", "cancel", "running", "queued", "new_script"):
            if state in text:
                return state
        return "unknown"

    def render(self, utterances: list[dict]) -> list[AudioSegment]:
        return self.render_many({"episode": utterances})["episode"]

    def render_many(self, episodes: dict[str, list[dict]]) -> dict[str, list[AudioSegment]]:
        if self.disabled:
            raise TTSError(self.disabled)
        with tempfile.TemporaryDirectory(prefix="journey-talk-kaggle-") as temp:
            folder = Path(temp) / "kernel"
            folder.mkdir()
            self.kernel_folder(folder, episodes)
            try:
                self.kaggle("kernels", "push", "-p", str(folder), "--accelerator", self.accelerator, timeout=600)
            except (TTSError, subprocess.SubprocessError, OSError) as exc:
                self.disabled = f"Kaggle push failed: {exc}"
                raise TTSError(self.disabled) from exc
            print(f"[tts] Kaggle kernel {self.kernel} started for {len(episodes)} episode(s)")
            deadline = self.clock() + self.timeout_minutes * 60
            state = "queued"
            while self.clock() < deadline:
                self.sleep(self.poll_seconds)
                try:
                    state = self.status()
                except (TTSError, subprocess.SubprocessError) as exc:
                    print(f"[tts] Kaggle status check failed ({exc}); retrying")
                    continue
                if state in ("complete", "error", "cancel"):
                    break
            if state != "complete":
                self.disabled = f"Kaggle kernel ended as {state!r}"
                raise TTSError(f"{self.disabled}; see https://www.kaggle.com/code/{self.kernel}")
            download = Path(temp) / "download"
            download.mkdir()
            self.kaggle("kernels", "output", self.kernel, "-p", str(download), "-o", "-q", timeout=1200)
            archive = download / "tts.zip"
            if not archive.is_file():
                raise TTSError("the Kaggle kernel produced no tts.zip")
            out = Path(temp) / "out"
            with zipfile.ZipFile(archive) as zipped:
                zipped.extractall(out)
            results = collect_batch(out, episodes)
            print(f"[tts] Kaggle returned {len(results)}/{len(episodes)} episode(s)")
            return results


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
