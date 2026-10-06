"""CosyVoice 3 (Alibaba, Apache-2.0) on Modal's serverless GPUs.

Deployed by the daily workflow with `modal deploy scripts/modal_cosyvoice.py` when the MODAL_TOKEN_ID and
MODAL_TOKEN_SECRET secrets exist; render_language_episodes.py then calls it through tts_engines.CosyVoiceModalTTS.
Modal's Starter plan includes $30 of compute a month without a credit card. One episode is one call, so the
model loads once per episode. When the credit runs out the call fails and the pipeline moves on to the next
engine, so this can never bill.

Each host's voice is cloned from a short reference clip (assets/voices/*.wav, made with the same Gemini voices),
so episodes sound like the same two hosts whichever engine rendered them.
"""
from __future__ import annotations

import modal

APP_NAME = "journey-talk-cosyvoice"
MODEL_REPO = "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
MODEL_DIR = "/models/cosyvoice3"
SOURCE_DIR = "/opt/CosyVoice"
PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"
# Training/serving extras the inference path does not need; they are the slowest and most fragile to install.
SKIP_REQUIREMENTS = "deepspeed|gradio|fastapi|uvicorn|tensorrt|tensorboard"


def download_model() -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(MODEL_REPO, local_dir=MODEL_DIR)


image = (
    modal.Image.from_registry("nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04", add_python="3.10")
    .apt_install("git", "git-lfs", "sox", "libsox-dev", "ffmpeg", "build-essential")
    .run_commands(
        f"git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git {SOURCE_DIR}",
        f"cd {SOURCE_DIR} && git submodule update --init --recursive",
        f"grep -viE '^({SKIP_REQUIREMENTS})' {SOURCE_DIR}/requirements.txt > /opt/requirements.txt",
        "pip install -r /opt/requirements.txt huggingface_hub",
    )
    .run_function(download_model)
)

app = modal.App(APP_NAME, image=image)


@app.cls(gpu="A10G", timeout=3600, scaledown_window=60)
class CosyVoice:
    @modal.enter()
    def load(self) -> None:
        import sys

        sys.path[:0] = [SOURCE_DIR, f"{SOURCE_DIR}/third_party/Matcha-TTS"]
        from cosyvoice.cli.cosyvoice import AutoModel

        self.model = AutoModel(model_dir=MODEL_DIR)

    @modal.method()
    def synthesize(self, lines: list[dict], voices: dict) -> list[bytes]:
        """lines: [{"speaker", "language", "text"}]; voices: {speaker: {"wav": bytes, "text": str, "language": str}}.

        Returns one WAV per line. Lines in the reference clip's language use zero-shot cloning with the clip's
        transcript; other languages use cross-lingual cloning, which keeps the voice but not the accent.
        """
        import io
        import tempfile

        import torch
        import torchaudio

        workdir = tempfile.mkdtemp(prefix="voices-")
        prompt_paths = {}
        for speaker, voice in voices.items():
            path = f"{workdir}/{speaker}.wav"
            with open(path, "wb") as handle:
                handle.write(voice["wav"])
            prompt_paths[speaker] = path

        results: list[bytes] = []
        for line in lines:
            voice = voices[line["speaker"]]
            if line["language"] == voice["language"]:
                chunks = self.model.inference_zero_shot(
                    line["text"], PROMPT_PREFIX + voice["text"], prompt_paths[line["speaker"]], stream=False
                )
            else:
                chunks = self.model.inference_cross_lingual(
                    PROMPT_PREFIX + line["text"], prompt_paths[line["speaker"]], stream=False
                )
            speech = torch.cat([chunk["tts_speech"] for chunk in chunks], dim=1)
            buffer = io.BytesIO()
            torchaudio.save(buffer, speech.cpu(), self.model.sample_rate, format="wav")
            results.append(buffer.getvalue())
        return results
