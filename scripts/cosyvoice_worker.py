"""Batch CosyVoice 3 synthesis for several episodes in one model load.

Runs wherever a GPU or CPU is free: inside a Kaggle kernel (KaggleCosyVoiceTTS ships this file there) or in a
virtualenv on the GitHub Actions runner (LocalCosyVoiceTTS). Standard library only until --setup has
installed the inference dependencies.

    python cosyvoice_worker.py --setup --source-dir SRC --model-dir MODEL --torch-index URL
    python cosyvoice_worker.py --job job.json --out OUT --source-dir SRC --model-dir MODEL [--deadline EPOCH]

job.json: {"voices": {speaker: {"wav_b64", "text", "language"}},
           "episodes": [{"slug", "lines": [{"speaker", "language", "text", "speed"}]}]}
Each finished episode gets OUT/<slug>/NNN.flac plus OUT/<slug>/DONE; an episode cut off by the deadline
gets no DONE marker, so the caller hands it to the next engine.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cosyvoice_core as core  # noqa: E402


TORCH = ["torch==2.3.1", "torchaudio==2.3.1"]  # the versions CosyVoice pins


def setup(source_dir: Path, model_dir: Path, torch_index: str) -> None:
    """Fetch the pinned CosyVoice source, install inference dependencies and download the model (all idempotent)."""
    if not (source_dir / "cosyvoice").is_dir():
        subprocess.run(["git", "clone", "--recursive", core.SOURCE_REPO, str(source_dir)], check=True)
        subprocess.run(["git", "-C", str(source_dir), "checkout", "-q", core.SOURCE_COMMIT], check=True)
        subprocess.run(["git", "-C", str(source_dir), "submodule", "update", "--init", "--recursive"], check=True)
    pip = [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    # openai-whisper 20231117 builds with pkg_resources, which current setuptools no longer ships; PIP_CONSTRAINT
    # also applies inside pip's isolated build environments.
    constraints = Path(tempfile.mkdtemp(prefix="cosyvoice-pip-")) / "build-constraints.txt"
    constraints.write_text("setuptools<70\n", encoding="utf-8")
    env = {**os.environ, "PIP_CONSTRAINT": str(constraints)}
    # torch first from the matching index (CPU build on the Actions runner, CUDA build on Kaggle).
    subprocess.run([*pip, *TORCH, "--index-url", torch_index], check=True, env=env)
    requirements = core.inference_requirements(
        (source_dir / "requirements.txt").read_text(encoding="utf-8"), skip={"torch", "torchaudio"}
    )
    subprocess.run([*pip, *requirements, "huggingface_hub"], check=True, env=env)
    if not (model_dir / "cosyvoice3.yaml").is_file():
        from huggingface_hub import snapshot_download

        snapshot_download(core.MODEL_REPO, local_dir=str(model_dir))


def run(job: dict, out: Path, source_dir: Path, model_dir: Path, deadline: float | None) -> int:
    import soundfile

    started = time.time()
    model = core.load(str(model_dir), str(source_dir))
    print(f"[cosyvoice] model loaded in {time.time() - started:.0f}s", flush=True)
    voices = {
        speaker: {"wav": base64.b64decode(v["wav_b64"]), "text": v["text"], "language": v["language"]}
        for speaker, v in job["voices"].items()
    }
    workdir = tempfile.mkdtemp(prefix="cosyvoice-voices-")
    finished = 0
    for episode in job["episodes"]:
        target = out / episode["slug"]
        target.mkdir(parents=True, exist_ok=True)
        episode_start, audio_seconds = time.time(), 0.0
        for index, line in enumerate(episode["lines"]):
            if deadline and time.time() > deadline:
                print(f"[cosyvoice] deadline reached in {episode['slug']} at line {index}; stopping", flush=True)
                return finished
            clip = target / f"{index:03d}.flac"
            if clip.is_file():
                continue
            (wav,) = core.synthesize(model, [line], voices, workdir)
            data, rate = soundfile.read(io.BytesIO(wav))
            soundfile.write(str(clip), data, rate, format="FLAC")
            audio_seconds += len(data) / rate
        (target / "DONE").write_text("ok\n", encoding="utf-8")
        finished += 1
        took = time.time() - episode_start
        rtf = took / audio_seconds if audio_seconds else 0.0
        print(f"[cosyvoice] {episode['slug']}: {len(episode['lines'])} lines, {audio_seconds:.0f}s audio in {took:.0f}s (RTF {rtf:.2f})", flush=True)
    return finished


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--job", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--torch-index", default="https://download.pytorch.org/whl/cpu")
    parser.add_argument("--deadline", type=float, default=0.0)
    args = parser.parse_args()
    if args.setup:
        setup(args.source_dir, args.model_dir, args.torch_index)
        return 0
    if not args.job or not args.out:
        parser.error("--job and --out are required")
    job = json.loads(args.job.read_text(encoding="utf-8"))
    finished = run(job, args.out, args.source_dir, args.model_dir, args.deadline or None)
    print(f"[cosyvoice] {finished}/{len(job['episodes'])} episodes finished", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
