from __future__ import annotations

import argparse
import json
import re
import subprocess
import unicodedata
from pathlib import Path

import yaml
from faster_whisper import WhisperModel

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "cloud_languages.yaml"


def load_config() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def decode_entire_file(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-i", str(path), "-f", "null", "-"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def detect_long_silence(path: Path, threshold: float) -> list[float]:
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(path),
            "-af", f"silencedetect=n=-50dB:d={threshold}",
            "-f", "null", "-",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    durations = [float(x) for x in re.findall(r"silence_duration:\s*([0-9.]+)", result.stderr)]
    return [x for x in durations if x >= threshold]


def canonical(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in value if ch.isalnum())


def expected_text(episode: dict) -> str:
    return " ".join(x["text"] for x in episode["utterances"])


def transcribe(model: WhisperModel, path: Path) -> str:
    segments, _ = model.transcribe(str(path), beam_size=1, vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments if segment.text.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode-dir", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    cfg = load_config()
    manifest = json.loads((args.episode_dir / "manifest.json").read_text(encoding="utf-8"))
    media = json.loads((args.media_dir / "media-manifest.json").read_text(encoding="utf-8"))
    by_slug = {x["slug"]: x for x in media["episodes"]}
    args.output_dir.mkdir(parents=True, exist_ok=True)

    model_name = str(cfg["qa"]["whisper_model"])
    whisper = WhisperModel(model_name, device="cpu", compute_type="int8")
    minimum_ratio = float(cfg["qa"]["minimum_transcript_ratio"])
    max_silence = float(cfg["qa"]["max_silence_seconds"])
    report = {"episode_date": manifest["episode_date"], "whisper_model": model_name, "episodes": []}

    for item in manifest["episodes"]:
        slug = item["slug"]
        episode = json.loads((args.episode_dir / item["json"]).read_text(encoding="utf-8"))
        media_item = by_slug[slug]
        audio = args.media_dir / media_item["audio"]
        decode_entire_file(audio)
        long_silences = detect_long_silence(audio, max_silence)
        if long_silences:
            raise RuntimeError(f"{slug}: detected long silence(s): {long_silences[:5]}")
        transcript = transcribe(whisper, audio)
        expected = canonical(expected_text(episode))
        heard = canonical(transcript)
        ratio = len(heard) / max(1, len(expected))
        if ratio < minimum_ratio:
            raise RuntimeError(
                f"{slug}: Whisper transcript ratio {ratio:.3f} below project QA minimum {minimum_ratio:.3f}"
            )
        (args.output_dir / f"{slug}-whisper.txt").write_text(transcript + "\n", encoding="utf-8")
        report["episodes"].append(
            {
                "slug": slug,
                "audio": media_item["audio"],
                "duration_seconds": media_item["duration_seconds"],
                "long_silences": long_silences,
                "transcript_ratio": round(ratio, 4),
                "status": "PASS",
            }
        )
        print(f"[qa] {slug}: PASS transcript_ratio={ratio:.3f}")

    (args.output_dir / "qa-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
