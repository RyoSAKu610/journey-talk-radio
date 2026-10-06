from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import edge_tts
from pydub import AudioSegment

from voice_director import VoiceDirector

ROOT = Path(__file__).resolve().parents[1]


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


async def _synthesize_one(
    director: VoiceDirector,
    episode: dict[str, Any],
    utterance: dict[str, Any],
    index: int,
    work: Path,
    previous: dict[str, Any] | None,
) -> tuple[Path, dict[str, Any]]:
    controls = director.controls(
        utterance,
        index,
        len(episode["utterances"]),
        previous=previous,
        episode_seed=f"{episode['episode_date']}:{episode['slug']}",
    )
    output = work / f"{index:03d}.mp3"
    max_attempts = int(director.config["qa"].get("max_failed_tts_attempts", 3))
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            if output.exists():
                output.unlink()
            communicator = edge_tts.Communicate(
                controls.tts_text,
                voice=controls.voice,
                rate=controls.rate,
                volume=controls.volume,
                pitch=controls.pitch,
            )
            await communicator.save(str(output))
            if not output.is_file() or output.stat().st_size < 700:
                raise RuntimeError("TTS output is missing or implausibly small")
            audio = AudioSegment.from_file(output, format="mp3")
            seconds = len(audio) / 1000.0
            qa = director.config["qa"]
            if not float(qa["min_segment_seconds"]) <= seconds <= float(qa["max_segment_seconds"]):
                raise RuntimeError(f"segment duration {seconds:.3f}s outside allowed range")
            return output, {
                "index": index,
                "speaker": utterance["speaker"],
                "language": utterance["language"],
                "intent": controls.intent,
                "voice": controls.voice,
                "rate": controls.rate,
                "volume": controls.volume,
                "pitch": controls.pitch,
                "pause_after_ms": controls.pause_after_ms,
                "segment_seconds": round(seconds, 3),
                "segment_bytes": output.stat().st_size,
            }
        except Exception as exc:  # network/TTS failures should be retried per utterance, not per episode
            last_error = exc
            if attempt == max_attempts:
                break
            await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"TTS failed for {episode['slug']} utterance {index} after {max_attempts} attempts: {last_error}")


async def synthesize_episode(
    director: VoiceDirector,
    episode: dict[str, Any],
    work: Path,
) -> tuple[list[Path], list[dict[str, Any]]]:
    paths: list[Path] = []
    plan: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for index, utterance in enumerate(episode["utterances"]):
        path, item = await _synthesize_one(director, episode, utterance, index, work, previous)
        paths.append(path)
        plan.append(item)
        previous = utterance
        # Be polite to the public Edge TTS endpoint and reduce burst failures.
        await asyncio.sleep(0.04)
    return paths, plan


def assemble(paths: list[Path], plan: list[dict[str, Any]]) -> tuple[AudioSegment, list[dict[str, Any]]]:
    audio = AudioSegment.empty()
    cursor_ms = 0
    resolved: list[dict[str, Any]] = []
    for path, item in zip(paths, plan):
        segment = AudioSegment.from_file(path, format="mp3")
        start_ms = cursor_ms
        audio += segment
        cursor_ms += len(segment)
        end_ms = cursor_ms
        pause_ms = int(item["pause_after_ms"])
        audio += AudioSegment.silent(duration=pause_ms)
        cursor_ms += pause_ms
        resolved_item = dict(item)
        resolved_item.update({"start_ms": start_ms, "end_ms": end_ms})
        resolved.append(resolved_item)
    return audio, resolved


def _first_pass_loudnorm(source: Path, target_i: float, target_tp: float) -> dict[str, float] | None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(source),
            "-af",
            f"loudnorm=I={target_i}:TP={target_tp}:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    matches = re.findall(r"\{\s*\"input_i\".*?\}", result.stderr, flags=re.S)
    if not matches:
        return None
    try:
        raw = json.loads(matches[-1])
        values = {
            "input_i": float(raw["input_i"]),
            "input_tp": float(raw["input_tp"]),
            "input_lra": float(raw["input_lra"]),
            "input_thresh": float(raw["input_thresh"]),
            "target_offset": float(raw["target_offset"]),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return values


def normalize_and_export(audio: AudioSegment, output: Path, director: VoiceDirector) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    program = director.config["program"]
    target_i = float(program["loudness_lufs"])
    target_tp = float(program["true_peak_db"])
    bitrate = str(program["mp3_bitrate"])
    with tempfile.TemporaryDirectory(prefix="journey-talk-normalize-") as temp_dir:
        source = Path(temp_dir) / "source.wav"
        audio.export(source, format="wav")
        measured = _first_pass_loudnorm(source, target_i, target_tp)
        if measured is None:
            filter_value = f"loudnorm=I={target_i}:TP={target_tp}:LRA=11"
            mode = "single-pass-fallback"
        else:
            filter_value = (
                f"loudnorm=I={target_i}:TP={target_tp}:LRA=11:"
                f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
                f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
                f"offset={measured['target_offset']}:linear=true:print_format=summary"
            )
            mode = "two-pass"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-af",
                filter_value,
                "-codec:a",
                "libmp3lame",
                "-b:a",
                bitrate,
                str(output),
            ],
            check=True,
        )
    return mode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=ROOT / "voice_profiles.yaml")
    args = parser.parse_args()

    director = VoiceDirector(args.profile)
    director.verify_voice_matrix()
    manifest = json.loads((args.episode_dir / "manifest.json").read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    media = {
        "episode_date": manifest["episode_date"],
        "voice_profile_version": director.version,
        "episodes": [],
    }
    minimum = float(director.config["program"]["target_min_seconds"])
    maximum = float(director.config["program"]["target_max_seconds"])

    for item in manifest["episodes"]:
        episode = json.loads((args.episode_dir / item["json"]).read_text(encoding="utf-8"))
        slug = item["slug"]
        output = args.output_dir / f"journey-talk-{manifest['episode_date']}-{slug}.mp3"
        print(f"[render] {slug}: {len(episode['utterances'])} utterances")
        with tempfile.TemporaryDirectory(prefix=f"journey-talk-{slug}-tts-") as temp_dir:
            paths, speech_plan = asyncio.run(synthesize_episode(director, episode, Path(temp_dir)))
            audio, speech_plan = assemble(paths, speech_plan)
            normalization = normalize_and_export(audio, output, director)

        duration = probe_duration(output)
        if not minimum <= duration <= maximum:
            raise RuntimeError(
                f"{slug}: rendered duration {duration:.2f}s outside production window {minimum:.0f}-{maximum:.0f}s; "
                "the script must be repaired instead of time-stretching audio"
            )
        media["episodes"].append(
            {
                "slug": slug,
                "language": episode["language"],
                "title": episode["title"],
                "audio": output.name,
                "duration_seconds": round(duration, 3),
                "bytes": output.stat().st_size,
                "normalization": normalization,
                "speech_plan": speech_plan,
            }
        )
        print(f"[audio] {slug}: PASS {duration:.2f}s, {normalization}")

    (args.output_dir / "media-manifest.json").write_text(
        json.dumps(media, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
