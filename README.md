# Mobile ComfyUI

> Remote mobile/web interface for ComfyUI — multi-user, real-time, with Telegram & push notifications.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100%2B-009688?logo=fastapi)](https://fastapi.tiangolo.com)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?logo=windows)](https://www.microsoft.com/windows)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

A FastAPI server that turns your local [ComfyUI](https://github.com/comfyanonymous/ComfyUI) installation into a fully-featured remote panel — accessible from any phone or browser, anywhere in the world via Cloudflare/ngrok tunnel.

---

## Features

| Category | Details |
|---|---|
| **Multi-user auth** | Login, password hashing, 2FA (TOTP), per-device revocation |
| **Workflow execution** | Queue jobs, inject parameters, track real-time GPU progress via WebSocket |
| **Telegram** | Auto-send generated images to Telegram chat with configurable retry |
| **Web Push (iOS)** | VAPID protocol, Apple APNs compatible — no app needed |
| **Remote access** | Cloudflare Named Tunnel or ngrok with static domain |
| **Admin panel** | Live stats (GPU/CPU/VRAM), log tail, user management, queue control |
| **Gallery** | Per-user image storage, thumbnails, metadata, batch download |
| **PWA** | Service Worker, installable on phone home screen |

---

## Architecture

```
Browser / Phone
      │  HTTPS
      ▼
Cloudflare / ngrok tunnel
      │
      ▼
 FastAPI server  (:8001)
  ├── /kreator       ← Workflow builder UI
  ├── /panel         ← Admin dashboard
  ├── /generate      ← Job queue
  ├── /api/gallery   ← Image browser
  └── /api/push      ← Web Push subscriptions
      │
      ▼
 ComfyUI (:8188)  ← local GPU inference
```

---

## Quick Start

### Prerequisites
- Windows 10/11
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) installed (default: `C:\AI\New_Comfy`)
- Python (embedded in ComfyUI, or system Python 3.10+)

### Install

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/Mobile_ComfyUI.git
cd Mobile_ComfyUI

# 2. Copy config template
copy config.example.py config.py

# 3. Edit config.py — fill in Telegram token, paths, etc.
notepad config.py

# 4. Install dependencies
C:\AI\New_Comfy\python_embeded\python.exe -m pip install fastapi uvicorn python-multipart pillow requests cryptography pystray websocket-client qrcode

# 5. Start
Start.bat
```

The server starts on `http://0.0.0.0:8001`. Open `http://localhost:8001` in your browser.

### Automated installer (optional)

```bash
# Copy Instalator/ to your ComfyUI root, then:
Instaluj.bat
```

The wizard detects paths, installs dependencies, generates HTTPS cert and creates desktop shortcuts.

---

## HTTPS & Remote Access

### Cloudflare Tunnel (recommended — free, stable domain)

1. Create a tunnel at [dash.cloudflare.com](https://dash.cloudflare.com) → Zero Trust → Tunnels
2. Copy the token and paste it in `config.py`:

```python
CLOUDFLARE_TUNNEL_TOKEN = "your-token-here"
```

3. Start with `start_mobile_https.bat` — the public URL is sent to your Telegram automatically.

### ngrok (alternative)

See `docs/README_ngrok.txt` for free static-domain setup.

---

## Configuration

Copy `config.example.py` to `config.py` and fill in your values:

```python
# Telegram — send generated images to chat
TELEGRAM_BOT_TOKEN = "your-bot-token"
TELEGRAM_CHAT_ID   = "your-chat-id"

# Cloudflare Named Tunnel (leave empty for Quick Tunnel)
CLOUDFLARE_TUNNEL_TOKEN = ""

# ComfyUI location
COMFY_DIR  = r"C:\AI\New_Comfy"
COMFY_URL  = "127.0.0.1:8188"

# Server
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8001
```

> `config.py` is listed in `.gitignore` and will never be committed.

---

## User Management

Users and their settings are stored in `users.json` (gitignored). Add users via the admin panel at `/panel` or edit the file directly.

Each user has:
- Role (`admin` / `user`)
- Personal image directory
- Telegram credentials (optional override)
- Allowed workflows list
- ntfy.sh / Web Push notifications

---

## Project Structure

```
Mobile_ComfyUI/
├── serwer_comfy.py        # Main FastAPI app (50+ endpoints)
├── menedzer_tray.py       # System tray: monitor, tunnel, shortcuts
├── workflow_manager.py    # Workflow JSON loader/scanner
├── config.example.py      # Config template (copy → config.py)
├── kreator.html           # Workflow builder UI
├── push_helper.py         # VAPID Web Push implementation
├── core/
│   ├── users_store.py     # User account persistence
│   ├── settings_store.py  # Global settings
│   ├── history_store.py   # Generation history
│   └── subscriptions_store.py  # Push subscriptions
├── workflows/             # ComfyUI workflow JSON files
├── docs/
│   ├── README_cloudflare.txt
│   └── README_ngrok.txt
└── Instalator/            # Automated setup wizard
```

---

## API Overview

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/login` | Authenticate |
| `POST` | `/generate` | Queue a workflow job |
| `GET` | `/api/gallery` | Browse generated images |
| `GET` | `/api/workflows` | List available workflows |
| `GET` | `/api/stats` | GPU/CPU/queue stats |
| `WS` | `/ws/progress` | Real-time job progress |
| `POST` | `/api/push/subscribe` | Register Web Push |
| `GET` | `/panel` | Admin dashboard |

Full endpoint list: see `serwer_comfy.py`.

---

## License

MIT — see [LICENSE](LICENSE).
