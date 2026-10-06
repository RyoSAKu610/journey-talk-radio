from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from faster_whisper import WhisperModel
from pydub import AudioSegment

from voice_director import VoiceDirector, normalize_spoken_text

ROOT = Path(__file__).resolve().parents[1]


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
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"silencedetect=n=-50dB:d={threshold}",
            "-f",
            "null",
            "-",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    durations = [float(value) for value in re.findall(r"silence_duration:\s*([0-9.]+)", result.stderr)]
    return [value for value in durations if value >= threshold]


def volume_stats(path: Path) -> tuple[float, float]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    mean_match = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", result.stderr)
    max_match = re.search(r"max_volume:\s*(-?[0-9.]+) dB", result.stderr)
    if not mean_match or not max_match:
        raise RuntimeError("ffmpeg volumedetect did not return mean/max volume")
    return float(mean_match.group(1)), float(max_match.group(1))


def probe_audio(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,bit_rate:format=duration,bit_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def canonical(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return "".join(ch for ch in value if ch.isalnum())


def similarity(expected: str, heard: str) -> float:
    left = canonical(expected)
    right = canonical(heard)
    if not left or not right:
        return 0.0
    return SequenceMatcher(a=left, b=right, autojunk=False).ratio()


def _even_pick(items: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if len(items) <= count:
        return items
    if count <= 1:
        return [items[len(items) // 2]]
    selected: list[dict[str, Any]] = []
    for slot in range(count):
        index = round(slot * (len(items) - 1) / (count - 1))
        selected.append(items[index])
    unique: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in selected:
        if int(item["index"]) not in seen:
            seen.add(int(item["index"]))
            unique.append(item)
    return unique


def choose_asr_samples(plan: list[dict[str, Any]], target_language: str, total: int) -> list[dict[str, Any]]:
    target_candidates = [
        item
        for item in plan
        if item["language"] == target_language and float(item["segment_seconds"]) >= 1.6 and item["intent"] not in {"reaction"}
    ]
    ja_candidates = [
        item
        for item in plan
        if item["language"] == "ja-JP" and float(item["segment_seconds"]) >= 1.6 and item["intent"] not in {"reaction"}
    ]
    target_count = max(3, total - 2)
    samples = _even_pick(target_candidates, target_count) + _even_pick(ja_candidates, 2)
    return sorted(samples, key=lambda item: int(item["index"]))


def transcribe_clip(model: WhisperModel, clip: AudioSegment, language: str, temp_dir: Path, index: int) -> str:
    path = temp_dir / f"sample-{index:03d}.wav"
    clip.export(path, format="wav")
    short_language = language.split("-", 1)[0]
    segments, _ = model.transcribe(
        str(path),
        language=short_language,
        beam_size=1,
        vad_filter=False,
        condition_on_previous_text=False,
    )
    return " ".join(segment.text.strip() for segment in segments if segment.text.strip())


def validate_speech_plan(
    director: VoiceDirector,
    episode: dict[str, Any],
    plan: list[dict[str, Any]],
) -> None:
    if len(plan) != len(episode["utterances"]):
        raise RuntimeError(f"{episode['slug']}: speech plan length does not match script")
    previous: dict[str, Any] | None = None
    for index, (utterance, actual) in enumerate(zip(episode["utterances"], plan)):
        expected = director.controls(
            utterance,
            index,
            len(episode["utterances"]),
            previous=previous,
            episode_seed=f"{episode['episode_date']}:{episode['slug']}",
        )
        for key, value in {
            "speaker": utterance["speaker"],
            "language": utterance["language"],
            "intent": expected.intent,
            "voice": expected.voice,
            "rate": expected.rate,
            "pitch": expected.pitch,
            "pause_after_ms": expected.pause_after_ms,
        }.items():
            if actual.get(key) != value:
                raise RuntimeError(f"{episode['slug']}: speech plan mismatch at {index} for {key}")
        previous = utterance


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode-dir", type=Path, required=True)
    parser.add_argument("--media-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=ROOT / "voice_profiles.yaml")
    args = parser.parse_args()

    director = VoiceDirector(args.profile)
    qa = director.config["qa"]
    program = director.config["program"]
    manifest = json.loads((args.episode_dir / "manifest.json").read_text(encoding="utf-8"))
    media = json.loads((args.media_dir / "media-manifest.json").read_text(encoding="utf-8"))
    by_slug = {item["slug"]: item for item in media["episodes"]}
    args.output_dir.mkdir(parents=True, exist_ok=True)

    model_name = str(qa["whisper_model"])
    whisper = WhisperModel(model_name, device="cpu", compute_type="int8")
    report = {
        "episode_date": manifest["episode_date"],
        "voice_profile_version": director.version,
        "whisper_model": model_name,
        "status": "PASS",
        "episodes": [],
    }

    for item in manifest["episodes"]:
        slug = item["slug"]
        episode = json.loads((args.episode_dir / item["json"]).read_text(encoding="utf-8"))
        media_item = by_slug[slug]
        audio_path = args.media_dir / media_item["audio"]
        if not audio_path.is_file():
            raise FileNotFoundError(audio_path)

        decode_entire_file(audio_path)
        technical = probe_audio(audio_path)
        duration = float(technical["format"]["duration"])
        if not float(program["target_min_seconds"]) <= duration <= float(program["target_max_seconds"]):
            raise RuntimeError(f"{slug}: duration {duration:.2f}s outside production window")

        long_silences = detect_long_silence(audio_path, float(qa["max_silence_seconds"]))
        if long_silences:
            raise RuntimeError(f"{slug}: detected long silence(s): {long_silences[:5]}")

        mean_db, peak_db = volume_stats(audio_path)
        if not float(qa["min_mean_volume_db"]) <= mean_db <= float(qa["max_mean_volume_db"]):
            raise RuntimeError(f"{slug}: mean volume {mean_db:.2f}dB outside configured range")
        if peak_db > float(qa["max_peak_volume_db"]):
            raise RuntimeError(f"{slug}: peak volume {peak_db:.2f}dB is too close to clipping")

        plan = media_item.get("speech_plan") or []
        validate_speech_plan(director, episode, plan)
        samples = choose_asr_samples(plan, episode["language"], int(qa["asr_samples_per_episode"]))
        if len(samples) < 4:
            raise RuntimeError(f"{slug}: not enough suitable utterances for ASR sampling")

        full_audio = AudioSegment.from_file(audio_path, format="mp3")
        sample_results: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix=f"journey-talk-{slug}-qa-") as temp:
            temp_dir = Path(temp)
            for sample in samples:
                index = int(sample["index"])
                start = max(0, int(sample["start_ms"]) - 80)
                end = min(len(full_audio), int(sample["end_ms"]) + 80)
                heard = transcribe_clip(whisper, full_audio[start:end], sample["language"], temp_dir, index)
                expected_text = normalize_spoken_text(episode["utterances"][index]["text"])
                score = similarity(expected_text, heard)
                sample_results.append(
                    {
                        "index": index,
                        "language": sample["language"],
                        "intent": sample["intent"],
                        "similarity": round(score, 4),
                        "heard": heard[:300],
                    }
                )

        scores = [float(sample["similarity"]) for sample in sample_results]
        average = sum(scores) / len(scores)
        minimum = min(scores)
        if average < float(qa["asr_min_average_similarity"]):
            raise RuntimeError(f"{slug}: sampled ASR average similarity {average:.3f} is too low")
        if minimum < float(qa["asr_min_single_similarity"]):
            worst = min(sample_results, key=lambda sample: sample["similarity"])
            raise RuntimeError(
                f"{slug}: sampled ASR similarity {minimum:.3f} is too low at utterance {worst['index']}"
            )

        episode_report = {
            "slug": slug,
            "audio": media_item["audio"],
            "duration_seconds": round(duration, 3),
            "mean_volume_db": mean_db,
            "peak_volume_db": peak_db,
            "long_silences": long_silences,
            "asr_average_similarity": round(average, 4),
            "asr_min_similarity": round(minimum, 4),
            "asr_samples": sample_results,
            "status": "PASS",
        }
        report["episodes"].append(episode_report)
        (args.output_dir / f"{slug}-qa.json").write_text(
            json.dumps(episode_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"[qa] {slug}: PASS duration={duration:.1f}s mean={mean_db:.1f}dB "
            f"peak={peak_db:.1f}dB asr_avg={average:.3f}"
        )

    (args.output_dir / "qa-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
