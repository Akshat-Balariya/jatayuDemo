"""Offline speech-to-text with faster-whisper (English / Hindi / Hinglish)."""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import config

# Bias Whisper towards romanised Hinglish + medical vocabulary instead of translating or
# forcing everything into Devanagari.
HINGLISH_PROMPT = (
    "Patient ko adrenaline 0.5 mg IM diya. Aspirin 300 mg chewed. IV cannula lagaya, NS start kiya. "
    "BP 120/80, SpO2 96 percent, GCS 15. Nitroglycerin sublingual, ondansetron 4 mg IV."
)
ENGLISH_PROMPT = (
    "Epinephrine 0.3 mg IM, aspirin 324 mg PO, nitroglycerin 0.4 mg SL, naloxone 2 mg intranasal, "
    "IV 18 gauge left AC, normal saline bolus, BP 120/80, SpO2 96 percent."
)

_model = None
_model_info: dict = {}
_lock = threading.Lock()


def _add_cuda_dll_dirs() -> None:
    """On Windows, CUDA/cuDNN come from the nvidia-* pip wheels; expose their DLLs."""
    if sys.platform != "win32":
        return
    try:
        import nvidia  # type: ignore
    except ImportError:
        return
    for base in nvidia.__path__:
        for sub in ("cublas", "cudnn", "cuda_runtime"):
            d = Path(base) / sub / "bin"
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def get_model():
    global _model, _model_info
    with _lock:
        if _model is not None:
            return _model
        _add_cuda_dll_dirs()
        from faster_whisper import WhisperModel

        attempts = []
        if config.ASR_DEVICE in ("auto", "cuda"):
            attempts.append(("cuda", "float16"))
        if config.ASR_DEVICE in ("auto", "cpu"):
            attempts.append(("cpu", "int8"))
        last = None
        for device, compute in attempts:
            try:
                try:  # offline first: use the cached model without touching the network
                    _model = WhisperModel(config.ASR_MODEL, device=device, compute_type=compute, local_files_only=True)
                except Exception:
                    _model = WhisperModel(config.ASR_MODEL, device=device, compute_type=compute)
                _model_info = {"asr_model": config.ASR_MODEL, "asr_device": device}
                return _model
            except Exception as e:  # CUDA libs missing etc. -> fall back to CPU
                last = e
        raise RuntimeError(f"Could not load Whisper model: {last}")


def transcribe(audio: bytes, language: str = "auto") -> dict:
    """language: auto | en | hi | hinglish."""
    model = get_model()
    audio_hash = hashlib.sha256(audio).hexdigest()
    lang = {"en": "en", "hi": "hi", "hinglish": None, "auto": None}.get(language)
    prompt = ENGLISH_PROMPT if language == "en" else HINGLISH_PROMPT
    with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as f:
        f.write(audio)
        path = f.name
    t0 = time.perf_counter()
    try:
        segments, info = model.transcribe(
            path, language=lang, task="transcribe", initial_prompt=prompt,
            beam_size=5, vad_filter=True, condition_on_previous_text=False,
        )
        segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
    finally:
        os.unlink(path)
    return {
        "text": " ".join(s["text"] for s in segs).strip(),
        "language": info.language,
        "language_probability": round(info.language_probability, 2),
        "duration_s": round(info.duration, 1),
        "asr_latency_s": round(time.perf_counter() - t0, 2),
        "segments": segs,
        "audio_sha256": audio_hash,
        **_model_info,
    }


def health() -> dict:
    return {"loaded": _model is not None, **(_model_info or {"asr_model": config.ASR_MODEL})}
