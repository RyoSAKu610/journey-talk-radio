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


def volume_stats(path: Path) -> tuple[float, float]:
    """Mean and peak level in dB (ffmpeg volumedetect)."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    mean = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", result.stderr)
    peak = re.search(r"max_volume:\s*(-?[0-9.]+) dB", result.stderr)
    if not mean or not peak:
        raise RuntimeError("ffmpeg volumedetect returned no levels")
    return float(mean.group(1)), float(peak.group(1))


def canonical(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in value if ch.isalnum())


def expected_text(episode: dict) -> str:
    return " ".join(x["text"] for x in episode["utterances"])


def transcribe(model: WhisperModel, path: Path) -> str:
    segments, _ = model.transcribe(str(path), beam_size=1, vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments if segment.text.strip())


def check_episode(slug: str, audio: Path, episode: dict, whisper: WhisperModel, qa: dict) -> tuple[dict, list[str], str]:
    """Run every check on one edition; returns (metrics, problems, transcript). No problems means PASS."""
    metrics: dict = {}
    problems: list[str] = []
    try:
        decode_entire_file(audio)
    except subprocess.CalledProcessError as exc:
        return metrics, [f"decode failed: {exc.stderr[:200] if exc.stderr else exc}"], ""
    long_silences = detect_long_silence(audio, float(qa["max_silence_seconds"]))
    metrics["long_silences"] = long_silences
    if long_silences:
        problems.append(f"long silence(s): {long_silences[:5]}")
    try:
        mean, peak = volume_stats(audio)
        metrics.update(mean_volume_db=mean, peak_volume_db=peak)
        low, high = qa.get("mean_volume_db", [-26.0, -11.0])
        if not float(low) <= mean <= float(high):
            problems.append(f"mean volume {mean:.1f} dB outside {low}..{high}")
        if peak > float(qa.get("max_peak_db", -0.4)):
            problems.append(f"peak {peak:.1f} dB is too close to clipping")
    except RuntimeError as exc:
        problems.append(str(exc))
    transcript = transcribe(whisper, audio)
    ratio = len(canonical(transcript)) / max(1, len(canonical(expected_text(episode))))
    metrics["transcript_ratio"] = round(ratio, 4)
    if ratio < float(qa["minimum_transcript_ratio"]):
        problems.append(f"Whisper transcript ratio {ratio:.3f} below {qa['minimum_transcript_ratio']}")
    return metrics, problems, transcript


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
    report = {"episode_date": manifest["episode_date"], "whisper_model": model_name, "status": "PASS", "episodes": []}

    # Each edition passes or fails on its own: one bad take must not hold back the other languages.
    for item in manifest["episodes"]:
        slug = item["slug"]
        if slug not in by_slug:  # audio rendering skipped this edition
            continue
        episode = json.loads((args.episode_dir / item["json"]).read_text(encoding="utf-8"))
        media_item = by_slug[slug]
        audio = args.media_dir / media_item["audio"]
        metrics, problems, transcript = check_episode(slug, audio, episode, whisper, cfg["qa"])
        if transcript:
            (args.output_dir / f"{slug}-whisper.txt").write_text(transcript + "\n", encoding="utf-8")
        status = "FAIL" if problems else "PASS"
        report["episodes"].append(
            {
                "slug": slug,
                "audio": media_item["audio"],
                "tts": media_item.get("tts", ""),
                "duration_seconds": media_item["duration_seconds"],
                "status": status,
                "problems": problems,
                **metrics,
            }
        )
        print(f"[qa] {slug}: {status} " + ("; ".join(problems) if problems else f"transcript_ratio={metrics['transcript_ratio']:.3f}"))

    passed = [x for x in report["episodes"] if x["status"] == "PASS"]
    if len(passed) != len(report["episodes"]):
        report["status"] = "PARTIAL" if passed else "FAIL"
    (args.output_dir / "qa-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if not passed:
        print("[qa] no edition passed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
