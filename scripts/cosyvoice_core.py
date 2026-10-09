"""CosyVoice 3 (Alibaba, Apache-2.0) inference shared by every place it runs.

- Modal (scripts/modal_cosyvoice.py): free $30/month GPU credit.
- Kaggle (tts_engines.KaggleCosyVoiceTTS): free weekly GPU hours, started with `kaggle kernels push`.
- The GitHub Actions runner itself (tts_engines.LocalCosyVoiceTTS): CPU only, free and unlimited for this
  public repository, but slow.

Only the standard library is imported at module level; torch and CosyVoice load inside load().
"""
from __future__ import annotations

import io
import re
import sys
import tempfile
from pathlib import Path

MODEL_REPO = "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
SOURCE_REPO = "https://github.com/FunAudioLLM/CosyVoice.git"
# Pinned so a change upstream cannot silently break the daily run.
SOURCE_COMMIT = "074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc"
PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"

# Training / serving extras the inference path never imports; they are the slowest and most fragile to install.
SKIP_REQUIREMENTS = re.compile(
    r"^(deepspeed|gradio|fastapi|fastapi-cli|uvicorn|tensorrt|tensorrt-cu12.*|tensorboard|grpcio|grpcio-tools|onnxruntime-gpu)\b",
    re.I,
)

UNPIN = {"matplotlib"}


def inference_requirements(requirements_txt: str, *, skip: set[str] = frozenset()) -> list[str]:
    """CosyVoice's requirements.txt reduced to what inference needs (CPU onnxruntime instead of the GPU build).

    `skip` drops further packages by name, e.g. torch/torchaudio when the host already has a CUDA build.
    """
    out: list[str] = []
    for raw in requirements_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[=<>!~;\[ ]", line, maxsplit=1)[0].strip()
        if SKIP_REQUIREMENTS.match(name) or name.lower() in {x.lower() for x in skip}:
            continue
        if name.lower() == "onnxruntime":  # listed for macOS/Windows only; the CPU build is added below for every OS
            continue
        # The pinned matplotlib has no wheels for newer Pythons (e.g. Kaggle's); any version works for inference.
        out.append(name if name.lower() in UNPIN else line)
    out.append("onnxruntime==1.18.0")
    return out


def load(model_dir: str, source_dir: str):
    """Load the model; CosyVoice picks the GPU when there is one and falls back to CPU otherwise."""
    for path in (source_dir, f"{source_dir}/third_party/Matcha-TTS"):
        if path not in sys.path:
            sys.path.insert(0, path)
    from cosyvoice.cli.cosyvoice import AutoModel

    return AutoModel(model_dir=model_dir)


def synthesize(model, lines: list[dict], voices: dict, workdir: str | None = None) -> list[bytes]:
    """lines: [{"speaker", "language", "text", "speed"?}]; voices: {speaker: {"wav": bytes, "text": str, "language": str}}.

    Returns one 16-bit WAV per line. Lines in the reference clip's language are cloned zero-shot with the
    clip's transcript; other languages use cross-lingual cloning, which keeps the voice but not the accent.
    "speed" (default 1.0) sets the pace natively, so no time-stretching is needed afterwards.
    """
    import soundfile
    import torch

    workdir = workdir or tempfile.mkdtemp(prefix="cosyvoice-voices-")
    prompt_paths = {}
    for speaker, voice in voices.items():
        path = Path(workdir) / f"{speaker}.wav"
        path.write_bytes(voice["wav"])
        prompt_paths[speaker] = str(path)

    results: list[bytes] = []
    for line in lines:
        voice = voices[line["speaker"]]
        speed = float(line.get("speed", 1.0))
        if line["language"] == voice["language"]:
            chunks = model.inference_zero_shot(
                line["text"], PROMPT_PREFIX + voice["text"], prompt_paths[line["speaker"]], stream=False, speed=speed
            )
        else:
            chunks = model.inference_cross_lingual(
                PROMPT_PREFIX + line["text"], prompt_paths[line["speaker"]], stream=False, speed=speed
            )
        speech = torch.cat([chunk["tts_speech"] for chunk in chunks], dim=1)
        buffer = io.BytesIO()
        # soundfile instead of torchaudio.save: newer torchaudio releases moved saving to another package.
        soundfile.write(buffer, speech.cpu().numpy().T, model.sample_rate, format="WAV", subtype="PCM_16")
        results.append(buffer.getvalue())
    return results
