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


def load_config():
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def voice_for(cfg, language, speaker):
    if language == "ja-JP":
        return cfg["japanese_voices"][speaker]
    for item in cfg["languages"]:
        if item["code"] == language:
            return item["voice_f"] if speaker == "MC_F" else item["voice_m"]
    raise ValueError("No voice for " + language + " / " + speaker)


async def synthesize(cfg, utterances, work):
    paths = []
    for i, utterance in enumerate(utterances):
        voice = voice_for(cfg, utterance["language"], utterance["speaker"])
        path = work / ("%03d.mp3" % i)
        await edge_tts.Communicate(utterance["text"], voice=voice).save(str(path))
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("TTS failed at utterance %d" % i)
        paths.append(path)
    return paths


def assemble(paths, utterances):
    audio = AudioSegment.empty()
    for path, utterance in zip(paths, utterances):
        audio += AudioSegment.from_file(path, format="mp3")
        pause = 650 if utterance["language"] == "ja-JP" else 500
        audio += AudioSegment.silent(duration=pause)
    return audio


def normalize_to_target(audio, target_ms, output):
    raw_ms = len(audio)
    ratio = raw_ms / target_ms
    if not 0.75 <= ratio <= 1.35:
        raise RuntimeError(
            "Raw duration %.1fs is too far from target %.1fs" % (raw_ms / 1000, target_ms / 1000)
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    if 0.97 <= ratio <= 1.03:
        audio.export(output, format="mp3", bitrate="192k", tags={"artist": "Journey Talk"})
        return raw_ms / 1000

    with tempfile.TemporaryDirectory(prefix="journey-talk-lang-") as temp:
        raw = Path(temp) / "raw.wav"
        adjusted = Path(temp) / "adjusted.wav"
        audio.export(raw, format="wav")
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(raw), "-filter:a", "atempo=%.6f" % ratio, str(adjusted),
            ],
            check=True,
        )
        adjusted_audio = AudioSegment.from_file(adjusted, format="wav")
        adjusted_audio.export(output, format="mp3", bitrate="192k", tags={"artist": "Journey Talk"})
        return len(adjusted_audio) / 1000


def probe(path):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode-dir", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()

    cfg = load_config()
    manifest = json.loads((args.episode_dir / "manifest.json").read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    media = {"episode_date": manifest["episode_date"], "episodes": []}

    for item in manifest["episodes"]:
        episode = json.loads((args.episode_dir / item["json"]).read_text(encoding="utf-8"))
        slug = item["slug"]
        output = args.output_dir / ("journey-talk-%s-%s.mp3" % (manifest["episode_date"], slug))
        with tempfile.TemporaryDirectory(prefix="journey-talk-tts-") as temp:
            paths = asyncio.run(synthesize(cfg, episode["utterances"], Path(temp)))
            audio = assemble(paths, episode["utterances"])
            normalize_to_target(audio, int(episode["target_seconds"]) * 1000, output)

        seconds = probe(output)
        if not 570 <= seconds <= 630:
            raise RuntimeError("%s duration %.2fs violates 10 minute target" % (slug, seconds))
        media["episodes"].append(
            {
                "slug": slug,
                "language": episode["language"],
                "audio": output.name,
                "duration_seconds": round(seconds, 3),
                "bytes": output.stat().st_size,
            }
        )
        print("[audio]", slug, round(seconds, 2), "seconds")

    (args.output_dir / "media-manifest.json").write_text(
        json.dumps(media, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
