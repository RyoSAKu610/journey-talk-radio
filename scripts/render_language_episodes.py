from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

import edge_tts
import yaml
from pydub import AudioSegment

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


async def synthesize(cfg: dict, utterances: list[dict], work: Path) -> list[Path]:
    paths: list[Path] = []
    for index, utterance in enumerate(utterances):
        voice = voice_for(cfg, utterance["language"], utterance["speaker"])
        path = work / f"{index:03d}.mp3"
        await edge_tts.Communicate(utterance["text"], voice=voice).save(str(path))
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"TTS failed at utterance {index}")
        paths.append(path)
    return paths


def assemble(paths: list[Path], utterances: list[dict]) -> AudioSegment:
    audio = AudioSegment.empty()
    previous_language = None
    for path, utterance in zip(paths, utterances):
        segment = AudioSegment.from_file(path, format="mp3")
        audio += segment
        if previous_language is None or utterance["language"] == previous_language:
            pause_ms = 480
        else:
            pause_ms = 650
        audio += AudioSegment.silent(duration=pause_ms)
        previous_language = utterance["language"]
    return audio


def export_normalized(audio: AudioSegment, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="journey-talk-normalize-") as temp:
        source = Path(temp) / "source.wav"
        audio.export(source, format="wav")
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-codec:a", "libmp3lame", "-b:a", "192k", str(output),
            ],
            check=True,
        )


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    cfg = load_config()
    manifest = json.loads((args.episode_dir / "manifest.json").read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    media = {"episode_date": manifest["episode_date"], "episodes": []}
    minimum = float(cfg["episode"]["minimum_seconds"])
    maximum = float(cfg["episode"]["maximum_seconds"])

    for item in manifest["episodes"]:
        episode = json.loads((args.episode_dir / item["json"]).read_text(encoding="utf-8"))
        slug = item["slug"]
        output = args.output_dir / f"journey-talk-{manifest['episode_date']}-{slug}.mp3"
        with tempfile.TemporaryDirectory(prefix="journey-talk-tts-") as temp:
            paths = asyncio.run(synthesize(cfg, episode["utterances"], Path(temp)))
            audio = assemble(paths, episode["utterances"])
            export_normalized(audio, output)
        duration = probe_duration(output)
        if not minimum <= duration <= maximum:
            raise RuntimeError(f"{slug} duration {duration:.2f}s outside {minimum:.0f}..{maximum:.0f}s")
        media["episodes"].append(
            {
                "slug": slug,
                "language": episode["language"],
                "japanese_name": episode["japanese_name"],
                "title": episode["title"],
                "audio": output.name,
                "duration_seconds": round(duration, 3),
                "bytes": output.stat().st_size,
            }
        )
        print(f"[audio] {slug}: {duration:.2f}s")

    (args.output_dir / "media-manifest.json").write_text(
        json.dumps(media, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
