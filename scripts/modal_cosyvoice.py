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

import cosyvoice_core as core

APP_NAME = "journey-talk-cosyvoice"
MODEL_DIR = "/models/cosyvoice3"
SOURCE_DIR = "/opt/CosyVoice"
# Training/serving extras the inference path does not need; they are the slowest and most fragile to install.
SKIP_REQUIREMENTS = "deepspeed|gradio|fastapi|uvicorn|tensorrt|tensorboard"


def download_model() -> None:
    from huggingface_hub import snapshot_download

    snapshot_download(core.MODEL_REPO, local_dir=MODEL_DIR)


image = (
    modal.Image.from_registry("nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04", add_python="3.10")
    .apt_install("git", "git-lfs", "sox", "libsox-dev", "ffmpeg", "build-essential")
    .run_commands(
        f"git clone --recursive {core.SOURCE_REPO} {SOURCE_DIR}",
        f"cd {SOURCE_DIR} && git checkout -q {core.SOURCE_COMMIT} && git submodule update --init --recursive",
        f"grep -viE '^({SKIP_REQUIREMENTS})' {SOURCE_DIR}/requirements.txt > /opt/requirements.txt",
        "pip install -r /opt/requirements.txt huggingface_hub",
    )
    # copy=True bakes the shared module into the image so the build step below can import this file too.
    .add_local_python_source("cosyvoice_core", copy=True)
    .run_function(download_model)
)

app = modal.App(APP_NAME, image=image)


@app.cls(gpu="A10G", timeout=3600, scaledown_window=60)
class CosyVoice:
    @modal.enter()
    def load(self) -> None:
        self.model = core.load(MODEL_DIR, SOURCE_DIR)

    @modal.method()
    def synthesize(self, lines: list[dict], voices: dict) -> list[bytes]:
        """lines: [{"speaker", "language", "text", "speed"}]; voices: {speaker: {"wav", "text", "language"}}.

        Returns one WAV per line (see cosyvoice_core.synthesize).
        """
        return core.synthesize(self.model, lines, voices)
