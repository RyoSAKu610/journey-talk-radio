from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import edge_tts
import yaml
from pydub import AudioSegment

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tts_engines import (  # noqa: E402
    CosyVoiceModalTTS,
    FishAudioTTS,
    GeminiEpisodeTTS,
    GoogleCloudTTS,
    KaggleCosyVoiceTTS,
    LocalCosyVoiceTTS,
    OpenAITTS,
    TTSError,
    estimate_word_timings,
    slow_down,
    tempo_from_rate,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "cloud_languages.yaml"


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def voice_for(cfg: dict, language: str, speaker: str) -> str:
    if language == "ja-JP":
        return cfg["japanese_voices"][speaker]
    for item in cfg["languages"]:
        if item["code"] == language:
            return item["voice_f"] if speaker == "MC_F" else item["voice_m"]
    raise ValueError(f"No voice for {language} / {speaker}")


async def synthesize_one(text: str, voice: str, rate: str, path: Path) -> list[list]:
    """Write one utterance to MP3 and return its word timings as [start_s, end_s, word]."""
    words: list[list] = []
    communicate = edge_tts.Communicate(text, voice=voice, rate=rate, boundary="WordBoundary")
    with path.open("wb") as handle:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                handle.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                # Edge TTS reports offsets in 100 ns ticks.
                start, end = chunk["offset"], chunk["offset"] + chunk["duration"]
                words.append([start / 10_000_000, end / 10_000_000, chunk["text"]])
    return words


async def synthesize(cfg: dict, utterances: list[dict], work: Path) -> tuple[list[Path], list[list[list]]]:
    slow_rate = str(cfg.get("learning", {}).get("slow_rate", "-25%"))
    paths: list[Path] = []
    word_timings: list[list[list]] = []
    for index, utterance in enumerate(utterances):
        voice = voice_for(cfg, utterance["language"], utterance["speaker"])
        rate = slow_rate if utterance.get("slow") else "+0%"
        path = work / f"{index:03d}.mp3"
        words = await synthesize_one(utterance["text"], voice, rate, path)
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"TTS failed at utterance {index}")
        paths.append(path)
        word_timings.append(words)
    return paths, word_timings


def clips_to_files(engine, cfg: dict, utterances: list[dict], clips: list, work: Path) -> tuple[list[Path], list[list[list]]]:
    """Write one engine's clips as WAV files; slow lines are time-stretched unless the engine set the pace itself."""
    slow_tempo = tempo_from_rate(str(cfg.get("learning", {}).get("slow_rate", "-25%")))
    learner_tempo = float(cfg["tts"].get("target_language_tempo", 1.0))
    if len(clips) != len(utterances):
        raise TTSError(f"{engine.name}: {len(clips)} clips for {len(utterances)} lines")
    paths: list[Path] = []
    word_timings: list[list[list]] = []
    for index, (utterance, audio) in enumerate(zip(utterances, clips)):
        if len(audio) < 150:
            raise TTSError(f"{engine.name}: line {index} produced only {len(audio)} ms of audio")
        if getattr(engine, "native_pace", False):
            pass  # the engine already spoke at the learner / shadowing pace
        elif utterance.get("slow"):
            audio = slow_down(audio, slow_tempo)
        elif utterance["language"] != "ja-JP":
            audio = slow_down(audio, learner_tempo)
        path = work / f"{index:03d}.wav"
        audio.export(path, format="wav")
        paths.append(path)
        word_timings.append(estimate_word_timings(utterance["text"], len(audio) / 1000))
    return paths, word_timings


def synthesize_with(engine, cfg: dict, utterances: list[dict], work: Path) -> tuple[list[Path], list[list[list]]]:
    """Render every line of one episode with one cloud engine."""
    return clips_to_files(engine, cfg, utterances, engine.render(utterances), work)


def tts_order(cfg: dict) -> list[str]:
    override = os.getenv("TTS_ORDER", "").strip()
    order = [x.strip() for x in override.split(",") if x.strip()] if override else list(cfg["tts"]["order"])
    return order if "edge" in order else [*order, "edge"]  # Edge needs no key: always the last resort


ENGINES = {
    "fish": FishAudioTTS,
    "gemini": GeminiEpisodeTTS,
    "kaggle": KaggleCosyVoiceTTS,
    "cosyvoice": CosyVoiceModalTTS,
    "google_cloud": GoogleCloudTTS,
    "cosyvoice_local": LocalCosyVoiceTTS,
    "openai": OpenAITTS,
}


def open_engines(cfg: dict) -> list:
    """Cloud engines in priority order, skipping those without credentials."""
    engines = []
    for name in tts_order(cfg):
        factory = ENGINES.get(name)
        if factory is None:
            continue
        try:
            engines.append(factory(cfg))
        except TTSError as exc:
            print(f"[tts] {name} skipped: {exc}")
    return engines


def pause_after(utterance: dict, previous_language: str | None, segment_ms: int, cfg: dict) -> int:
    if utterance.get("slow"):
        # Leave room for the listener to repeat (shadow) the slow line. Kept below the QA silence limit.
        low, high = cfg.get("learning", {}).get("shadowing_pause_ms", [1200, 2000])
        return int(min(high, max(low, segment_ms * 0.8)))
    if previous_language is None or utterance["language"] == previous_language:
        return 480
    return 650


def assemble(
    paths: list[Path], utterances: list[dict], cfg: dict, word_timings: list[list[list]] | None = None
) -> tuple[AudioSegment, list[list[float]], list[list[list]]]:
    """Concatenate utterances.

    Returns the audio, each utterance's [start, end] in seconds, and each utterance's words as
    [start, end, word] in seconds from the beginning of the episode.
    """
    audio = AudioSegment.empty()
    timeline: list[list[float]] = []
    words: list[list[list]] = []
    previous_language = None
    for index, (path, utterance) in enumerate(zip(paths, utterances)):
        segment = AudioSegment.from_file(path, format=path.suffix.lstrip(".") or "mp3")
        start = len(audio)
        audio += segment
        timeline.append([round(start / 1000, 2), round(len(audio) / 1000, 2)])
        local = word_timings[index] if word_timings else []
        words.append([[round(start / 1000 + a, 2), round(start / 1000 + b, 2), text] for a, b, text in local])
        audio += AudioSegment.silent(duration=pause_after(utterance, previous_language, len(segment), cfg))
        previous_language = utterance["language"]
    return audio, timeline, words


def offline_name(audio_name: str) -> str:
    """journey-talk-DATE-es.mp3 -> journey-talk-DATE-es.offline.mp3"""
    return audio_name[: -len(".mp3")] + ".offline.mp3"


def export_normalized(audio: AudioSegment, output: Path, offline: Path | None = None) -> None:
    """Loudness-normalise once and encode the 192 kbps master plus, optionally, a small offline copy.

    The offline copy (64 kbps mono, about a third of the size) is served from GitHub Pages so the
    web player can save it for offline listening; both files share the same timeline.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="journey-talk-normalize-") as temp:
        source = Path(temp) / "source.wav"
        audio.export(source, format="wav")
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source)]
        if offline is None:
            command += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-codec:a", "libmp3lame", "-b:a", "192k", str(output)]
        else:
            command += [
                "-filter_complex", "[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,asplit=2[master][small]",
                "-map", "[master]", "-codec:a", "libmp3lame", "-b:a", "192k", str(output),
                "-map", "[small]", "-ac", "1", "-codec:a", "libmp3lame", "-b:a", "64k", str(offline),
            ]
        subprocess.run(command, check=True)


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def render_audio(cfg: dict, utterances: list[dict], work: Path, engines: list) -> tuple[list[Path], list, str]:
    """Try each engine in order, then Edge; one episode never mixes voices."""
    for engine in engines:
        try:
            paths, words = synthesize_with(engine, cfg, utterances, work)
            return paths, words, f"{engine.name}:{engine.model_in_use}"
        except TTSError as exc:
            print(f"::warning::{engine.name} TTS failed for this episode ({exc}); trying the next engine")
            for leftover in work.iterdir():
                leftover.unlink()
    paths, words = asyncio.run(synthesize(cfg, utterances, work))
    return paths, words, "edge"


def engine_clips(engine, pending: dict[str, list[dict]]) -> dict:
    """{slug: clips or TTSError} for the episodes one engine could render. Batch engines get them all at once."""
    if getattr(engine, "batch", False):
        try:
            return dict(engine.render_many(pending))
        except TTSError as exc:
            print(f"::warning::{engine.name} TTS failed ({exc}); trying the next engine")
            return {}
    results: dict = {}
    for slug, utterances in pending.items():
        try:
            results[slug] = engine.render(utterances)
        except TTSError as exc:
            print(f"::warning::{engine.name} TTS failed for {slug} ({exc}); trying the next engine")
    return results


def finish_episode(cfg: dict, episode: dict, output: Path, paths: list[Path], word_timings: list, engine: str, window: tuple[float, float]) -> dict:
    """Assemble, normalise and check one episode; raises when the result is unusable."""
    minimum, maximum = window
    audio, timeline, words = assemble(paths, episode["utterances"], cfg, word_timings)
    offline = output.with_name(offline_name(output.name))
    export_normalized(audio, output, offline)
    duration = probe_duration(output)
    if not minimum <= duration <= maximum:
        for path in (output, offline):
            path.unlink(missing_ok=True)
        raise RuntimeError(f"duration {duration:.2f}s outside {minimum:.0f}..{maximum:.0f}s")
    return {
        "slug": episode["slug"],
        "language": episode["language"],
        "japanese_name": episode["japanese_name"],
        "title": episode["title"],
        "audio": output.name,
        "duration_seconds": round(duration, 3),
        "bytes": output.stat().st_size,
        "offline_audio": offline.name,
        "offline_bytes": offline.stat().st_size,
        "tts": engine,
        "words_estimated": engine != "edge",
        "timeline": timeline,
        "words": words,
    }


def audio_window(cfg: dict, episode: dict) -> tuple[float, float]:
    episode_cfg = cfg["episode"]
    key = "bundle_audio_seconds" if episode.get("source") == "bundle" else "audio_seconds"
    low, high = episode_cfg.get(key) or episode_cfg.get("audio_seconds") or [episode_cfg["minimum_seconds"], episode_cfg["maximum_seconds"]]
    return float(low), float(high)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    cfg = load_config()
    manifest = json.loads((args.episode_dir / "manifest.json").read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    date = manifest["episode_date"]
    media = {"episode_date": date, "episodes": [], "failed": []}
    episodes = {
        item["slug"]: json.loads((args.episode_dir / item["json"]).read_text(encoding="utf-8"))
        for item in manifest["episodes"]
    }
    finished: dict[str, dict] = {}
    errors: dict[str, str] = {}

    def output_for(slug: str) -> Path:
        return args.output_dir / f"journey-talk-{date}-{slug}.mp3"

    # Engine by engine, each one gets every episode still without audio. An episode whose take fails the
    # checks (e.g. duration) also goes on to the next engine.
    for engine in open_engines(cfg):
        pending = {slug: e["utterances"] for slug, e in episodes.items() if slug not in finished}
        if not pending:
            break
        for slug, clips in engine_clips(engine, pending).items():
            episode = episodes[slug]
            try:
                with tempfile.TemporaryDirectory(prefix="journey-talk-tts-") as temp:
                    paths, timings = clips_to_files(engine, cfg, episode["utterances"], clips, Path(temp))
                    label = f"{engine.name}:{engine.model_in_use}"
                    finished[slug] = finish_episode(cfg, episode, output_for(slug), paths, timings, label, audio_window(cfg, episode))
                print(f"[audio] {slug}: {finished[slug]['duration_seconds']:.2f}s via {label}")
            except Exception as exc:  # noqa: BLE001 - the next engine gets another chance
                errors[slug] = f"{engine.name}: {exc}"
                print(f"::warning::{slug}: {engine.name} audio rejected ({exc}); trying the next engine")

    # Edge TTS, the last resort, for whatever is left.
    for slug, episode in episodes.items():
        if slug in finished:
            continue
        try:
            with tempfile.TemporaryDirectory(prefix="journey-talk-tts-") as temp:
                paths, timings = asyncio.run(synthesize(cfg, episode["utterances"], Path(temp)))
                finished[slug] = finish_episode(cfg, episode, output_for(slug), paths, timings, "edge", audio_window(cfg, episode))
            print(f"[audio] {slug}: {finished[slug]['duration_seconds']:.2f}s via edge")
        except Exception as exc:  # noqa: BLE001 - one language failing must not cost the others
            print(f"::warning::{slug} audio skipped: {exc}")
            media["failed"].append({"slug": slug, "error": (errors.get(slug, "") + " | edge: " + str(exc))[:500]})

    media["episodes"] = [finished[slug] for slug in episodes if slug in finished]
    (args.output_dir / "media-manifest.json").write_text(
        json.dumps(media, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not media["episodes"]:
        print("[error] no episode could be rendered")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
