from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import edge_tts
from PIL import Image, ImageDraw, ImageFont
from pydub import AudioSegment


VOICE_MAP = {
    ("ja-JP", "MC_F"): "ja-JP-NanamiNeural",
    ("ja-JP", "MC_M"): "ja-JP-KeitaNeural",
    ("en-US", "MC_F"): "en-US-JennyNeural",
    ("en-US", "MC_M"): "en-US-GuyNeural",
    ("de-DE", "MC_F"): "de-DE-KatjaNeural",
    ("de-DE", "MC_M"): "de-DE-ConradNeural",
    ("es-ES", "MC_F"): "es-ES-ElviraNeural",
    ("es-ES", "MC_M"): "es-ES-AlvaroNeural",
    ("ru-RU", "MC_F"): "ru-RU-SvetlanaNeural",
    ("ru-RU", "MC_M"): "ru-RU-DmitryNeural",
    ("zh-CN", "MC_F"): "zh-CN-XiaoxiaoNeural",
    ("zh-CN", "MC_M"): "zh-CN-YunxiNeural",
    ("ko-KR", "MC_F"): "ko-KR-SunHiNeural",
    ("ko-KR", "MC_M"): "ko-KR-InJoonNeural",
}


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def render_cover(path: Path, episode_date: str) -> None:
    image = Image.new("RGB", (1280, 720), "#090d1a")
    draw = ImageDraw.Draw(image)
    for x in range(0, 1280, 32):
        color = (18 + (x // 32) % 12, 29, 55)
        draw.line((x, 0, x, 720), fill=color, width=1)
    draw.rounded_rectangle((90, 80, 1190, 640), radius=40, fill="#111a33", outline="#62e6ff", width=4)
    draw.text((140, 145), "JOURNEY TALK", font=_font(78), fill="#f6fbff")
    draw.text((145, 270), "世界のニュースで、毎日ことばを旅する。", font=_font(42), fill="#b6c8ff")
    draw.text((145, 390), "DE  ·  ES  ·  RU  ·  ZH  ·  KO", font=_font(42), fill="#62e6ff")
    draw.text((145, 505), episode_date, font=_font(46), fill="#f7c75e")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)


async def synthesize(utterances: list[dict], work_dir: Path) -> list[Path]:
    work_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, utterance in enumerate(utterances):
        key = (utterance["language"], utterance["speaker"])
        voice = VOICE_MAP.get(key)
        if voice is None:
            raise ValueError(f"No reviewed voice for {key}")
        path = work_dir / f"{index:03d}.mp3"
        await edge_tts.Communicate(utterance["text"], voice=voice).save(str(path))
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"TTS produced no audio for slot {utterance['slot_id']}")
        paths.append(path)
    return paths


def assemble_audio(paths: list[Path], utterances: list[dict], destination: Path, target_ms: int) -> int:
    combined = AudioSegment.empty()
    for path, utterance in zip(paths, utterances):
        combined += AudioSegment.from_file(path, format="mp3")
        combined += AudioSegment.silent(duration=utterance["pause_after_ms"])
    if len(combined) < 600_000 or len(combined) > 900_000:
        with tempfile.TemporaryDirectory(prefix="journey-talk-tempo-") as temp:
            raw = Path(temp) / "raw.wav"
            adjusted = Path(temp) / "adjusted.wav"
            combined.export(raw, format="wav")
            tempo = len(combined) / target_ms
            if not 0.5 <= tempo <= 2.0:
                raise RuntimeError(f"Required tempo adjustment {tempo:.3f} is outside ffmpeg atempo bounds")
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                 "-filter:a", f"atempo={tempo:.6f}", str(adjusted)],
                check=True,
            )
            combined = AudioSegment.from_file(adjusted, format="wav")
    destination.parent.mkdir(parents=True, exist_ok=True)
    combined.export(destination, format="mp3", bitrate="192k", tags={"artist": "Journey Talk"})
    return len(combined)


def render_video(cover: Path, audio: Path, destination: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-loop", "1", "-i", str(cover), "-i", str(audio),
            "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
            str(destination),
        ],
        check=True,
    )


def probe_seconds(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    episode = json.loads(args.episode.read_text(encoding="utf-8"))
    episode_date = episode["episode_date"]
    utterances = episode["utterances"]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    audio = args.output_dir / f"journey-talk-{episode_date}.mp3"
    cover = args.output_dir / f"journey-talk-{episode_date}.png"
    video = args.output_dir / f"journey-talk-{episode_date}.mp4"
    with tempfile.TemporaryDirectory(prefix="journey-talk-tts-") as temp:
        paths = asyncio.run(synthesize(utterances, Path(temp)))
        duration_ms = assemble_audio(paths, utterances, audio, int(episode["plan"].get("target_seconds", 720) * 1000))
    render_cover(cover, episode_date)
    render_video(cover, audio, video)

    audio_seconds = probe_seconds(audio)
    video_seconds = probe_seconds(video)
    if not 600 <= audio_seconds <= 900:
        raise RuntimeError(f"Audio duration {audio_seconds:.2f}s violates the 10-15 minute contract")
    if abs(audio_seconds - video_seconds) > 2:
        raise RuntimeError("Audio/video durations do not match")
    manifest = {
        "episode_date": episode_date,
        "audio": audio.name,
        "video": video.name,
        "cover": cover.name,
        "duration_seconds": round(audio_seconds, 3),
        "audio_bytes": audio.stat().st_size,
        "video_bytes": video.stat().st_size,
        "utterances": len(utterances),
    }
    (args.output_dir / "media-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
