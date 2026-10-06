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
        if item["language"] == target_language
        and float(item["segment_seconds"]) >= 1.6
        and item["intent"] not in {"reaction"}
    ]
    ja_candidates = [
        item
        for item in plan
        if item["language"] == "ja-JP"
        and float(item["segment_seconds"]) >= 1.6
        and item["intent"] not in {"reaction"}
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


def issue(category: str, message: str, *, severity: str = "error", **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "category": category,
        "severity": severity,
        "message": message,
    }
    payload.update(details)
    return payload


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
        "issues": [],
        "episodes": [],
    }

    for item in manifest["episodes"]:
        slug = item["slug"]
        episode_issues: list[dict[str, Any]] = []
        metrics: dict[str, Any] = {}
        sample_results: list[dict[str, Any]] = []
        media_item = by_slug.get(slug)
        episode = json.loads((args.episode_dir / item["json"]).read_text(encoding="utf-8"))

        if not media_item:
            episode_issues.append(issue("missing_media_manifest", f"{slug}: media manifest entry is missing"))
            episode_report = {"slug": slug, "status": "FAIL", "issues": episode_issues}
            report["episodes"].append(episode_report)
            report["issues"].extend({"slug": slug, **entry} for entry in episode_issues)
            continue

        audio_path = args.media_dir / media_item["audio"]
        if not audio_path.is_file():
            episode_issues.append(issue("missing_audio", f"{slug}: rendered MP3 is missing", audio=str(audio_path)))
        else:
            try:
                decode_entire_file(audio_path)
            except Exception as exc:
                episode_issues.append(issue("decode", f"{slug}: full-file decode failed", error=str(exc)))

            try:
                technical = probe_audio(audio_path)
                duration = float(technical["format"]["duration"])
                metrics["duration_seconds"] = round(duration, 3)
                if not float(program["target_min_seconds"]) <= duration <= float(program["target_max_seconds"]):
                    episode_issues.append(
                        issue(
                            "duration",
                            f"{slug}: duration {duration:.2f}s outside production window",
                            observed=duration,
                            minimum=float(program["target_min_seconds"]),
                            maximum=float(program["target_max_seconds"]),
                            suggested_action="repair script length; do not time-stretch final audio",
                        )
                    )
            except Exception as exc:
                episode_issues.append(issue("probe", f"{slug}: ffprobe failed", error=str(exc)))

            try:
                long_silences = detect_long_silence(audio_path, float(qa["max_silence_seconds"]))
                metrics["long_silences"] = long_silences
                if long_silences:
                    episode_issues.append(
                        issue(
                            "silence",
                            f"{slug}: detected unnatural long silence(s)",
                            observed=long_silences[:10],
                            threshold=float(qa["max_silence_seconds"]),
                            suggested_action="rerender affected episode and inspect pause/segment boundaries",
                        )
                    )
            except Exception as exc:
                episode_issues.append(issue("silence_scan", f"{slug}: silence scan failed", error=str(exc)))

            try:
                mean_db, peak_db = volume_stats(audio_path)
                metrics["mean_volume_db"] = mean_db
                metrics["peak_volume_db"] = peak_db
                if not float(qa["min_mean_volume_db"]) <= mean_db <= float(qa["max_mean_volume_db"]):
                    episode_issues.append(
                        issue(
                            "mean_volume",
                            f"{slug}: mean volume is outside configured range",
                            observed=mean_db,
                            minimum=float(qa["min_mean_volume_db"]),
                            maximum=float(qa["max_mean_volume_db"]),
                            suggested_action="rerun normalization or repair source segment levels",
                        )
                    )
                if peak_db > float(qa["max_peak_volume_db"]):
                    episode_issues.append(
                        issue(
                            "peak_volume",
                            f"{slug}: peak level is too close to clipping",
                            observed=peak_db,
                            maximum=float(qa["max_peak_volume_db"]),
                            suggested_action="rerun normalization with safer headroom",
                        )
                    )
            except Exception as exc:
                episode_issues.append(issue("volume_scan", f"{slug}: volume scan failed", error=str(exc)))

            plan = media_item.get("speech_plan") or []
            try:
                validate_speech_plan(director, episode, plan)
            except Exception as exc:
                episode_issues.append(
                    issue(
                        "speech_plan",
                        str(exc),
                        suggested_action="rebuild render plan from current Voice Director profile before publishing",
                    )
                )

            if not any(entry["category"] in {"missing_audio", "decode", "speech_plan"} for entry in episode_issues):
                try:
                    samples = choose_asr_samples(plan, episode["language"], int(qa["asr_samples_per_episode"]))
                    if len(samples) < 4:
                        episode_issues.append(
                            issue(
                                "asr_sampling",
                                f"{slug}: not enough suitable utterances for ASR sampling",
                                observed=len(samples),
                                minimum=4,
                                suggested_action="repair turn structure or speech plan so enough representative samples exist",
                            )
                        )
                    else:
                        full_audio = AudioSegment.from_file(audio_path, format="mp3")
                        with tempfile.TemporaryDirectory(prefix=f"journey-talk-{slug}-qa-") as temp:
                            temp_dir = Path(temp)
                            for sample in samples:
                                index = int(sample["index"])
                                start = max(0, int(sample["start_ms"]) - 80)
                                end = min(len(full_audio), int(sample["end_ms"]) + 80)
                                heard = transcribe_clip(
                                    whisper,
                                    full_audio[start:end],
                                    sample["language"],
                                    temp_dir,
                                    index,
                                )
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
                        metrics["asr_average_similarity"] = round(average, 4)
                        metrics["asr_min_similarity"] = round(minimum, 4)
                        metrics["asr_samples"] = sample_results
                        if average < float(qa["asr_min_average_similarity"]):
                            episode_issues.append(
                                issue(
                                    "asr_average",
                                    f"{slug}: sampled ASR average similarity is too low",
                                    observed=round(average, 4),
                                    minimum=float(qa["asr_min_average_similarity"]),
                                    suggested_action="inspect low-scoring utterances, pronunciation normalization, and rerender only affected speech when possible",
                                )
                            )
                        single_limit = float(qa["asr_min_single_similarity"])
                        for sample in sample_results:
                            if float(sample["similarity"]) < single_limit:
                                episode_issues.append(
                                    issue(
                                        "asr_segment",
                                        f"{slug}: low ASR similarity at utterance {sample['index']}",
                                        utterance_index=int(sample["index"]),
                                        language=sample["language"],
                                        intent=sample["intent"],
                                        observed=float(sample["similarity"]),
                                        minimum=single_limit,
                                        heard=sample["heard"],
                                        expected=episode["utterances"][int(sample["index"])]["text"][:300],
                                        suggested_action="repair pronunciation text or rerender this utterance with conservative prosody; keep the rest of the episode",
                                    )
                                )
                except Exception as exc:
                    episode_issues.append(issue("asr_runtime", f"{slug}: ASR QA failed to run", error=str(exc)))

        episode_status = "PASS" if not episode_issues else "FAIL"
        episode_report = {
            "slug": slug,
            "audio": media_item.get("audio") if media_item else None,
            "status": episode_status,
            "metrics": metrics,
            "issues": episode_issues,
        }
        report["episodes"].append(episode_report)
        report["issues"].extend({"slug": slug, **entry} for entry in episode_issues)
        (args.output_dir / f"{slug}-qa.json").write_text(
            json.dumps(episode_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if episode_status == "PASS":
            print(
                f"[qa] {slug}: PASS duration={metrics.get('duration_seconds', 0):.1f}s "
                f"mean={metrics.get('mean_volume_db', 0):.1f}dB "
                f"peak={metrics.get('peak_volume_db', 0):.1f}dB "
                f"asr_avg={metrics.get('asr_average_similarity', 0):.3f}"
            )
        else:
            categories = ", ".join(entry["category"] for entry in episode_issues)
            print(f"[qa] {slug}: FAIL {categories}")

    if report["issues"]:
        report["status"] = "FAIL"
    (args.output_dir / "qa-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if report["status"] != "PASS":
        print(f"[qa] overall FAIL with {len(report['issues'])} actionable issue(s)")
        return 2
    print("[qa] overall PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
