"""Runtime configuration. Everything runs locally; override via environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("FTR_DATA_DIR", ROOT / "data"))
DB_PATH = DATA_DIR / "records.db"

# Local LLM served by Ollama on this machine (no network egress).
OLLAMA_URL = os.getenv("FTR_OLLAMA_URL", "http://127.0.0.1:11434")
LLM_MODEL = os.getenv("FTR_LLM_MODEL", "qwen2.5:7b")
LLM_TIMEOUT_S = float(os.getenv("FTR_LLM_TIMEOUT", "120"))

# Speech-to-text (faster-whisper). large-v3-turbo handles Hindi/Hinglish well on GPU;
# on CPU-only machines set FTR_ASR_MODEL=small.
ASR_MODEL = os.getenv("FTR_ASR_MODEL", "large-v3-turbo")
ASR_DEVICE = os.getenv("FTR_ASR_DEVICE", "auto")  # auto | cuda | cpu

DEVICE_ID = os.getenv("FTR_DEVICE_ID", "laptop-01")

DATA_DIR.mkdir(parents=True, exist_ok=True)
