# ============================================================
#  config.example.py – Skopiuj jako config.py i uzupełnij dane
#  config.py jest w .gitignore i NIE trafia do repozytorium!
# ============================================================
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# --- ComfyUI ---
COMFY_URL = "127.0.0.1:8188"

# --- Katalog z obrazami ---
IMAGE_DIR = os.environ.get("COMFY_IMAGE_DIR", str(BASE_DIR / "images"))

# --- Pliki workflow ---
WORKFLOW_1 = str(BASE_DIR / "workflows" / "Ricky_v4.json")
WORKFLOW_2 = str(BASE_DIR / "workflows" / "PhotoRicky_v1.0.json")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = "WKLEJ_TOKEN_BOTA_TUTAJ"
TELEGRAM_CHAT_ID   = "WKLEJ_CHAT_ID_TUTAJ"

# --- Ścieżki ---
COMFY_DIR         = os.environ.get("COMFY_DIR", r"C:\AI\New_Comfy")
PYTHON_EXE        = os.environ.get("PYTHON_EXE", r"C:\AI\New_Comfy\python_embeded\python.exe")
SERVER_DIR        = str(BASE_DIR)
CLOUDFLARED_EXE   = os.environ.get("CLOUDFLARED_EXE", r"C:\AI\cloudflared.exe")

# --- Cloudflare Named Tunnel (zostaw puste = Quick Tunnel) ---
CLOUDFLARE_TUNNEL_TOKEN = ""

# --- Serwer FastAPI ---
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8001

# --- Limity ---
MAX_IMAGE_SIZE     = 1500
JPEG_QUALITY       = 90
TELEGRAM_RETRY     = 3
TELEGRAM_RETRY_DELAY = 3
VRAM_FREE_WAIT     = 3
