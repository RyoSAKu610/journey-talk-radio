from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import edge_tts

from voice_director import VoiceDirector

ROOT = Path(__file__).resolve().parents[1]


async def check(director: VoiceDirector) -> None:
    expected = set(director.verify_voice_matrix())
    voices = await edge_tts.list_voices()
    available = {str(item.get("ShortName", "")) for item in voices}
    missing = sorted(expected - available)
    if missing:
        raise RuntimeError(f"Configured Edge TTS voices are unavailable: {missing}")
    print(f"[voices] PASS: {len(expected)} configured voices are available")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, default=ROOT / "voice_profiles.yaml")
    args = parser.parse_args()
    director = VoiceDirector(args.profile)
    asyncio.run(check(director))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
