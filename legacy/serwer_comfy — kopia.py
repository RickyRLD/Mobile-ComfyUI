import json
import urllib.request
import urllib.parse
import os
import uuid
import random
import io
import time
import base64
import asyncio
import requests
import subprocess
import signal
import logging
import sys
import threading
from datetime import datetime
from PIL import Image
from fastapi import FastAPI, File, Form, UploadFile, BackgroundTasks, Cookie
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
import hashlib, secrets

import workflow_manager as wm
try:
    import push_helper as _push
    _PUSH_AVAILABLE = True
except ImportError:
    _PUSH_AVAILABLE = False

# === KONFIGURACJA (z config.py) ===
try:
    from config import (
        COMFY_URL, IMAGE_DIR, WORKFLOW_1, WORKFLOW_2,
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
        MAX_IMAGE_SIZE, JPEG_QUALITY, TELEGRAM_RETRY, TELEGRAM_RETRY_DELAY, VRAM_FREE_WAIT
    )
except ImportError:
    COMFY_URL            = "127.0.0.1:8188"
    IMAGE_DIR            = r"C:\AI\IMAGES\OlaPL"
    WORKFLOW_1           = "workflows/Ricky_v4.json"
    WORKFLOW_2           = "workflows/PhotoRicky_v1.0.json"
    TELEGRAM_BOT_TOKEN   = ""
    TELEGRAM_CHAT_ID     = "8011392687"
    MAX_IMAGE_SIZE       = 1500
    JPEG_QUALITY         = 90
    TELEGRAM_RETRY       = 3
    TELEGRAM_RETRY_DELAY = 3
    VRAM_FREE_WAIT       = 3

# === KONFIGURACJA LOGOW ===
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serwer_comfy.log")

# Rotacja logu - max 300KB, 1 backup = zawsze swiezy log
from logging.handlers import RotatingFileHandler as _RFH
_log_handler = _RFH(LOG_FILE, maxBytes=300_000, backupCount=1, encoding="utf-8")
_log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        _log_handler,
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("comfy")
log.info(f"=== SERWER URUCHOMIONY === Log: {LOG_FILE}")

# Push przy starcie (po załadowaniu konfiguracji - wywołamy później przez lifespan)
def _push_server_start():
    try:
        send_push_to_all("🟢 Serwer uruchomiony", "ComfyUI Mobile jest gotowy")
    except Exception:
        pass

# Auto-sprzątanie starych plików (>7 dni) przy starcie
def cleanup_old_files():
    try:
        cutoff = time.time() - 7 * 24 * 3600
        cleaned = 0
        for fname in os.listdir(IMAGE_DIR):
            if not (fname.startswith("telegram_result_") or fname.startswith("mobile_")):
                continue
            fpath = os.path.join(IMAGE_DIR, fname)
            if os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                cleaned += 1
        if cleaned:
            log.info(f"Auto-sprzatanie: usunieto {cleaned} starych plikow (>7 dni)")
    except Exception as e:
        log.warning(f"Auto-sprzatanie blad: {e}")

try:
    cleanup_old_files()
except Exception:
    pass

import requests
from io import BytesIO
from PIL import Image
import os


# Wczytaj ustawienia z settings.json (nadpisuje wartosci z config.py)
_startup_settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
if os.path.exists(_startup_settings_path):
    try:
        with open(_startup_settings_path, "r", encoding="utf-8") as _f:
            _s = json.load(_f)
        if _s.get("telegram_token"):  TELEGRAM_BOT_TOKEN = _s["telegram_token"]
        if _s.get("telegram_chat_id"): TELEGRAM_CHAT_ID  = _s["telegram_chat_id"]
        if _s.get("image_dir"):        IMAGE_DIR          = _s["image_dir"]
        if _s.get("comfy_url"):        COMFY_URL          = _s["comfy_url"]
        if _s.get("vram_free_wait"):   VRAM_FREE_WAIT     = _s["vram_free_wait"]
        log.info("Wczytano ustawienia z settings.json")
    except Exception as _e:
        log.warning(f"Blad wczytywania settings.json: {_e}")

app = FastAPI()

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Sprawdza autoryzację dla każdego żądania."""
    path = request.url.path
    # Ścieżki zawsze dostępne bez logowania
    public_paths = {"/login", "/logout", "/favicon.ico", "/manifest.json", "/sw.js", "/icon-192.png"}
    if path in public_paths or path.startswith("/static"):
        return await call_next(request)
    # Sprawdź sesję
    if not check_auth(request):
        if path.startswith("/api") or path.startswith("/generate") or path.startswith("/sse"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"status": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/login", status_code=302)
    return await call_next(request)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# === STAN GLOBALNY ===
app.state.should_stop     = False
app.state.is_processing   = False
app.state.status_text     = "Serwer gotowy. Czekam na pliki z telefonu..."
app.state.current_iter    = 0
app.state.total_iter      = 0
app.state.last_workflow   = None
app.state.current_node    = None
app.state.step_value      = 0
app.state.step_max        = 0
app.state.remote_url      = None
app.state.qr_base64_cache = None  # cache – generowany raz przy ustawieniu URL
app.state.preview_b64     = None  # live preview z ComfyUI (base64 JPEG)
app.state.processing_uid  = ""    # uid użytkownika który aktualnie generuje
app.state.current_style   = ""    # wylosowany styl z RandomOrManual3LevelChoicesRelaxed

_comfy_ws_active = False  # True jeśli websocket-client zainstalowany i działa


# =========================================================
# SSE – Server-Sent Events (live status, bez dodatkowych pakietow)
# =========================================================
def make_sse_payload() -> str:
    data = {
        "status_text":   app.state.status_text,
        "current_iter":  app.state.current_iter,
        "total_iter":    app.state.total_iter,
        "is_processing": app.state.is_processing,
        "current_node":  app.state.current_node,
        "step_value":    app.state.step_value,
        "step_max":      app.state.step_max,
        "current_style": app.state.current_style,
    }
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

@app.get("/api/sse")
async def sse_stream():
    """Server-Sent Events – live status dla klienta mobilnego, co 1 sekunde."""
    async def generator():
        try:
            while True:
                yield make_sse_payload()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# =========================================================
# OPCJONALNY WATEK: ODCZYT KROKOW GPU Z COMFYUI WEBSOCKET
# Wymaga: pip install websocket-client
# Bez niego: krok GPU pobierany z /history (mniej precyzyjny)
# =========================================================
def comfy_ws_listener():
    global _comfy_ws_active
    import websocket as ws_lib
    client_id = str(uuid.uuid4())
    url       = f"ws://{COMFY_URL}/ws?clientId={client_id}"

    def on_message(ws, message):
        # ComfyUI wysyla 2 typy wiadomosci przez WS:
        # 1) JSON tekstowy: {"type":"progress",...} / {"type":"executing",...}
        # 2) Binarne preview obrazu (kazdej klatki diffusji)
        #
        # Problem: websocket-client dostarcza binarne dane czasem jako bytes,
        # czasem jako str (gdy skip_utf8_validation=True). Dlatego NIE uzywamy
        # isinstance(bytes) – zamiast tego prubujemy zdekodowac i sprawdzamy '{'.
        try:
            if isinstance(message, bytes):
                # ComfyUI wysyla binarne preview: 8 bajtow naglowka + JPEG
                if len(message) > 8:
                    import base64 as _b64
                    try:
                        # Pierwsze 4 bajty = typ (1=JPEG preview), kolejne 4 = indeks
                        msg_type_int = int.from_bytes(message[:4], 'big')
                        if msg_type_int == 1:  # JPEG preview
                            jpeg_data = message[8:]
                            app.state.preview_b64 = _b64.b64encode(jpeg_data).decode()
                    except Exception:
                        pass
                try:
                    msg_str = message.decode("utf-8")
                except Exception:
                    return
            else:
                msg_str = message

            # Binarne preview dostarczone jako str nie zaczyna sie od '{'
            if not msg_str.lstrip().startswith("{"):
                return

            data     = json.loads(msg_str)
            msg_type = data.get("type", "")
            if msg_type == "progress":
                d = data.get("data", {})
                app.state.step_value = d.get("value", 0)
                app.state.step_max   = d.get("max", 0)
                node = d.get("node")
                if node:
                    app.state.current_node = f"Node {node}"
            elif msg_type == "executing":
                node = data.get("data", {}).get("node")
                if node:
                    app.state.current_node = f"Node {node}"
                elif node is None and app.state.is_processing:
                    app.state.current_node = "Gotowe"
            elif msg_type == "execution_start":
                app.state.current_node = "Startuje..."
                app.state.step_value   = 0
                app.state.step_max     = 0
        except Exception:
            pass  # binarne dane – cicho ignoruj, nie zasmiecaj logow

    def on_open(ws):
        log.info("comfy_ws: polaczono z ComfyUI WebSocket")

    def on_close(ws, *args):
        log.debug("comfy_ws: rozlaczono, retry za 5s...")
        time.sleep(5)
        _start_ws()

    def on_error(ws, error):
        log.debug(f"comfy_ws error: {error}")

    try:
        ws_app = ws_lib.WebSocketApp(url, on_open=on_open, on_message=on_message,
                                      on_close=on_close, on_error=on_error)
        _comfy_ws_active = True
        ws_app.run_forever(ping_interval=30, skip_utf8_validation=True)
    except Exception as e:
        log.warning(f"comfy_ws start blad: {e}, retry za 5s")
        time.sleep(5)
        _start_ws()

def _start_ws():
    t = threading.Thread(target=comfy_ws_listener, daemon=True)
    t.start()

def _fallback_queue_poller():
    """Fallback – polling /queue co 2s gdy brak websocket-client."""
    while True:
        if app.state.is_processing:
            try:
                req    = urllib.request.Request(f"http://{COMFY_URL}/queue")
                q_data = json.loads(urllib.request.urlopen(req, timeout=2).read())
                running = q_data.get("queue_running", [])
                if running:
                    app.state.current_node = "Pracuje..."
                elif not app.state.is_processing:
                    app.state.current_node = "Gotowe"
            except Exception:
                pass
        time.sleep(2)

@app.on_event("startup")
async def on_startup():
    try:
        import websocket  # noqa: F401
        _start_ws()
        log.info("OK: websocket-client dostepny – live krok GPU przez WS")
    except ImportError:
        log.warning("BRAK websocket-client – fallback polling. Zainstaluj: pip install websocket-client")
        t = threading.Thread(target=_fallback_queue_poller, daemon=True)
        t.start()
    # Push przy starcie (w osobnym wątku żeby nie blokować startu)
    threading.Thread(target=_push_server_start, daemon=True).start()
    init_default_admin()
    _start_tg_watchdog()

@app.on_event("shutdown")
async def on_shutdown():
    try:
        send_push_to_all("🔴 Serwer zatrzymany", "ComfyUI Mobile jest offline")
    except Exception:
        pass


# =========================================================
# FUNKCJE POMOCNICZE
# =========================================================

def get_gpu_info():
    try:
        output = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total',
             '--format=csv,noheader,nounits'],
            encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW
        )
        temp, util, mem_used, mem_total = output.strip().split(', ')
        return {"temp": f"{temp}C", "util": f"{util}%", "vram": f"{mem_used} / {mem_total} MB"}
    except Exception as e:
        log.warning(f"get_gpu_info blad: {e}")
        return {"temp": "N/A", "util": "N/A", "vram": "N/A"}

def get_comfy_realtime_status():
    try:
        req  = urllib.request.Request(f"http://{COMFY_URL}/queue")
        resp = json.loads(urllib.request.urlopen(req, timeout=1).read())
        running = len(resp.get("queue_running", []))
        pending = len(resp.get("queue_pending", []))
        if running > 0:
            return f"Pracuje (Zadan: {running + pending})"
        elif pending > 0:
            return f"W kolejce (Zadan: {pending})"
        else:
            return "Gotowy (Czeka na zadanie)"
    except Exception:
        return "Uruchamianie (lub wylaczony)..."

def comfy_get(path: str, timeout: int = 5):
    """Bezpieczny GET do ComfyUI z retry przy ConnectionReset (WinError 10054)."""
    for attempt in range(3):
        try:
            req  = urllib.request.Request(f"http://{COMFY_URL}{path}")
            resp = urllib.request.urlopen(req, timeout=timeout)
            return json.loads(resp.read())
        except ConnectionResetError as e:
            log.warning(f"comfy_get {path}: ConnectionReset (proba {attempt+1}/3): {e}")
            time.sleep(1)
        except Exception as e:
            log.debug(f"comfy_get {path}: blad: {e}")
            return None
    return None

def send_telegram_photo(out_path: str, iter_num: int, tg_token: str = "", tg_chat: str = ""):
    """Wysyla zdjecie na Telegram. Jesli za duze - kompresuje do <10MB."""
    _TG_TOKEN = tg_token or _TG_TOKEN
    _TG_CHAT  = tg_chat  or _TG_CHAT
    if not _TG_TOKEN or not _TG_CHAT:
        return
    TELEGRAM_MAX_BYTES = 10 * 1024 * 1024  # 10MB limit Telegrama

    def compress_to_limit(path, max_bytes):
        """Kompresuje JPEG az zmiesci sie w limicie."""
        img = Image.open(path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        for quality in [85, 75, 65, 55, 45, 35]:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            if buf.tell() <= max_bytes:
                buf.seek(0)
                return buf
            # Dodatkowo zmniejsz rozmiar jesli nadal za duze
            if quality <= 45:
                w, h = img.size
                img = img.resize((int(w*0.8), int(h*0.8)), Image.Resampling.LANCZOS)
        buf.seek(0)
        return buf

    file_size = os.path.getsize(out_path)
    url_photo = f"https://api.telegram.org/bot{_TG_TOKEN}/sendPhoto"

    for attempt in range(1, TELEGRAM_RETRY + 1):
        try:
            if file_size > TELEGRAM_MAX_BYTES:
                log.info(f"Iter {iter_num}: Plik {file_size//1024}KB > 10MB, kompresuje...")
                photo_data = compress_to_limit(out_path, TELEGRAM_MAX_BYTES)
                tg_resp = requests.post(url_photo, data={'chat_id': _TG_CHAT},
                                        files={'photo': ('photo.jpg', photo_data, 'image/jpeg')}, timeout=30)
            else:
                with open(out_path, 'rb') as photo:
                    tg_resp = requests.post(url_photo, data={'chat_id': _TG_CHAT},
                                            files={'photo': photo}, timeout=30)
            log.info(f"Iter {iter_num}: Telegram [{attempt}] status={tg_resp.status_code}")
            if tg_resp.ok:
                return True
            log.warning(f"Iter {iter_num}: Telegram blad HTTP {tg_resp.status_code}: {tg_resp.text[:200]}")
        except Exception as e:
            log.warning(f"Iter {iter_num}: Telegram wyjatek [{attempt}/{TELEGRAM_RETRY}]: {e}")
        if attempt < TELEGRAM_RETRY:
            time.sleep(TELEGRAM_RETRY_DELAY)
    log.error(f"Iter {iter_num}: Nie udalo sie wyslac na Telegram po {TELEGRAM_RETRY} probach.")
    return False


# =========================================================
# BACKGROUND – GLOWNA PETLA GENEROWANIA
# =========================================================
def process_in_background(workflow_template: dict, iterations: int, workflow_type: str):
    log.info(f"=== BACKGROUND START: workflow={workflow_type}, iterations={iterations} ===")
    app.state.is_processing = True
    app.state.total_iter    = iterations

    try:
        for i in range(iterations):
            if app.state.should_stop:
                log.info(f"Iter {i+1}: should_stop=True, przerywam.")
                app.state.status_text = "Przerwano recznie!"
                break

            app.state.current_iter = i + 1
            app.state.status_text  = "Przygotowywanie wezlow..."
            app.state.current_node = None
            app.state.step_value   = 0
            app.state.step_max     = 0
            log.info(f"--- Iter {i+1}/{iterations} ---")

            seed = random.randint(1, 999999999999999)
            if workflow_type == "Ricky_v4":
                workflow_template["9:209:211"]["inputs"]["seed"] = seed
            elif workflow_type == "PhotoRicky_v1.0":
                workflow_template["9:225:227"]["inputs"]["noise_seed"] = seed
            log.debug(f"Iter {i+1}: seed={seed}")

            log.info(f"Iter {i+1}: Wysylanie prompt do ComfyUI...")
            # Auto-fix: zamień seed=-1 na losowy
            for _nid, _node in workflow_template.items():
                for _sf in ("seed", "noise_seed"):
                    if isinstance(_node, dict) and _sf in _node.get("inputs", {}) and _node["inputs"][_sf] == -1:
                        _node["inputs"][_sf] = random.randint(1, 999999999999999)
            data      = json.dumps({"prompt": workflow_template})
            _resp = requests.post(
                f"http://{COMFY_URL}/prompt",
                data=data,
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            if not _resp.ok:
                log.error(f"V2 ComfyUI HTTP {_resp.status_code} body: {_resp.text[:3000]}")
                raise Exception(f"ComfyUI HTTP {_resp.status_code}: {_resp.text[:500]}")
            response  = _resp.json()
            prompt_id = response['prompt_id']
            log.info(f"Iter {i+1}: prompt_id={prompt_id}")

            app.state.status_text = "Generowanie w ComfyUI (praca karty GPU)..."
            out_filenames = []
            poll_count    = 0

            # PETLA OCZEKIWANIA – naprawiony WinError 10054
            # comfy_get() uzywa timeout=5 i robi retry przy ConnectionReset
            # Krok GPU i node aktualizowane przez WS listener (lub fallback)
            while True:
                if app.state.should_stop:
                    app.state.status_text = "Przerwano recznie!"
                    break

                poll_count += 1
                if poll_count % 10 == 0:
                    log.info(f"Iter {i+1}: poll #{poll_count} (~{poll_count*2}s)")

                hist_resp = comfy_get(f"/history/{prompt_id}", timeout=5)

                if hist_resp is None:
                    time.sleep(2)
                    continue

                if prompt_id in hist_resp:
                    log.info(f"Iter {i+1}: Historia gotowa po {poll_count} pollach.")
                    app.state.status_text  = "Wysylanie wynikow na Telegram..."
                    app.state.current_node = "Gotowe"
                    outputs = hist_resp[prompt_id]['outputs']

                    # Fallback: odczyt node z historii gdy brak WS
                    if not _comfy_ws_active:
                        try:
                            msgs = hist_resp[prompt_id].get("status", {}).get("messages", [])
                            for msg_type, msg_data in msgs:
                                if msg_type == "executing" and isinstance(msg_data, dict) and msg_data.get("node"):
                                    app.state.current_node = f"Node {msg_data['node']}"
                        except Exception:
                            pass

                    for node_id in ["60", "74", "77"]:
                        if node_id in outputs and 'images' in outputs[node_id]:
                            for img_data in outputs[node_id]['images']:
                                out_filenames.append(img_data['filename'])
                                log.debug(f"Iter {i+1}: node={node_id}, plik={img_data['filename']}")
                    break

                time.sleep(2)

            log.info(f"Iter {i+1}: out_filenames={out_filenames}")
            for out_filename, out_subfolder, out_type in out_filenames:
                if app.state.should_stop:
                    break

                log.info(f"Iter {i+1}: Pobieranie pliku: {out_filename}")
                url_img = f"http://{COMFY_URL}/view?filename={urllib.parse.quote(out_filename)}&type={out_type}"
                if out_subfolder:
                    url_img += f"&subfolder={urllib.parse.quote(out_subfolder)}"
                req_img  = urllib.request.Request(url_img)
                out_path = os.path.join(IMAGE_DIR, f"telegram_result_{uuid.uuid4().hex[:8]}.jpg")
                _raw = urllib.request.urlopen(req_img, timeout=30).read()
                # Konwertuj do JPEG (ComfyUI może zwrócić PNG)
                try:
                    _pil = Image.open(io.BytesIO(_raw))
                    if _pil.mode in ("RGBA", "P", "LA"):
                        _pil = _pil.convert("RGB")
                    _pil.save(out_path, format="JPEG", quality=92)
                except Exception:
                    with open(out_path, 'wb') as f:
                        f.write(_raw)
                log.info(f"Iter {i+1}: Zapisano: {out_path}")

                if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                    send_telegram_photo(out_path, i + 1)
                # Push notification - tylko do właściciela generowania
                try:
                    if _uid:
                        send_push_to_user(_uid, "✅ Gotowe!",
                                         f"Obraz {i+1}/{iterations} wygenerowany")
                    else:
                        send_push_to_all("✅ Generowanie gotowe",
                                        f"Iter {i+1}/{iterations} – {workflow_id}")
                except Exception:
                    pass

    except Exception as e:
        log.exception(f"BLAD KRYTYCZNY w process_in_background: {e}")
        app.state.status_text = "Wystapil blad podczas generowania."
    finally:
        log.info("=== BACKGROUND KONIEC ===")
        app.state.is_processing = False
        if not app.state.should_stop:
            app.state.status_text = "Serwer gotowy. Czekam na pliki z telefonu..."


# =========================================================
# HTML – INTERFEJS MOBILNY (wieloekranowy z nawigacja)
# =========================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<title>ComfyUI Mobile</title>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#0a0a0a">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0d0d0f;color:#e8e8e8;min-height:100vh;padding-bottom:72px}
.top-bar{position:sticky;top:0;z-index:100;background:#161618;border-bottom:1px solid #2a2a2e;padding:13px 16px;display:flex;align-items:center;justify-content:space-between}
.top-bar h1{font-size:16px;font-weight:600;color:#fff}
.badge{background:#4CAF50;color:#000;font-size:11px;font-weight:700;padding:3px 8px;border-radius:20px}
.badge.working{background:#ff9800}.badge.error{background:#f44336;color:#fff}
.nav-bar{position:fixed;bottom:0;left:0;right:0;z-index:200;background:#161618;border-top:1px solid #2a2a2e;display:flex;padding-bottom:env(safe-area-inset-bottom)}
.nav-btn{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:9px 2px 7px;background:none;border:none;color:#555;font-size:10px;font-weight:500;cursor:pointer;position:relative}
.nav-btn .ni{font-size:20px;margin-bottom:2px;line-height:1}
.nav-btn.active{color:#4CAF50}
.notif{position:absolute;top:5px;right:calc(50% - 15px);background:#f44336;color:#fff;font-size:9px;width:15px;height:15px;border-radius:50%;display:none;align-items:center;justify-content:center;font-weight:700}
.notif.show{display:flex}
.screen{display:none;padding:14px}
.screen.active{display:block}
.card{background:#1c1c1f;border:1px solid #2a2a2e;border-radius:14px;padding:15px;margin-bottom:11px}
.card-title{font-size:11px;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.6px;margin-bottom:11px}
label{display:block;font-size:13px;color:#888;margin-bottom:4px;margin-top:11px}
label:first-child{margin-top:0}
input[type=file],textarea,select,input[type=text],input[type=password],input[type=number]{width:100%;padding:10px 12px;background:#252528;color:#e8e8e8;border:1px solid #333;border-radius:10px;font-size:15px;font-family:inherit;appearance:none;-webkit-appearance:none}
input[type=range]{width:100%;accent-color:#4CAF50;margin-top:6px}
select{background-image:url("data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2212%22 height=%228%22 viewBox=%220 0 12 8%22%3E%3Cpath d=%22M1 1l5 5 5-5%22 stroke=%22%23666%22 stroke-width=%221.5%22 fill=%22none%22 stroke-linecap=%22round%22/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:32px}
textarea{resize:vertical;min-height:78px;line-height:1.4}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
.btn{width:100%;padding:14px;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;transition:opacity .15s,transform .1s;margin-top:8px}
.btn:active{opacity:.8;transform:scale(.98)}
.btn:disabled{opacity:.4}
.btn-green{background:#4CAF50;color:#000}
.btn-red{background:#f44336;color:#fff}
.btn-blue{background:#2196F3;color:#fff}
.btn-dark{background:#2a2a2e;color:#e8e8e8}
.btn-sm{width:auto;padding:9px 16px;font-size:14px;margin-top:0}
.live-box{background:#141416;border:1px solid #2a2a2e;border-radius:14px;padding:15px;margin-bottom:11px;text-align:center}
.live-label{font-size:12px;color:#666;margin-bottom:5px}
.live-text{font-size:17px;font-weight:600;color:#4CAF50;line-height:1.3}
.live-text.busy{color:#ff9800}
.detail-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #222;font-size:14px}
.detail-row:last-child{border-bottom:none}
.detail-key{color:#888}
.detail-val{color:#fff;font-weight:500;text-align:right;max-width:55%;word-break:break-word}
.progress-wrap{background:#252528;border-radius:6px;height:8px;margin:9px 0 3px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,#4CAF50,#8BC34A);border-radius:6px;transition:width .3s ease}
.queue-item{background:#252528;border-radius:10px;padding:11px 13px;margin-bottom:7px;display:flex;align-items:center;gap:11px}
.q-icon{font-size:19px}
.q-info{flex:1}
.q-title{font-size:14px;font-weight:600;color:#e8e8e8}
.q-badge{font-size:11px;font-weight:700;padding:3px 8px;border-radius:20px}
.q-badge.running{background:#ff9800;color:#000}
.q-badge.pending{background:#2a2a2e;color:#888}
.queue-empty{text-align:center;color:#555;padding:28px 0;font-size:14px}
.sse-row{display:flex;align-items:center;gap:6px;margin-bottom:9px}
.dot{width:7px;height:7px;border-radius:50%;background:#444;flex-shrink:0}
.dot.ok{background:#4CAF50}
.dot.err{background:#f44336}
.sse-label{font-size:11px;color:#555}
#loading-screen{position:fixed;inset:0;background:#0d0d0f;z-index:9999;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px}
.spinner{width:42px;height:42px;border:4px solid #222;border-top:4px solid #4CAF50;border-radius:50%;animation:spin .9s linear infinite;margin-bottom:10px}
@keyframes spin{to{transform:rotate(360deg)}}
#loading-screen h2{font-size:17px;color:#fff}
#loading-status{font-size:13px;color:#4CAF50;margin-top:5px}
#loading-retry{display:none;margin-top:14px;padding:9px 22px;background:#2196F3;color:#fff;border:none;border-radius:10px;font-size:15px;cursor:pointer}
/* Ustawienia */
.settings-link{display:flex;align-items:center;gap:10px;padding:13px 14px;background:#1c1c1f;border:1px solid #2a2a2e;border-radius:12px;margin-bottom:9px;color:#e8e8e8;font-size:14px;font-weight:500;cursor:pointer;text-decoration:none}
.settings-link .sl-icon{font-size:22px}
.settings-link .sl-arrow{margin-left:auto;color:#555}
.settings-link:active{opacity:.7}
.form-row{display:flex;gap:9px;align-items:flex-end}
.form-row .btn{margin-top:0}
.alert-sm{padding:10px 13px;border-radius:9px;font-size:13px;margin-top:10px}
.alert-ok{background:#1a2a1a;border:1px solid #4CAF50;color:#4CAF50}
.alert-err{background:#2a1a1a;border:1px solid #f44336;color:#f44336}
/* Loader wewnetrzny */
.inline-spinner{display:inline-block;width:14px;height:14px;border:2px solid #333;border-top:2px solid #4CAF50;border-radius:50%;animation:spin .7s linear infinite;vertical-align:middle;margin-right:6px}
/* Dynamiczne pola custom */
.custom-field{background:#252528;border-radius:10px;padding:12px;margin-top:10px}
.custom-field label{margin-top:0;color:#aaa}
.slider-val{font-size:13px;color:#4CAF50;font-weight:600;margin-left:8px}
.recent-thumbs{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 4px}
.recent-thumb{width:56px;height:56px;object-fit:cover;border-radius:8px;cursor:pointer;border:2px solid transparent;transition:border-color .15s}
.recent-thumb:active,.recent-thumb.selected{border-color:#4CAF50}
.recent-thumb-wrap{position:relative;display:inline-block}
.recent-label{font-size:10px;color:#555;margin-top:2px;text-align:center;width:56px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gallery-actions{display:flex;gap:6px;padding:6px;background:rgba(0,0,0,.6);position:absolute;top:4px;right:4px;border-radius:8px}
.gallery-btn{background:rgba(30,30,30,.9);border:1px solid #333;color:#ccc;border-radius:6px;padding:4px 8px;font-size:13px;cursor:pointer;line-height:1}
.gallery-btn:active{opacity:.7}
</style>
</head>
<body>

<div id="loading-screen">
  <div class="spinner"></div>
  <h2>Laczenie...</h2>
  <div id="loading-status">Ladowanie workflow...</div>
  <button id="loading-retry" onclick="location.reload()">Odswiez</button>
</div>

<div class="top-bar" id="app-header" style="display:none">
  <h1 id="screen-title">Generuj</h1>
  <div style="display:flex;align-items:center;gap:8px">
    <button id="profile-switcher-btn" onclick="openProfileSwitcher()"
      style="background:none;border:1px solid #333;border-radius:20px;padding:3px 10px 3px 6px;
             color:#ccc;font-size:13px;display:flex;align-items:center;gap:5px;cursor:pointer;
             white-space:nowrap;max-width:130px;overflow:hidden">
      <span id="profile-avatar" style="font-size:16px">👤</span>
      <span id="profile-name-label" style="overflow:hidden;text-overflow:ellipsis">Brak profilu</span>
    </button>
    <span class="badge" id="status-badge">Gotowy</span>
  </div>
</div>

<!-- Modal: przełącznik profili -->
<!-- Modal zapisu presetu -->
<div id="preset-modal" style="display:none;position:fixed;inset:0;z-index:1100;background:rgba(0,0,0,0.75);align-items:center;justify-content:center">
  <div style="background:#111;border:1px solid #2a2a2a;border-radius:14px;padding:24px;width:92%;max-width:400px;margin:auto">
    <div style="font-size:16px;font-weight:600;margin-bottom:16px">⭐ Zapisz preset</div>
    <div style="margin-bottom:12px">
      <div style="font-size:11px;color:#555;margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px">Emoji</div>
      <div id="preset-emoji-row" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:4px">
        <!-- wypełniane JS -->
      </div>
    </div>
    <div style="margin-bottom:12px">
      <div style="font-size:11px;color:#555;margin-bottom:5px;text-transform:uppercase;letter-spacing:.5px">Nazwa presetu</div>
      <input id="preset-name-inp" type="text" maxlength="30" placeholder="np. Portret wieczorowy"
        style="width:100%;background:#0a0a0a;border:1px solid #2a2a2a;border-radius:8px;color:#eee;font-size:14px;padding:9px 12px;outline:none">
    </div>
    <div style="font-size:11px;color:#444;margin-bottom:16px" id="preset-summary">
      <!-- podsumowanie co będzie zapisane -->
    </div>
    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button onclick="closePresetModal()" style="padding:9px 18px;background:none;border:1px solid #333;border-radius:8px;color:#777;cursor:pointer;font-size:13px">Anuluj</button>
      <button onclick="doSavePreset()" style="padding:9px 18px;background:#e8b84b;border:none;border-radius:8px;color:#000;font-weight:600;cursor:pointer;font-size:13px">Zapisz ⭐</button>
    </div>
  </div>
</div>

<div id="profile-modal" style="display:none;position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,0.75)" onclick="closeProfileSwitcher()">
  <div onclick="event.stopPropagation()" style="position:absolute;bottom:0;left:0;right:0;background:#1a1a1a;border-radius:20px 20px 0 0;padding:20px;max-height:80vh;overflow-y:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div style="font-size:16px;font-weight:600;color:#fff">Profile</div>
      <button onclick="closeProfileSwitcher()" style="background:none;border:none;color:#666;font-size:22px;cursor:pointer">✕</button>
    </div>
    <div id="profile-list-modal"></div>
    <button onclick="openNewProfile()" class="btn btn-dark" style="width:100%;margin-top:12px;border:1px dashed #333">
      + Nowy profil
    </button>
  </div>
</div>

<!-- Modal: edycja profilu -->
<div id="profile-edit-modal" style="display:none;position:fixed;inset:0;z-index:1001;background:rgba(0,0,0,0.85)" onclick="closeEditProfile()">
  <div onclick="event.stopPropagation()" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:#1a1a1a;border-radius:16px;padding:20px;width:90%;max-width:400px;max-height:85vh;overflow-y:auto">
    <div style="font-size:16px;font-weight:600;color:#fff;margin-bottom:16px" id="profile-edit-title">Nowy profil</div>

    <label style="font-size:12px;color:#666;display:block;margin-bottom:4px">Avatar (emoji)</label>
    <div id="emoji-picker" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">
    </div>

    <label style="font-size:12px;color:#666;display:block;margin-bottom:4px">Nazwa profilu</label>
    <input id="pe-name" type="text" placeholder="np. Ola, Ricky..." style="width:100%;margin-bottom:12px">

    <label style="font-size:12px;color:#666;display:block;margin-bottom:4px">Domyślny workflow</label>
    <select id="pe-workflow" style="width:100%;margin-bottom:12px">
      <option value="">— brak —</option>
    </select>

    <label style="font-size:12px;color:#666;display:block;margin-bottom:4px">Domyślny prefix</label>
    <textarea id="pe-prefix" rows="2" placeholder="Tekst przed stylami..." style="width:100%;margin-bottom:12px;resize:vertical"></textarea>

    <label style="font-size:12px;color:#666;display:block;margin-bottom:4px">Domyślny styl</label>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-bottom:16px">
      <select id="pe-style-mode"><option value="">Tryb</option><option value="auto">auto</option><option value="manual_main">manual_main</option><option value="manual_sub">manual_sub</option><option value="manual_all">manual_all</option></select>
      <select id="pe-style-main"><option value="">Main</option></select>
      <select id="pe-style-sub"><option value="">Sub</option></select>
      <select id="pe-style-subsub"><option value="">Subsub</option></select>
    </div>

    <div style="display:flex;gap:8px">
      <button onclick="saveEditProfile()" class="btn btn-green" style="flex:1">Zapisz</button>
      <button id="pe-delete-btn" onclick="deleteEditProfile()" class="btn btn-dark" style="background:#3a0000;border-color:#660000;color:#f44">Usuń</button>
    </div>
    <div id="pe-alert" style="display:none;margin-top:8px" class="alert-sm"></div>
  </div>
</div>

<nav class="nav-bar" id="app-nav" style="display:none">
  <button class="nav-btn active" id="nav-new" onclick="showScreen('new')">
    <span class="ni">✏️</span>Generuj
  </button>
  <button class="nav-btn" id="nav-status" onclick="showScreen('status')">
    <span class="ni">📊</span>Status
    <span class="notif" id="notif-status"></span>
  </button>
  <button class="nav-btn" id="nav-queue" onclick="showScreen('queue')">
    <span class="ni">📋</span>Kolejka
    <span class="notif" id="notif-queue"></span>
  </button>
  <button class="nav-btn" id="nav-gallery" onclick="showScreen('gallery')">
    <span class="ni">🖼️</span>Galeria
  </button>
  <button class="nav-btn" id="nav-gpu" onclick="showScreen('gpu')">
    <span class="ni">🖥️</span>GPU
  </button>
  <button class="nav-btn" id="nav-stats" onclick="showScreen('stats')">
    <span class="ni">📈</span>Statystyki
  </button>
  <button class="nav-btn" id="nav-settings" onclick="showScreen('settings')">
    <span class="ni">⚙️</span>Więcej
  </button>
  <button class="nav-btn" id="nav-admin" onclick="location.href='/admin'" style="display:none">
    <span class="ni">🛡️</span>Admin
  </button>
</nav>

<!-- ══ EKRAN 1: GENERUJ ══ -->
<div class="screen active" id="screen-new">
  <div class="card">
    <div class="card-title">Workflow</div>
    <label>Proces</label>
    <select id="wf-select">
      <option value="">Ladowanie...</option>
    </select>
    <label>Ilosc wariantow</label>
    <select id="iterations">
      <option value="1">1 wariant</option>
      <option value="5">5 wariantow</option>
      <option value="10">10 wariantow</option>
      <option value="50">50 wariantow (max)</option>
    </select>
  </div>

  <!-- Dynamiczne pola generowane z konfiguracji workflow -->
  <div id="dynamic-fields"></div>

  <div id="last-settings-bar" style="display:none;margin-bottom:8px">
    <button class="btn btn-dark" onclick="restoreLastSettings()" style="width:100%;font-size:13px;padding:10px;border:1px solid #333">
      ↩ Wczytaj ostatnie ustawienia: <span id="last-settings-label" style="color:#4CAF50"></span>
    </button>
  </div>
  <button class="btn btn-green" id="submit-btn" onclick="submitGenerate()">Generuj obrazy</button>
  <button class="btn" id="save-preset-btn" onclick="openSavePreset()"
    style="background:none;border:1px solid #444;border-radius:8px;padding:10px 14px;color:#aaa;font-size:13px;cursor:pointer"
    title="Zapisz obecne ustawienia jako preset">⭐</button>
</div>

<!-- ══ PRESETY - pasek szybkiego dostępu ══ -->
<div id="presets-bar" style="display:none;margin:8px 0 4px;overflow-x:auto;white-space:nowrap;padding-bottom:4px">
  <span style="font-size:11px;color:#555;margin-right:6px">Presety:</span>
  <span id="presets-chips"></span>
</div>

<!-- ══ EKRAN SIMPLE MODE ══ -->
<div class="screen" id="screen-simple" style="padding:16px">

  <!-- Kafelki akcji -->
  <div id="simple-tiles" style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px">
    <!-- wypełniane przez JS na podstawie simple_workflows -->
  </div>

  <!-- Upload zdjęcia -->
  <div id="simple-upload-zone" style="display:none;margin-bottom:16px">

    <!-- Nagłówek z powrotem -->
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
      <button onclick="simpleReset()" style="background:none;border:none;color:#555;font-size:22px;cursor:pointer;padding:0;line-height:1">←</button>
      <div id="simple-action-title" style="font-size:15px;font-weight:600;color:#eee"></div>
    </div>

    <!-- Instrukcja -->
    <div id="simple-hint" style="background:#111;border:1px solid #1e1e1e;border-radius:10px;padding:12px 14px;margin-bottom:14px;font-size:13px;color:#888;line-height:1.5"></div>

    <!-- Slot 1 -->
    <div id="simple-slot-1" style="background:#111;border:2px dashed #2a2a2a;border-radius:14px;padding:20px;text-align:center;margin-bottom:10px">
      <div id="simple-slot-1-label" style="font-size:12px;color:#555;margin-bottom:10px;text-transform:uppercase;letter-spacing:.5px"></div>
      <div id="simple-preview-wrap" style="display:none;margin-bottom:10px">
        <img id="simple-preview-img" style="max-width:100%;max-height:150px;border-radius:8px;object-fit:cover">
      </div>
      <label for="simple-file-inp" style="display:inline-flex;align-items:center;gap:8px;padding:10px 20px;background:#1a1a1a;border:1px solid #333;border-radius:10px;cursor:pointer;color:#ccc;font-size:14px">
        📷 Wybierz zdjęcie
      </label>
      <input id="simple-file-inp" type="file" accept="image/*" style="display:none" onchange="simplePreview(this)">
      <div id="simple-recent" style="margin-top:12px;display:none">
        <div style="font-size:11px;color:#444;margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">Ostatnie</div>
        <div id="simple-recent-thumbs" style="display:flex;gap:8px;overflow-x:auto;padding-bottom:4px"></div>
      </div>
    </div>

    <!-- Slot 2: tylko dla try-on -->
    <div id="simple-slot-2" style="display:none;background:#111;border:2px dashed #2a2a2a;border-radius:14px;padding:20px;text-align:center;margin-bottom:10px">
      <div id="simple-slot-2-label" style="font-size:12px;color:#555;margin-bottom:10px;text-transform:uppercase;letter-spacing:.5px"></div>
      <div id="simple-preview2-wrap" style="display:none;margin-bottom:10px">
        <img id="simple-preview2-img" style="max-width:100%;max-height:150px;border-radius:8px;object-fit:cover">
      </div>
      <label for="simple-file-inp2" style="display:inline-flex;align-items:center;gap:8px;padding:10px 20px;background:#1a1a1a;border:1px solid #333;border-radius:10px;cursor:pointer;color:#ccc;font-size:14px">
        👗 Wybierz zdjęcie ciuchu
      </label>
      <input id="simple-file-inp2" type="file" accept="image/*" style="display:none" onchange="simplePreview2(this)">
    </div>

    <!-- Odblokowane pola stylu — wypełniane dynamicznie przez openSimpleAction -->
    <div id="simple-style-fields" style="display:none;background:#111;border:1px solid #1e1e2e;border-radius:14px;padding:14px;margin-bottom:10px">
      <div style="font-size:11px;color:#668;font-weight:600;margin-bottom:10px">🎨 Styl</div>
      <div id="simple-style-fields-inner"></div>
    </div>
  </div>


  <!-- Wybór ilości generacji -->
  <div id="simple-iterations-wrap" style="display:none;margin-bottom:8px">
    <select id="simple-iterations" style="width:100%;background:#111;border:1px solid #1e1e1e;color:#ccc;border-radius:10px;padding:10px 14px;font-size:14px">
      <option value="1">1 wariant</option>
      <option value="2">2 warianty</option>
      <option value="3">3 warianty</option>
      <option value="5">5 wariantów</option>
      <option value="10">10 wariantów</option>
    </select>
  </div>

  <!-- Przycisk Start -->
  <button id="simple-go-btn" onclick="simpleGenerate()" style="display:none;width:100%;padding:16px;background:linear-gradient(135deg,#e8b84b,#f0a020);border:none;border-radius:12px;color:#000;font-size:17px;font-weight:700;cursor:pointer;letter-spacing:.3px">
    ✨ Generuj
  </button>

  <!-- Status -->
  <div id="simple-status" style="display:none;padding:16px">
    <!-- Live preview -->
    <div id="simple-live-wrap" style="display:none;margin-bottom:12px;position:relative">
      <img id="simple-live-img" style="width:100%;border-radius:10px;object-fit:cover;max-height:300px">
      <div style="position:absolute;top:8px;right:8px;background:rgba(0,0,0,.6);border-radius:6px;padding:3px 8px;font-size:11px;color:#e8b84b">⟳ generowanie...</div>
    </div>
    <div id="simple-status-text" style="font-size:14px;color:#aaa;text-align:center;margin-bottom:10px">Trwa generowanie...</div>
    <div id="simple-progress-bar" style="height:3px;background:#1a1a1a;border-radius:2px;overflow:hidden;margin-bottom:14px">
      <div id="simple-progress-fill" style="height:100%;background:linear-gradient(90deg,#e8b84b,#f0a020);width:0%;transition:width .5s"></div>
    </div>

    <!-- Przygotuj kolejne zdjęcie -->
    <div id="simple-next-zone" style="display:none;border-top:1px solid #1e1e1e;padding-top:12px;margin-top:4px">
      <div style="font-size:11px;color:#555;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px">Następne zdjęcie (gotowe do wysyłki po zakończeniu)</div>
      <div id="simple-next-preview-wrap" style="display:none;margin-bottom:8px">
        <img id="simple-next-preview-img" style="max-width:100%;max-height:120px;border-radius:8px;object-fit:cover">
      </div>
      <label for="simple-next-file-inp" style="display:inline-flex;align-items:center;gap:8px;padding:9px 18px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;cursor:pointer;color:#ccc;font-size:13px">
        📷 Wybierz zdjęcie
      </label>
      <input id="simple-next-file-inp" type="file" accept="image/*" style="display:none" onchange="simpleNextPreview(this)">
      <div id="simple-next-recent" style="margin-top:10px;display:none">
        <div style="font-size:11px;color:#444;margin-bottom:5px">Ostatnie</div>
        <div id="simple-next-recent-thumbs" style="display:flex;gap:7px;overflow-x:auto;padding-bottom:4px"></div>
      </div>
      <div id="simple-next-ready-badge" style="display:none;margin-top:8px;padding:6px 10px;background:#1a3a1a;border:1px solid #4CAF50;border-radius:8px;font-size:12px;color:#4CAF50">
        ✓ Zdjęcie gotowe — zostanie wysłane automatycznie
      </div>
    </div>

    <button onclick="simpleNewWhileGenerating()" id="simple-next-btn" style="width:100%;padding:10px;background:#1a1a1a;border:1px solid #333;border-radius:10px;color:#888;font-size:13px;cursor:pointer;margin-top:10px">
      + Przygotuj kolejne generowanie
    </button>
  </div>

  <!-- Wynik -->
  <div id="simple-result" style="display:none;text-align:center">
    <img id="simple-result-img" style="width:100%;border-radius:12px;margin-bottom:16px">
    <div style="display:flex;gap:10px;justify-content:center;margin-bottom:14px">
      <button onclick="simpleDownload()" style="padding:11px 20px;background:#1a1a1a;border:1px solid #333;border-radius:10px;color:#ccc;font-size:14px;cursor:pointer">💾 Zapisz</button>
      <button onclick="simpleGoBack()" style="padding:11px 20px;background:#e8b84b;border:none;border-radius:10px;color:#000;font-size:14px;font-weight:600;cursor:pointer">🔄 Nowe zdjęcie</button>
      <button onclick="simpleGenerateAgain()" style="padding:11px 20px;background:#1a3a1a;border:1px solid #4CAF50;border-radius:10px;color:#4CAF50;font-size:14px;font-weight:600;cursor:pointer">✨ Jeszcze raz</button>
    </div>
    <!-- Ostatnie zdjęcia - szybki wybór nowego -->
    <div id="simple-result-recent" style="display:none;text-align:left">
      <div style="font-size:11px;color:#444;margin-bottom:6px;text-transform:uppercase;letter-spacing:.5px">Użyj innego zdjęcia</div>
      <div id="simple-result-recent-thumbs" style="display:flex;gap:8px;overflow-x:auto;padding-bottom:4px"></div>
    </div>
  </div>

</div>

<!-- ══ EKRAN 2: STATUS ══ -->
<div class="screen" id="screen-status">
  <div class="live-box">
    <div class="live-label">Status</div>
    <div class="live-text" id="live-text">Czekam...</div>
  </div>
  <div class="sse-row">
    <div class="dot" id="sse-dot"></div>
    <span class="sse-label" id="sse-label">Live: laczenie...</span>
  </div>
  <div id="live-preview-box" style="display:none;margin:12px 0;border-radius:12px;overflow:hidden;background:#111;text-align:center">
    <img id="live-preview-img" src="" style="width:100%;max-height:320px;object-fit:contain;display:block" />
    <div style="font-size:11px;color:#555;padding:4px">Live preview</div>
  </div>
  <div class="card">
    <div class="detail-row"><span class="detail-key">Wariant</span><span class="detail-val" id="d-iter">—</span></div>
    <div class="detail-row" id="d-style-row" style="display:none"><span class="detail-key">🎨 Styl</span><span class="detail-val" id="d-style" style="color:#4CAF50;font-size:12px;word-break:break-word">—</span></div>
    <div class="detail-row"><span class="detail-key">Aktualny node</span><span class="detail-val" id="d-node">—</span></div>
    <div class="detail-row"><span class="detail-key">Krok GPU</span><span class="detail-val" id="d-step">—</span></div>
    <div class="progress-wrap"><div class="progress-fill" id="d-bar" style="width:0%"></div></div>
  </div>
  <button class="btn btn-red" id="stop-btn" onclick="stopGen()">Zatrzymaj generowanie</button>
  <div style="display:flex;gap:8px;margin-top:4px">
    <button class="btn btn-blue" id="new-task-btn" style="display:none;flex:1" onclick="showScreen('new')">Nowe zadanie</button>
    <button id="wake-lock-btn" onclick="toggleWakeLock()" title="Nie wygaszaj ekranu"
      style="background:#1a1a2e;border:1px solid #333;color:#888;border-radius:10px;padding:10px 16px;font-size:20px;flex-shrink:0">
      💡
    </button>
  </div>
</div>

<!-- ══ EKRAN 3: KOLEJKA ══ -->
<div class="screen" id="screen-queue">
  <div class="card">
    <div class="card-title">Aktywne</div>
    <div id="q-running-list"><div class="queue-empty">Brak aktywnych zadan</div></div>
  </div>
  <div class="card">
    <div class="card-title">Oczekujace</div>
    <div id="q-pending-list"><div class="queue-empty">Kolejka pusta</div></div>
  </div>
  <button class="btn btn-red" onclick="clearQueue()">Przerwij i wyczysc kolejke</button>
</div>

<!-- ══ EKRAN 4: GPU ══ -->
<div class="screen" id="screen-gpu">
  <div class="card">
    <div class="card-title">Karta graficzna</div>
    <div class="detail-row"><span class="detail-key">Temperatura</span><span class="detail-val" id="g-temp">—</span></div>
    <div class="detail-row"><span class="detail-key">Uzycie rdzenia</span><span class="detail-val" id="g-util">—</span></div>
    <div class="detail-row"><span class="detail-key">VRAM</span><span class="detail-val" id="g-vram">—</span></div>
  </div>
  <div class="card">
    <div class="card-title">Serwer</div>
    <div class="detail-row"><span class="detail-key">ComfyUI</span><span class="detail-val" id="g-comfy">—</span></div>
    <div class="detail-row"><span class="detail-key">Ostatni workflow</span><span class="detail-val" id="g-wf">—</span></div>
    <div class="detail-row"><span class="detail-key">W kolejce</span><span class="detail-val" id="g-pending">—</span></div>
  </div>
</div>

<!-- ══ EKRAN 5: USTAWIENIA / WIĘCEJ ══ -->
<div class="screen" id="screen-settings">

  <div class="card">
    <div class="card-title">Panele</div>
    <a class="settings-link" href="/panel" target="_blank">
      <span class="sl-icon">🖥️</span>
      <span>Panel laptopa</span>
      <span class="sl-arrow">↗</span>
    </a>
    <a class="settings-link" href="/kreator" target="_blank">
      <span class="sl-icon">⚙️</span>
      <span>Kreator workflow</span>
      <span class="sl-arrow">↗</span>
    </a>
  </div>

  <div class="card">
    <div class="card-title">Telegram</div>
    <label>Token bota</label>
    <input type="password" id="s-tg-token" placeholder="Pozostaw puste = bez zmian">
    <label>Chat ID</label>
    <input type="text" id="s-tg-chat" placeholder="np. 8011392687">
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="btn btn-dark btn-sm" onclick="saveTelegram()">Zapisz</button>
      <button class="btn btn-blue btn-sm" onclick="testTelegram()">Test wysylki</button>
    </div>
    <div id="tg-alert" style="display:none" class="alert-sm"></div>
  </div>

  <div class="card">
    <div class="card-title">Serwer</div>
    <label>Adres ComfyUI</label>
    <input type="text" id="s-comfy-url" placeholder="127.0.0.1:8188">
    <label>Folder zapisu obrazow</label>
    <input type="text" id="s-image-dir" placeholder="C:\\AI\\IMAGES\\...">
    <label>Oczekiwanie VRAM po zmianie workflow (sekundy)</label>
    <input type="number" id="s-vram-wait" min="0" max="60" value="3">
    <button class="btn btn-dark" onclick="saveServerSettings()">Zapisz ustawienia serwera</button>
    <div id="srv-alert" style="display:none" class="alert-sm"></div>
  </div>

  <div class="card">
    <div class="card-title">🔔 Powiadomienia Push</div>
    <div id="push-status" style="font-size:13px;color:#555;margin-bottom:10px">Sprawdzanie...</div>
    <button class="btn btn-dark btn-sm" id="push-btn" onclick="togglePush()">Włącz powiadomienia</button>
    <div id="push-alert" style="display:none" class="alert-sm"></div>
    <div style="margin-top:8px;font-size:11px;color:#444">Powiadomienia o: ukończeniu generowania, starcie i zamknięciu serwera</div>
    <div style="margin-top:16px;border-top:1px solid #1a1a1a;padding-top:12px">
      <div class="card-title" style="margin-bottom:8px">💡 Wygaszanie ekranu</div>
      <div style="font-size:12px;color:#555;margin-bottom:10px">Zapobiegaj wygaszaniu ekranu podczas generowania</div>
      <button class="btn btn-dark btn-sm" id="wake-lock-btn-s" onclick="toggleWakeLockSettings()">💡 Włącz blokadę wygaszania</button>
      <div style="margin-top:6px;font-size:11px;color:#444">Wymaga Chrome na Androidzie lub Safari iOS 16.4+</div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">🔐 Bezpieczenstwo</div>
    <label>Nowe haslo dostepu</label>
    <input type="password" id="s-new-password" placeholder="Wpisz nowe haslo..." autocomplete="new-password">
    <label>Potwierdz haslo</label>
    <input type="password" id="s-confirm-password" placeholder="Powtorz haslo..." autocomplete="new-password">
    <div style="margin-top:4px;font-size:11px;color:#555">Haslo dotyczy dostepu przez internet. Zostaw puste jesli chcesz wylaczyc ochrone.</div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="btn btn-dark btn-sm" onclick="savePassword()">Zapisz haslo</button>
      <button class="btn btn-dark btn-sm" onclick="disablePassword()">Wylacz ochrone</button>
    </div>
    <div id="pwd-alert" style="display:none" class="alert-sm"></div>
    <div id="pwd-status" style="margin-top:8px;font-size:12px;color:#555">Ladowanie...</div>
  </div>

  <div class="card">
    <div class="card-title">⏰ Harmonogram</div>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <span style="font-size:14px;color:#ccc">Automatyczny start generowania</span>
      <label style="display:flex;align-items:center;gap:8px;margin:0;cursor:pointer">
        <input type="checkbox" id="sched-toggle" onchange="updateScheduleUI(this.checked)" style="width:18px;height:18px;accent-color:#4CAF50">
      </label>
    </div>
    <div id="sched-time-row" style="display:none">
      <label>Godzina startu</label>
      <input type="time" id="sched-time" value="22:00" style="width:140px">
      <div style="margin-top:6px;font-size:11px;color:#555">Serwer uruchomi ostatnio zapisane ustawienia generowania o wybranej godzinie</div>
    </div>
    <div id="sched-status" style="margin-top:8px;font-size:12px;color:#555">Wyłączony</div>
    <div id="sched-config-info" style="display:none;font-size:11px;color:#555;margin-top:4px;padding:5px 8px;background:#0f0f0f;border-radius:6px"></div>
    <button class="btn btn-dark btn-sm" onclick="saveSchedule()" style="margin-top:10px">Zapisz harmonogram</button>
    <div id="sched-alert" style="display:none" class="alert-sm"></div>
  </div>

  <div class="card">
    <div class="card-title">🔄 Zarządzanie silnikiem</div>
    <div style="font-size:12px;color:#555;margin-bottom:10px">Restartuje proces ComfyUI bez zatrzymywania serwera mobilnego</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn btn-dark btn-sm" onclick="restartComfyUI()" id="restart-comfy-btn">🔄 Restart ComfyUI</button>
      <button class="btn btn-red btn-sm" onclick="if(confirm('Zatrzymać serwer?')) window.location.href='/api/shutdown'">⏹ Stop serwer</button>
    </div>
    <div id="restart-alert" style="display:none" class="alert-sm"></div>
  </div>

  <div class="card">
    <button class="btn btn-dark" onclick="window.location.href='/logout'" style="width:100%">Wyloguj sie</button>
  </div>

</div>

<script>
// ══════════════════════════════════════════════
// NAWIGACJA
// ══════════════════════════════════════════════
const SCREEN_TITLES = {new:'Generuj',status:'Status generowania',queue:'Kolejka',gpu:'GPU / Sprzet',settings:'Ustawienia',gallery:'Galeria',stats:'Statystyki'};

function showScreen(name) {
  // W trybie uproszczonym ignoruj wszystkie przełączenia - zostań na screen-simple
  if (_simpleMode && name !== 'simple' && name !== 'gallery' && name !== 'settings') return;
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  var screenEl = document.getElementById('screen-' + name);
  if (!screenEl) return;
  screenEl.classList.add('active');
  var navEl = document.getElementById('nav-' + name);
  if (navEl) navEl.classList.add('active');
  var titleEl = document.getElementById('screen-title');
  if (titleEl) titleEl.textContent = SCREEN_TITLES[name] || '';
  if (name === 'status')   hideNotif('status');
  if (name === 'queue')    { hideNotif('queue'); refreshQueue(); }
  if (name === 'gpu')      refreshGPU();
  if (name === 'settings') { loadSettings(); initPush(); loadSchedule(); }
  if (window._onShowScreen) window._onShowScreen(name);
}
function showNotif(id,n){ const e=document.getElementById('notif-'+id); e.textContent=n>9?'9+':n; e.classList.add('show'); }
function hideNotif(id){ document.getElementById('notif-'+id).classList.remove('show'); }

// ══════════════════════════════════════════════
// WCZYTYWANIE WORKFLOW (dynamiczne)
// ══════════════════════════════════════════════
let workflows = [];        // lista z /api/workflows
let currentWf = null;      // aktualnie wybrany workflow
let styleOptions = {};     // opcje stylu dla aktualnego wf

// ══ LIVE PREVIEW ══
let _previewInterval = null;

function startPreview() {
  if (_previewInterval) return;
  _previewInterval = setInterval(async () => {
    try {
      const r = await fetch('/api/preview');
      const d = await r.json();
      const box = document.getElementById('live-preview-box');
      const img = document.getElementById('live-preview-img');
      if (d.image) {
        img.src = 'data:image/jpeg;base64,' + d.image;
        box.style.display = 'block';
      } else if (!d.processing) {
        box.style.display = 'none';
      }
    } catch(e) {}
  }, 2000);
}

function stopPreview() {
  if (_previewInterval) { clearInterval(_previewInterval); _previewInterval = null; }
  const box = document.getElementById('live-preview-box');
  if (box) box.style.display = 'none';
}

// ══ GALERIA ══
async function loadGallery() {
  const grid = document.getElementById('gallery-grid');
  const empty = document.getElementById('gallery-empty');
  grid.innerHTML = '<div style="color:#444;text-align:center;padding:30px;grid-column:1/-1">Ładowanie...</div>';
  try {
    const r = await fetch('/api/gallery');
    const d = await r.json();
    if (!d.files || !d.files.length) {
      grid.innerHTML = '';
      empty.style.display = 'block';
      return;
    }
    empty.style.display = 'none';
    grid.innerHTML = d.files.map(function(f) {
      var fJson = JSON.stringify(f).replace(/"/g, '&quot;');
      var label = f.style ? f.style.replace('/', '').trim() : '';
      var thumbUrl = '/gallery_thumb/' + f.filename + '?size=300';
      var html = '<div style="border-radius:10px;overflow:hidden;background:#111;position:relative">';
      html += '<img src="' + thumbUrl + '" loading="lazy" onclick="openGalleryModal(this)" data-f="' + fJson + '" style="width:100%;aspect-ratio:1;object-fit:cover;display:block;cursor:pointer" />';
      if (label) html += '<div style="position:absolute;bottom:0;left:0;right:0;background:linear-gradient(transparent,rgba(0,0,0,0.8));padding:8px 6px 5px;font-size:10px;color:#ccc;line-height:1.3;pointer-events:none">' + label + '</div>';
      html += '<div class="gallery-actions">';
      html += '<button class="gallery-btn" onclick="downloadGalleryImage(' + JSON.stringify(f.filename) + ', event)" title="Pobierz">⬇</button>';
      html += '<button class="gallery-btn" onclick="deleteGalleryImage(' + JSON.stringify(f.filename) + ', event)" title="Usun">🗑</button>';
      html += '</div>';
      html += '</div>';
      return html;
    }).join('');
  } catch(e) {
    grid.innerHTML = '<div style="color:#f55;text-align:center;grid-column:1/-1">Błąd ładowania galerii</div>';
  }
}

var _modalCurrentFile = null;

function openGalleryModal(el) {
  var raw = el.getAttribute('data-f').replace(/&quot;/g, '"');
  const f = JSON.parse(raw);
  _modalCurrentFile = f;
  document.getElementById('modal-img').src = f.url;
  document.getElementById('modal-workflow').textContent = f.workflow || '—';
  document.getElementById('modal-style').textContent = f.style || '—';
  document.getElementById('modal-suffix').textContent = f.suffix || '—';
  document.getElementById('modal-prefix').textContent = f.prefix || '';
  document.getElementById('modal-prefix-row').style.display = f.prefix ? '' : 'none';
  const date = f.timestamp ? new Date(f.timestamp * 1000).toLocaleString('pl-PL') : '—';
  document.getElementById('modal-date').textContent = date;
  // Przyciski akcji
  var dlBtn = document.getElementById('modal-download-btn');
  var delBtn = document.getElementById('modal-delete-btn');
  var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
  if (dlBtn) {
    dlBtn.textContent = isIOS ? '🖼 Otwórz (przytrzymaj→Zapisz)' : '⬇ Pobierz';
    dlBtn.onclick = function() { downloadGalleryImage(f.filename, event); };
  }
  if (delBtn) delBtn.onclick = function(e) { deleteGalleryImage(f.filename, e); };
  document.getElementById('gallery-modal').style.display = 'block';
  document.body.style.overflow = 'hidden';
}

function closeGalleryModal() {
  document.getElementById('gallery-modal').style.display = 'none';
  document.body.style.overflow = '';
}

// ══ OSTATNIE ZDJĘCIA ══
var _recentImages = [];

async function loadRecentImages() {
  try {
    const r = await fetch('/api/recent_images');
    const d = await r.json();
    _recentImages = d.files || [];
  } catch(e) { _recentImages = []; }
}

function renderRecentThumbs(role) {
  var container = document.getElementById('recent-' + role);
  if (!container || !_recentImages.length) return;
  var html = '<div style="font-size:10px;color:#555;width:100%;margin-bottom:2px">Ostatnio:</div>';
  _recentImages.slice(0, 10).forEach(function(f) {
    html += '<div class="recent-thumb-wrap">'
      + '<img class="recent-thumb" src="' + f.url + '"'
      + ' data-role="' + role + '"'
      + ' data-url="' + f.url + '"'
      + ' data-filename="' + f.filename + '"'
      + ' />'
      + '</div>';
  });
  container.innerHTML = html;
  // Dodaj eventy po wstawieniu do DOM
  container.querySelectorAll('.recent-thumb').forEach(function(img) {
    img.addEventListener('click', function() {
      selectRecentImage(this, this.dataset.role, this.dataset.url, this.dataset.filename);
    });
  });
}

function selectRecentImage(el, role, url, filename) {
  // Oznacz wybraną
  document.querySelectorAll('#recent-' + role + ' .recent-thumb').forEach(function(t) {
    t.classList.remove('selected');
  });
  el.classList.add('selected');
  // Ustaw jako wybrany plik - stwórz DataTransfer żeby podmienić input
  var input = document.getElementById('img-' + role);
  if (!input) return;
  // Zapisz URL do pobrania przy wysyłaniu (zamiast pliku)
  input.dataset.recentUrl = url;
  input.dataset.recentFilename = filename;
  // Pokaż podgląd
  var preview = document.getElementById('thumb-preview-' + role);
  if (!preview) {
    preview = document.createElement('img');
    preview.id = 'thumb-preview-' + role;
    preview.style.cssText = 'width:64px;height:64px;object-fit:cover;border-radius:8px;margin-top:6px;border:2px solid #4CAF50';
    input.parentNode.insertBefore(preview, input.nextSibling);
  }
  preview.src = url;
  preview.style.display = 'block';
}

function onImageSelected(input) {
  // Wyczyść wybór z ostatnich gdy użytkownik wybrał nowy plik
  delete input.dataset.recentUrl;
  delete input.dataset.recentFilename;
  var role = input.id.replace('img-', '');
  document.querySelectorAll('#recent-' + role + ' .recent-thumb').forEach(function(t) {
    t.classList.remove('selected');
  });
  var preview = document.getElementById('thumb-preview-' + role);
  if (preview) preview.style.display = 'none';
}

// ══ GALERIA - POBIERANIE I USUWANIE ══
function downloadGalleryImage(filename, event) {
  event.stopPropagation();
  // iOS Safari nie obsługuje atrybutu "download" - otwieramy obraz bezpośrednio
  // Użytkownik może nacisnąć i przytrzymać -> "Dodaj do Zdjęć"
  var isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent);
  if (isIOS) {
    window.open('/gallery_image/' + filename, '_blank');
  } else {
    var a = document.createElement('a');
    a.href = '/api/download_image/' + filename;
    a.download = filename.replace(/\.jpg\.png$/, '.jpg').replace(/\.png$/, '.jpg');
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }
}

async function deleteGalleryImage(filename, event) {
  event.stopPropagation();
  if (!confirm('Usunac to zdjecie?')) return;
  try {
    const r = await fetch('/api/delete_image/' + filename, {method: 'DELETE'});
    const d = await r.json();
    if (d.status === 'ok') {
      // Odśwież galerię
      loadGallery();
      // Zamknij modal jeśli otwarty
      closeGalleryModal();
    } else {
      alert('Blad usuwania: ' + (d.message || '?'));
    }
  } catch(e) { alert('Blad polaczenia'); }
}

// ══ STATYSTYKI ══
async function loadStats() {
  try {
    var r = await fetch('/api/stats');
    var d = await r.json();

    // Podsumowanie
    document.getElementById('stat-total-count').textContent  = d.total_count  || 0;
    document.getElementById('stat-total-images').textContent = d.total_images  || 0;
    document.getElementById('stat-avg-time').textContent     = d.avg ? d.avg + 's' : '—';

    // Wykres
    renderStatsChart(d.entries || [], d.avg || 0);

    // Lista ostatnich
    renderStatsList(d.entries || []);
  } catch(e) {
    document.getElementById('stats-list').innerHTML = '<div style="color:#555;text-align:center;padding:20px">Brak danych</div>';
  }
}

function renderStatsChart(entries, avg) {
  var svg = document.getElementById('stats-chart');
  if (!svg || !entries.length) {
    if (svg) svg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" fill="#444" font-size="13">Brak danych – wygeneruj pierwszą grafikę</text>';
    return;
  }

  var W = svg.getBoundingClientRect().width || 320;
  var H = 200;
  var padL = 38, padR = 10, padT = 16, padB = 28;
  var chartW = W - padL - padR;
  var chartH = H - padT - padB;

  var times = entries.map(function(e) { return e.duration_s || 0; });
  var maxT  = Math.max.apply(null, times) * 1.15 || 60;
  var n     = times.length;
  var barW  = Math.max(4, Math.floor(chartW / n) - 3);
  var step  = chartW / n;

  var html = '';

  // Siatka pozioma (3 linie)
  for (var gi = 0; gi <= 3; gi++) {
    var gy = padT + chartH - (gi / 3) * chartH;
    var gv = Math.round(maxT * gi / 3);
    html += '<line x1="' + padL + '" y1="' + gy + '" x2="' + (W - padR) + '" y2="' + gy
          + '" stroke="#222" stroke-width="1"/>';
    html += '<text x="' + (padL - 4) + '" y="' + (gy + 4) + '" text-anchor="end" fill="#555" font-size="10">'
          + gv + '</text>';
  }

  // Słupki
  entries.forEach(function(e, i) {
    var t   = e.duration_s || 0;
    var bh  = Math.max(2, (t / maxT) * chartH);
    var bx  = padL + i * step + (step - barW) / 2;
    var by  = padT + chartH - bh;

    // Kolor: szybkie=zielony, wolne=pomarańczowy/czerwony
    var ratio = t / (avg || t || 1);
    var clr   = ratio < 1.3 ? '#4CAF50' : ratio < 1.8 ? '#ff9800' : '#f44336';

    html += '<rect x="' + bx + '" y="' + by + '" width="' + barW + '" height="' + bh
          + '" fill="' + clr + '" rx="2" opacity="0.85"/>';

    // Wartość nad słupkiem (tylko jeśli jest miejsce)
    if (barW >= 16) {
      html += '<text x="' + (bx + barW/2) + '" y="' + (by - 3) + '" text-anchor="middle" fill="#aaa" font-size="9">'
            + t + '</text>';
    }

    // Oś X: numer generacji (co 5)
    if (i % 5 === 0 || i === n - 1) {
      html += '<text x="' + (bx + barW/2) + '" y="' + (H - 4) + '" text-anchor="middle" fill="#444" font-size="9">'
            + (i + 1) + '</text>';
    }
  });

  // Linia średniej
  if (avg > 0) {
    var avgY = padT + chartH - (avg / maxT) * chartH;
    html += '<line x1="' + padL + '" y1="' + avgY + '" x2="' + (W - padR) + '" y2="' + avgY
          + '" stroke="#ff9800" stroke-width="1.5" stroke-dasharray="4,3"/>';
    html += '<text x="' + (W - padR - 2) + '" y="' + (avgY - 3) + '" text-anchor="end" fill="#ff9800" font-size="9">avg ' + avg + 's</text>';
  }

  svg.setAttribute('height', H);
  svg.innerHTML = html;
}

function renderStatsList(entries) {
  var el = document.getElementById('stats-list');
  if (!entries.length) {
    el.innerHTML = '<div style="color:#555;text-align:center;padding:16px">Brak historii</div>';
    return;
  }
  var rev = entries.slice().reverse();
  el.innerHTML = rev.slice(0, 10).map(function(e) {
    var d = new Date((e.timestamp || 0) * 1000);
    var dateStr = d.toLocaleString('pl-PL', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'});
    var dur = e.duration_s ? e.duration_s + 's' : '—';
    var wf  = (e.workflow || '').replace(/_/g, ' ');
    var prof = e.profile_name ? '<span style="background:#1a3a1a;color:#4CAF50;border-radius:4px;padding:1px 5px;font-size:10px;margin-left:4px">' + e.profile_name + '</span>' : '';
    return '<div style="display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid #1e1e1e">'
      + '<div><div style="font-size:13px;color:#ccc">' + wf + prof + '</div>'
      + '<div style="font-size:11px;color:#555;margin-top:1px">' + dateStr + ' · iter ' + (e.iter_num||'?') + '/' + (e.iterations||'?') + '</div></div>'
      + '<div style="font-size:16px;font-weight:700;color:#4CAF50;min-width:48px;text-align:right">' + dur + '</div>'
      + '</div>';
  }).join('');
}

// ══ PWA / PUSH NOTIFICATIONS ══
var _pushSubscription = null;

async function initPush() {
  var btn = document.getElementById('push-btn');
  var status = document.getElementById('push-status');
  if (!status) return;

  if (location.protocol !== 'https:' && location.hostname !== 'localhost') {
    status.textContent = '🔒 Push wymaga HTTPS — uruchom setup_https.bat na komputerze';
    status.style.color = '#f90';
    if (btn) btn.style.display = 'none';
    return;
  }
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    status.textContent = 'Powiadomienia push nie są obsługiwane przez tę przeglądarkę';
    if (btn) btn.style.display = 'none';
    return;
  }

  try {
    // Zarejestruj Service Worker
    var reg = await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;

    // Pobierz klucz VAPID
    var r = await fetch('/api/push/vapid-public');
    var d = await r.json();
    if (!d.public_key) {
      status.textContent = 'Serwer nie ma skonfigurowanych kluczy push';
      return;
    }

    // Sprawdź aktualną subskrypcję
    var existing = await reg.pushManager.getSubscription();
    if (existing) {
      _pushSubscription = existing;
      status.textContent = '✅ Powiadomienia WŁĄCZONE na tym urządzeniu';
      status.style.color = '#4CAF50';
      if (btn) { btn.textContent = 'Wyłącz powiadomienia'; btn.style.background = '#3a1a1a'; }
      return;
    }

    status.textContent = 'Powiadomienia wyłączone';
    status.style.color = '#888';
    if (btn) btn.textContent = 'Włącz powiadomienia';

  } catch(e) {
    status.textContent = 'Błąd inicjalizacji: ' + e.message;
  }
}

async function togglePush() {
  var btn = document.getElementById('push-btn');
  var status = document.getElementById('push-status');
  if (!btn) return;

  if (_pushSubscription) {
    // Wyłącz
    try {
      await fetch('/api/push/subscribe', {
        method: 'DELETE',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({endpoint: _pushSubscription.endpoint})
      });
      await _pushSubscription.unsubscribe();
      _pushSubscription = null;
      status.textContent = 'Powiadomienia wyłączone';
      status.style.color = '#888';
      btn.textContent = 'Włącz powiadomienia';
      btn.style.background = '';
      showAlert('push-alert', 'Powiadomienia wyłączone', 'ok');
    } catch(e) {
      showAlert('push-alert', 'Błąd: ' + e.message, 'err');
    }
    return;
  }

  // Włącz
  try {
    var perm = await Notification.requestPermission();
    if (perm !== 'granted') {
      showAlert('push-alert', 'Brak zgody na powiadomienia', 'err');
      return;
    }

    var reg = await navigator.serviceWorker.ready;
    var r = await fetch('/api/push/vapid-public');
    var d = await r.json();

    // Konwertuj klucz VAPID do Uint8Array
    var key = d.public_key.replace(/-/g, '+').replace(/_/g, '/');
    var pad = 4 - key.length % 4; if (pad !== 4) key += '='.repeat(pad);
    var raw = Uint8Array.from(atob(key), c => c.charCodeAt(0));

    var sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: raw
    });

    // Wyślij subskrypcję na serwer
    var resp = await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subscription: sub.toJSON()})
    });
    var result = await resp.json();

    if (result.status === 'ok') {
      _pushSubscription = sub;
      status.textContent = '✅ Powiadomienia WŁĄCZONE na tym urządzeniu';
      status.style.color = '#4CAF50';
      btn.textContent = 'Wyłącz powiadomienia';
      btn.style.background = '#3a1a1a';
      showAlert('push-alert', 'Powiadomienia włączone! Dostaniesz info o starcie, zakończeniu i wyłączeniu serwera.', 'ok');
    } else {
      showAlert('push-alert', 'Błąd rejestracji na serwerze', 'err');
    }
  } catch(e) {
    showAlert('push-alert', 'Błąd: ' + e.message, 'err');
  }
}

// ══ SIMPLE MODE ══════════════════════════════════════════════════
var _simpleMode          = false;
var _simpleWorkflows     = {};
var _simpleActiveWf      = null;
var _simpleActiveTile    = null;
var _simpleActiveOverrides = {};
var _simpleLastResult    = null;

// Kafelki - definicje wizualne
var SIMPLE_TILES = [
  { key: 'try-on', icon: '👗', label: 'Przymierz ciuch',
    hint: '1. Wgraj swoje zdjęcie (sylwetka, całe ciało)<br>2. Wgraj zdjęcie ciuchu który chcesz przymierzyć',
    slot1: 'Twoje zdjęcie', slot2: 'Zdjęcie ciuchu', twoSlots: true,
    defaultPrefix: 'virtual try-on, wearing the outfit, realistic, high quality',
    defaultSuffix: 'deformed, bad anatomy, wrong clothing' },
  { key: 'style',  icon: '🎨', label: 'Zmień styl',
    hint: 'Wgraj zdjęcie — AI zastosuje na nim artystyczny styl',
    slot1: 'Twoje zdjęcie', twoSlots: false,
    defaultPrefix: 'artistic style transfer, beautiful, detailed, high quality',
    defaultSuffix: 'ugly, blurry, low quality' },
  { key: 'bg',     icon: '🌅', label: 'Zmień tło',
    hint: 'Wgraj zdjęcie — AI automatycznie zmieni tło na inne',
    slot1: 'Twoje zdjęcie', twoSlots: false,
    defaultPrefix: 'beautiful background, professional photo, high quality',
    defaultSuffix: 'ugly, blurry, low quality' },
];

function initSimpleMode(me) {
  _simpleMode      = me.simple_mode;
  _simpleWorkflows = me.simple_workflows || {};
  if (!_simpleMode) return;

  // Na ekranie settings - ukryj karty niezwiązane z push
  var settingsCards = document.querySelectorAll('#screen-settings .card');
  settingsCards.forEach(function(card) {
    var title = card.querySelector('.card-title');
    if (title) {
      var txt = title.textContent;
      // Pokaż tylko Push i ukryj resztę
      var keep = txt.indexOf('Push') >= 0 || txt.indexOf('Powiadomienia') >= 0 || txt.indexOf('Wygaszanie') >= 0 || txt.indexOf('ekran') >= 0 || txt.indexOf('Wake') >= 0;
      card.style.display = keep ? '' : 'none';
    }
  });
  // Dodaj przycisk "Wróć" na settings w simple mode
  var settingsScreen = document.getElementById('screen-settings');
  if (settingsScreen && !settingsScreen.querySelector('.simple-back-btn')) {
    var backBtn = document.createElement('button');
    backBtn.className = 'simple-back-btn';
    backBtn.textContent = '← Wróć';
    backBtn.style.cssText = 'background:#1a1a1a;border:1px solid #333;color:#aaa;border-radius:8px;padding:8px 16px;cursor:pointer;margin-bottom:12px;font-size:14px';
    backBtn.onclick = function() { showScreen('simple'); };
    settingsScreen.insertBefore(backBtn, settingsScreen.firstChild);
  }

  // Zastąp normalną nawigację prostą belką z wyloguj
  var nav = document.getElementById('app-nav');
  if (nav) {
    nav.style.cssText = 'display:flex!important;justify-content:space-between;align-items:center;padding:0 16px;height:56px;background:#0a0a0a;border-top:1px solid #1a1a1a;position:fixed;bottom:0;left:0;right:0;z-index:100';
    nav.innerHTML =
      '<div style="font-size:15px;font-weight:600;color:#eee">Cześć, ' + me.name.split(' ')[0] + '! 👋</div>' +
      '<div style="display:flex;gap:8px">' +
        '<button onclick="simpleOpenGallery()" style="background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#aaa;font-size:18px;cursor:pointer;padding:8px 12px">🖼</button>' +
        '<button onclick="simpleOpenSettings()" style="background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#aaa;font-size:18px;cursor:pointer;padding:8px 12px">⚙️</button>' +
        '<button onclick="simpleLogout()" style="background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#aaa;font-size:13px;cursor:pointer;padding:8px 14px">Wyloguj</button>' +
      '</div>';
  }

  // Dodaj padding na dole żeby belka nie zasłaniała treści
  var wrap = document.getElementById('screen-simple');
  if (wrap) wrap.style.paddingBottom = '72px';

  // Ukryj header aplikacji
  var header = document.getElementById('app-header');
  if (header) header.style.display = 'none';

  renderSimpleTiles();
  showScreen('simple');
}

function renderSimpleTiles() {
  var grid = document.getElementById('simple-tiles');
  if (!grid) return;
  grid.innerHTML = '';

  SIMPLE_TILES.forEach(function(tile) {
    var hasWf = !!_simpleWorkflows[tile.key];
    if (!hasWf) return;  // nie pokazuj jeśli workflow niezdefiniowany

    var card = document.createElement('div');
    card.style.cssText = 'background:#111;border:1px solid #1e1e1e;border-radius:16px;padding:20px 14px;text-align:center;cursor:pointer;transition:all .25s;-webkit-tap-highlight-color:transparent';
    card.innerHTML =
      '<div style="font-size:38px;margin-bottom:10px">' + tile.icon + '</div>' +
      '<div style="font-size:14px;font-weight:600;color:#eee;margin-bottom:4px">' + tile.label + '</div>' +
      '<div style="font-size:11px;color:#555;line-height:1.4">' + tile.hint + '</div>';

    card.ontouchstart = function() { card.style.transform = 'scale(.96)'; card.style.borderColor = '#e8b84b'; };
    card.ontouchend   = function() { card.style.transform = ''; card.style.borderColor = '#1e1e1e'; };
    card.onclick = function() { openSimpleAction(tile); };
    grid.appendChild(card);
  });

  // Jeśli żaden workflow nie skonfigurowany - pokaż info
  if (!grid.children.length) {
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:#444;padding:40px 0;font-size:14px">' +
      'Zapytaj administratora o skonfigurowanie dostępnych akcji.</div>';
  }
}

function openSimpleAction(tile) {
  var swfVal = _simpleWorkflows[tile.key] || {};
  if (typeof swfVal === 'string') swfVal = {wf: swfVal, overrides: {}};
  _simpleActiveWf        = swfVal.wf || '';
  _simpleActiveOverrides = swfVal.overrides || {};
  _simpleActiveTile = tile;
  document.getElementById('simple-tiles').style.display        = 'none';
  document.getElementById('simple-upload-zone').style.display  = 'block';
  document.getElementById('simple-action-title').textContent   = tile.icon + ' ' + tile.label;
  document.getElementById('simple-go-btn').style.display       = 'none';
  document.getElementById('simple-status').style.display       = 'none';
  document.getElementById('simple-result').style.display       = 'none';
  document.getElementById('simple-preview-wrap').style.display = 'none';
  document.getElementById('simple-preview2-wrap').style.display= 'none';

  // Instrukcja
  var hintEl = document.getElementById('simple-hint');
  if (hintEl) hintEl.innerHTML = tile.hint;

  // Etykieta slotu 1
  var s1label = document.getElementById('simple-slot-1-label');
  if (s1label) s1label.textContent = tile.slot1 || 'Zdjęcie';

  // Slot 2 (tylko try-on)
  var slot2 = document.getElementById('simple-slot-2');
  if (slot2) slot2.style.display = tile.twoSlots ? 'block' : 'none';
  var s2label = document.getElementById('simple-slot-2-label');
  if (s2label) s2label.textContent = tile.slot2 || '';

  // Reset plików
  var inp = document.getElementById('simple-file-inp');
  if (inp) inp.value = '';
  var inp2 = document.getElementById('simple-file-inp2');
  if (inp2) inp2.value = '';

  // Renderuj odblokowane pola stylu
  renderSimpleStyleFields(_simpleActiveWf, _simpleActiveOverrides);

  // Załaduj ostatnie zdjęcia
  loadSimpleRecent();
}

async function renderSimpleStyleFields(wfId, overrides) {
  var wrap  = document.getElementById('simple-style-fields');
  var inner = document.getElementById('simple-style-fields-inner');
  if (!wrap || !inner) return;

  // Znajdź klucze stylu w overrides (klucz::style)
  var styleKeys = Object.keys(overrides).filter(function(k) {
    return typeof overrides[k] === 'object' && overrides[k] !== null && !Array.isArray(overrides[k]);
  });
  if (!styleKeys.length) { wrap.style.display = 'none'; return; }

  // Pobierz opcje z ComfyUI
  var opts = {};
  try {
    var r = await fetch('/api/workflows/' + encodeURIComponent(wfId) + '/style_options');
    var d = await r.json();
    opts = d.options || {};
  } catch(e) {}

  var mainList   = Array.isArray(opts.main)   ? opts.main   : [];
  var subList    = Array.isArray(opts.sub)    ? opts.sub    : [];
  var subsubList = Array.isArray(opts.subsub) ? opts.subsub : [];
  var modeList   = Array.isArray(opts.mode)   ? opts.mode   : ['auto','manual_main','manual_sub','manual_all'];

  var html = '';
  styleKeys.forEach(function(sk) {
    var ov = overrides[sk] || {};
    [
      { field: 'mode',   label: 'Tryb',           list: modeList,   locked: ov.mode_locked,   val: ov.mode   || 'auto' },
      { field: 'main',   label: 'Styl główny',    list: mainList,   locked: ov.main_locked,   val: ov.main   || '' },
      { field: 'sub',    label: 'Substyl',         list: subList,    locked: ov.sub_locked,    val: ov.sub    || '' },
      { field: 'subsub', label: 'Substyl podrz.', list: subsubList, locked: ov.subsub_locked, val: ov.subsub || '' },
    ].forEach(function(item) {
      if (item.locked) return; // zablokowane - niewidoczne
      if (!item.list.length) return; // brak opcji (ComfyUI offline)
      html += '<div style="margin-bottom:8px">';
      html += '<div style="font-size:11px;color:#555;margin-bottom:3px">' + item.label + '</div>';
      html += '<select data-style-key="' + sk + '" data-style-field="' + item.field + '" '
            + 'style="width:100%;background:#0a0a0a;border:1px solid #2a2a2a;color:#eee;font-size:13px;border-radius:8px;padding:8px 10px">';
      html += '<option value="">— auto —</option>';
      item.list.forEach(function(v) {
        html += '<option value="' + v + '"' + (v === item.val ? ' selected' : '') + '>' + v + '</option>';
      });
      html += '</select></div>';
    });
  });

  if (!html) { wrap.style.display = 'none'; return; }
  inner.innerHTML = html;
  wrap.style.display = 'block';
}

function loadSimpleRecentInto(thumbsId, wrapId, onSelect) {
  fetch('/api/recent_images').then(function(r){ return r.json(); }).then(function(d) {
    var thumbsEl = document.getElementById(thumbsId);
    var wrapEl   = document.getElementById(wrapId);
    if (!thumbsEl || !d.images || !d.images.length) return;
    thumbsEl.innerHTML = '';
    d.images.slice(0, 10).forEach(function(img) {
      var el = document.createElement('img');
      el.src = '/mobile_image/' + img.filename;
      el.style.cssText = 'width:56px;height:56px;object-fit:cover;border-radius:8px;flex-shrink:0;cursor:pointer;border:2px solid transparent;transition:border-color .2s';
      el.onclick = function() {
        thumbsEl.querySelectorAll('img').forEach(function(i){ i.style.borderColor='transparent'; });
        el.style.borderColor = '#e8b84b';
        fetch('/mobile_image/' + img.filename)
          .then(function(r){ return r.blob(); })
          .then(function(blob) {
            onSelect(new File([blob], img.filename, {type: blob.type}));
          });
      };
      thumbsEl.appendChild(el);
    });
    if (wrapEl) wrapEl.style.display = 'block';
  }).catch(function(){});
}

async function loadSimpleRecent() {
  // Slot 1 — upload zone
  loadSimpleRecentInto('simple-recent-thumbs', 'simple-recent', function(file) {
    var dt = new DataTransfer(); dt.items.add(file);
    var inp = document.getElementById('simple-file-inp');
    inp.files = dt.files;
    simplePreview(inp);
  });
  // Ekran wyniku — "Użyj innego zdjęcia"
  loadSimpleRecentInto('simple-result-recent-thumbs', 'simple-result-recent', function(file) {
    var dt = new DataTransfer(); dt.items.add(file);
    var inp = document.getElementById('simple-file-inp');
    inp.files = dt.files;
    simpleGoBack();
    setTimeout(function() { simplePreview(inp); }, 50);
  });
}

function simplePreview(inp) {
  if (!inp.files || !inp.files[0]) return;
  var reader = new FileReader();
  reader.onload = function(e) {
    document.getElementById('simple-preview-img').src = e.target.result;
    document.getElementById('simple-preview-wrap').style.display = 'block';
    simpleCheckReady();
  };
  reader.readAsDataURL(inp.files[0]);
}

function simplePreview2(inp) {
  if (!inp.files || !inp.files[0]) return;
  var reader = new FileReader();
  reader.onload = function(e) {
    document.getElementById('simple-preview2-img').src = e.target.result;
    document.getElementById('simple-preview2-wrap').style.display = 'block';
    simpleCheckReady();
  };
  reader.readAsDataURL(inp.files[0]);
}

function simpleCheckReady() {
  // Pokaż przycisk Generuj gdy wszystkie wymagane zdjęcia są wybrane
  var inp1 = document.getElementById('simple-file-inp');
  var inp2 = document.getElementById('simple-file-inp2');
  var slot2visible = document.getElementById('simple-slot-2').style.display !== 'none';
  var ready = inp1.files && inp1.files[0] && (!slot2visible || (inp2.files && inp2.files[0]));
  document.getElementById('simple-go-btn').style.display = ready ? 'block' : 'none';
  document.getElementById('simple-iterations-wrap').style.display = ready ? 'block' : 'none';
}

async function simpleGenerate() {
  var inp = document.getElementById('simple-file-inp');
  if (!inp.files || !inp.files[0]) { alert('Wybierz najpierw zdjęcie'); return; }

  // Pokaż status
  _simplePendingNext = false;
  document.getElementById('simple-upload-zone').style.display  = 'none';
  document.getElementById('simple-go-btn').style.display       = 'none';
  document.getElementById('simple-iterations-wrap').style.display = 'none';
  document.getElementById('simple-status').style.display       = 'block';
  document.getElementById('simple-progress-fill').style.width = '0%';
  document.getElementById('simple-live-wrap').style.display   = 'none';
  simpleStartLivePreview();

  // Animuj pasek
  var prog = 0;
  var progInt = setInterval(async function() {
    try {
      var r = await fetch('/api/status');
      var d = await r.json();
      // Użyj node_progress z ComfyUI jeśli dostępny
      if (d.step_max && d.step_max > 0) {
        // Rzeczywisty postęp samplerów ComfyUI
        var stepPct = (d.step_value / d.step_max) * 90;
        prog = Math.max(prog, stepPct);
      } else if (d.is_processing) {
        // Brak danych kroków - bardzo wolny increment
        prog = Math.min(prog + 0.25, 85);
      }
      document.getElementById('simple-progress-fill').style.width = prog + '%';
    } catch(e) {
      prog = Math.min(prog + 0.2, 80);
      document.getElementById('simple-progress-fill').style.width = prog + '%';
    }
  }, 1500);

  try {
    var fd = new FormData();
    fd.append('workflow_id', _simpleActiveWf);
    var iterSel = document.getElementById('simple-iterations');
    fd.append('iterations', iterSel ? (iterSel.value || '1') : '1');
    // Scal overrides admina z wartościami wybranymi przez usera z widocznych selectów
    var mergedOverrides = JSON.parse(JSON.stringify(_simpleActiveOverrides || {}));
    document.querySelectorAll('#simple-style-fields-inner [data-style-key]').forEach(function(sel) {
      var sk    = sel.getAttribute('data-style-key');
      var field = sel.getAttribute('data-style-field');
      if (!mergedOverrides[sk]) mergedOverrides[sk] = {};
      mergedOverrides[sk][field] = sel.value;
    });
    fd.append('simple_overrides', JSON.stringify(mergedOverrides));
    fd.append('image_1',     inp.files[0]);
    var inp2 = document.getElementById('simple-file-inp2');
    if (inp2 && inp2.files && inp2.files[0]) {
      fd.append('image_2', inp2.files[0]);
    }

    var r = await fetch('/generate_v2', {method: 'POST', body: fd});
    var d = await r.json();

    if (d.status === 'ok') {
      // Czekaj na wynik przez polling
      await waitForSimpleResult(progInt);
    } else {
      clearInterval(progInt);
      document.getElementById('simple-status-text').textContent = 'Błąd: ' + (d.message || 'nieznany');
    }
  } catch(e) {
    clearInterval(progInt);
    document.getElementById('simple-status-text').textContent = 'Błąd połączenia';
  }
}

async function waitForSimpleResult(progInt) {
  var maxWait = 900; // max 15 minut (długie workflow mogą trwać 6+ minut)
  var waited  = 0;
  // Odczekaj 4s żeby background task zdążył ustawić is_processing=true
  await new Promise(function(r){ setTimeout(r, 4000); });
  waited = 4;
  while (waited < maxWait) {
    await new Promise(function(r){ setTimeout(r, 2000); });
    waited += 2;
    try {
      var r = await fetch('/api/status');
      var d = await r.json();
      var txt = d.status_text || '';
      document.getElementById('simple-status-text').textContent = txt || 'Trwa generowanie...';
      if (!d.is_processing && waited > 8) {
        // Gotowe (waited>8 żeby nie złapać momentu przed startem)
        clearInterval(progInt);
        document.getElementById('simple-progress-fill').style.width = '100%';
        await showSimpleResult();
        return;
      }
    } catch(e) {}
  }
  clearInterval(progInt);
  document.getElementById('simple-status-text').textContent = 'Serwer nie odpowiada – sprawdź połączenie';
}

async function showSimpleResult() {
  try {
    var r  = await fetch('/api/gallery');
    var d  = await r.json();
    var images = d.images || [];
    if (!images.length) return;
    var latest = images[0]; // najnowsze
    _simpleLastResult = latest.filename;

    // Zatrzymaj live preview
    if (_previewInterval) { clearInterval(_previewInterval); _previewInterval = null; }

    if (_simplePendingNext) {
      // Sprawdź czy user wybrał już kolejne zdjęcie
      var nextInp = document.getElementById('simple-next-file-inp');
      var hasNext = nextInp && nextInp.files && nextInp.files[0];
      simpleResetNextZone();
      document.getElementById('simple-status').style.display = 'none';

      if (hasNext) {
        // Przenieś zdjęcie z "next" do slot 1 i od razu generuj
        var dt = new DataTransfer(); dt.items.add(nextInp.files[0]);
        var slot1 = document.getElementById('simple-file-inp');
        slot1.files = dt.files;
        // Pokaż podgląd i uruchom generowanie
        var reader = new FileReader();
        reader.onload = function(e) {
          document.getElementById('simple-preview-img').src = e.target.result;
          document.getElementById('simple-preview-wrap').style.display = 'block';
        };
        reader.readAsDataURL(nextInp.files[0]);
        await simpleGenerate();
      } else {
        // Nie wybrał zdjęcia - wróć do upload zone
        openSimpleAction(_simpleActiveTile);
      }
    } else {
      document.getElementById('simple-status').style.display = 'none';
      document.getElementById('simple-result').style.display = 'block';
      document.getElementById('simple-result-img').src       = '/gallery_image/' + latest.filename;
      loadSimpleRecent();
    }
  } catch(e) {}
}

function simpleDownload() {
  if (!_simpleLastResult) return;
  var a = document.createElement('a');
  a.href     = '/download/' + _simpleLastResult;
  a.download = _simpleLastResult;
  a.click();
}

var _simplePendingNext = false;  // czy po zakończeniu zacząć nowe

function simpleNewWhileGenerating() {
  // Pokaż strefę wyboru następnego zdjęcia
  _simplePendingNext = true;
  document.getElementById('simple-next-zone').style.display = 'block';
  document.getElementById('simple-next-btn').style.display  = 'none';
  // Załaduj ostatnie zdjęcia do strefy "następne"
  loadSimpleRecentInto('simple-next-recent-thumbs', 'simple-next-recent', function(file) {
    var dt = new DataTransfer(); dt.items.add(file);
    var inp = document.getElementById('simple-next-file-inp');
    inp.files = dt.files;
    simpleNextPreview(inp);
  });
}

function simpleNextPreview(inp) {
  if (!inp.files || !inp.files[0]) return;
  var reader = new FileReader();
  reader.onload = function(e) {
    document.getElementById('simple-next-preview-img').src = e.target.result;
    document.getElementById('simple-next-preview-wrap').style.display = 'block';
    document.getElementById('simple-next-ready-badge').style.display  = 'block';
  };
  reader.readAsDataURL(inp.files[0]);
}

function simpleResetNextZone() {
  _simplePendingNext = false;
  document.getElementById('simple-next-zone').style.display         = 'none';
  document.getElementById('simple-next-btn').style.display          = 'block';
  document.getElementById('simple-next-preview-wrap').style.display = 'none';
  document.getElementById('simple-next-ready-badge').style.display  = 'none';
  var inp = document.getElementById('simple-next-file-inp');
  if (inp) inp.value = '';
}

function simpleStartLivePreview() {
  if (_previewInterval) clearInterval(_previewInterval);
  _previewInterval = setInterval(async function() {
    try {
      var r = await fetch('/api/preview');
      var d = await r.json();
      var wrap = document.getElementById('simple-live-wrap');
      var img  = document.getElementById('simple-live-img');
      if (d.image) {
        img.src = 'data:image/jpeg;base64,' + d.image;
        if (wrap) wrap.style.display = 'block';
      } else if (d.processing !== false) {
        // Brak obrazu ale generowanie trwa - pokaż placeholder jeśli nie ma jeszcze img
        if (wrap && !img.src) wrap.style.display = 'block';
      }
    } catch(e) {}
  }, 2000);
}

function simpleGoBack() {
  // Wróć do upload z tym samym kafelkiem (zachowaj wybrane zdjęcie)
  var tile = _simpleActiveTile;
  document.getElementById('simple-result').style.display = 'none';
  if (tile) {
    openSimpleAction(tile);
  } else {
    simpleReset();
  }
}

function simpleGenerateAgain() {
  // Generuj ponownie z tym samym zdjęciem — wróć do upload zone bez resetowania pliku
  document.getElementById('simple-result').style.display        = 'none';
  document.getElementById('simple-upload-zone').style.display   = 'block';
  document.getElementById('simple-go-btn').style.display        = 'block';
  document.getElementById('simple-iterations-wrap').style.display = 'block';
}

function simpleReset() {
  _simpleActiveWf   = null;
  _simpleActiveTile = null;
  _simpleLastResult = null;
  simpleResetNextZone();
  document.getElementById('simple-tiles').style.display           = 'grid';
  document.getElementById('simple-upload-zone').style.display     = 'none';
  document.getElementById('simple-go-btn').style.display          = 'none';
  document.getElementById('simple-iterations-wrap').style.display = 'none';
  document.getElementById('simple-status').style.display          = 'none';
  document.getElementById('simple-result').style.display          = 'none';
  document.getElementById('simple-preview-wrap').style.display    = 'none';
  document.getElementById('simple-preview2-wrap').style.display   = 'none';
  var inp = document.getElementById('simple-file-inp');
  if (inp) inp.value = '';
  var inp2 = document.getElementById('simple-file-inp2');
  if (inp2) inp2.value = '';
}

async function restartComfyUI() {
  var btn = document.getElementById('restart-comfy-btn');
  var alert = document.getElementById('restart-alert');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Restartowanie...'; }
  try {
    var r = await fetch('/api/restart_comfy', {method: 'POST'});
    var d = await r.json();
    if (alert) {
      alert.textContent = d.status === 'ok' ? '✓ Restart ComfyUI zlecony. Odczekaj ~30s.' : '✗ Błąd: ' + (d.message || 'nieznany');
      alert.style.display = 'block';
      alert.style.background = d.status === 'ok' ? '#1a3a1a' : '#3a0000';
      alert.style.borderColor = alert.style.color = d.status === 'ok' ? '#4CAF50' : '#f44';
    }
  } catch(e) {
    if (alert) { alert.textContent = '✗ Błąd połączenia'; alert.style.display = 'block'; }
  }
  setTimeout(function() {
    if (btn) { btn.disabled = false; btn.textContent = '🔄 Restart ComfyUI'; }
  }, 5000);
}

function simpleOpenSettings() {
  showScreen('settings');
}

function simpleOpenGallery() {
  showScreen('gallery');
  // Dodaj przycisk powrotu do galerii jeśli go nie ma
  var galleryEl = document.getElementById('screen-gallery');
  if (galleryEl && !document.getElementById('simple-back-btn')) {
    var btn = document.createElement('button');
    btn.id = 'simple-back-btn';
    btn.textContent = '← Wróć';
    btn.style.cssText = 'margin:12px 16px 0;padding:8px 16px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#aaa;font-size:14px;cursor:pointer;display:block';
    btn.onclick = function() { showScreen('simple'); };
    galleryEl.insertBefore(btn, galleryEl.firstChild);
  }
}

function simpleLogout() {
  window.location.href = '/logout';
}

// ══ PRESETY ══════════════════════════════════════════════════════
var _presets = [];
var _presetEmojis = ['⭐','🎨','🌅','🌙','🔥','💫','🎭','🌊','🌿','💎','🦋','🎪'];
var _selectedPresetEmoji = '⭐';

async function loadPresets() {
  try {
    var r = await fetch('/api/presets');
    var d = await r.json();
    _presets = d.presets || [];
    renderPresetsBar();
  } catch(e) {}
}

function renderPresetsBar() {
  var bar   = document.getElementById('presets-bar');
  var chips = document.getElementById('presets-chips');
  if (!bar || !chips) return;
  if (!_presets.length) { bar.style.display = 'none'; return; }
  bar.style.display = 'block';
  chips.innerHTML = '';
  _presets.forEach(function(p) {
    var chip = document.createElement('button');
    chip.style.cssText = 'display:inline-flex;align-items:center;gap:5px;margin-right:6px;padding:5px 12px;background:#1a1a1a;border:1px solid #2a2a2a;border-radius:20px;color:#ccc;font-size:12px;cursor:pointer;white-space:nowrap;transition:all .2s';
    chip.innerHTML = (p.emoji || '⭐') + ' ' + (p.name || 'Preset');
    chip.onclick = function() { applyPreset(p.id); };
    chip.onmouseover = function() { chip.style.borderColor = '#e8b84b'; chip.style.color = '#e8b84b'; };
    chip.onmouseout  = function() { chip.style.borderColor = '#2a2a2a'; chip.style.color = '#ccc'; };

    // Long press = usuń
    var pressTimer;
    chip.addEventListener('touchstart', function() {
      pressTimer = setTimeout(function() { confirmDeletePreset(p.id, p.name); }, 700);
    });
    chip.addEventListener('touchend', function() { clearTimeout(pressTimer); });
    chips.appendChild(chip);
  });
}

async function applyPreset(pid) {
  try {
    var r = await fetch('/api/presets/' + pid + '/load', {method: 'POST'});
    var d = await r.json();
    if (d.status !== 'ok') return;
    var p = d.preset;
    // Wczytaj ustawienia do formularza
    if (p.workflow_id) {
      var wfSel = document.getElementById('workflow-select');
      if (wfSel) { wfSel.value = p.workflow_id; wfSel.dispatchEvent(new Event('change')); }
    }
    setTimeout(function() {
      if (p.iterations) { var it = document.getElementById('iterations'); if (it) it.value = p.iterations; }
      if (p.prefix  !== undefined) { var el = document.getElementById('prompt-prefix');  if (el) el.value = p.prefix; }
      if (p.suffix  !== undefined) { var el = document.getElementById('prompt-suffix');  if (el) el.value = p.suffix; }
      if (p.style_mode) { var el = document.getElementById('style-mode'); if (el) { el.value = p.style_mode; el.dispatchEvent(new Event('change')); } }
      setTimeout(function() {
        if (p.style_main)   { var el = document.getElementById('style-main');   if (el) { el.value = p.style_main;   el.dispatchEvent(new Event('change')); } }
        setTimeout(function() {
          if (p.style_sub)    { var el = document.getElementById('style-sub');    if (el) { el.value = p.style_sub;    el.dispatchEvent(new Event('change')); } }
          if (p.style_subsub) { var el = document.getElementById('style-subsub'); if (el) el.value = p.style_subsub; }
          // Zdjęcie - zaznacz w recent
          if (p.image_filename) {
            var thumb = document.querySelector('.recent-thumb[data-filename="' + p.image_filename + '"]');
            if (thumb) thumb.click();
          }
        }, 150);
      }, 150);
    }, 300);

    // Pokaż feedback
    var btn = document.getElementById('save-preset-btn');
    if (btn) { btn.textContent = (p.emoji || '⭐'); setTimeout(function(){ btn.textContent = '⭐'; }, 1200); }
  } catch(e) {}
}

function openSavePreset() {
  var modal = document.getElementById('preset-modal');
  if (!modal) return;
  // Wypełnij emoji picker
  var row = document.getElementById('preset-emoji-row');
  if (row) {
    row.innerHTML = '';
    _presetEmojis.forEach(function(e) {
      var btn = document.createElement('button');
      btn.textContent = e;
      btn.style.cssText = 'font-size:20px;background:none;border:2px solid ' + (e===_selectedPresetEmoji ? '#e8b84b' : '#2a2a2a') + ';border-radius:8px;padding:4px 8px;cursor:pointer';
      btn.onclick = function() {
        _selectedPresetEmoji = e;
        row.querySelectorAll('button').forEach(function(b){ b.style.borderColor = '#2a2a2a'; });
        btn.style.borderColor = '#e8b84b';
      };
      row.appendChild(btn);
    });
  }
  // Podsumowanie
  var wfEl = document.getElementById('workflow-select');
  var wfName = wfEl ? (wfEl.options[wfEl.selectedIndex]?.text || '') : '';
  var img = document.getElementById('img-image_1');
  var imgName = (img && img.dataset.recentFilename) ? img.dataset.recentFilename.substring(0,12) + '...' : 'brak';
  var sumEl = document.getElementById('preset-summary');
  if (sumEl) sumEl.textContent = 'Workflow: ' + wfName + ' · Zdjęcie: ' + imgName;
  modal.style.display = 'flex';
  var inp = document.getElementById('preset-name-inp');
  if (inp) { inp.value = ''; inp.focus(); }
}

function closePresetModal() {
  var modal = document.getElementById('preset-modal');
  if (modal) modal.style.display = 'none';
}

async function doSavePreset() {
  var name = (document.getElementById('preset-name-inp')?.value || '').trim();
  if (!name) { document.getElementById('preset-name-inp').focus(); return; }

  var wfSel = document.getElementById('workflow-select');
  var img   = document.getElementById('img-image_1');
  var preset = {
    name:          name,
    emoji:         _selectedPresetEmoji,
    workflow_id:   wfSel ? wfSel.value : '',
    iterations:    document.getElementById('iterations')?.value || '1',
    prefix:        document.getElementById('prompt-prefix')?.value  || '',
    suffix:        document.getElementById('prompt-suffix')?.value  || '',
    style_mode:    document.getElementById('style-mode')?.value     || '',
    style_main:    document.getElementById('style-main')?.value     || '',
    style_sub:     document.getElementById('style-sub')?.value      || '',
    style_subsub:  document.getElementById('style-subsub')?.value   || '',
    image_filename: (img && img.dataset.recentFilename) ? img.dataset.recentFilename : '',
  };
  try {
    var r = await fetch('/api/presets', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(preset)
    });
    var d = await r.json();
    if (d.status === 'ok') {
      closePresetModal();
      await loadPresets();
    }
  } catch(e) {}
}

async function confirmDeletePreset(pid, name) {
  if (confirm('Usunąć preset "' + name + '"?')) {
    await fetch('/api/presets/' + pid, {method: 'DELETE'});
    await loadPresets();
  }
}

// ══ HISTORIA USTAWIEŃ ══
var _lastSettings = null;

async function loadLastSettings() {
  try {
    var r = await fetch('/api/last_settings');
    var d = await r.json();
    if (d && d.workflow_id) {
      _lastSettings = d;
      var bar = document.getElementById('last-settings-bar');
      var lbl = document.getElementById('last-settings-label');
      if (bar && lbl) {
        var ago = '';
        if (d.saved_at) {
          var mins = Math.round((Date.now() - d.saved_at) / 60000);
          ago = mins < 60 ? mins + ' min temu' : Math.round(mins/60) + 'h temu';
        }
        lbl.textContent = d.workflow_name + (ago ? ' (' + ago + ')' : '');
        bar.style.display = 'block';
      }
    }
  } catch(e) {}
}

async function restoreLastSettings() {
  if (!_lastSettings) return;
  var d = _lastSettings;

  // Wybierz workflow
  var sel = document.getElementById('wf-select');
  if (sel && d.workflow_id) {
    sel.value = d.workflow_id;
    await selectWorkflow(d.workflow_id);
    // Poczekaj na wyrenderowanie formularza
    await new Promise(function(r) { setTimeout(r, 100); });
  }

  // Ustaw iterations
  var iter = document.getElementById('iterations');
  if (iter && d.iterations) iter.value = d.iterations;

  // Ustaw styl
  if (d.style_mode)   { var e = document.getElementById('style-mode');   if (e) e.value = d.style_mode; }
  if (d.style_main)   { var e = document.getElementById('style-main');   if (e) e.value = d.style_main; }
  if (d.style_sub)    { var e = document.getElementById('style-sub');    if (e) e.value = d.style_sub; }
  if (d.style_subsub) { var e = document.getElementById('style-subsub'); if (e) e.value = d.style_subsub; }

  // Ustaw prompt
  if (d.prefix) { var e = document.getElementById('prompt-prefix'); if (e) e.value = d.prefix; }
  if (d.suffix) { var e = document.getElementById('prompt-suffix'); if (e) e.value = d.suffix; }

  // Flash przycisk
  var bar = document.getElementById('last-settings-bar');
  if (bar) { bar.style.border = '1px solid #4CAF50'; setTimeout(function() { bar.style.border = ''; }, 1000); }
}

// ══ HARMONOGRAM ══
async function loadSchedule() {
  try {
    var r = await fetch('/api/schedule');
    var d = await r.json();
    var tog  = document.getElementById('sched-toggle');
    var time = document.getElementById('sched-time');
    if (tog)  tog.checked = !!d.enabled;
    if (time) time.value  = d.time || '22:00';
    updateScheduleUI(!!d.enabled, d);
    var info = document.getElementById('sched-config-info');
    if (info) {
      var wf  = d.last_workflow_name || '—';
      var img = d.last_image ? d.last_image.replace('mobile_','').substring(0,8) : '—';
      var lf  = d.last_fired ? ' · ostatnio: ' + d.last_fired : '';
      info.innerHTML = 'Workflow: <b>' + wf + '</b>  ·  Zdjęcie: <b>' + img + '</b>' + lf;
      info.style.display = (d.last_workflow_name || d.last_image) ? 'block' : 'none';
    }
  } catch(e) {}
}

function updateScheduleUI(enabled, data) {
  var row = document.getElementById('sched-time-row');
  var status = document.getElementById('sched-status');
  if (row) row.style.display = enabled ? 'block' : 'none';
  if (status) {
    if (enabled) {
      var t = document.getElementById('sched-time')?.value || '22:00';
      var wfInfo = (data && data.last_workflow_name) ? ' · ' + data.last_workflow_name : '';
      var lastFired = (data && data.last_fired) ? ' · ostatnio: ' + data.last_fired : '';
      status.textContent = 'Aktywny – uruchomienie o ' + t + wfInfo + lastFired;
      status.style.color = '#4CAF50';
    } else {
      status.textContent = 'Wyłączony';
      status.style.color = '#555';
    }
  }
}

async function saveSchedule() {
  var enabled = document.getElementById('sched-toggle').checked;
  var time    = document.getElementById('sched-time').value;
  try {
    var r = await fetch('/api/schedule', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: enabled, time: time})
    });
    var d = await r.json();
    if (d.status === 'ok') {
      updateScheduleUI(enabled);
      showAlert('sched-alert', enabled ? 'Harmonogram zapisany: ' + time : 'Harmonogram wyłączony', 'ok');
    }
  } catch(e) { showAlert('sched-alert', 'Błąd zapisu', 'err'); }
}

// ══ PROFILE ══
var _profiles = [];
var _activeProfile = null;
var _editingProfileId = null;

var PROFILE_EMOJIS = ['👤','👩','👨','🧑','👧','👦','🧔','👱','🧒','🧓','👴','👵',
  '🦊','🐱','🐶','🐻','🐼','🦁','🐯','🐸','🐧','🦋','🌸','🌟','⭐','🔥','💎','🎭'];

async function loadProfiles() {
  try {
    var r = await fetch('/api/profiles');
    var d = await r.json();
    _profiles      = d.profiles || [];
    _activeProfile = d.active   || null;
    updateProfileBadge();
  } catch(e) {}
  // Pokaż przycisk Admin jeśli rola = admin
  try {
    var rm = await fetch('/api/me');
    var dm = await rm.json();
    var adminBtn = document.getElementById('nav-admin');
    if (adminBtn && dm.role === 'admin') adminBtn.style.display = '';
    // Tryb uproszczony - przejdź na simple UI
    if (dm.simple_mode) {
      initSimpleMode(dm);
    }
  } catch(e) {}
}

function updateProfileBadge() {
  var btn   = document.getElementById('profile-switcher-btn');
  var avatar = document.getElementById('profile-avatar');
  var label  = document.getElementById('profile-name-label');
  if (!btn) return;
  if (_activeProfile) {
    var p = _profiles.find(function(x) { return x.id === _activeProfile; });
    if (p) {
      if (avatar) avatar.textContent = p.avatar || '👤';
      if (label)  label.textContent  = p.name   || 'Profil';
      btn.style.borderColor = '#4CAF50';
    }
  } else {
    if (avatar) avatar.textContent = '👤';
    if (label)  label.textContent  = 'Brak profilu';
    btn.style.borderColor = '#333';
  }
}

function openProfileSwitcher() {
  var modal = document.getElementById('profile-modal');
  if (!modal) return;
  renderProfileList();
  modal.style.display = 'block';
}

function closeProfileSwitcher() {
  var modal = document.getElementById('profile-modal');
  if (modal) modal.style.display = 'none';
}

function renderProfileList() {
  var el = document.getElementById('profile-list-modal');
  if (!el) return;
  if (!_profiles.length) {
    el.innerHTML = '<div style="color:#555;text-align:center;padding:16px">Brak profili – dodaj pierwszy</div>';
    return;
  }
  el.innerHTML = _profiles.map(function(p) {
    var isActive = p.id === _activeProfile;
    var bg  = isActive ? '#1a3a1a' : '#252528';
    var bdr = isActive ? '#4CAF50' : 'transparent';
    var clr = isActive ? '#4CAF50' : '#fff';
    return '<div class="prof-row" data-pid="' + p.id + '" style="display:flex;align-items:center;gap:12px;padding:10px 8px;border-radius:10px;margin-bottom:6px;background:' + bg + ';border:1px solid ' + bdr + ';cursor:pointer">'
      + '<span style="font-size:24px">' + (p.avatar || '👤') + '</span>'
      + '<div style="flex:1">'
      + '<div style="font-size:14px;font-weight:600;color:' + clr + '">' + (p.name || 'Profil') + (isActive ? ' ✓' : '') + '</div>'
      + '<div style="font-size:11px;color:#555;margin-top:1px">' + (p.workflow_name || 'brak workflow') + '</div>'
      + '</div>'
      + '<button class="prof-edit-btn" data-pid="' + p.id + '" style="background:none;border:none;color:#555;font-size:18px;cursor:pointer;padding:4px">✏️</button>'
      + '</div>';
  }).join('');
  // Podepnij eventy przez delegation
  el.querySelectorAll('.prof-row').forEach(function(row) {
    row.addEventListener('click', function(ev) {
      if (ev.target.classList.contains('prof-edit-btn') || ev.target.closest('.prof-edit-btn')) return;
      activateProfile(row.dataset.pid);
    });
  });
  el.querySelectorAll('.prof-edit-btn').forEach(function(btn) {
    btn.addEventListener('click', function(ev) {
      ev.stopPropagation();
      openEditProfile(btn.dataset.pid);
    });
  });
}

async function activateProfile(pid) {
  try {
    var r = await fetch('/api/profiles/activate/' + pid, {method: 'POST'});
    var d = await r.json();
    if (d.status === 'ok') {
      _activeProfile = pid;
      updateProfileBadge();
      closeProfileSwitcher();
      // Załaduj ustawienia profilu do formularza
      await loadLastSettings();
      showAlert && showAlert('', '', '');
    }
  } catch(e) {}
}

function openNewProfile() {
  _editingProfileId = null;
  document.getElementById('profile-edit-title').textContent = 'Nowy profil';
  document.getElementById('pe-name').value    = '';
  document.getElementById('pe-prefix').value  = '';
  document.getElementById('pe-workflow').value = '';
  document.getElementById('pe-style-mode').value   = '';
  document.getElementById('pe-style-main').value   = '';
  document.getElementById('pe-style-sub').value    = '';
  var pss = document.getElementById('pe-style-subsub'); if (pss) pss.value = '';
  document.getElementById('pe-delete-btn').style.display = 'none';
  // Ustaw domyślny avatar
  _selectedEmoji = '👤';
  renderEmojiPicker();
  closeProfileSwitcher();
  document.getElementById('profile-edit-modal').style.display = 'block';
  // Załaduj workflow options
  populateProfileWorkflows();
}

function openEditProfile(pid) {
  var p = _profiles.find(function(x) { return x.id === pid; });
  if (!p) return;
  _editingProfileId = pid;
  _selectedEmoji    = p.avatar || '👤';
  document.getElementById('profile-edit-title').textContent = 'Edytuj profil';
  document.getElementById('pe-name').value     = p.name    || '';
  document.getElementById('pe-prefix').value   = p.prefix  || '';
  document.getElementById('pe-delete-btn').style.display = 'inline-block';
  renderEmojiPicker();
  closeProfileSwitcher();
  document.getElementById('profile-edit-modal').style.display = 'block';
  populateProfileWorkflows(p.workflow_id);
  // Styl
  setTimeout(function() {
    var sm  = document.getElementById('pe-style-mode');
    var sM  = document.getElementById('pe-style-main');
    var ss  = document.getElementById('pe-style-sub');
    var sss = document.getElementById('pe-style-subsub');
    if (sm)  sm.value  = p.style_mode   || '';
    if (sM)  sM.value  = p.style_main   || '';
    if (ss)  ss.value  = p.style_sub    || '';
    if (sss) sss.value = p.style_subsub || '';
  }, 100);
}

function closeEditProfile() {
  document.getElementById('profile-edit-modal').style.display = 'none';
}

var _selectedEmoji = '👤';

function renderEmojiPicker() {
  var el = document.getElementById('emoji-picker');
  if (!el) return;
  var html = '';
  PROFILE_EMOJIS.forEach(function(e) {
    var sel = e === _selectedEmoji;
    var bdr = sel ? '#4CAF50' : 'transparent';
    html += '<span class="ep-btn" data-emoji="' + e.codePointAt(0) + '" style="font-size:22px;cursor:pointer;padding:4px;border-radius:6px;border:2px solid ' + bdr + ';transition:border 0.15s">' + e + '</span>';
  });
  el.innerHTML = html;
  el.querySelectorAll('.ep-btn').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var cp = parseInt(btn.dataset.emoji);
      selectEmoji(String.fromCodePoint(cp));
    });
  });
}

function selectEmoji(e) {
  _selectedEmoji = e;
  renderEmojiPicker();
}

function populateProfileWorkflows(selectedId) {
  var sel = document.getElementById('pe-workflow');
  if (!sel || typeof workflows === 'undefined') return;
  sel.innerHTML = '<option value="">— brak —</option>'
    + (workflows || []).map(function(w) {
      return '<option value="' + w.id + '"' + (w.id === selectedId ? ' selected' : '') + '>' + w.name + '</option>';
    }).join('');
  async function loadStylesForWf(wfId) {
    var mainSel   = document.getElementById('pe-style-main');
    var subSel    = document.getElementById('pe-style-sub');
    var subsubSel = document.getElementById('pe-style-subsub');
    if (!wfId) return;
    try {
      var r = await fetch('/api/workflows/' + encodeURIComponent(wfId) + '/style_options');
      var d = await r.json();
      var opts = d.options || {};
      var mainList   = Array.isArray(opts.main)   ? opts.main   : [];
      var subList    = Array.isArray(opts.sub)    ? opts.sub    : [];
      var subsubList = Array.isArray(opts.subsub) ? opts.subsub : [];
      if (mainSel)   mainSel.innerHTML   = '<option value="">— brak —</option>' + mainList.map(function(k)   { return '<option value="'+k+'">'+k+'</option>'; }).join('');
      if (subSel)    subSel.innerHTML    = '<option value="">— brak —</option>' + subList.map(function(k)    { return '<option value="'+k+'">'+k+'</option>'; }).join('');
      if (subsubSel) subsubSel.innerHTML = '<option value="">— brak —</option>' + subsubList.map(function(k) { return '<option value="'+k+'">'+k+'</option>'; }).join('');
    } catch(e) {}
  }
  sel.onchange = function() { loadStylesForWf(sel.value); };
  if (selectedId) loadStylesForWf(selectedId);
}

async function saveEditProfile() {
  var name = document.getElementById('pe-name').value.trim();
  if (!name) { showAlert('pe-alert', 'Podaj nazwę profilu', 'err'); return; }
  var wfSel = document.getElementById('pe-workflow');
  var wfId  = wfSel ? wfSel.value : '';
  var wfName = wfId ? (wfSel.options[wfSel.selectedIndex] || {}).text || '' : '';
  var profile = {
    id:           _editingProfileId || '',
    avatar:       _selectedEmoji,
    name:         name,
    workflow_id:  wfId,
    workflow_name: wfName,
    prefix:       document.getElementById('pe-prefix').value,
    style_mode:   document.getElementById('pe-style-mode').value,
    style_main:   document.getElementById('pe-style-main').value,
    style_sub:    document.getElementById('pe-style-sub').value,
    style_subsub: document.getElementById('pe-style-subsub')?.value || '',
  };
  try {
    var r = await fetch('/api/profiles', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(profile)
    });
    var d = await r.json();
    if (d.status === 'ok') {
      await loadProfiles();
      closeEditProfile();
    }
  } catch(e) { showAlert('pe-alert', 'Błąd zapisu', 'err'); }
}

async function deleteEditProfile() {
  if (!_editingProfileId) return;
  if (!confirm('Usunąć ten profil?')) return;
  try {
    await fetch('/api/profiles/' + _editingProfileId, {method: 'DELETE'});
    await loadProfiles();
    closeEditProfile();
  } catch(e) {}
}

// ══ WAKE LOCK ══
var _wakeLock = null;
var _wakeLockEnabled = false;

async function toggleWakeLockSettings() {
  await toggleWakeLock();
  // Synchronizuj przycisk w settings
  var btnS = document.getElementById('wake-lock-btn-s');
  if (btnS) {
    btnS.textContent = _wakeLockEnabled ? '🔆 Blokada aktywna' : '💡 Włącz blokadę wygaszania';
    btnS.style.background = _wakeLockEnabled ? '#1a3a00' : '';
    btnS.style.borderColor = _wakeLockEnabled ? '#4CAF50' : '';
    btnS.style.color = _wakeLockEnabled ? '#4CAF50' : '';
  }
}

async function toggleWakeLock() {
  var btn = document.getElementById('wake-lock-btn');
  if (_wakeLockEnabled) {
    // Wyłącz
    if (_wakeLock) { try { await _wakeLock.release(); } catch(e) {} _wakeLock = null; }
    _wakeLockEnabled = false;
    if (btn) { btn.textContent = '💡'; btn.style.background = '#1a1a2e'; btn.style.borderColor = '#333'; btn.style.color = '#888'; btn.title = 'Nie wygaszaj ekranu'; }
  } else {
    // Włącz
    if (!('wakeLock' in navigator)) {
      alert('Wake Lock nie jest wspierany przez tę przeglądarkę. Użyj Chrome/Safari na iOS 16.4+');
      return;
    }
    try {
      _wakeLock = await navigator.wakeLock.request('screen');
      _wakeLockEnabled = true;
      if (btn) { btn.textContent = '🔆'; btn.style.background = '#1a3a00'; btn.style.borderColor = '#4CAF50'; btn.style.color = '#4CAF50'; btn.title = 'Ekran aktywny – kliknij aby wyłączyć'; }
      // Auto-przywróć po powrocie do karty (iOS/Android zwalniają wakeLock przy hide)
      _wakeLock.addEventListener('release', function() {
        _wakeLock = null;
        if (_wakeLockEnabled) {
          // Spróbuj przywrócić
          setTimeout(async function() {
            if (_wakeLockEnabled && document.visibilityState === 'visible') {
              try {
                _wakeLock = await navigator.wakeLock.request('screen');
              } catch(e) {}
            }
          }, 500);
        }
      });
    } catch(e) {
      alert('Nie można aktywować Wake Lock: ' + e.message);
    }
  }
}

// Przywróć wake lock po powrocie do karty
document.addEventListener('visibilitychange', async function() {
  if (_wakeLockEnabled && document.visibilityState === 'visible' && !_wakeLock) {
    try {
      _wakeLock = await navigator.wakeLock.request('screen');
    } catch(e) {}
  }
});

// Rejestracja hooków showScreen dla preview i galerii
window._onShowScreen = function(name) {
  if (name === 'status')  startPreview();
  else                    stopPreview();
  if (name === 'gallery') loadGallery();
  if (name === 'stats') loadStats();
  if (name === 'new') {
    loadRecentImages().then(function() {
      document.querySelectorAll('[id^="recent-"]').forEach(function(el) {
        var role = el.id.replace('recent-', '');
        renderRecentThumbs(role);
      });
    });
    loadLastSettings();
  }
};

// Załaduj recent images, ostatnie ustawienia i profile przy starcie
loadRecentImages();
loadLastSettings();
loadProfiles();

// ── Ladowanie listy workflow ──
async function loadWorkflows() {
  try {
    workflows = await fetch('/api/workflows').then(r => r.json());
    const sel = document.getElementById('wf-select');
    if (!workflows.length) {
      sel.innerHTML = '<option value="">Brak workflow – dodaj w Kreatorze</option>';
      document.getElementById('dynamic-fields').innerHTML =
        '<div class="card" style="text-align:center;padding:24px">' +
        '<div style="font-size:36px;margin-bottom:10px">⚙️</div>' +
        '<div style="font-weight:600;margin-bottom:6px">Brak skonfigurowanych workflow</div>' +
        '<div style="color:#666;font-size:13px;margin-bottom:16px">Dodaj workflow przez Kreator, potem wróć tutaj.</div>' +
        '<a href="/kreator" target="_blank" style="display:inline-block;padding:10px 20px;background:#4CAF50;color:#000;border-radius:10px;font-weight:700;text-decoration:none">Otwórz Kreator →</a>' +
        '</div>';
      document.getElementById('submit-btn').style.display = 'none';
      return true;
    }
    sel.innerHTML = workflows.map(w =>
      '<option value="' + w.id + '">' + w.name + '</option>'
    ).join('');
    document.getElementById('submit-btn').style.display = 'block';
    await selectWorkflow(workflows[0].id);
    return true;
  } catch(e) { return false; }
}

document.getElementById('wf-select').addEventListener('change', async function() {
  await selectWorkflow(this.value);
});

async function selectWorkflow(wfId) {
  currentWf = workflows.find(w => w.id === wfId);
  if (!currentWf) return;
  styleOptions = {};

  // Pobierz pelne mappings z API (z wartościami domyslnymi)
  try {
    const cfgRes = await fetch('/api/workflows/' + wfId + '/config').then(r => r.json());
    if (cfgRes && cfgRes.mappings) {
      currentWf._mappings = cfgRes.mappings;
    }
  } catch(e) { currentWf._mappings = []; }

  // Pobierz opcje stylu jesli workflow ma styl
  if (currentWf.has_style) {
    try {
      const r = await fetch('/api/workflows/' + wfId + '/style_options').then(r => r.json());
      styleOptions = r.options || {};
    } catch(e) {}
  }
  renderDynamicFields();
}

function renderDynamicFields() {
  const el = document.getElementById('dynamic-fields');
  if (!currentWf) { el.innerHTML = ''; return; }
  let html = '';

  // Pobierz mappings z pełnej konfiguracji (currentWf.mappings_full jeśli załadowane)
  const mappings = currentWf._mappings || [];

  // Zdjecia – zawsze
  html += '<div class="card"><div class="card-title">Zdjecia</div>';
  const imgMappings = mappings.filter(function(m) { return m.role === 'image_1' || m.role === 'image_2'; });
  if (imgMappings.length) {
    imgMappings.forEach(function(m) {
      html += '<label>' + (m.label || (m.role === 'image_1' ? 'Zdjecie glowne' : 'Zdjecie 2')) + '</label>';
      html += '<input type="file" id="img-' + m.role + '" accept="image/*">';
      html += '<div id="recent-' + m.role + '" class="recent-thumbs"></div>';
    });
  } else {
    html += '<label>Zdjecie glowne</label>';
    html += '<input type="file" id="img-image_1" accept="image/*">';
    html += '<div id="recent-image_1" class="recent-thumbs"></div>';
    if (currentWf.has_image2) {
      html += '<label>Zdjecie 2</label>';
      html += '<input type="file" id="img-image_2" accept="image/*">';
      html += '<div id="recent-image_2" class="recent-thumbs"></div>';
    }
  }
  html += '</div>';

  // Styl
  if (currentWf.has_style && Object.keys(styleOptions).length) {
    html += '<div class="card"><div class="card-title">Styl</div>';
    html += '<label>Tryb</label>';
    html += '<select id="style-mode">' + (styleOptions.mode||[]).map(function(v) { return '<option>' + v + '</option>'; }).join('') + '</select>';
    html += '<div class="grid-3">';
    html += '<div><label>Main</label><select id="style-main">' + (styleOptions.main||[]).map(function(v) { return '<option>' + v + '</option>'; }).join('') + '</select></div>';
    html += '<div><label>Sub</label><select id="style-sub">' + (styleOptions.sub||[]).map(function(v) { return '<option>' + v + '</option>'; }).join('') + '</select></div>';
    html += '<div><label>Subsub</label><select id="style-subsub">' + (styleOptions.subsub||[]).map(function(v) { return '<option>' + v + '</option>'; }).join('') + '</select></div>';
    html += '</div></div>';
  }

  // Prompt – obsługa wielu mapowań prompt
  const promptMappings = mappings.filter(function(m) { return m.role === 'prompt'; });
  if (promptMappings.length) {
    html += '<div class="card"><div class="card-title">Prompt</div>';
    promptMappings.forEach(function(pm, pmIdx) {
      const hasPrefix = pm.prefix_field;
      // Dla pierwszego prompt z prefix_field używamy id=prompt-prefix / prompt-suffix
      // dla pozostałych używamy id=prompt-prefix-N / prompt-suffix-N
      const prefixId = pmIdx === 0 ? 'prompt-prefix' : ('prompt-prefix-' + pmIdx);
      const suffixId = pmIdx === 0 ? 'prompt-suffix' : ('prompt-suffix-' + pmIdx);

      // Etykieta sekcji jeśli jest więcej promptów
      if (promptMappings.length > 1) {
        const secLabel = pm.simple_label || pm.node_title || ('Prompt ' + (pmIdx+1));
        html += '<div style="font-size:11px;color:#555;margin-top:' + (pmIdx>0?'14px':'2px') + ';margin-bottom:4px;text-transform:uppercase;letter-spacing:0.5px">' + escHtml(secLabel) + '</div>';
      }

      if (hasPrefix) {
        const prefixDef = pm._prefix_default || '';
        html += '<label>Prefix (przed stylami)</label>';
        html += '<textarea id="' + prefixId + '" rows="3" data-prompt-idx="' + pmIdx + '">' + escHtml(prefixDef) + '</textarea>';
      }
      const suffixDef = pm._suffix_default || '';
      const suffixLabel = hasPrefix ? 'Suffix (po stylach)' : (pm.simple_label || 'Opis / instrukcje');
      html += '<label>' + escHtml(suffixLabel) + '</label>';
      html += '<textarea id="' + suffixId + '" rows="' + (pmIdx===0?'5':'3') + '" data-prompt-idx="' + pmIdx + '">' + escHtml(suffixDef) + '</textarea>';
    });
    html += '</div>';
  }

  // Custom pola
  const customMappings = mappings.filter(function(m) { return m.role === 'custom'; });
  if (customMappings.length) {
    html += '<div class="card"><div class="card-title">Opcje zaawansowane</div>';
    customMappings.forEach(function(m) {
      const fid = 'custom-' + m.form_key;
      html += '<div class="custom-field">';
      html += '<label>' + (m.label || m.field) + '</label>';
      if (m.ctrl_type === 'slider') {
        const defVal = parseFloat(m.default||0.5).toFixed(2);
        html += '<div style="display:flex;align-items:center">';
        html += '<input type="range" id="' + fid + '" data-val="' + fid + '-val" min="0" max="1" step="0.05" value="' + defVal + '" class="wf-slider">';
        html += '<span class="slider-val" id="' + fid + '-val">' + defVal + '</span>';
        html += '</div>';
      } else if (m.ctrl_type === 'number') {
        html += '<input type="number" id="' + fid + '" value="' + (m.default||0) + '">';
      } else if (m.ctrl_type === 'select' && m.options && m.options.length) {
        var defVal = String(m.default||'');
        var opts = m.options.map(function(o) {
          return '<option value="' + escHtml(String(o)) + '"' + (String(o)===defVal?' selected':'') + '>' + escHtml(String(o)) + '</option>';
        }).join('');
        html += '<select id="' + fid + '" style="width:100%;padding:10px;background:#1a1a1d;color:#e0e0e0;border:1px solid #333;border-radius:8px;font-size:14px">' + opts + '</select>';
      } else {
        html += '<input type="text" id="' + fid + '" value="' + escHtml(String(m.default||'')) + '">';
      }
      html += '</div>';
    });
    html += '</div>';
  }

  el.innerHTML = html;
  el.querySelectorAll('.wf-slider').forEach(function(s) {
    const valEl = document.getElementById(s.getAttribute('data-val'));
    if (valEl) s.addEventListener('input', function() { valEl.textContent = parseFloat(this.value).toFixed(2); });
  });
  // Renderuj miniatury ostatnich zdjęć
  el.querySelectorAll('[id^="recent-"]').forEach(function(div) {
    var role = div.id.replace('recent-', '');
    renderRecentThumbs(role);
  });
  // Eventy dla file inputów - czyszczenie recent przy wyborze pliku
  el.querySelectorAll('input[type="file"]').forEach(function(input) {
    input.addEventListener('change', function() { onImageSelected(this); });
  });
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ══════════════════════════════════════════════
// GENEROWANIE
// ══════════════════════════════════════════════
async function submitGenerate() {
  if (!currentWf) { alert('Wybierz workflow'); return; }
  const img1 = document.getElementById('img-image_1');
  const img1HasFile = img1 && img1.files.length > 0;
  const img1HasRecent = img1 && img1.dataset.recentUrl;
  if (!img1HasFile && !img1HasRecent) { alert('Dodaj zdjecie'); return; }

  const btn = document.getElementById('submit-btn');
  btn.disabled = true; btn.textContent = 'Wysylanie...';

  try {
    // Jeśli wybrano ostatnie zdjęcie - pobierz blob i wstaw do FormData
    async function getImageBlob(inputEl) {
      if (inputEl && inputEl.files.length > 0) return inputEl.files[0];
      if (inputEl && inputEl.dataset.recentUrl) {
        const resp = await fetch(inputEl.dataset.recentUrl);
        const blob = await resp.blob();
        return new File([blob], inputEl.dataset.recentFilename || 'photo.jpg', {type: 'image/jpeg'});
      }
      return null;
    }

    const fd = new FormData();
    fd.append('workflow_id', currentWf.id);
    fd.append('iterations', document.getElementById('iterations').value);
    const blob1 = await getImageBlob(img1);
    if (blob1) fd.append('image_1', blob1);

    const img2 = document.getElementById('img-image_2');
    const blob2 = await getImageBlob(img2);
    if (blob2) fd.append('image_2', blob2);

    // Zbierz wartości WSZYSTKICH pól prompt (może być kilka)
    const promptMappingsSubmit = (currentWf.mappings||[]).filter(function(m){return m.role==='prompt';});
    promptMappingsSubmit.forEach(function(pm, pmIdx) {
      const suffixId = pmIdx === 0 ? 'prompt-suffix' : ('prompt-suffix-' + pmIdx);
      const prefixId = pmIdx === 0 ? 'prompt-prefix' : ('prompt-prefix-' + pmIdx);
      const suffixEl = document.getElementById(suffixId);
      const prefixEl = document.getElementById(prefixId);
      if (pmIdx === 0) {
        // Pierwszy prompt zachowuje stare klucze 'suffix'/'prefix' dla kompatybilności
        if (suffixEl) fd.append('suffix', suffixEl.value);
        if (prefixEl) fd.append('prefix', prefixEl.value);
      } else {
        // Kolejne prompty wysyłane jako 'extra_suffix_N' i 'extra_prefix_N'
        // oraz jako klucze per node_id::field dla inject_workflow_values
        if (suffixEl) {
          fd.append('extra_suffix_' + pmIdx, suffixEl.value);
          fd.append(pm.node_id + '::' + pm.field, suffixEl.value);
        }
        if (prefixEl && pm.prefix_field) {
          fd.append('extra_prefix_' + pmIdx, prefixEl.value);
          fd.append(pm.node_id + '::' + pm.prefix_field, prefixEl.value);
        }
      }
    });

    // Styl
    ['style-mode','style-main','style-sub','style-subsub'].forEach(id => {
      const el = document.getElementById(id);
      if (el) fd.append(id.replace('-','_').replace('style_','style_'), el.value);
    });
    fd.append('style_mode',   document.getElementById('style-mode')   ?.value || 'auto');
    fd.append('style_main',   document.getElementById('style-main')   ?.value || '');
    fd.append('style_sub',    document.getElementById('style-sub')    ?.value || '');
    fd.append('style_subsub', document.getElementById('style-subsub') ?.value || '');

    // Custom pola
    if (currentWf.custom_inputs) {
      currentWf.custom_inputs.forEach(m => {
        const el = document.getElementById('custom-' + m.form_key);
        if (el) fd.append(m.form_key, el.value);
      });
    }

    // Zapisz ostatnie ustawienia
    try {
      var lastSettings = {
        workflow_id:   currentWf.id,
        workflow_name: currentWf.name,
        iterations:    document.getElementById('iterations').value,
        suffix:        document.getElementById('prompt-suffix')?.value || '',
        prefix:        document.getElementById('prompt-prefix')?.value || '',
        style_mode:    document.getElementById('style-mode')?.value    || '',
        style_main:    document.getElementById('style-main')?.value    || '',
        style_sub:     document.getElementById('style-sub')?.value     || '',
        style_subsub:  document.getElementById('style-subsub')?.value  || '',
        image_filename: (document.getElementById('img-image_1')?.dataset?.recentFilename) || '',
        saved_at:      Date.now()
      };
      await fetch('/api/last_settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(lastSettings)
      });
    } catch(e) {}

    const r = await fetch('/generate_v2', { method: 'POST', body: fd });
    const data = await r.json();
    if (data.status === 'ok') {
      btn.disabled = false; btn.textContent = 'Generuj obrazy';
      showScreen('status');
    } else {
      alert('Blad: ' + (data.message || r.status));
      btn.disabled = false; btn.textContent = 'Generuj obrazy';
    }
  } catch(err) {
    alert('Blad polaczenia: ' + err);
    btn.disabled = false; btn.textContent = 'Generuj obrazy';
  }
}

// ══════════════════════════════════════════════
// SSE
// ══════════════════════════════════════════════
let sseSource=null, sseRetry=2000, wasProcessing=false;

function connectSSE() {
  if (sseSource) sseSource.close();
  sseSource = new EventSource('/api/sse');
  sseSource.onopen = () => {
    document.getElementById('sse-dot').className='dot ok';
    document.getElementById('sse-label').textContent='Live: polaczony';
    sseRetry=2000;
  };
  sseSource.onmessage = e => { try { applySSE(JSON.parse(e.data)); } catch(x){} };
  sseSource.onerror = () => {
    document.getElementById('sse-dot').className='dot err';
    document.getElementById('sse-label').textContent='Live: retry...';
    sseSource.close();
    setTimeout(connectSSE, sseRetry);
    sseRetry=Math.min(sseRetry*1.5, 15000);
  };
}

function applySSE(d) {
  const badge = document.getElementById('status-badge');
  if (d.is_processing) { badge.textContent='Pracuje'; badge.className='badge working'; }
  else                 { badge.textContent='Gotowy';  badge.className='badge'; }

  if (d.is_processing && !document.getElementById('screen-status').classList.contains('active'))
    showNotif('status', d.current_iter||1);

  const lv = document.getElementById('live-text');
  lv.textContent = d.status_text;
  lv.className = 'live-text ' + (d.is_processing ? 'busy' : 'done');
  document.getElementById('stop-btn').style.display     = d.is_processing ? 'block' : 'none';
  document.getElementById('new-task-btn').style.display = d.is_processing ? 'none'  : 'block';
  if (wasProcessing && !d.is_processing) refreshQueue();
  wasProcessing = d.is_processing;

  document.getElementById('d-iter').textContent = d.total_iter>0 ? d.current_iter+' / '+d.total_iter : '—';
  document.getElementById('d-node').textContent = d.current_node || '—';
  // Wylosowany styl
  var styleRow = document.getElementById('d-style-row');
  var styleEl  = document.getElementById('d-style');
  if (d.current_style && styleRow && styleEl) {
    styleEl.textContent = d.current_style;
    styleRow.style.display = '';
  } else if (styleRow) {
    styleRow.style.display = 'none';
  }
  const sm=d.step_max||0, sv=d.step_value||0;
  if (sm>0) {
    const pct=Math.round(sv/sm*100);
    document.getElementById('d-step').textContent = sv+' / '+sm+' ('+pct+'%)';
    document.getElementById('d-bar').style.width  = pct+'%';
  } else {
    document.getElementById('d-step').textContent = d.current_node?'czeka...':'—';
    document.getElementById('d-bar').style.width  = '0%';
  }
}

// ══════════════════════════════════════════════
// KOLEJKA
// ══════════════════════════════════════════════
async function refreshQueue() {
  try {
    const q = await fetch('/api/queue').then(r=>r.json());
    renderQueue('q-running-list', q.queue_running_details||[], 'running', 'Brak aktywnych zadan');
    renderQueue('q-pending-list', q.queue_pending_details||[], 'pending', 'Kolejka pusta');
    const total=(q.queue_running||0)+(q.queue_pending||0);
    if (total>0 && !document.getElementById('screen-queue').classList.contains('active'))
      showNotif('queue', total);
    document.getElementById('g-pending').textContent = total>0 ? total+' zadan' : 'Brak';
  } catch(e){}
}
function renderQueue(cid, items, type, empty) {
  const el=document.getElementById(cid);
  if (!items.length) { el.innerHTML='<div class="queue-empty">'+empty+'</div>'; return; }
  el.innerHTML=items.map((item,i)=>{
    const label=item.prompt_id?item.prompt_id.slice(0,8)+'...':('Zadanie '+(i+1));
    const badge=type==='running'?'<span class="q-badge running">Aktywne</span>':'<span class="q-badge pending">#'+(i+1)+'</span>';
    return '<div class="queue-item"><span class="q-icon">'+(type==='running'?'⚡':'⏳')+'</span><div class="q-info"><div class="q-title">'+label+'</div></div>'+badge+'</div>';
  }).join('');
}
async function clearQueue() {
  if (!confirm('Przerwac i wyczysc?')) return;
  try { await fetch('/stop',{method:'POST'}); } catch(e){}
  setTimeout(refreshQueue, 800);
}

// ══════════════════════════════════════════════
// GPU
// ══════════════════════════════════════════════
async function refreshGPU() {
  try {
    const d = await fetch('/api/laptop_status').then(r=>r.json());
    document.getElementById('g-temp').textContent  = d.gpu.temp;
    document.getElementById('g-util').textContent  = d.gpu.util;
    document.getElementById('g-vram').textContent  = d.gpu.vram;
    document.getElementById('g-comfy').textContent = d.realtime_status;
    document.getElementById('g-wf').textContent    = d.last_workflow||'—';
  } catch(e){}
}
async function pollGPU()     { await refreshGPU();     setTimeout(pollGPU,     4000); }
window.addEventListener('resize', function() {
  if (document.getElementById('screen-stats').classList.contains('active')) loadStats();
});
async function pollQueueBg() { await refreshQueue();   setTimeout(pollQueueBg, 5000); }

// ══════════════════════════════════════════════
// USTAWIENIA
// ══════════════════════════════════════════════
async function loadSettings() {
  try {
    const s = await fetch('/api/settings').then(r=>r.json());
    document.getElementById('s-tg-chat').value    = s.telegram_chat_id || '';
    document.getElementById('s-comfy-url').value  = s.comfy_url        || '';
    document.getElementById('s-image-dir').value  = s.image_dir        || '';
    document.getElementById('s-vram-wait').value  = s.vram_free_wait   || 3;
    if (s.telegram_token_set)
      document.getElementById('s-tg-token').placeholder = 'Zapisany: ' + s.telegram_token_masked;
    const pwdStatus = document.getElementById('pwd-status');
    if (pwdStatus) {
      pwdStatus.textContent = s.password_set
        ? 'Status: Ochrona WLACZONA (haslo ustawione)'
        : 'Status: Ochrona WYLACZONA (brak hasla)';
      pwdStatus.style.color = s.password_set ? '#22c55e' : '#f59e0b';
    }
  } catch(e){}
}

async function savePassword() {
  const np = document.getElementById('s-new-password').value;
  const cp = document.getElementById('s-confirm-password').value;
  const alert = document.getElementById('pwd-alert');
  if (!np) { showAlert('pwd-alert', 'Wpisz haslo', false); return; }
  if (np !== cp) { showAlert('pwd-alert', 'Hasla nie sa zgodne!', false); return; }
  if (np.length < 4) { showAlert('pwd-alert', 'Haslo minimum 4 znaki', false); return; }
  try {
    const r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({set_password: np})});
    const d = await r.json();
    if (d.status === 'ok') {
      showAlert('pwd-alert', 'Haslo zapisane! Sesja aktywna przez 7 dni.', true);
      document.getElementById('s-new-password').value = '';
      document.getElementById('s-confirm-password').value = '';
      loadSettings();
    } else showAlert('pwd-alert', 'Blad zapisu', false);
  } catch(e) { showAlert('pwd-alert', 'Blad polaczenia', false); }
}

async function disablePassword() {
  if (!confirm('Na pewno wylaczyc ochrone haslam? Kazdyz bedzie mial dostep!')) return;
  try {
    const r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({set_password: ''})});
    const d = await r.json();
    if (d.status === 'ok') {
      showAlert('pwd-alert', 'Ochrona wylaczona.', true);
      loadSettings();
    }
  } catch(e) { showAlert('pwd-alert', 'Blad polaczenia', false); }
}

async function saveTelegram() {
  const token = document.getElementById('s-tg-token').value.trim();
  const chat  = document.getElementById('s-tg-chat').value.trim();
  const body  = { telegram_chat_id: chat };
  if (token) body.telegram_token = token;
  const r = await fetch('/api/settings', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d = await r.json();
  showAlert('tg-alert', d.status==='ok'?'Zapisano!':'Blad: '+d.message, d.status==='ok'?'ok':'err');
  if (d.status==='ok') document.getElementById('s-tg-token').value='';
}

async function testTelegram() {
  const token = document.getElementById('s-tg-token').value.trim();
  const chat  = document.getElementById('s-tg-chat').value.trim();
  const body  = {};
  if (token) body.token = token;
  if (chat)  body.chat_id = chat;
  showAlert('tg-alert', '<span class="inline-spinner"></span>Wysylam...', 'ok');
  const r = await fetch('/api/settings/test_telegram',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d = await r.json();
  showAlert('tg-alert', d.message, d.status==='ok'?'ok':'err');
}

async function saveServerSettings() {
  const body = {
    comfy_url:       document.getElementById('s-comfy-url').value.trim(),
    image_dir:       document.getElementById('s-image-dir').value.trim(),
    vram_free_wait:  document.getElementById('s-vram-wait').value,
  };
  const r = await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d = await r.json();
  showAlert('srv-alert', d.status==='ok'?'Zapisano!':'Blad: '+d.message, d.status==='ok'?'ok':'err');
}

function showAlert(id, msg, type) {
  const el=document.getElementById(id);
  el.innerHTML=msg; el.className='alert-sm alert-'+type; el.style.display='block';
  if (type==='ok') setTimeout(()=>el.style.display='none', 4000);
}

async function stopGen() {
  try { await fetch('/stop',{method:'POST'}); document.getElementById('live-text').textContent='Przerywanie...'; } catch(e){}
}

// ══════════════════════════════════════════════
// START
// ══════════════════════════════════════════════
document.addEventListener('DOMContentLoaded', async () => {
  connectSSE();
  // Rejestracja Service Worker przy starcie
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(function(e) { console.log('SW error:', e); });
  }
  pollGPU();
  pollQueueBg();

  let attempts=0;
  async function tryLoad() {
    attempts++;
    document.getElementById('loading-status').textContent = 'Laczenie... (' + attempts + ')';
    try {
      // Najpierw sprawdz czy serwer odpowiada
      await fetch('/api/workflows');
      // Sprawdz czy ComfyUI jest gotowy (opcjonalne - nie blokuj jesli nie ma)
      try {
        const q = await fetch('/api/queue', {signal: AbortSignal.timeout(2000)});
        const qd = await q.json();
        if (qd.comfy === 'offline' && attempts < 20) {
          document.getElementById('loading-status').textContent = 'Czekam na ComfyUI... (' + attempts + '/20)';
          setTimeout(tryLoad, 3000);
          return;
        }
      } catch(e) { /* ComfyUI moze jeszcze nie byc gotowy - OK */ }
      await loadWorkflows();
      loadPresets();
      // Niezaleznie czy sa workflow czy nie – pokazujemy interfejs
      document.getElementById('loading-screen').style.display = 'none';
      document.getElementById('app-header').style.display     = 'flex';
      document.getElementById('app-nav').style.display        = 'flex';
    } catch(e) {
      if (attempts < 20) setTimeout(tryLoad, 3000);
      else {
        document.getElementById('loading-status').textContent = 'Blad polaczenia z serwerem.';
        document.getElementById('loading-status').style.color = '#f44336';
        document.getElementById('loading-retry').style.display = 'block';
      }
    }
  }
  tryLoad();
});
</script>

<!-- ══ EKRAN: STATYSTYKI ══ -->
<div class="screen" id="screen-stats">
  <div class="card">
    <div class="card-title">Podsumowanie</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;text-align:center">
      <div style="background:#252528;border-radius:10px;padding:12px 6px">
        <div style="font-size:22px;font-weight:700;color:#4CAF50" id="stat-total-count">—</div>
        <div style="font-size:11px;color:#666;margin-top:2px">Generacji</div>
      </div>
      <div style="background:#252528;border-radius:10px;padding:12px 6px">
        <div style="font-size:22px;font-weight:700;color:#2196F3" id="stat-total-images">—</div>
        <div style="font-size:11px;color:#666;margin-top:2px">Obrazów</div>
      </div>
      <div style="background:#252528;border-radius:10px;padding:12px 6px">
        <div style="font-size:22px;font-weight:700;color:#ff9800" id="stat-avg-time">—</div>
        <div style="font-size:11px;color:#666;margin-top:2px">Śr. czas (s)</div>
      </div>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <div class="card-title" style="margin:0">Czas generowania (ostatnie 20)</div>
      <button onclick="loadStats()" style="background:#252528;border:1px solid #333;color:#888;border-radius:8px;padding:4px 12px;font-size:12px">↻</button>
    </div>
    <div id="stats-chart-wrap" style="width:100%;overflow-x:auto">
      <svg id="stats-chart" width="100%" height="200" style="display:block"></svg>
    </div>
    <div style="display:flex;align-items:center;gap:14px;margin-top:8px;font-size:11px;color:#666">
      <span><span style="display:inline-block;width:10px;height:10px;background:#4CAF50;border-radius:2px;margin-right:4px"></span>Czas (s)</span>
      <span><span style="display:inline-block;width:18px;height:2px;background:#ff9800;vertical-align:middle;margin-right:4px"></span>Średnia</span>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Ostatnie generacje</div>
    <div id="stats-list" style="font-size:13px"></div>
  </div>
</div>

<!-- ══ EKRAN: GALERIA ══ -->
<div class="screen" id="screen-gallery">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
    <h2 style="margin:0;font-size:17px;font-weight:700">Galeria</h2>
    <button onclick="loadGallery()" style="background:#222;border:1px solid #333;color:#aaa;border-radius:8px;padding:6px 14px;font-size:13px">↻ Odśwież</button>
  </div>
  <div id="gallery-grid" style="display:grid;grid-template-columns:1fr 1fr;gap:8px"></div>
  <div id="gallery-empty" style="display:none;text-align:center;color:#444;padding:40px 0;font-size:14px">Brak zdjęć w galerii</div>
</div>

<!-- Modal szczegółów zdjęcia -->
<div id="gallery-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:1000;overflow-y:auto;padding:16px">
  <div style="max-width:500px;margin:0 auto">
    <div style="display:flex;gap:8px;margin-bottom:12px">
      <button onclick="closeGalleryModal()" style="background:#222;border:1px solid #333;color:#aaa;border-radius:8px;padding:8px 18px;font-size:14px;flex:1">✕ Zamknij</button>
      <button id="modal-download-btn" onclick="" style="background:#1a3a1a;border:1px solid #2a5a2a;color:#4CAF50;border-radius:8px;padding:8px 14px;font-size:14px">⬇ Pobierz</button>
      <button id="modal-delete-btn" onclick="" style="background:#3a1a1a;border:1px solid #5a2a2a;color:#f55;border-radius:8px;padding:8px 14px;font-size:14px">🗑</button>
    </div>
    <img id="modal-img" src="" style="width:100%;border-radius:10px;display:block;margin-bottom:14px" />
    <div class="card">
      <div class="detail-row"><span class="detail-key">Workflow</span><span class="detail-val" id="modal-workflow">—</span></div>
      <div class="detail-row"><span class="detail-key">Styl</span><span class="detail-val" id="modal-style">—</span></div>
      <div id="modal-prefix-row" class="detail-row"><span class="detail-key">Prefix</span><span class="detail-val" id="modal-prefix">—</span></div>
      <div id="modal-suffix-row" class="detail-row"><span class="detail-key">Prompt</span><span class="detail-val" id="modal-suffix" style="word-break:break-word;white-space:pre-wrap">—</span></div>
      <div class="detail-row"><span class="detail-key">Data</span><span class="detail-val" id="modal-date">—</span></div>
    </div>
  </div>
</div>

</body>
</html>"""


# =========================================================
# HTML – KREATOR WORKFLOW (panel laptopa /kreator)
# =========================================================
KREATOR_HTML = """<!DOCTYPE html>
<html>
<head>
<title>Kreator Workflow</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', sans-serif; background: #0d0d0f; color: #e0e0e0; }
.layout { display: grid; grid-template-columns: 280px 1fr; min-height: 100vh; }
.sidebar { background: #111113; border-right: 1px solid #222; padding: 20px; }
.sidebar h2 { font-size: 13px; color: #555; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 16px; }
.wf-item { background: #1a1a1d; border: 1px solid #2a2a2e; border-radius: 10px; padding: 12px 14px; margin-bottom: 8px; cursor: pointer; transition: border-color 0.2s; }
.wf-item:hover { border-color: #4CAF50; }
.wf-item.active { border-color: #4CAF50; background: #1a2a1a; }
.wf-item .wf-name { font-weight: 600; font-size: 14px; }
.wf-item .wf-file { font-size: 11px; color: #555; margin-top: 3px; }
.wf-item .wf-tags { display: flex; gap: 4px; margin-top: 6px; flex-wrap: wrap; }
.tag { font-size: 10px; padding: 2px 6px; border-radius: 10px; background: #252528; color: #888; }
.tag.style { background: #1a2a1a; color: #4CAF50; }
.tag.img2 { background: #1a1a2a; color: #2196F3; }
.tag.custom { background: #2a1a1a; color: #ff9800; }
.btn-add { width: 100%; padding: 11px; background: #4CAF50; color: #000; border: none; border-radius: 10px; font-size: 14px; font-weight: 700; cursor: pointer; margin-bottom: 12px; }
.btn-add:hover { background: #66BB6A; }

.main { padding: 30px; overflow-y: auto; }
.main h1 { font-size: 22px; font-weight: 700; margin-bottom: 6px; }
.main .subtitle { color: #666; font-size: 14px; margin-bottom: 28px; }

/* Kroki kreatora */
.steps { display: flex; gap: 8px; margin-bottom: 28px; }
.step { flex: 1; padding: 10px 14px; background: #1a1a1d; border: 1px solid #2a2a2e; border-radius: 10px; font-size: 13px; color: #555; text-align: center; }
.step.active { border-color: #4CAF50; color: #4CAF50; font-weight: 600; background: #1a2a1a; }
.step.done { border-color: #333; color: #4CAF50; }

.panel { background: #161618; border: 1px solid #2a2a2e; border-radius: 14px; padding: 24px; margin-bottom: 16px; }
.panel h3 { font-size: 15px; font-weight: 600; margin-bottom: 16px; color: #fff; }

label { display: block; font-size: 12px; color: #888; margin-bottom: 5px; margin-top: 14px; }
label:first-child { margin-top: 0; }
input[type=text], select { width: 100%; padding: 10px 12px; background: #252528; color: #e0e0e0; border: 1px solid #333; border-radius: 8px; font-size: 14px; }
input[type=file] { width: 100%; padding: 10px; background: #252528; color: #e0e0e0; border: 1px solid #333; border-radius: 8px; font-size: 13px; cursor: pointer; }

.btn { padding: 11px 20px; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; transition: opacity 0.15s; }
.btn:hover { opacity: 0.85; }
.btn-primary { background: #4CAF50; color: #000; }
.btn-blue { background: #2196F3; color: #fff; }
.btn-dark { background: #252528; color: #e0e0e0; }
.btn-red { background: #f44336; color: #fff; }
.btn-row { display: flex; gap: 10px; margin-top: 20px; }

/* Lista nodow */
.node-list { display: grid; gap: 8px; max-height: 400px; overflow-y: auto; padding-right: 4px; }
.node-card { background: #1c1c1f; border: 1px solid #2a2a2e; border-radius: 10px; padding: 12px 14px; }
.node-card .nc-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.node-card .nc-title { font-weight: 600; font-size: 13px; }
.node-card .nc-type { font-size: 11px; color: #555; background: #252528; padding: 2px 7px; border-radius: 10px; }
.node-card .nc-id { font-size: 11px; color: #444; margin-left: auto; }
.nc-inputs { display: flex; gap: 6px; flex-wrap: wrap; }
.nc-input-tag { font-size: 11px; color: #888; background: #222; padding: 2px 7px; border-radius: 6px; }

/* Mapowania */
.mapping-row { background: #1c1c1f; border: 1px solid #2a2a2e; border-radius: 10px; padding: 14px; margin-bottom: 8px; }
.mapping-row .mr-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.mapping-row .mr-role { font-weight: 700; font-size: 13px; }
.role-badge { padding: 3px 10px; border-radius: 20px; font-size: 11px; font-weight: 700; }
.rb-image  { background: #1a1a2e; color: #2196F3; }
.rb-prompt { background: #1a2a2a; color: #00BCD4; }
.rb-style  { background: #1a2a1a; color: #4CAF50; }
.rb-seed   { background: #2a2a1a; color: #ff9800; }
.rb-custom { background: #2a1a1a; color: #f44336; }
.rb-output { background: #2a1a2a; color: #9C27B0; }

.mr-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.mr-grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }

/* Przyciski rol */
.role-picker { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 16px; }
.role-btn { padding: 12px 8px; background: #1c1c1f; border: 2px solid #2a2a2e; border-radius: 10px; cursor: pointer; text-align: center; transition: all 0.15s; }
.role-btn:hover { border-color: #4CAF50; }
.role-btn.selected { border-color: #4CAF50; background: #1a2a1a; }
.role-btn .rb-icon { font-size: 22px; margin-bottom: 4px; }
.role-btn .rb-name { font-size: 12px; font-weight: 600; }
.role-btn .rb-desc { font-size: 11px; color: #666; margin-top: 2px; }

.empty-state { text-align: center; padding: 40px; color: #555; }
.empty-state .es-icon { font-size: 48px; margin-bottom: 12px; }

.alert { padding: 12px 16px; border-radius: 10px; margin-bottom: 16px; font-size: 14px; }
.alert-ok  { background: #1a2a1a; border: 1px solid #4CAF50; color: #4CAF50; }
.alert-err { background: #2a1a1a; border: 1px solid #f44336; color: #f44336; }

.hidden { display: none !important; }
</style>
</head>
<body>
<div class="layout">

<!-- SIDEBAR: lista workflow -->
<div class="sidebar">
  <button class="btn-add" onclick="startNew()">+ Nowy Workflow</button>
  <h2>Zapisane</h2>
  <div id="wf-list">
    <div style="color:#555;font-size:13px">Ladowanie...</div>
  </div>
</div>

<!-- MAIN: kreator -->
<div class="main">
  <div id="welcome-state">
    <div class="empty-state">
      <div class="es-icon">⚙️</div>
      <div style="font-size:18px;font-weight:600;margin-bottom:8px">Kreator Workflow</div>
      <div style="color:#666;font-size:14px">Wybierz workflow z listy lub dodaj nowy</div>
    </div>
  </div>

  <div id="editor-state" class="hidden">
    <h1 id="editor-title">Nowy Workflow</h1>
    <div class="subtitle" id="editor-subtitle">Krok 1 z 3: Podstawowe informacje i plik JSON</div>

    <div class="steps">
      <div class="step active" id="step-1">1. Plik i nazwa</div>
      <div class="step" id="step-2">2. Mapowanie rol</div>
      <div class="step" id="step-3">3. Zapisz</div>
    </div>

    <div id="alert-box" class="hidden"></div>

    <!-- KROK 1 -->
    <div id="panel-step1" class="panel">
      <h3>Podstawowe informacje</h3>
      <label>Nazwa workflow (widoczna na telefonie)</label>
      <input type="text" id="wf-name" placeholder="np. Ricky v4 – Styl artystyczny">
      <label>ID (krotka, bez spacji)</label>
      <input type="text" id="wf-id" placeholder="np. ricky_v4_art">
      <label>Plik JSON workflow (wgraj z ComfyUI Export)</label>
      <input type="file" id="wf-file" accept=".json">
      <div class="btn-row">
        <button class="btn btn-primary" onclick="step1Next()">Dalej: Skanuj nody →</button>
      </div>
    </div>

    <!-- KROK 2 -->
    <div id="panel-step2" class="hidden">
      <div class="panel">
        <h3>Wykryte nody w workflow</h3>
        <div style="font-size:13px;color:#666;margin-bottom:14px">
          Kliknij "Dodaj mapowanie" przy nodzie, ktory chcesz kontrolowac z telefonu. Mozesz tez kliknac nazwy nodow zeby rozwinac ich pola.
        </div>
        <div id="node-list" class="node-list"></div>
      </div>

      <div class="panel">
        <h3>Skonfigurowane mapowania</h3>
        <div id="mappings-list">
          <div style="color:#555;font-size:13px">Brak mapowań – dodaj je z listy nodow powyzej.</div>
        </div>
        <div class="btn-row">
          <button class="btn btn-dark" onclick="showStep(1)">← Wstecz</button>
          <button class="btn btn-primary" onclick="showStep(3)">Dalej: Podgląd →</button>
        </div>
      </div>
    </div>

    <!-- KROK 3 -->
    <div id="panel-step3" class="hidden">
      <div class="panel">
        <h3>Podsumowanie</h3>
        <div id="summary"></div>
        <div class="btn-row">
          <button class="btn btn-dark" onclick="showStep(2)">← Wstecz</button>
          <button class="btn btn-primary" onclick="saveWorkflow()">💾 Zapisz Workflow</button>
          <button class="btn btn-red" id="btn-delete" onclick="deleteWorkflow()" style="margin-left:auto;display:none">🗑 Usuń</button>
        </div>
      </div>
    </div>
  </div>
</div>
</div>

<!-- MODAL: wybor roli dla noda -->
<div id="modal-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:1000;display:none;align-items:center;justify-content:center;">
<div style="background:#1c1c1f;border:1px solid #333;border-radius:16px;padding:28px;width:90%;max-width:540px;max-height:90vh;overflow-y:auto;">
  <h3 style="margin-bottom:6px" id="modal-node-name">Wybierz role dla noda</h3>
  <div style="font-size:12px;color:#555;margin-bottom:20px" id="modal-node-type"></div>

  <div class="role-picker">
    <div class="role-btn" onclick="selectRole('image_1')">
      <div class="rb-icon">🖼️</div>
      <div class="rb-name">Zdjecie 1</div>
      <div class="rb-desc">Glowne wejscie</div>
    </div>
    <div class="role-btn" onclick="selectRole('image_2')">
      <div class="rb-icon">🖼️</div>
      <div class="rb-name">Zdjecie 2</div>
      <div class="rb-desc">Dodatkowe wejscie</div>
    </div>
    <div class="role-btn" onclick="selectRole('prompt')">
      <div class="rb-icon">✏️</div>
      <div class="rb-name">Prompt</div>
      <div class="rb-desc">Tekst generacji</div>
    </div>
    <div class="role-btn" onclick="selectRole('style')">
      <div class="rb-icon">🎨</div>
      <div class="rb-name">Styl</div>
      <div class="rb-desc">3-poziomowy wybor</div>
    </div>
    <div class="role-btn" onclick="selectRole('seed')">
      <div class="rb-icon">🎲</div>
      <div class="rb-name">Seed</div>
      <div class="rb-desc">Losowe/stale</div>
    </div>
    <div class="role-btn" onclick="selectRole('output')">
      <div class="rb-icon">💾</div>
      <div class="rb-name">Output</div>
      <div class="rb-desc">Node zapisu obrazu</div>
    </div>
    <div class="role-btn" onclick="selectRole('custom')">
      <div class="rb-icon">🔧</div>
      <div class="rb-name">Custom</div>
      <div class="rb-desc">Dowolne pole</div>
    </div>
  </div>

  <!-- Konfiguracja po wyborze roli -->
  <div id="role-config" class="hidden">
    <div id="config-image">
      <label>Pole w nodzie (nazwa pola selected_image / image itd.)</label>
      <select id="cfg-image-field"></select>
      <label>Etykieta na telefonie</label>
      <input type="text" id="cfg-image-label" placeholder="np. Zdjecie twarzy">
    </div>
    <div id="config-prompt" class="hidden">
      <label>Pole SUFFIX (glowny prompt)</label>
      <select id="cfg-prompt-field"></select>
      <label>Pole PREFIX (opcjonalne)</label>
      <select id="cfg-prefix-field"><option value="">-- brak --</option></select>
    </div>
    <div id="config-style" class="hidden">
      <div style="font-size:13px;color:#aaa;margin-bottom:10px">Pola stylu 3-poziomowego:</div>
      <div class="mr-grid">
        <div><label>Pole MODE</label><select id="cfg-style-mode"></select></div>
        <div><label>Pole MAIN</label><select id="cfg-style-main"></select></div>
        <div><label>Pole SUB</label><select id="cfg-style-sub"></select></div>
        <div><label>Pole SUBSUB</label><select id="cfg-style-subsub"></select></div>
      </div>
    </div>
    <div id="config-seed" class="hidden">
      <label>Pole seed w nodzie</label>
      <select id="cfg-seed-field"></select>
    </div>
    <div id="config-output" class="hidden">
      <div style="font-size:13px;color:#aaa">Ten node bedzie traktowany jako wyjscie – jego obrazy beda wysylane na Telegram.</div>
    </div>
    <div id="config-custom" class="hidden">
      <label>Pole do sterowania</label>
      <select id="cfg-custom-field"></select>
      <label>Etykieta na telefonie</label>
      <input type="text" id="cfg-custom-label" placeholder="np. Sila efektu">
      <label>Typ kontrolki</label>
      <select id="cfg-custom-type">
        <option value="text">Pole tekstowe</option>
        <option value="number">Liczba</option>
        <option value="slider">Suwak (0.0 – 1.0)</option>
      </select>
    </div>
  </div>

  <div class="btn-row" id="modal-btns">
    <button class="btn btn-dark" onclick="closeModal()">Anuluj</button>
    <button class="btn btn-primary hidden" id="btn-confirm-role" onclick="confirmMapping()">Dodaj mapowanie</button>
  </div>
</div>
</div>

<script>
let allNodes     = [];
let mappings     = [];
let currentNodeForModal = null;
let selectedRole = null;
let currentWfId  = null;
let editingIndex = -1;

// ── Ladowanie listy workflow ──
async function loadWorkflows() {
  const res  = await fetch('/api/workflows');
  const list = await res.json();
  const el   = document.getElementById('wf-list');
  if (!list.length) { el.innerHTML = '<div style="color:#555;font-size:13px">Brak zapisanych workflow</div>'; return; }
  el.innerHTML = list.map(w => {
    const tags = [];
    if (w.has_style)  tags.push('<span class="tag style">styl</span>');
    if (w.has_image2) tags.push('<span class="tag img2">2 zdjecia</span>');
    if (w.custom_inputs?.length) tags.push('<span class="tag custom">custom×' + w.custom_inputs.length + '</span>');
    return '<div class="wf-item" onclick="loadWorkflow(\'\' + (w.id) + '\')" id="wf-sidebar-\' + (w.id) + '">
      <div class="wf-name">\' + (w.name) + '</div>
      <div class="wf-file">\' + (w.file) + '</div>
      <div class="wf-tags">\' + (tags.join('')) + '</div>
    </div>';
  }).join('');
}

function startNew() {
  currentWfId  = null;
  mappings     = [];
  allNodes     = [];
  editingIndex = -1;
  document.getElementById('wf-name').value = '';
  document.getElementById('wf-id').value   = '';
  document.getElementById('wf-file').value  = '';
  document.querySelectorAll('.wf-item').forEach(e => e.classList.remove('active'));
  document.getElementById('welcome-state').classList.add('hidden');
  document.getElementById('editor-state').classList.remove('hidden');
  document.getElementById('editor-title').textContent = 'Nowy Workflow';
  document.getElementById('btn-delete').style.display = 'none';
  showStep(1);
}

async function loadWorkflow(wid) {
  document.querySelectorAll('.wf-item').forEach(e => e.classList.remove('active'));
  document.getElementById('wf-sidebar-' + wid)?.classList.add('active');
  const res  = await fetch('/api/workflows');
  const list = await res.json();
  const wf   = list.find(w => w.id === wid);
  if (!wf) return;
  currentWfId = wid;
  document.getElementById('wf-name').value = wf.name;
  document.getElementById('wf-id').value   = wid;
  document.getElementById('welcome-state').classList.add('hidden');
  document.getElementById('editor-state').classList.remove('hidden');
  document.getElementById('editor-title').textContent = wf.name;
  document.getElementById('btn-delete').style.display = 'inline-block';
  // Zaladuj pelna konfiguracje
  const cfg_res = await fetch('/api/workflows');
  const cfg_list = await cfg_res.json();
  const full = cfg_list.find(w => w.id === wid);
  mappings = full?.custom_inputs ? [] : [];
  // Zeskanuj plik (jesli istnieje)
  showStep(3);
  renderSummary();
}

// ── Kroki kreatora ──
function showStep(n) {
  [1,2,3].forEach(i => {
    document.getElementById('panel-step' + i).classList.toggle('hidden', i !== n);
    const s = document.getElementById('step-' + i);
    s.classList.toggle('active', i === n);
    s.classList.toggle('done',   i <  n);
  });
  document.getElementById('editor-subtitle').textContent = 'Krok ' + n + ' z 3: ' +
    ['Podstawowe informacje i plik JSON', 'Mapowanie rol', 'Podglad i zapis'][n-1];
}

async function step1Next() {
  const name = document.getElementById('wf-name').value.trim();
  const wid  = document.getElementById('wf-id').value.trim().replace(/\s+/g, '_');
  const file = document.getElementById('wf-file').files[0];
  if (!name) { showAlert('Podaj nazwe workflow', 'err'); return; }
  if (!wid)  { showAlert('Podaj ID workflow',    'err'); return; }
  if (!file) { showAlert('Wybierz plik JSON',    'err'); return; }

  const fd = new FormData();
  fd.append('file', file);
  showAlert('Skanowanie nodow...', 'ok');
  const res  = await fetch('/api/workflows/upload_json', { method: 'POST', body: fd });
  const data = await res.json();
  if (data.status !== 'ok') { showAlert('Blad: ' + data.message, 'err'); return; }

  allNodes = data.nodes;
  document.getElementById('wf-id').value = wid;
  renderNodeList();
  renderMappingsList();
  hideAlert();
  showStep(2);
}

// ── Lista nodow ──
function renderNodeList() {
  const el = document.getElementById('node-list');
  el.innerHTML = allNodes.map((n, idx) => {
    const inputTags = n.inputs.slice(0,5).map(i =>
      '<span class="nc-input-tag">' + i.name + '</span>'
    ).join('') + (n.inputs.length > 5 ? '<span class="nc-input-tag">+' + (n.inputs.length-5) + '</span>' : '');
    return '<div class="node-card">
      <div class="nc-header">
        <span class="nc-title">\' + (n.title || '—') + '</span>
        <span class="nc-type">\' + (n.type) + '</span>
        <span class="nc-id">#\' + (n.id) + '</span>
        <button style="margin-left:8px;padding:4px 10px;background:#4CAF50;color:#000;border:none;border-radius:6px;cursor:pointer;font-size:12px;font-weight:700" onclick="openModal(\' + (idx) + ')">+ Mapuj</button>
      </div>
      <div class="nc-inputs">\' + (inputTags) + '</div>
    </div>';
  }).join('');
}

function renderMappingsList() {
  const el = document.getElementById('mappings-list');
  if (!mappings.length) {
    el.innerHTML = '<div style="color:#555;font-size:13px">Brak mapowań</div>';
    return;
  }
  el.innerHTML = mappings.map((m, i) => {
    const roleColors = { image_1: 'rb-image', image_2: 'rb-image', prompt: 'rb-prompt', style: 'rb-style', seed: 'rb-seed', output: 'rb-output', custom: 'rb-custom' };
    const roleLabels = { image_1: 'Zdjecie 1', image_2: 'Zdjecie 2', prompt: 'Prompt', style: 'Styl', seed: 'Seed', output: 'Output', custom: 'Custom' };
    const details = m.label ? ' – \' + (m.label) + '' : (m.field ? ' (\' + (m.field) + ')' : '');
    return '<div class="mapping-row">
      <div class="mr-header">
        <span class="mr-role"><span class="role-badge \' + (roleColors[m.role]) + '">\' + (roleLabels[m.role]) + '</span> Node: \' + (m.node_title || m.node_id) + '\' + (details) + '</span>
        <button style="padding:4px 10px;background:#f44336;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:12px" onclick="removeMapping(\' + (i) + ')">✕ Usuń</button>
      </div>
    </div>';
  }).join('');
}

function removeMapping(i) { mappings.splice(i, 1); renderMappingsList(); }

// ── Modal ──
function openModal(nodeIdx) {
  currentNodeForModal = allNodes[nodeIdx];
  selectedRole        = null;
  document.getElementById('modal-node-name').textContent = currentNodeForModal.title || currentNodeForModal.type;
  document.getElementById('modal-node-type').textContent = '#' + currentNodeForModal.id + ' | ' + currentNodeForModal.type;
  document.getElementById('role-config').classList.add('hidden');
  document.getElementById('btn-confirm-role').classList.add('hidden');
  document.querySelectorAll('.role-btn').forEach(b => b.classList.remove('selected'));
  document.getElementById('modal-overlay').style.display = 'flex';
}

function closeModal() { document.getElementById('modal-overlay').style.display = 'none'; }

function selectRole(role) {
  selectedRole = role;
  document.querySelectorAll('.role-btn').forEach(b => b.classList.remove('selected'));
  event.currentTarget.classList.add('selected');

  // Wypelnij selecty polami noda
  const fields = currentNodeForModal.inputs;
  const opts = '<option value="">-- brak --</option>' + fields.map(f =>
    '<option value="\' + (f.name) + '">\' + (f.name) + ' (domyslnie: \' + (f.default) + ')</option>'
  ).join('');
  const opts_req = fields.map(f =>
    '<option value="\' + (f.name) + '">\' + (f.name) + '</option>'
  ).join('');

  ['config-image','config-prompt','config-style','config-seed','config-output','config-custom'].forEach(id =>
    document.getElementById(id).classList.add('hidden')
  );

  if (role === 'image_1' || role === 'image_2') {
    document.getElementById('cfg-image-field').innerHTML = opts_req;
    document.getElementById('cfg-image-label').value = role === 'image_1' ? 'Zdjecie (glowne)' : 'Zdjecie (2)';
    document.getElementById('config-image').classList.remove('hidden');
  } else if (role === 'prompt') {
    document.getElementById('cfg-prompt-field').innerHTML = opts_req;
    document.getElementById('cfg-prefix-field').innerHTML = opts;
    document.getElementById('config-prompt').classList.remove('hidden');
  } else if (role === 'style') {
    ['cfg-style-mode','cfg-style-main','cfg-style-sub','cfg-style-subsub'].forEach(id => {
      document.getElementById(id).innerHTML = opts;
    });
    document.getElementById('config-style').classList.remove('hidden');
  } else if (role === 'seed') {
    document.getElementById('cfg-seed-field').innerHTML = opts_req;
    document.getElementById('config-seed').classList.remove('hidden');
  } else if (role === 'output') {
    document.getElementById('config-output').classList.remove('hidden');
  } else if (role === 'custom') {
    document.getElementById('cfg-custom-field').innerHTML = opts_req;
    document.getElementById('config-custom').classList.remove('hidden');
  }

  document.getElementById('role-config').classList.remove('hidden');
  document.getElementById('btn-confirm-role').classList.remove('hidden');
}

function confirmMapping() {
  if (!selectedRole || !currentNodeForModal) return;
  const n = currentNodeForModal;
  let mapping = { role: selectedRole, node_id: n.id, node_title: n.title || n.type, node_type: n.type };

  if (selectedRole === 'image_1' || selectedRole === 'image_2') {
    mapping.field = document.getElementById('cfg-image-field').value;
    mapping.label = document.getElementById('cfg-image-label').value;
  } else if (selectedRole === 'prompt') {
    mapping.field        = document.getElementById('cfg-prompt-field').value;
    mapping.prefix_field = document.getElementById('cfg-prefix-field').value || null;
  } else if (selectedRole === 'style') {
    mapping.mode_field   = document.getElementById('cfg-style-mode').value;
    mapping.main_field   = document.getElementById('cfg-style-main').value;
    mapping.sub_field    = document.getElementById('cfg-style-sub').value;
    mapping.subsub_field = document.getElementById('cfg-style-subsub').value;
  } else if (selectedRole === 'seed') {
    mapping.field = document.getElementById('cfg-seed-field').value;
  } else if (selectedRole === 'custom') {
    mapping.field    = document.getElementById('cfg-custom-field').value;
    mapping.label    = document.getElementById('cfg-custom-label').value;
    mapping.form_key = 'custom_' + n.id + '_' + mapping.field;
    mapping.ctrl_type = document.getElementById('cfg-custom-type').value;
    mapping.default  = n.inputs.find(i => i.name === mapping.field)?.default ?? '';
  }

  mappings.push(mapping);
  renderMappingsList();
  closeModal();
}

// ── Podsumowanie i zapis ──
function renderSummary() {
  const name = document.getElementById('wf-name').value;
  const wid  = document.getElementById('wf-id').value;
  const roles = { image_1: '🖼 Zdjecie 1', image_2: '🖼 Zdjecie 2', prompt: '✏️ Prompt', style: '🎨 Styl', seed: '🎲 Seed', output: '💾 Output', custom: '🔧 Custom' };
  const mapHtml = mappings.length ? mappings.map(m =>
    '<div style="padding:8px 0;border-bottom:1px solid #222;font-size:13px"><b>' + (roles[m.role] || m.role) + '</b> → Node: ' + (m.node_title || m.node_id) + (m.field ? ' (' + m.field + ')' : '') + '</div>'
  ).join('') : '<div style="color:#555">Brak mapowań</div>';

  document.getElementById('summary').innerHTML = '
    <div style="margin-bottom:14px">
      <div style="font-size:20px;font-weight:700">\' + (name || '—') + '</div>
      <div style="color:#555;font-size:13px">ID: \' + (wid) + '</div>
    </div>
    <div>\' + (mapHtml) + '</div>
  ';
}

async function saveWorkflow() {
  const name = document.getElementById('wf-name').value.trim();
  const wid  = document.getElementById('wf-id').value.trim();
  const file = document.getElementById('wf-file').files[0];

  const output_node_ids = mappings.filter(m => m.role === 'output').map(m => m.node_id);
  const payload = { id: wid, name, file: file?.name || '', mappings, output_node_ids };

  const res  = await fetch('/api/workflows/save', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const data = await res.json();
  if (data.status === 'ok') {
    showAlert('Workflow zapisany pomyslnie!', 'ok');
    loadWorkflows();
  } else {
    showAlert('Blad: ' + data.message, 'err');
  }
}

async function deleteWorkflow() {
  if (!currentWfId || !confirm('Na pewno usunac ten workflow?')) return;
  await fetch('/api/workflows/' + currentWfId, { method: 'DELETE' });
  document.getElementById('editor-state').classList.add('hidden');
  document.getElementById('welcome-state').classList.remove('hidden');
  loadWorkflows();
}

function showAlert(msg, type) {
  const el = document.getElementById('alert-box');
  el.className = 'alert alert-' + type;
  el.textContent = msg;
  el.classList.remove('hidden');
}
function hideAlert() { document.getElementById('alert-box').classList.add('hidden'); }

document.getElementById('wf-name').addEventListener('input', function() {
  if (!currentWfId) {
    document.getElementById('wf-id').value = this.value.toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '');
  }
});
document.getElementById('modal-overlay').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});

loadWorkflows();
</script>
</body>
</html>"""


# =========================================================
# HTML – PANEL LAPTOPA
# =========================================================
DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>ComfyUI Control Panel</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family:'Segoe UI',sans-serif; background:#0f0f11; color:#e0e0e0; padding:30px; margin:0; }
        .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:20px; max-width:1000px; margin:auto; }
        .card { background:#1e1e24; border:1px solid #333; border-radius:12px; padding:20px; }
        h2 { color:#fff; margin-top:0; font-size:1.2rem; border-bottom:1px solid #333; padding-bottom:10px; }
        .metric { display:flex; justify-content:space-between; margin:15px 0; font-size:1.1rem; }
        .val { font-weight:bold; color:#4CAF50; }
        .val.warn { color:#ff9800; } .val.danger { color:#f44336; }
        button { width:100%; padding:15px; background:#f44336; color:white; border:none; border-radius:8px; font-size:16px; font-weight:bold; cursor:pointer; margin-top:10px; }
        .header-title { text-align:center; margin-bottom:30px; color:#aaa; }
    </style>
</head>
<body>
    <div class="header-title">
        <h1>Silnik ComfyUI - Panel Zarzadzania</h1>
        <div style="display:flex;gap:10px;margin-top:10px">
            <a href="/kreator" target="_blank" style="padding:8px 16px;background:#4CAF50;color:#000;border-radius:8px;font-size:13px;font-weight:700;text-decoration:none">⚙️ Kreator workflow</a>
            <a href="/" target="_blank" style="padding:8px 16px;background:#2196F3;color:#fff;border-radius:8px;font-size:13px;font-weight:700;text-decoration:none">📱 Interfejs mobilny</a>
        </div>
    </div>
    <div class="grid">
        <div class="card">
            <h2>Monitor GPU</h2>
            <div class="metric"><span>Temperatura:</span><span id="gpu-temp" class="val">...</span></div>
            <div class="metric"><span>Uzycie Rdzenia:</span><span id="gpu-util" class="val">...</span></div>
            <div class="metric"><span>Pamiec VRAM:</span><span id="gpu-vram" class="val">...</span></div>
        </div>
        <div class="card">
            <h2>Status Silnika</h2>
            <div class="metric"><span>Stan ComfyUI:</span><span id="comfy-realtime" class="val" style="color:#2196F3">...</span></div>
            <div class="metric"><span>Ostatni Workflow:</span><span id="last-wf" class="val" style="color:#aaa">Brak</span></div>
            <div style="margin-top:20px;color:#888;font-size:0.9rem;">Serwer mobilny:</div>
            <div id="current-action" style="color:#4CAF50;font-weight:bold;font-size:1.1rem;margin-top:5px;">Czekam...</div>
        </div>
        <div class="card">
            <h2>Dostep Zdalny</h2>
            <div id="qr-status" style="color:#888;font-size:0.9rem;margin-bottom:10px;">Oczekuje na adres zdalny...</div>
            <div id="qr-url" style="color:#4CAF50;font-size:0.8rem;word-break:break-all;margin-bottom:12px;"></div>
            <div style="text-align:center;"><img id="qr-img" src="" style="display:none;width:180px;height:180px;border-radius:8px;background:#fff;padding:6px;" /></div>
            <button id="copy-url-btn" onclick="copyUrl()" style="background:#2196F3;display:none;">Kopiuj URL</button>
        </div>
        <div class="card" style="border-color:#f44336;">
            <h2>Kontrola Awaryjna</h2>
            <p style="font-size:0.9rem;color:#888;">Calkowite wylaczenie silnika ComfyUI i serwera.</p>
            <button onclick="shutdownEngine()">WYLACZ SILNIK</button>
        </div>
    </div>
    <script>
        let remoteUrl = '';
        setInterval(async () => {
            try {
                const d = await fetch('/api/laptop_status').then(r => r.json());
                document.getElementById('gpu-temp').innerText    = d.gpu.temp;
                document.getElementById('gpu-util').innerText    = d.gpu.util;
                document.getElementById('gpu-vram').innerText    = d.gpu.vram;
                const t = parseInt(d.gpu.temp), el = document.getElementById('gpu-temp');
                el.className = t > 80 ? 'val danger' : t > 70 ? 'val warn' : 'val';
                document.getElementById('comfy-realtime').innerText = d.realtime_status;
                document.getElementById('last-wf').innerText        = d.last_workflow || 'Brak';
                document.getElementById('current-action').innerText = d.server_status;
            } catch(e) { document.getElementById('current-action').innerText = 'Blad polaczenia!'; }
        }, 2000);
        async function pollQr() {
            try {
                const d = await fetch('/api/remote_url').then(r => r.json());
                if (d.url) {
                    remoteUrl = d.url;
                    document.getElementById('qr-status').textContent = 'Tunel aktywny';
                    document.getElementById('qr-status').style.color = '#4CAF50';
                    document.getElementById('qr-url').textContent    = d.url;
                    document.getElementById('copy-url-btn').style.display = 'block';
                    if (d.qr_base64) {
                        const img = document.getElementById('qr-img');
                        img.src = 'data:image/png;base64,' + d.qr_base64;
                        img.style.display = 'block';
                    }
                } else setTimeout(pollQr, 3000);
            } catch(e) { setTimeout(pollQr, 3000); }
        }
        pollQr();
        function copyUrl() {
            if (remoteUrl) { navigator.clipboard.writeText(remoteUrl); document.getElementById('copy-url-btn').textContent = 'Skopiowano!'; setTimeout(() => document.getElementById('copy-url-btn').textContent = 'Kopiuj URL', 2000); }
        }
        async function shutdownEngine() {
            if (confirm('Czy na pewno chcesz wylczyc silnik?')) {
                try { await fetch('/shutdown_engine',{method:'POST'}); document.body.innerHTML = "<h2 style='text-align:center;color:#f44336;margin-top:50px;'>Zamknieto.</h2>"; } catch(e) {}
            }
        }
    </script>
</body>
</html>"""


# =========================================================
# ENDPOINTY
# =========================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(content=HTML_TEMPLATE)

@app.get("/panel", response_class=HTMLResponse)
async def panel():
    return HTMLResponse(content=DASHBOARD_HTML)

@app.get("/api/laptop_status")
async def get_laptop_status():
    return {
        "gpu":             get_gpu_info(),
        "realtime_status": get_comfy_realtime_status(),
        "server_status":   app.state.status_text,
        "last_workflow":   app.state.last_workflow,
    }

@app.get("/api/status")
async def get_status():
    return {
        "status_text":   app.state.status_text,
        "current_iter":  app.state.current_iter,
        "total_iter":    app.state.total_iter,
        "is_processing": app.state.is_processing,
        "step_value":    app.state.step_value,
        "step_max":      app.state.step_max,
        "current_style": app.state.current_style,
    }

@app.get("/api/queue")
async def get_queue_details():
    result = {
        "queue_running": 0, "queue_pending": 0,
        "queue_running_details": [], "queue_pending_details": [],
        "current_node":  app.state.current_node,
        "step_value":    app.state.step_value,
        "step_max":      app.state.step_max,
        "gpu_util":      None, "gpu_vram": None,
    }
    q = comfy_get("/queue", timeout=2)
    if q:
        running = q.get("queue_running", [])
        pending = q.get("queue_pending", [])
        result["queue_running"] = len(running)
        result["queue_pending"] = len(pending)
        # Szczegoly do ekranu kolejki: kazde zadanie to lista [numer, prompt_id, workflow, ...]
        result["queue_running_details"] = [
            {"prompt_id": item[1] if len(item) > 1 else "?"} for item in running
        ]
        result["queue_pending_details"] = [
            {"prompt_id": item[1] if len(item) > 1 else "?"} for item in pending
        ]
    try:
        gpu = get_gpu_info()
        result["gpu_util"] = gpu["util"]
        result["gpu_vram"] = gpu["vram"]
    except Exception:
        pass
    return result

@app.get("/api/remote_url")
async def get_remote_url():
    return {"url": app.state.remote_url, "qr_base64": app.state.qr_base64_cache}

@app.post("/api/set_remote_url")
async def set_remote_url(data: dict):
    url = data.get("url", "")
    if url.startswith("https://"):
        app.state.remote_url = url
        log.info(f"Ustawiono remote_url: {url}")
        try:
            import qrcode
            qr  = qrcode.make(url)
            buf = io.BytesIO()
            qr.save(buf, format='PNG')
            app.state.qr_base64_cache = base64.b64encode(buf.getvalue()).decode('utf-8')
            log.info("QR code wygenerowany i zcache'owany.")
        except ImportError:
            log.warning("Brak pakietu 'qrcode'")
    return {"status": "ok"}

@app.get("/api/options")
async def get_options():
    log.debug("/api/options: odpytuję ComfyUI...")
    data = comfy_get("/object_info/RandomOrManual3LevelChoicesRelaxed", timeout=5)
    if data:
        try:
            node_info = data.get("RandomOrManual3LevelChoicesRelaxed", {})
            inputs    = node_info.get("input", {}).get("required", {})
            def extract(field):
                if field in inputs and isinstance(inputs[field][0], list):
                    return inputs[field][0]
                return []
            result = {
                "mode": extract("mode"), "main_style": extract("main_style"),
                "sub_style": extract("sub_style"), "subsub_style": extract("subsub_style"),
            }
            log.debug(f"/api/options OK: {len(result['main_style'])} main_style opcji")
            return result
        except Exception as e:
            log.warning(f"/api/options parse blad: {e}")
    return {"mode": [], "main_style": [], "sub_style": [], "subsub_style": []}

@app.get("/api/preview")
async def get_preview(request: Request):
    """Zwraca aktualny live preview z ComfyUI (base64 JPEG) — tylko właścicielowi sesji."""
    uid = get_uid_from_request(request)
    is_owner = (uid and uid == app.state.processing_uid)
    if is_owner and app.state.is_processing:
        # Właściciel zawsze widzi processing=True i obraz jeśli jest
        return {"status": "ok", "image": app.state.preview_b64, "processing": True}
    if app.state.preview_b64 and is_owner:
        return {"status": "ok", "image": app.state.preview_b64, "processing": app.state.is_processing}
    # Inny użytkownik widzi tylko status "zajęty" bez obrazu
    if app.state.is_processing and not is_owner:
        return {"status": "ok", "image": None, "processing": True}
    # Fallback: spróbuj pobrać preview bezpośrednio z ComfyUI
    if app.state.is_processing:
        try:
            import urllib.request as _ur
            resp = _ur.urlopen(f"http://{COMFY_URL}/view?filename=ComfyUI_temp_preview&type=temp", timeout=2)
            import base64 as _b64
            data = resp.read()
            if data:
                return {"status": "ok", "image": _b64.b64encode(data).decode(), "processing": True}
        except Exception:
            pass
    return {"status": "no_preview", "processing": app.state.is_processing}


# Metadane zdjęć - słownik {filename: {prompt, workflow, timestamp}}
_gallery_meta: dict = {}

def save_gallery_meta(filename: str, meta: dict, img_dir: str = ""):
    """Zapisuje metadane do pliku JSON obok zdjęć."""
    _gallery_meta[filename] = meta
    meta_path = os.path.join(img_dir or IMAGE_DIR, "_gallery_meta.json")
    try:
        existing = {}
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        existing[filename] = meta
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"gallery_meta save error: {e}")

def load_gallery_meta(img_dir: str = "") -> dict:
    """Wczytuje metadane z pliku JSON."""
    meta_path = os.path.join(img_dir or IMAGE_DIR, "_gallery_meta.json")
    try:
        if os.path.exists(meta_path):
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

@app.get("/api/gallery")
async def get_gallery(request: Request):
    """Zwraca listę zdjęć z galerii (telegram_result_*) posortowanych od najnowszych."""
    _u = get_user_from_request(request)
    _img_dir = user_image_dir(_u)

    try:
        files = []
        meta = load_gallery_meta()
        for fname in os.listdir(_img_dir):
            if not fname.startswith("telegram_result_"):
                continue
            fpath = os.path.join(_img_dir, fname)
            mtime = os.path.getmtime(fpath)
            fmeta = meta.get(fname, {})
            files.append({
                "filename": fname,
                "url": f"/gallery_image/{fname}",
                "timestamp": mtime,
                "workflow": fmeta.get("workflow", ""),
                "suffix": fmeta.get("suffix", ""),
                "prefix": fmeta.get("prefix", ""),
                "style": fmeta.get("style", ""),
                "prompt_full": fmeta.get("prompt_full", ""),
            })
        files.sort(key=lambda x: x["timestamp"], reverse=True)
        return {"status": "ok", "files": files}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/gallery_image/{filename}")
async def gallery_image(filename: str, request: Request):
    """Serwuje zdjęcie z galerii (iOS-friendly)."""
    _u = get_user_from_request(request)
    _img_dir = user_image_dir(_u)

    from fastapi.responses import Response
    fpath = os.path.join(_img_dir, filename)
    if os.path.exists(fpath) and filename.startswith("telegram_result_"):
        # Jeśli plik jest faktycznie PNG (stare pliki) - konwertuj na bieżąco
        with open(fpath, "rb") as f:
            raw = f.read()
        if raw[:8] == b"\x89PNG\r\n\x1a\n":  # PNG magic bytes
            try:
                buf = io.BytesIO()
                Image.open(io.BytesIO(raw)).convert("RGB").save(buf, format="JPEG", quality=92)
                raw = buf.getvalue()
            except Exception:
                pass
        return Response(raw, media_type="image/jpeg",
                       headers={"Cache-Control": "max-age=3600",
                                "Content-Disposition": "inline"})
    return {"error": "not found"}


@app.get("/gallery_thumb/{filename}")
async def gallery_thumb(filename: str, request: Request, size: int = 300):
    """Serwuje miniaturę zdjęcia z galerii (szybkie ładowanie w siatce)."""
    _u = get_user_from_request(request)
    _img_dir = user_image_dir(_u)

    from fastapi.responses import Response
    fpath = os.path.join(_img_dir, filename)
    if not os.path.exists(fpath) or not filename.startswith("telegram_result_"):
        return {"error": "not found"}

    # Ogranicz rozmiar do rozsądnych wartości
    size = max(100, min(size, 600))

    # Sprawdź cache miniatury
    thumb_dir = os.path.join(_img_dir, "_thumbs")
    os.makedirs(thumb_dir, exist_ok=True)
    thumb_path = os.path.join(thumb_dir, f"{size}_{filename}")

    if os.path.exists(thumb_path):
        # Sprawdź czy oryginał nie jest nowszy
        if os.path.getmtime(thumb_path) >= os.path.getmtime(fpath):
            with open(thumb_path, "rb") as f:
                raw = f.read()
            return Response(raw, media_type="image/jpeg",
                           headers={"Cache-Control": "max-age=86400", "Content-Disposition": "inline"})

    try:
        with open(fpath, "rb") as f:
            raw = f.read()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # Crop do kwadratu ze środka
        w, h = img.size
        min_dim = min(w, h)
        left   = (w - min_dim) // 2
        top    = (h - min_dim) // 2
        img    = img.crop((left, top, left + min_dim, top + min_dim))
        img    = img.resize((size, size), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75, optimize=True)
        raw = buf.getvalue()
        # Zapisz cache
        try:
            with open(thumb_path, "wb") as f:
                f.write(raw)
        except Exception:
            pass
        return Response(raw, media_type="image/jpeg",
                       headers={"Cache-Control": "max-age=86400", "Content-Disposition": "inline"})
    except Exception as e:
        log.warning(f"Thumbnail error {filename}: {e}")
        return {"error": "thumbnail failed"}


@app.get("/login")
async def login_page(request: Request):
    """Strona logowania — multi-user."""
    if check_auth(request):
        return RedirectResponse(url="/", status_code=302)
    error = request.query_params.get("error", "")
    error_msg = "Nieprawidłowy login lub hasło." if error else ""
    html = """<!DOCTYPE html><html lang="pl"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Logowanie – ComfyUI Mobile</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0a;color:#eee;font-family:-apple-system,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#141414;border:1px solid #222;border-radius:16px;padding:36px 28px;width:90%;max-width:360px}
h2{text-align:center;font-size:22px;margin-bottom:28px;color:#fff}
label{display:block;font-size:12px;color:#666;margin-bottom:5px}
input{width:100%;background:#0a0a0a;border:1px solid #2a2a2a;border-radius:8px;
      color:#eee;font-size:15px;padding:11px 14px;margin-bottom:14px;outline:none}
input:focus{border-color:#4CAF50}
button{width:100%;background:#4CAF50;color:#000;font-weight:700;font-size:15px;
       border:none;border-radius:10px;padding:13px;cursor:pointer;margin-top:4px}
.err{color:#f44;font-size:13px;text-align:center;margin-bottom:14px}
.logo{text-align:center;font-size:36px;margin-bottom:16px}
</style></head><body>
<div class="box">
  <div class="logo">🎨</div>
  <h2>ComfyUI Mobile</h2>""" + (f'<div class="err">{error_msg}</div>' if error_msg else "") + """
  <form method="post" action="/login">
    <label>Login</label>
    <input type="text" name="username" autocomplete="username" autofocus required placeholder="Twój login">
    <label>Hasło</label>
    <input type="password" name="password" autocomplete="current-password" required placeholder="••••••••">
    <button type="submit">Zaloguj</button>
  </form>
</div></body></html>"""
    return HTMLResponse(content=html)

@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    """Obsługuje formularz logowania — multi-user."""
    users = load_users()
    # Szukaj użytkownika po nazwie (case-insensitive)
    matched_uid = None
    matched_user = None
    for uid, u in users.items():
        if uid.lower() == username.lower() or u.get("name", "").lower() == username.lower():
            matched_uid = uid
            matched_user = u
            break
    if matched_user:
        stored = matched_user.get("password_hash", "")
        # Auto-fix: jeśli hash nie wygląda jak SHA256 (64 hex znaki) - zahaszuj go teraz
        if stored and len(stored) != 64:
            stored = hash_password(stored)
            matched_user["password_hash"] = stored
            users = load_users()
            if matched_uid in users:
                users[matched_uid]["password_hash"] = stored
                save_users(users)
                log.info(f"Auto-fix: zahaszowano hasło dla {matched_uid}")
        if hash_password(password) != stored:
            return RedirectResponse(url="/login?error=1", status_code=302)
    if matched_user:
        token = create_session(matched_uid)
        resp = RedirectResponse(url="/", status_code=302)
        resp.set_cookie("session_token", token, max_age=SESSION_DURATION,
                        httponly=True, samesite="lax")
        log.info(f"Zalogowano: {matched_uid}")
        return resp
    return RedirectResponse(url="/login?error=1", status_code=302)

@app.get("/logout")
async def logout():
    """Wylogowanie - usuwa sesję."""
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie("session_token")
    return resp


@app.get("/api/recent_images")
async def get_recent_images(request: Request):
    """Zwraca listę ostatnio użytych zdjęć wejściowych (mobile_*)."""
    _u = get_user_from_request(request)
    _img_dir = user_image_dir(_u)

    try:
        files = []
        for fname in os.listdir(_img_dir):
            if not fname.startswith("mobile_"):
                continue
            fpath = os.path.join(_img_dir, fname)
            mtime = os.path.getmtime(fpath)
            files.append({"filename": fname, "url": f"/mobile_image/{fname}", "timestamp": mtime})
        files.sort(key=lambda x: x["timestamp"], reverse=True)
        return {"status": "ok", "files": files[:20]}  # max 20 ostatnich
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/mobile_image/{filename}")
async def mobile_image(filename: str, request: Request):
    """Serwuje zdjęcie wejściowe (mobile_*)."""
    from fastapi.responses import FileResponse
    _u_mob = get_user_from_request(request)
    fpath = os.path.join(user_image_dir(_u_mob), filename)
    if os.path.exists(fpath) and filename.startswith("mobile_"):
        return FileResponse(fpath, media_type="image/jpeg")
    return {"error": "not found"}

@app.delete("/api/delete_image/{filename}")
async def delete_image(filename: str, request: Request):
    """Usuwa zdjęcie z galerii."""
    _u = get_user_from_request(request)
    _img_dir = user_image_dir(_u)

    if not filename.startswith("telegram_result_"):
        return {"status": "error", "message": "Niedozwolony plik"}
    fpath = os.path.join(_img_dir, filename)
    if os.path.exists(fpath):
        os.remove(fpath)
        # Usuń też metadane
        meta_path = os.path.join(_img_dir, "_gallery_meta.json")
        try:
            if os.path.exists(meta_path):
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                meta.pop(filename, None)
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        log.info(f"Usunieto: {filename}")
        return {"status": "ok"}
    return {"status": "error", "message": "Plik nie istnieje"}

@app.get("/api/download_image/{filename}")
async def download_image(filename: str, request: Request):
    """Pobieranie zdjęcia z galerii."""
    _u = get_user_from_request(request)
    _img_dir = user_image_dir(_u)

    from fastapi.responses import FileResponse
    if not filename.startswith("telegram_result_"):
        return {"error": "forbidden"}
    fpath = os.path.join(_img_dir, filename)
    if os.path.exists(fpath):
        return FileResponse(fpath, media_type="image/jpeg",
                           headers={"Content-Disposition": f"attachment; filename={filename}"})
    return {"error": "not found"}


@app.get("/sw.js")
async def service_worker():
    """Service Worker dla Web Push."""
    from fastapi.responses import Response
    sw_code = """
self.addEventListener('push', function(e) {
  var data = {};
  try { data = e.data.json(); } catch(x) { data = {title: 'ComfyUI', body: e.data ? e.data.text() : ''}; }
  e.waitUntil(
    self.registration.showNotification(data.title || 'ComfyUI', {
      body: data.body || '',
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      vibrate: [200, 100, 200],
      tag: 'comfyui-notification',
      renotify: true
    })
  );
});
self.addEventListener('notificationclick', function(e) {
  e.notification.close();
  e.waitUntil(clients.openWindow('/'));
});
self.addEventListener('install', function(e) { self.skipWaiting(); });
self.addEventListener('activate', function(e) { e.waitUntil(clients.claim()); });
"""
    return Response(sw_code, media_type="application/javascript")

@app.get("/manifest.json")
async def manifest():
    from fastapi.responses import JSONResponse
    return JSONResponse({
        "name": "ComfyUI Mobile",
        "short_name": "ComfyUI",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0a0a0a",
        "theme_color": "#0a0a0a",
        "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"}]
    })

@app.get("/icon-192.png")
async def icon():
    """Minimalna ikona PNG 192x192 dla PWA."""
    from fastapi.responses import Response
    import struct, zlib
    def make_png(size=192, color=(34, 197, 94)):
        def chunk(name, data):
            c = struct.pack('>I', len(data)) + name + data
            return c + struct.pack('>I', zlib.crc32(name + data) & 0xffffffff)
        ihdr = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)
        r, g, b = color
        raw = b''
        for _ in range(size):
            row = b'\x00' + bytes([r, g, b] * size)
            raw += row
        idat = zlib.compress(raw)
        return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')
    return Response(make_png(), media_type="image/png")

@app.post("/api/push/subscribe")
async def push_subscribe(request: Request):
    """Rejestruje subskrypcję push przeglądarki."""
    body = await request.json()
    sub = body.get("subscription")
    if not sub or "endpoint" not in sub:
        return {"status": "error", "message": "Brak subskrypcji"}
    # Dodaj uid usera do subskrypcji
    uid = get_uid_from_request(request)
    sub["_uid"] = uid
    # Unikaj duplikatów
    endpoint = sub["endpoint"]
    for existing in _push_subscriptions:
        if existing.get("endpoint") == endpoint:
            # Zaktualizuj uid jeśli się zmienił (np. zalogował jako inny user)
            if existing.get("_uid") != uid:
                existing["_uid"] = uid
                _save_push_subs()
                log.info(f"Zaktualizowano uid subskrypcji push: {uid}")
            return {"status": "ok", "message": "already_registered"}
    _push_subscriptions.append(sub)
    _save_push_subs()
    log.info(f"Nowa subskrypcja push: {endpoint[:60]}...")
    return {"status": "ok"}

@app.delete("/api/push/subscribe")
async def push_unsubscribe(request: Request):
    """Wyrejestrowuje subskrypcję push."""
    body = await request.json()
    endpoint = body.get("endpoint", "")
    global _push_subscriptions
    before = len(_push_subscriptions)
    _push_subscriptions = [s for s in _push_subscriptions if s.get("endpoint") != endpoint]
    _save_push_subs()
    return {"status": "ok", "removed": before - len(_push_subscriptions)}

@app.get("/api/push/vapid-public")
async def push_vapid_public():
    """Zwraca publiczny klucz VAPID dla przeglądarki."""
    _, pub = _get_vapid_keys()
    return {"public_key": pub or ""}


@app.get("/api/me")
async def get_me(request: Request):
    """Zwraca dane zalogowanego użytkownika."""
    _u = get_user_from_request(request)
    uid = get_uid_from_request(request)
    if not _u:
        return {"uid": "", "name": "", "role": ""}
    return {
        "uid":              uid,
        "name":             _u.get("name", uid),
        "role":             _u.get("role", "user"),
        "avatar":           _u.get("name","?")[0].upper() if _u.get("name") else "?",
        "simple_mode":      bool(_u.get("simple_mode", False)),
        "simple_workflows": _u.get("simple_workflows", {}),
    }

@app.get("/api/profiles")
async def get_profiles():
    s = load_settings()
    profiles = s.get("profiles", [])
    active   = s.get("active_profile", None)
    return {"profiles": profiles, "active": active}

@app.post("/api/profiles")
async def save_profile(request: Request):
    body = await request.json()
    s = load_settings()
    profiles = s.get("profiles", [])
    pid = body.get("id")
    if not pid:
        import time as _t
        pid = "p_" + str(int(_t.time() * 1000))
        body["id"] = pid
    # Update lub dodaj
    existing = next((i for i, p in enumerate(profiles) if p.get("id") == pid), None)
    if existing is not None:
        profiles[existing] = body
    else:
        profiles.append(body)
    s["profiles"] = profiles
    save_settings_file(s)
    return {"status": "ok", "id": pid}

@app.delete("/api/profiles/{pid}")
async def delete_profile(pid: str):
    s = load_settings()
    profiles = s.get("profiles", [])
    s["profiles"] = [p for p in profiles if p.get("id") != pid]
    if s.get("active_profile") == pid:
        s["active_profile"] = None
    save_settings_file(s)
    return {"status": "ok"}

@app.post("/api/profiles/activate/{pid}")
async def activate_profile(pid: str):
    s = load_settings()
    profiles = s.get("profiles", [])
    profile = next((p for p in profiles if p.get("id") == pid), None)
    if not profile:
        return {"status": "error", "message": "Nie znaleziono profilu"}
    s["active_profile"] = pid
    # Zapisz też last_generate_settings z profilu
    last = {}
    if profile.get("workflow_id"):   last["workflow_id"]   = profile["workflow_id"]
    if profile.get("workflow_name"): last["workflow_name"] = profile["workflow_name"]
    if profile.get("prefix"):        last["prefix"]        = profile["prefix"]
    if profile.get("suffix"):        last["suffix"]        = profile.get("suffix", "")
    if profile.get("style_main"):    last["style_main"]    = profile["style_main"]
    if profile.get("style_sub"):     last["style_sub"]     = profile["style_sub"]
    if profile.get("style_subsub"):  last["style_subsub"]  = profile["style_subsub"]
    if profile.get("style_mode"):    last["style_mode"]    = profile["style_mode"]
    if last:
        s["last_generate_settings"] = last
    save_settings_file(s)
    return {"status": "ok", "profile": profile}


# ══ PRESETY ═══════════════════════════════════════════════════════
@app.get("/api/presets")
async def get_presets(request: Request):
    """Zwraca presety zalogowanego użytkownika."""
    uid = get_uid_from_request(request)
    s   = load_settings()
    all_presets = s.get("presets", {})
    return {"presets": all_presets.get(uid, [])}

@app.post("/api/presets")
async def save_preset(request: Request):
    """Zapisuje lub aktualizuje preset użytkownika."""
    uid  = get_uid_from_request(request)
    body = await request.json()
    s    = load_settings()
    all_presets = s.get("presets", {})
    user_presets = all_presets.get(uid, [])
    pid = body.get("id")
    if not pid:
        pid = "pr_" + str(int(time.time() * 1000))
        body["id"] = pid
    body["updated_at"] = int(time.time())
    existing = next((i for i, p in enumerate(user_presets) if p.get("id") == pid), None)
    if existing is not None:
        user_presets[existing] = body
    else:
        user_presets.insert(0, body)
    all_presets[uid] = user_presets
    s["presets"] = all_presets
    save_settings_file(s)
    return {"status": "ok", "id": pid}

@app.delete("/api/presets/{pid}")
async def delete_preset(pid: str, request: Request):
    """Usuwa preset użytkownika."""
    uid = get_uid_from_request(request)
    s   = load_settings()
    all_presets = s.get("presets", {})
    all_presets[uid] = [p for p in all_presets.get(uid, []) if p.get("id") != pid]
    s["presets"] = all_presets
    save_settings_file(s)
    return {"status": "ok"}

@app.post("/api/presets/{pid}/load")
async def load_preset(pid: str, request: Request):
    """Zwraca dane presetu do załadowania w UI."""
    uid = get_uid_from_request(request)
    s   = load_settings()
    presets = s.get("presets", {}).get(uid, [])
    p = next((p for p in presets if p.get("id") == pid), None)
    if not p:
        return {"status": "error", "message": "Preset nie istnieje"}
    return {"status": "ok", "preset": p}

# ══════════════════════════════════════════════════════════════════

@app.get("/api/last_settings")
async def get_last_settings():
    """Zwraca ostatnio użyte ustawienia generowania."""
    s = load_settings()
    return s.get("last_generate_settings", {})

@app.post("/api/last_settings")
async def save_last_settings(request: Request):
    """Zapisuje ostatnio użyte ustawienia generowania."""
    body = await request.json()
    s = load_settings()
    s["last_generate_settings"] = body
    save_settings_file(s)
    return {"status": "ok"}

@app.get("/api/schedule")
async def get_schedule():
    """Zwraca ustawienia harmonogramu."""
    s = load_settings()
    sched = dict(s.get("schedule", {"enabled": False, "time": "22:00"}))
    # Dodaj info o ostatnim uruchomieniu
    last = s.get("last_generate_settings", {})
    sched["last_workflow_name"] = last.get("workflow_name", "")
    sched["last_image"]         = last.get("image_filename", "")
    return sched

@app.post("/api/schedule")
async def save_schedule(request: Request):
    """Zapisuje/aktywuje harmonogram."""
    global _schedule_job
    body = await request.json()
    s = load_settings()
    s["schedule"] = body
    save_settings_file(s)
    _setup_schedule(body)
    return {"status": "ok"}


@app.get("/api/stats")
async def get_stats(request: Request):
    """Zwraca historię czasów generowań do wykresu - per user."""
    uid = get_uid_from_request(request)
    hist_file = user_gen_history_file(uid) if uid else _GEN_HISTORY_FILE
    try:
        if os.path.exists(hist_file):
            with open(hist_file, "r", encoding="utf-8") as f:
                history = json.load(f)
        else:
            history = []
    except Exception:
        history = []
    # Ostatnie 20
    recent = history[-20:]
    if not recent:
        return {"status": "ok", "entries": [], "avg": 0, "total_count": 0}
    times = [e["duration_s"] for e in recent if "duration_s" in e]
    avg = round(sum(times) / len(times), 1) if times else 0
    return {
        "status": "ok",
        "entries": recent,
        "avg": avg,
        "total_count": len(history),
        "total_images": sum(e.get("iterations", 1) for e in history),
    }


@app.post("/generate")
async def generate(
    request: Request,
    background_tasks: BackgroundTasks,
    workflow_choice: str       = Form("Ricky_v4"),
    image:           UploadFile = File(...),
    iterations:      int       = Form(1),
    prefix:          str       = Form(""),
    mode:            str       = Form("auto"),
    main_style:      str       = Form("Claude"),
    sub_style:       str       = Form("Art"),
    subsub_style:    str       = Form("Boho"),
    suffix:          str       = Form(...)
):

    _u_g       = get_user_from_request(request)
    _img_dir_g = user_image_dir(_u_g)
    _comfy_g   = user_comfy_url(_u_g)
    _tg_tok_g  = user_tg_token(_u_g)
    _tg_cht_g  = user_tg_chat(_u_g)
    log.info(f"=== /generate: workflow={workflow_choice}, iterations={iterations}, plik={image.filename} ===")
    log.warning(f"=== STARY /generate wywolany: workflow={workflow_choice} - powinno byc /generate_v2! ===")
    app.state.should_stop   = False
    app.state.status_text   = "Przyjmowanie pliku od telefonu..."
    app.state.is_processing = True

    if app.state.last_workflow is None:
        app.state.last_workflow = workflow_choice

    if app.state.last_workflow != workflow_choice:
        log.info(f"Zmiana workflow {app.state.last_workflow} na {workflow_choice}, czyszcze VRAM...")
        app.state.status_text   = "Zmiana procesu: Odsmiecanie pamieci VRAM..."
        app.state.last_workflow = workflow_choice
        try:
            free_data = json.dumps({"unload_models": True, "free_memory": True}).encode('utf-8')
            req_free  = urllib.request.Request(
                f"http://{COMFY_URL}/free", data=free_data,
                headers={'Content-Type': 'application/json'}, method="POST"
            )
            urllib.request.urlopen(req_free, timeout=5)
        except Exception as e:
            log.warning(f"Blad czyszczenia VRAM: {e}")

    os.makedirs(_img_dir_g, exist_ok=True)
    filename  = f"mobile_{uuid.uuid4().hex[:8]}.jpg"
    file_path = os.path.join(_img_dir_g, filename)

    image_data = await image.read()
    log.info(f"Odebrano: {len(image_data)} bajtow -> {filename}")
    img = Image.open(io.BytesIO(image_data))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    if max(img.size) > MAX_IMAGE_SIZE:
        img.thumbnail((MAX_IMAGE_SIZE, MAX_IMAGE_SIZE), Image.Resampling.LANCZOS)
    img.save(file_path, format="JPEG", quality=JPEG_QUALITY)
    log.info(f"Zapisano: {file_path}, rozmiar: {img.size}")

    target_file = WORKFLOW_1 if workflow_choice == "Ricky_v4" else WORKFLOW_2
    with open(target_file, "r", encoding="utf-8") as f:
        workflow = json.load(f)

    workflow["13:3"]["inputs"]["selected_image"] = filename

    if workflow_choice == "Ricky_v4":
        p = workflow["9:209:211"]["inputs"]
        p["prefix"] = prefix; p["mode"] = mode
        p["main_style"] = main_style; p["sub_style"] = sub_style
        p["subsub_style"] = subsub_style; p["suffix"] = suffix
        log.info(f"Prompter Ricky_v4: mode={mode}, main={main_style}, sub={sub_style}, subsub={subsub_style}")
    elif workflow_choice == "PhotoRicky_v1.0":
        workflow["9:421"]["inputs"]["string_a"] = suffix
        log.info("Prompter PhotoRicky ustawiony.")

    background_tasks.add_task(process_in_background, workflow, iterations, workflow_choice)
    return {"status": "ok"}


# =========================================================
# WORKFLOW MANAGER – nowe endpointy
# =========================================================


# =========================================================
# USTAWIENIA – Telegram i inne (GET/POST)
# =========================================================
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

# ═══════════════════════════════════════════
# SESJE / AUTORYZACJA
# ═══════════════════════════════════════════
SESSION_DURATION = 7 * 24 * 3600  # 7 dni w sekundach
_active_sessions: dict = {}        # token -> {expires, user_id}

# ─── System użytkowników ───────────────────────────────────────────────────
USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")

def load_users() -> dict:
    """Wczytuje users.json. Format: {user_id: {name, password_hash, role, image_dir, ...}}"""
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_users(users: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

def get_user_by_id(uid: str) -> dict:
    return load_users().get(uid, {})

def get_user_from_request(request: Request) -> dict:
    """Zwraca słownik użytkownika na podstawie sesji."""
    token = request.cookies.get("session_token", "")
    session = _active_sessions.get(token, {})
    if not session or time.time() > session.get("expires", 0):
        return {}
    uid = session.get("user_id", "")
    return get_user_by_id(uid) if uid else {}

def get_uid_from_request(request: Request) -> str:
    token = request.cookies.get("session_token", "")
    session = _active_sessions.get(token, {})
    if not session or time.time() > session.get("expires", 0):
        return ""
    return session.get("user_id", "")

def user_image_dir(user: dict) -> str:
    """Zwraca katalog zdjęć użytkownika (fallback na globalny IMAGE_DIR)."""
    d = user.get("image_dir", "") or IMAGE_DIR
    os.makedirs(d, exist_ok=True)
    return d

def user_comfy_url(user: dict) -> str:
    return user.get("comfy_url", "") or COMFY_URL

def user_tg_token(user: dict) -> str:
    return user.get("telegram_token", "") or TELEGRAM_BOT_TOKEN

def user_tg_chat(user: dict) -> str:
    return user.get("telegram_chat", "") or TELEGRAM_CHAT_ID

def user_gen_history_file(uid: str) -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"gen_history_{uid}.json")
    return path

def user_allowed_workflows(user: dict) -> list:
    """Lista workflow_id dozwolonych dla użytkownika (pusta = wszystkie)."""
    return user.get("allowed_workflows", [])

def init_default_admin():
    """Tworzy domyślnego admina jeśli users.json nie istnieje."""
    if os.path.exists(USERS_FILE):
        return
    s = load_settings()
    existing_hash = s.get("password_hash", "")
    users = {
        "admin": {
            "name": "Admin",
            "role": "admin",
            "password_hash": existing_hash,
            "image_dir": s.get("image_dir", IMAGE_DIR),
            "comfy_url": s.get("comfy_url", COMFY_URL),
            "telegram_token": s.get("telegram_token", TELEGRAM_BOT_TOKEN),
            "telegram_chat":  s.get("telegram_chat_id", TELEGRAM_CHAT_ID),
            "allowed_workflows": [],
        }
    }
    save_users(users)
    log.info("Utworzono domyślnego admina w users.json")

# ─── Historia generowań ────────────────────────────────────────────────────
_GEN_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_history.json")

def load_gen_history() -> list:
    try:
        if os.path.exists(_GEN_HISTORY_FILE):
            with open(_GEN_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_gen_history(entry: dict, uid: str = "", hist_file: str = ""):
    """Dodaje wpis do historii generowań (max 200 ostatnich). Per-user."""
    try:
        target_file = hist_file or (user_gen_history_file(uid) if uid else _GEN_HISTORY_FILE)
        try:
            if os.path.exists(target_file):
                with open(target_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
            else:
                history = []
        except Exception:
            history = []
        history.append(entry)
        if len(history) > 200:
            history = history[-200:]
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"save_gen_history blad: {e}")

# ─── Push subscriptions ────────────────────────────────────────────────────
_push_subscriptions: list = []     # lista subskrypcji przeglądarek
_PUSH_SUBS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "push_subs.json")

def _load_push_subs():
    global _push_subscriptions
    try:
        if os.path.exists(_PUSH_SUBS_FILE):
            with open(_PUSH_SUBS_FILE, "r") as f:
                _push_subscriptions = json.load(f)
    except Exception:
        _push_subscriptions = []

def _save_push_subs():
    try:
        with open(_PUSH_SUBS_FILE, "w") as f:
            json.dump(_push_subscriptions, f)
    except Exception:
        pass

def _get_vapid_keys():
    """Zwraca klucze VAPID z settings, generuje jeśli brak."""
    if not _PUSH_AVAILABLE:
        return None, None
    s = load_settings()
    if "vapid_private" not in s or "vapid_public" not in s:
        keys = _push.generate_vapid_keys()
        s["vapid_private"] = keys["private_key"]
        s["vapid_public"]  = keys["public_key"]
        save_settings_file(s)
        log.info("Wygenerowano nowe klucze VAPID")
    return s["vapid_private"], s["vapid_public"]

def send_push_to_all(title: str, body: str):
    """Wysyła push notification do wszystkich zarejestrowanych przeglądarek."""
    if not _PUSH_AVAILABLE or not _push_subscriptions:
        return
    priv, pub = _get_vapid_keys()
    if not priv:
        return
    dead = []
    for sub in _push_subscriptions:
        ok = _push.send_push(sub, title, body, priv, pub)
        if not ok:
            dead.append(sub)
    for d in dead:
        try:
            _push_subscriptions.remove(d)
        except ValueError:
            pass
    if dead:
        _save_push_subs()
        log.info(f"Push: usunieto {len(dead)} wygaslych subskrypcji")

_load_push_subs()

# ─── Harmonogram ───────────────────────────────────────────────────────────
_schedule_job = None   # aktywny timer

def send_push_to_user(uid: str, title: str, body: str):
    """Wysyła push notification tylko do subskrypcji danego użytkownika."""
    if not _PUSH_AVAILABLE or not _push_subscriptions:
        log.warning(f"Push: niedostępne lub brak subskrypcji (available={_PUSH_AVAILABLE}, subs={len(_push_subscriptions)})")
        return
    priv, pub = _get_vapid_keys()
    if not priv:
        log.warning("Push: brak klucza VAPID")
        return
    matching = [s for s in _push_subscriptions if s.get("_uid") == uid]
    log.info(f"Push do {uid}: {len(matching)}/{len(_push_subscriptions)} subskrypcji pasuje. Title: {title}")
    dead = []
    for sub in _push_subscriptions:
        if sub.get("_uid") != uid:
            continue
        try:
            webpush(
                subscription_info=sub,
                data=json.dumps({"title": title, "body": body}),
                vapid_private_key=priv,
                vapid_claims={"sub": "mailto:admin@localhost"},
            )
            log.info(f"Push wysłany do {uid} OK")
        except WebPushException as e:
            err_str = str(e)
            log.warning(f"Push błąd dla {uid}: {err_str[:200]}")
            if "410" in err_str or "404" in err_str:
                dead.append(sub)
        except Exception as e:
            log.warning(f"Push nieznany błąd: {e}")
    for d in dead:
        _push_subscriptions.remove(d)
    if dead:
        _save_push_subs()

def _setup_schedule(schedule_cfg: dict):
    """Konfiguruje lub anuluje harmonogram startu generowania."""
    global _schedule_job
    if _schedule_job:
        _schedule_job.cancel()
        _schedule_job = None
    if not schedule_cfg.get("enabled"):
        return
    target_time = schedule_cfg.get("time", "22:00")
    try:
        h, m = map(int, target_time.split(":"))
    except Exception:
        log.warning(f"Harmonogram: nieprawidlowy czas {target_time!r}")
        return

    def _schedule_tick():
        global _schedule_job
        now = time.localtime()
        if now.tm_hour == h and now.tm_min == m:
            log.info(f"Harmonogram: czas {target_time} – sprawdzam czy mam co uruchomic")
            _fire_schedule()
        # Sprawdź znowu za minutę
        _schedule_job = threading.Timer(60, _schedule_tick)
        _schedule_job.daemon = True
        _schedule_job.start()

    # Pierwsze wywołanie za 30s (żeby nie odpalić od razu przy starcie)
    _schedule_job = threading.Timer(30, _schedule_tick)
    _schedule_job.daemon = True
    _schedule_job.start()
    log.info(f"Harmonogram aktywny: uruchomienie o {target_time}")

def _fire_schedule():
    """Uruchamia generowanie wg ostatnich ustawień (jeśli serwer wolny)."""
    if app.state.is_processing:
        log.info("Harmonogram: serwer zajety, pomijam")
        send_push_to_all("⏰ Harmonogram", "Serwer zajęty – generowanie pominięte")
        return

    s       = load_settings()
    last    = s.get("last_generate_settings")
    if not last:
        log.info("Harmonogram: brak ostatnich ustawien, pomijam")
        send_push_to_all("⏰ Harmonogram", "Brak ostatnich ustawień – ustaw i spróbuj ponownie")
        return

    workflow_id = last.get("workflow_id", "")
    configs     = wm.load_configs()
    cfg         = configs.get(workflow_id)
    if not cfg:
        log.warning(f"Harmonogram: nieznany workflow {workflow_id}")
        send_push_to_all("⏰ Harmonogram", f"Błąd: nieznany workflow {workflow_id}")
        return

    # Ustal plik zdjęcia - z last_settings lub pierwsze recent
    image_filename = last.get("image_filename", "")
    if not image_filename:
        # Fallback: znajdź najnowszy mobile_* w IMAGE_DIR
        try:
            files = [f for f in os.listdir(IMAGE_DIR) if f.startswith("mobile_")]
            if files:
                files.sort(key=lambda f: os.path.getmtime(os.path.join(IMAGE_DIR, f)), reverse=True)
                image_filename = files[0]
        except Exception:
            pass

    if not image_filename:
        log.warning("Harmonogram: brak zdjecia do generowania")
        send_push_to_all("⏰ Harmonogram", "Brak zdjęcia – wgraj zdjęcie i spróbuj ponownie")
        return

    image_path = os.path.join(IMAGE_DIR, image_filename)
    if not os.path.exists(image_path):
        log.warning(f"Harmonogram: brak pliku {image_path}")
        send_push_to_all("⏰ Harmonogram", f"Plik zdjęcia nie istnieje: {image_filename}")
        return

    try:
        iterations  = int(last.get("iterations", 1))
        form_values = {
            "suffix":       last.get("suffix",       ""),
            "prefix":       last.get("prefix",       ""),
            "style_mode":   last.get("style_mode",   "auto"),
            "style_main":   last.get("style_main",   ""),
            "style_sub":    last.get("style_sub",    ""),
            "style_subsub": last.get("style_subsub", ""),
            "image_1_filename": image_filename,
        }

        # Wczytaj plik workflow JSON
        wf_file = cfg.get("file", "")
        wf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), wf_file)
        if not os.path.exists(wf_path):
            log.warning(f"Harmonogram: brak pliku workflow {wf_file}")
            send_push_to_all("⏰ Harmonogram", f"Brak pliku workflow: {wf_file}")
            return

        with open(wf_path, "r", encoding="utf-8") as f:
            workflow = json.load(f)

        # Wstrzyknij wartości (plik zdjęcia + pola formularza)
        workflow = wm.inject_workflow_values(workflow, cfg, form_values)
        cfg["_last_form_values"] = form_values

        # Output node IDs
        output_ids = wm.get_output_node_ids(cfg)

        send_push_to_all("⏰ Harmonogram", f"Uruchamiam: {cfg.get('name', workflow_id)} ({iterations}×)")
        log.info(f"Harmonogram: start workflow={workflow_id}, iter={iterations}, img={image_filename}")
        # Zapisz timestamp ostatniego uruchomienia
        _s = load_settings()
        _sched_s = _s.get("schedule", {})
        _sched_s["last_fired"] = time.strftime("%Y-%m-%d %H:%M")
        _s["schedule"] = _sched_s
        save_settings_file(_s)

        app.state.should_stop   = False
        app.state.is_processing = True
        app.state.status_text   = f"Harmonogram: {cfg.get('name', workflow_id)}..."
        app.state.last_workflow = workflow_id

        # Uruchom w osobnym wątku
        t = threading.Thread(
            target=process_in_background_v2,
            args=(workflow, iterations, workflow_id, output_ids),
            daemon=True
        )
        t.start()

    except Exception as e:
        log.exception(f"Harmonogram: blad triggera: {e}")
        send_push_to_all("⏰ Harmonogram", f"Błąd uruchamiania: {e}")

# ══════════════════════════════════════════════════════════════
# TELEGRAM WATCHDOG
# ══════════════════════════════════════════════════════════════

_tg_watchdog_active   = False
_tg_watchdog_interval = 120   # sekund między pingami
_tg_last_ok: dict     = {}    # uid -> timestamp ostatniego OK
_tg_fail_count: dict  = {}    # uid -> liczba kolejnych błędów

def _tg_ping(token: str, label: str = "") -> bool:
    """Pinguje Telegram API /getMe. Zwraca True jeśli OK."""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getMe",
            timeout=10
        )
        return r.ok and r.json().get("ok", False)
    except Exception as e:
        log.warning(f"TG watchdog ping błąd [{label}]: {e}")
        return False

def _tg_watchdog_loop():
    """Wątek watchdoga — sprawdza wszystkich userów co _tg_watchdog_interval s."""
    global _tg_watchdog_active
    _tg_watchdog_active = True
    log.info("TG Watchdog: uruchomiony")
    while _tg_watchdog_active:
        try:
            users = load_users()
            checked = set()
            for uid, u in users.items():
                token = u.get("telegram_token", "") or TELEGRAM_BOT_TOKEN
                if not token or token in checked:
                    continue
                checked.add(token)
                label = u.get("name", uid)
                ok = _tg_ping(token, label)
                if ok:
                    _tg_last_ok[token]   = time.time()
                    _tg_fail_count[token] = 0
                    log.debug(f"TG Watchdog [{label}]: OK")
                else:
                    _tg_fail_count[token] = _tg_fail_count.get(token, 0) + 1
                    fails = _tg_fail_count[token]
                    log.warning(f"TG Watchdog [{label}]: BŁĄD (seria {fails})")
                    # Push do admina co 3 błędy z rzędu
                    if fails % 3 == 1:
                        send_push_to_all(
                            "⚠️ Telegram niedostępny",
                            f"Bot [{label}] nie odpowiada ({fails}× z rzędu)"
                        )
        except Exception as e:
            log.warning(f"TG Watchdog loop błąd: {e}")
        time.sleep(_tg_watchdog_interval)

def _start_tg_watchdog():
    t = threading.Thread(target=_tg_watchdog_loop, daemon=True, name="tg-watchdog")
    t.start()
    return t

@app.get("/api/tg_status")
async def tg_status(request: Request):
    """Status Telegram botów dla panelu admina."""
    _u = get_user_from_request(request)
    if _u.get("role") != "admin":
        return {"status": "error", "message": "Brak uprawnień"}
    users = load_users()
    result = []
    seen = set()
    for uid, u in users.items():
        token = u.get("telegram_token", "") or TELEGRAM_BOT_TOKEN
        if not token or token in seen:
            continue
        seen.add(token)
        last_ok = _tg_last_ok.get(token)
        fails   = _tg_fail_count.get(token, 0)
        result.append({
            "user":    u.get("name", uid),
            "ok":      fails == 0 and last_ok is not None,
            "fails":   fails,
            "last_ok": time.strftime("%H:%M:%S", time.localtime(last_ok)) if last_ok else "—",
        })
    return {"bots": result, "interval": _tg_watchdog_interval}

# Wczytaj harmonogram przy starcie
try:
    _sched_cfg = load_settings().get("schedule", {})
    if _sched_cfg.get("enabled"):
        _setup_schedule(_sched_cfg)
except Exception:
    pass

def _save_push_subs():
    try:
        with open(_PUSH_SUBS_FILE, "w") as f:
            json.dump(_push_subscriptions, f)
    except Exception:
        pass

def _get_vapid_keys():
    """Zwraca klucze VAPID z settings, generuje jeśli brak."""
    if not _PUSH_AVAILABLE:
        return None, None
    s = load_settings()
    if "vapid_private" not in s or "vapid_public" not in s:
        keys = _push.generate_vapid_keys()
        s["vapid_private"] = keys["private_key"]
        s["vapid_public"]  = keys["public_key"]
        save_settings_file(s)
        log.info("Wygenerowano nowe klucze VAPID")
    return s["vapid_private"], s["vapid_public"]

def send_push_to_all(title: str, body: str):
    """Wysyła push notification do wszystkich zarejestrowanych przeglądarek."""
    if not _PUSH_AVAILABLE or not _push_subscriptions:
        return
    priv, pub = _get_vapid_keys()
    if not priv:
        return
    dead = []
    for sub in _push_subscriptions:
        ok = _push.send_push(sub, title, body, priv, pub)
        if not ok:
            dead.append(sub)
    # Usuń martwe subskrypcje
    for d in dead:
        try:
            _push_subscriptions.remove(d)
        except ValueError:
            pass
    if dead:
        _save_push_subs()
        log.info(f"Push: usunieto {len(dead)} wygaslych subskrypcji")

_load_push_subs()

def get_password_hash() -> str:
    """Zwraca hash hasła z settings.json lub pusty string (brak hasła = brak ochrony)."""
    s = load_settings()
    return s.get("access_password_hash", "")

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def create_session(user_id: str = "") -> str:
    token = secrets.token_hex(32)
    _active_sessions[token] = {"expires": time.time() + SESSION_DURATION, "user_id": user_id}
    return token

def validate_session(token: str) -> bool:
    if not token or token not in _active_sessions:
        return False
    if time.time() > _active_sessions[token].get("expires", 0):
        del _active_sessions[token]
        return False
    return True

def is_protected() -> bool:
    """True jeśli hasło jest ustawione."""
    return bool(get_password_hash())

def check_auth(request: Request) -> bool:
    """Sprawdza czy request ma ważną sesję (lub brak ochrony)."""
    if not is_protected():
        return True
    token = request.cookies.get("session_token", "")
    return validate_session(token)

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_settings_file(data: dict):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.get("/api/settings")
async def get_settings():
    s = load_settings()
    # Nie wysylaj pelnego tokenu – maskuj
    token = s.get("telegram_token", TELEGRAM_BOT_TOKEN or "")
    masked = token[:6] + "..." + token[-4:] if len(token) > 12 else ("skonfigurowany" if token else "")
    return {
        "telegram_token_masked": masked,
        "telegram_token_set":    bool(token),
        "telegram_chat_id":      s.get("telegram_chat_id", TELEGRAM_CHAT_ID or ""),
        "image_dir":             s.get("image_dir", IMAGE_DIR or ""),
        "comfy_url":             s.get("comfy_url", COMFY_URL or "127.0.0.1:8188"),
        "vram_free_wait":        s.get("vram_free_wait", VRAM_FREE_WAIT or 3),
        "password_set":          bool(s.get("access_password_hash", "")),
    }

@app.post("/api/settings")
async def post_settings(request: Request):
    global TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, IMAGE_DIR, COMFY_URL, VRAM_FREE_WAIT
    body = await request.json()
    s = load_settings()
    
    if "telegram_token" in body and body["telegram_token"].strip():
        s["telegram_token"] = body["telegram_token"].strip()
        TELEGRAM_BOT_TOKEN  = s["telegram_token"]
    if "telegram_chat_id" in body:
        s["telegram_chat_id"] = body["telegram_chat_id"].strip()
        TELEGRAM_CHAT_ID = s["telegram_chat_id"]
    if "image_dir" in body and body["image_dir"].strip():
        s["image_dir"] = body["image_dir"].strip()
        IMAGE_DIR = s["image_dir"]
    if "comfy_url" in body and body["comfy_url"].strip():
        s["comfy_url"] = body["comfy_url"].strip()
        COMFY_URL = s["comfy_url"]
    if "vram_free_wait" in body:
        try:
            s["vram_free_wait"] = int(body["vram_free_wait"])
            VRAM_FREE_WAIT = s["vram_free_wait"]
        except Exception:
            pass
    
    # Obsługa hasła dostępu
    if "set_password" in body:
        pwd = body["set_password"].strip()
        if pwd:
            s["access_password_hash"] = hash_password(pwd)
            log.info("Haslo dostepu zostalo zmienione")
        else:
            s.pop("access_password_hash", None)
            log.info("Ochrona haslam wylaczona")

    save_settings_file(s)
    log.info(f"Zapisano ustawienia: {list(body.keys())}")
    return {"status": "ok"}

@app.post("/api/settings/test_telegram")
async def test_telegram(request: Request):
    body  = await request.json()
    token = body.get("token", TELEGRAM_BOT_TOKEN)
    chat  = body.get("chat_id", TELEGRAM_CHAT_ID)
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, data={"chat_id": chat, "text": "✅ Test z ComfyUI Mobile – polaczenie dziala!"}, timeout=10)
        if resp.ok:
            return {"status": "ok", "message": "Wiadomosc wyslana!"}
        return {"status": "error", "message": f"Blad Telegram: {resp.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/kreator", response_class=HTMLResponse)
async def kreator_page():
    kreator_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kreator.html")
    if os.path.exists(kreator_path):
        with open(kreator_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Brak pliku kreator.html</h1>")

@app.get("/api/workflows")
async def list_workflows():
    """Lista wszystkich zarejestrowanych workflow."""
    configs = wm.load_configs()
    result = []
    for wid, cfg in configs.items():
        result.append({
            "id":          wid,
            "name":        cfg.get("name", wid),
            "file":        cfg.get("file", ""),
            "has_style":   any(m["role"] == "style"   for m in cfg.get("mappings", [])),
            "has_image2":  any(m["role"] == "image_2" for m in cfg.get("mappings", [])),
            "custom_inputs": [m for m in cfg.get("mappings", []) if m["role"] == "custom"],
        })
    return result

@app.post("/api/workflows/scan")
async def scan_workflow(file: UploadFile = File(...)):
    """Przyjmuje plik JSON workflow i zwraca liste nodow do mapowania."""
    try:
        data = await file.read()
        workflow_json = json.loads(data)
        nodes = wm.scan_workflow_nodes(workflow_json)
        return {"filename": file.filename, "nodes": nodes, "status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/workflows/save")
async def save_workflow(request: Request):
    """Zapisuje konfiguracje workflow (po mapowaniu w kreatorze)."""
    try:
        body    = await request.json()
        wid     = body.get("id", "").strip().replace(" ", "_")
        name    = body.get("name", wid)
        fname   = body.get("file", "")
        mappings = body.get("mappings", [])
        output_node_ids = body.get("output_node_ids", [])
        
        if not wid or not fname:
            return {"status": "error", "message": "Brak id lub file"}
        
        # Skopiuj plik JSON workflow do folderu serwera jesli go tam nie ma
        src = body.get("file_path_temp", "")
        
        configs = wm.load_configs()
        configs[wid] = {
            "name":            name,
            "file":            fname,
            "mappings":        mappings,
            "output_node_ids": output_node_ids,
        }
        wm.save_configs(configs)
        log.info(f"Zapisano workflow: {wid} ({name})")
        return {"status": "ok", "id": wid}
    except Exception as e:
        log.error(f"save_workflow error: {e}")
        return {"status": "error", "message": str(e)}

@app.post("/api/workflows/upload_json")
async def upload_workflow_json(file: UploadFile = File(...)):
    """Uploaduje plik JSON workflow do folderu serwera i zwraca nazwe pliku."""
    try:
        data     = await file.read()
        workflow_json = json.loads(data)  # walidacja
        dest_dir = os.path.dirname(os.path.abspath(__file__))
        dest     = os.path.join(dest_dir, file.filename)
        with open(dest, "wb") as f:
            f.write(data)
        nodes = wm.scan_workflow_nodes(workflow_json)
        log.info(f"Upload workflow JSON: {file.filename}, {len(nodes)} nodow")
        return {"status": "ok", "filename": file.filename, "nodes": nodes}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.delete("/api/workflows/{workflow_id}")
async def delete_workflow(workflow_id: str):
    configs = wm.load_configs()
    if workflow_id in configs:
        del configs[workflow_id]
        wm.save_configs(configs)
        return {"status": "ok"}
    return {"status": "error", "message": "Nie znaleziono"}

@app.get("/api/workflows/{workflow_id}/config")
async def get_workflow_config(workflow_id: str):
    """Zwraca pelna konfiguracje workflow z wartosciami domyslnymi z pliku JSON."""
    configs = wm.load_configs()
    cfg = configs.get(workflow_id)
    if not cfg:
        return {"mappings": []}

    # Spróbuj wczytać plik workflow żeby pobrać wartości domyślne
    wf_file = cfg.get("file", "")
    wf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), wf_file)
    wf_data = {}
    if os.path.exists(wf_path):
        try:
            with open(wf_path, "r", encoding="utf-8") as f:
                wf_data = json.load(f)
        except Exception:
            pass

    # Wzbogać mappings o wartości domyślne z pliku
    mappings = []
    for m in cfg.get("mappings", []):
        mapping = dict(m)
        node_id = m.get("node_id", "")
        node = wf_data.get(node_id, {})
        inputs = node.get("inputs", {})

        if m.get("role") == "prompt":
            field = m.get("field", "")
            prefix_field = m.get("prefix_field", "")
            mapping["_suffix_default"] = inputs.get(field, "")
            if prefix_field:
                mapping["_prefix_default"] = inputs.get(prefix_field, "")

        mappings.append(mapping)

    return {"mappings": mappings}


@app.get("/api/workflows/{workflow_id}/nodes")
async def get_workflow_nodes(workflow_id: str):
    """Zwraca liste nodow z pliku JSON workflow dla kreatora."""
    configs = wm.load_configs()
    cfg = configs.get(workflow_id)
    if not cfg:
        return {"status": "error", "message": "Workflow nie znaleziony"}
    wf_file = cfg.get("file", "")
    wf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), wf_file)
    if not os.path.exists(wf_path):
        return {"status": "error", "message": f"Plik {wf_file} nie istnieje"}
    try:
        with open(wf_path, "r", encoding="utf-8") as f:
            wf_data = json.load(f)
        nodes = wm.scan_workflow_nodes(wf_data)
        return {"status": "ok", "nodes": nodes}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/node_info/{node_type}")
async def get_node_info(node_type: str):
    """Pobiera definicje noda z ComfyUI — typy i opcje pol."""
    data = comfy_get(f"/object_info/{node_type}", timeout=5)
    if not data or node_type not in data:
        return {"status": "error", "message": f"Brak info o nodzie {node_type}"}
    try:
        node_def = data[node_type]
        inputs_raw = {}
        inputs_raw.update(node_def.get("input", {}).get("required", {}))
        inputs_raw.update(node_def.get("input", {}).get("optional", {}))
        fields = {}
        for fname, fdef in inputs_raw.items():
            if not isinstance(fdef, (list, tuple)) or not fdef:
                continue
            ftype = fdef[0]
            entry = {}
            if isinstance(ftype, list):
                entry["type"]    = "select"
                entry["options"] = ftype
            elif ftype in ("INT", "FLOAT"):
                entry["type"] = "number"
                if len(fdef) > 1 and isinstance(fdef[1], dict):
                    entry["min"]  = fdef[1].get("min")
                    entry["max"]  = fdef[1].get("max")
                    entry["step"] = fdef[1].get("step")
            elif ftype == "BOOLEAN":
                entry["type"] = "bool"
            elif ftype == "STRING":
                entry["type"] = "text"
                if len(fdef) > 1 and isinstance(fdef[1], dict):
                    entry["multiline"] = fdef[1].get("multiline", False)
            else:
                continue
            fields[fname] = entry
        return {"status": "ok", "node_type": node_type, "fields": fields}
    except Exception as e:
        log.warning(f"get_node_info {node_type}: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/workflows/{workflow_id}/style_options")
async def get_style_options_for_workflow(workflow_id: str):
    """Pobiera opcje stylu dla danego workflow ze skonfigurowanego noda."""
    configs = wm.load_configs()
    cfg = configs.get(workflow_id)
    if not cfg:
        return {"options": {}}
    
    # Znajdz mapowanie roli 'style'
    style_mapping = next((m for m in cfg.get("mappings", []) if m["role"] == "style"), None)
    if not style_mapping:
        return {"options": {}}
    
    node_type = style_mapping.get("node_type", "")
    data = comfy_get(f"/object_info/{node_type}", timeout=5)
    if not data or node_type not in data:
        return {"options": {}}
    
    try:
        inputs = data[node_type]["input"]["required"]
        def extract(field):
            if field in inputs and isinstance(inputs[field][0], list):
                return inputs[field][0]
            return []
        mode_field   = style_mapping.get("mode_field", "mode")
        main_field   = style_mapping.get("main_field", "main_style")
        sub_field    = style_mapping.get("sub_field", "sub_style")
        subsub_field = style_mapping.get("subsub_field", "subsub_style")
        return {"options": {
            "mode":   extract(mode_field),
            "main":   extract(main_field),
            "sub":    extract(sub_field),
            "subsub": extract(subsub_field),
        }}
    except Exception as e:
        log.warning(f"get_style_options: {e}")
        return {"options": {}}

@app.post("/api/restart_comfy")
async def restart_comfy(request: Request):
    """Restartuje proces ComfyUI przez taskkill i nowy start."""
    _user = get_user_from_request(request)
    if not _user or _user.get("role") != "admin":
        return {"status": "error", "message": "Tylko admin może restartować"}
    import subprocess as _sp
    import threading
    comfy_url = COMFY_URL or "127.0.0.1:8188"
    comfy_path = r"C:\AI\New_Comfy"
    bat_path   = r"C:\AI\New_Comfy\run_nvidia_gpu - NEW.bat"
    def do_restart():
        import time as _time
        try:
            # Zabij procesy na porcie 8188 przez tasklist+taskkill
            result = _sp.run(
                ["cmd", "/c", "netstat -aon"],
                capture_output=True, text=True, timeout=10
            )
            for line in result.stdout.splitlines():
                if ":8188 " in line and "LISTENING" in line:
                    parts = line.split()
                    pid = parts[-1]
                    _sp.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=5)
                    log.info(f"Zabito PID {pid} (ComfyUI)")
            _time.sleep(3)
            # Uruchom ComfyUI ponownie
            _sp.Popen(
                ["cmd", "/c", "start", "", bat_path],
                cwd=comfy_path,
                creationflags=getattr(_sp, "CREATE_NEW_CONSOLE", 0)
            )
            log.info("ComfyUI restart zlecony OK")
        except Exception as e:
            log.error(f"Restart ComfyUI blad: {e}")
    threading.Thread(target=do_restart, daemon=True).start()
    return {"status": "ok", "message": "Restart zlecony"}

@app.post("/generate_v2")
async def generate_v2(request: Request, background_tasks: BackgroundTasks):
    """
    Nowy endpoint generowania – uzywa systemu workflow_manager.
    Przyjmuje multipart/form-data z polami dynamicznymi.
    """
    form = await request.form()

    # Kontekst użytkownika
    _user = get_user_from_request(request)
    _uid  = get_uid_from_request(request)
    _user_image_dir = user_image_dir(_user)
    _user_comfy_url = user_comfy_url(_user)
    _user_tg_token  = user_tg_token(_user)
    _user_tg_chat   = user_tg_chat(_user)
    _allowed_wf     = user_allowed_workflows(_user)

    workflow_id = form.get("workflow_id", "")
    iterations  = int(form.get("iterations", 1))
    log.info(f"=== generate_v2 wywolany: workflow_id={workflow_id!r}, iterations={iterations}, user={_uid} ===")

    configs = wm.load_configs()
    # Filtruj workflow per user (pusta lista = wszystkie)
    # Workflow przypisane w simple_workflows są zawsze dozwolone
    _simple_wf_ids = set()
    for v in _user.get("simple_workflows", {}).values():
        wid = v.get("wf", "") if isinstance(v, dict) else v
        if wid:
            _simple_wf_ids.add(wid)
    if _allowed_wf and workflow_id not in _allowed_wf and workflow_id not in _simple_wf_ids:
        return {"status": "error", "message": f"Brak dostępu do workflow: {workflow_id}"}
    cfg = configs.get(workflow_id)
    if not cfg:
        return {"status": "error", "message": f"Nieznany workflow: {workflow_id}"}
    
    wf_file = cfg.get("file", "")
    wf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), wf_file)
    if not os.path.exists(wf_path):
        return {"status": "error", "message": f"Brak pliku: {wf_file}"}
    
    with open(wf_path, "r", encoding="utf-8") as f:
        workflow = json.load(f)
    
    # --- Przetworz obrazy (moze byc 1 lub 2) ---
    app.state.should_stop    = False
    app.state.is_processing  = True
    app.state.processing_uid = _uid
    app.state.status_text    = "Przyjmowanie plikow..."
    
    os.makedirs(_user_image_dir, exist_ok=True)
    
    form_values = {}
    
    # Obrazy wejsciowe
    for role in ["image_1", "image_2"]:
        file_field = form.get(role)
        if file_field and hasattr(file_field, "read"):
            img_data = await file_field.read()
            if img_data:
                fname    = f"mobile_{uuid.uuid4().hex[:8]}.jpg"
                fpath    = os.path.join(_user_image_dir, fname)
                img      = Image.open(io.BytesIO(img_data))
                # Napraw orientację EXIF (iPhone zapisuje obrócone zdjęcia)
                try:
                    from PIL import ImageOps
                    img = ImageOps.exif_transpose(img)
                except Exception:
                    pass
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                if max(img.size) > MAX_IMAGE_SIZE:
                    img.thumbnail((MAX_IMAGE_SIZE, MAX_IMAGE_SIZE), Image.Resampling.LANCZOS)
                img.save(fpath, format="JPEG", quality=JPEG_QUALITY)
                form_values[f"{role}_filename"] = fname
                log.info(f"Zapisano obraz {role}: {fpath}")
    
    # Pozostale pola formularza
    import json as _json
    for key in form:
        if key not in ("image_1", "image_2", "workflow_id", "iterations"):
            form_values[key] = form.get(key, "")
    # Parsuj simple_overrides JSON
    if "simple_overrides" in form_values:
        try:
            form_values["_simple_overrides"] = _json.loads(form_values["simple_overrides"])
        except:
            form_values["_simple_overrides"] = {}
    else:
        form_values["_simple_overrides"] = {}
    
    # Wstrzyknij wartosci do workflow
    workflow = wm.inject_workflow_values(workflow, cfg, form_values)
    cfg["_last_form_values"] = form_values  # dla galerii meta
    
    # VRAM cleanup przy zmianie workflow
    if app.state.last_workflow and app.state.last_workflow != workflow_id:
        log.info(f"Zmiana workflow, czyszcze VRAM...")
        try:
            free_data = json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8")
            req_free  = urllib.request.Request(
                f"http://{COMFY_URL}/free", data=free_data,
                headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(req_free, timeout=5)
        except Exception as e:
            log.warning(f"VRAM cleanup error: {e}")
    
    app.state.last_workflow = workflow_id
    
    # Pobierz output node ids z konfiguracji (lub fallback do starych)
    output_ids = wm.get_output_node_ids(cfg)
    if not output_ids:
        # Fallback: wyciagnij output node_ids z mappings
        output_ids = [m["node_id"] for m in cfg.get("mappings", []) if m.get("role") == "output"]
    if not output_ids:
        log.warning("Brak output_node_ids w konfiguracji! Sprawdz kreator.")
        output_ids = []
    
    background_tasks.add_task(
        process_in_background_v2, workflow, iterations, workflow_id, output_ids,
        _uid, _user_image_dir, _user_comfy_url, _user_tg_token, _user_tg_chat
    )
    return {"status": "ok"}


def process_in_background_v2(workflow_template: dict, iterations: int,
                              workflow_id: str, output_node_ids: list,
                              uid: str = "", img_dir: str = "",
                              comfy_url: str = "", tg_token: str = "", tg_chat: str = ""):
    """Wersja process_in_background uzywajaca dynamicznych output node ids."""
    # Lokalne zmienne per-user (fallback na globalne)
    _IMG_DIR    = img_dir    or IMAGE_DIR
    _COMFY_URL  = comfy_url  or COMFY_URL
    _TG_TOKEN   = tg_token   or TELEGRAM_BOT_TOKEN
    _TG_CHAT    = tg_chat    or TELEGRAM_CHAT_ID
    _GEN_HIST   = user_gen_history_file(uid) if uid else _GEN_HISTORY_FILE
    log.info(f"=== BACKGROUND V2 START: workflow={workflow_id}, iterations={iterations} ===")
    log.info(f"V2: output_node_ids={output_node_ids}")
    app.state.is_processing = True
    app.state.total_iter    = iterations

    try:
        for i in range(iterations):
            if app.state.should_stop:
                app.state.status_text = "Przerwano recznie!"
                break

            _iter_start_time = time.time()
            app.state.current_iter = i + 1
            app.state.status_text  = "Generowanie w ComfyUI..."
            app.state.current_node = None
            app.state.step_value   = 0
            app.state.step_max     = 0
            log.info(f"--- V2 Iter {i+1}/{iterations} ---")
            app.state.current_style = ""  # reset stylu przed nową iteracją

            # Seed jest juz wstrzykniety przez inject_workflow_values,
            # ale przy wielu iteracjach musimy go losowac na nowo
            import random
            configs = wm.load_configs()
            cfg = configs.get(workflow_id, {})
            for mapping in cfg.get("mappings", []):
                if mapping.get("role") == "seed":
                    node_id = mapping.get("node_id")
                    field   = mapping.get("field")
                    node    = workflow_template.get(node_id)
                    if node and field in node.get("inputs", {}):
                        node["inputs"][field] = random.randint(1, 999999999999999)

            # Randomizuj seed w nodach RandomOrManual3LevelChoicesRelaxed
            # żeby każda iteracja losowała inny styl (nie ten sam przy tym samym seedzie)
            for _nid, _node in workflow_template.items():
                if not isinstance(_node, dict): continue
                if _node.get("class_type") == "RandomOrManual3LevelChoicesRelaxed":
                    if "seed" in _node.get("inputs", {}):
                        new_style_seed = random.randint(1, 999999999999999)
                        _node["inputs"]["seed"] = new_style_seed
                        log.info(f"V2 iter {i+1}: Nowy seed stylu dla noda {_nid}: {new_style_seed}")

            # ── Auto-fix workflow przed wysłaniem do ComfyUI ──────────────────
            # 0. Uploaduj pliki zdjęć do ComfyUI /upload/image (LoadImage tego wymaga)
            def _upload_to_comfy(local_path: str) -> str:
                """Uploaduje plik do ComfyUI i zwraca nazwę pliku w ComfyUI input/."""
                fname = os.path.basename(local_path)
                with open(local_path, "rb") as _f:
                    _file_bytes = _f.read()
                boundary = "----FormBoundary" + uuid.uuid4().hex
                CRLF = b"\r\n"
                body_parts = (
                    ("--" + boundary + "\r\n").encode()
                    + ('Content-Disposition: form-data; name="image"; filename="' + fname + '"\r\n').encode()
                    + b"Content-Type: image/jpeg\r\n\r\n"
                    + _file_bytes
                    + ("\r\n--" + boundary + "--\r\n").encode()
                )
                _req = urllib.request.Request(
                    "http://" + _COMFY_URL + "/upload/image",
                    data=body_parts,
                    headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
                    method="POST"
                )
                _resp = urllib.request.urlopen(_req, timeout=30)
                _result = json.loads(_resp.read())
                uploaded_name = _result.get("name", fname)
                log.info(f"Upload do ComfyUI: {fname} -> {uploaded_name}")
                return uploaded_name

            # Uploaduj wszystkie pliki mobile_* użyte w workflow (LoadImage nodes)
            for _nid, _node in workflow_template.items():
                if not isinstance(_node, dict): continue
                if _node.get("class_type") == "LoadImage":
                    _img_val = _node.get("inputs", {}).get("image", "")
                    if isinstance(_img_val, str) and _img_val.startswith("mobile_"):
                        _local = os.path.join(_IMG_DIR, _img_val)
                        if os.path.exists(_local):
                            try:
                                _uploaded = _upload_to_comfy(_local)
                                _node["inputs"]["image"] = _uploaded
                            except Exception as _ue:
                                log.error(f"Upload blad dla {_img_val}: {_ue}")

            # 1. Usuń nody nieużywane (ComfyUI waliduje ALL nody, nawet niepodłączone)
            def _get_needed_nodes(wf, start_ids):
                """BFS wstecz od output nodów."""
                visited = set()
                queue = list(start_ids)
                while queue:
                    nid = queue.pop()
                    if nid in visited or nid not in wf:
                        continue
                    visited.add(nid)
                    for val in wf[nid].get("inputs", {}).values():
                        if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                            queue.append(val[0])
                return visited

            _all_node_ids   = set(workflow_template.keys())
            _needed_ids     = _get_needed_nodes(workflow_template, output_node_ids)
            _unused_ids     = _all_node_ids - _needed_ids
            if _unused_ids:
                log.debug(f"Auto-fix: usuwam {len(_unused_ids)} nieuzywanych nodow: {sorted(_unused_ids)}")
                for _uid in _unused_ids:
                    del workflow_template[_uid]

            # 2. Fix seed=-1 + usuń niestandardowe pola UI (dict w inputs)
            _seed_fields = {"seed", "noise_seed"}
            for _nid, _node in workflow_template.items():
                if not isinstance(_node, dict):
                    continue
                _inputs = _node.get("inputs", {})
                for _sf in _seed_fields:
                    if _sf in _inputs and _inputs[_sf] == -1:
                        _inputs[_sf] = random.randint(1, 999999999999999)
                        log.debug(f"Auto-fix seed=-1 -> {_inputs[_sf]} (node {_nid}.{_sf})")
                _bad_keys = [k for k, v in _inputs.items() if isinstance(v, dict)]
                for _bk in _bad_keys:
                    log.debug(f"Auto-fix: usuwam pole UI {_nid}.{_bk}")
                    del _inputs[_bk]

            data      = json.dumps({"prompt": workflow_template})
            _resp = requests.post(
                f"http://{_COMFY_URL}/prompt",
                data=data,
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            if not _resp.ok:
                log.error(f"V2 ComfyUI HTTP {_resp.status_code} body: {_resp.text[:3000]}")
                raise Exception(f"ComfyUI HTTP {_resp.status_code}: {_resp.text[:500]}")
            response  = _resp.json()
            prompt_id = response["prompt_id"]
            log.info(f"V2 Iter {i+1}: prompt_id={prompt_id}")

            out_filenames = []
            poll_count    = 0
            while True:
                if app.state.should_stop:
                    app.state.status_text = "Przerwano recznie!"
                    break

                poll_count += 1
                if poll_count % 10 == 0:
                    log.info(f"V2 Iter {i+1}: poll #{poll_count}")

                hist_resp = comfy_get(f"/history/{prompt_id}", timeout=5)
                if hist_resp is None:
                    time.sleep(2)
                    continue

                if prompt_id in hist_resp:
                    app.state.status_text  = "Wysylanie na Telegram..."
                    app.state.current_node = "Gotowe"
                    outputs = hist_resp[prompt_id]["outputs"]
                    log.info(f"V2: output_node_ids={output_node_ids}")
                    log.info(f"V2: outputs keys={list(outputs.keys())}")

                    # Wyciągnij wylosowany styl z nodów ShowText lub RandomOrManual
                    for _nid, _out in outputs.items():
                        # ShowText|pysssss zwraca {"text": ["..."]}
                        if "text" in _out and isinstance(_out["text"], list) and _out["text"]:
                            # Sprawdź czy to node wejścia stylu (Prompter/RandomOrManual)
                            src_node = workflow_template.get(_nid, {})
                            src_type = src_node.get("class_type", "")
                            if src_type in ("ShowText|pysssss", "RandomOrManual3LevelChoicesRelaxed"):
                                _style_txt = str(_out["text"][0]).strip()
                                if _style_txt and len(_style_txt) > 2:
                                    app.state.current_style = _style_txt[:120]  # maks 120 znaków
                                    log.info(f"V2: wylosowany styl: {app.state.current_style}")
                                    break
                    for node_id in output_node_ids:
                        log.info(f"V2: sprawdzam node_id={node_id!r}, in_outputs={node_id in outputs}")
                        if node_id in outputs and "images" in outputs[node_id]:
                            log.info(f"V2: images={outputs[node_id]['images'][:2]}")  # pierwsze 2 dla debug
                        if node_id in outputs and "images" in outputs[node_id]:
                            for img_data in outputs[node_id]["images"]:
                                # Pomijaj miniatury (type=temp to preview 128x128)
                                if img_data.get("type", "output") == "temp":
                                    log.debug(f"V2: pomijam temp image {img_data['filename']}")
                                    continue
                                out_filenames.append((
                                    img_data["filename"] if isinstance(img_data["filename"], str) else img_data["filename"].decode("utf-8"),
                                    img_data.get("subfolder", "") or "",
                                    img_data.get("type", "output") or "output"
                                ))
                    break
                time.sleep(2)

            for out_filename, out_subfolder, out_type in out_filenames:
                if app.state.should_stop:
                    break
                url_img = f"http://{_COMFY_URL}/view?filename={urllib.parse.quote(out_filename)}&type={out_type}"
                if out_subfolder:
                    url_img += f"&subfolder={urllib.parse.quote(out_subfolder)}"
                log.info(f"V2 Iter {i+1}: Pobieranie pliku: {out_filename}")
                out_path = os.path.join(_IMG_DIR, f"telegram_result_{uuid.uuid4().hex[:8]}.jpg")
                with open(out_path, "wb") as f:
                    f.write(urllib.request.urlopen(url_img, timeout=30).read())
                log.info(f"V2 Iter {i+1}: Zapisano: {out_path}")
                # Zapisz metadane do galerii
                try:
                    form_vals = cfg.get("_last_form_values", {})
                    save_gallery_meta(os.path.basename(out_path), {
                        "workflow": workflow_id,
                        "suffix":   form_vals.get("suffix", ""),
                        "prefix":   form_vals.get("prefix", ""),
                        "style":    form_vals.get("style_main", "") + " / " + form_vals.get("style_sub", ""),
                        "timestamp": __import__("time").time(),
                    }, img_dir=_IMG_DIR)
                except Exception as _me:
                    log.debug(f"gallery meta error: {_me}")
                # Zapisz czas generowania do historii statystyk
                try:
                    _duration = round(time.time() - _iter_start_time, 1)
                    save_gen_history({
                        "timestamp":  time.time(),
                        "duration_s": _duration,
                        "workflow":   workflow_id,
                        "iterations": iterations,
                        "iter_num":   i + 1,
                    }, uid=uid, hist_file=_GEN_HIST)
                    log.info(f"V2 Iter {i+1}: czas={_duration}s")
                except Exception as _he:
                    log.debug(f"gen_history error: {_he}")
                if _TG_TOKEN and _TG_CHAT:
                    send_telegram_photo(out_path, i + 1, tg_token=_TG_TOKEN, tg_chat=_TG_CHAT)

    except Exception as e:
        log.exception(f"BLAD V2: {e}")
        app.state.status_text = "Wystapil blad."
    finally:
        app.state.is_processing  = False
        app.state.processing_uid = ""
        app.state.preview_b64    = None
        if not app.state.should_stop:
            app.state.status_text = "Serwer gotowy."

@app.post("/stop")
async def stop_generation():
    log.info("=== /stop ===")
    app.state.should_stop = True
    app.state.status_text = "Przerywanie pracy karty graficznej..."
    try:
        req = urllib.request.Request(f"http://{COMFY_URL}/interrupt", method="POST")
        urllib.request.urlopen(req, timeout=3)
    except Exception as e:
        log.warning(f"/stop blad interrupt: {e}")
    return {"status": "zatrzymano"}

@app.post("/shutdown_engine")
async def shutdown_engine():
    try:
        result = subprocess.check_output(
            "netstat -ano | findstr :8188", shell=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in result.splitlines():
            if "LISTENING" in line:
                pid = line.strip().split()[-1]
                subprocess.run(f"taskkill /F /PID {pid}", shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
                break
    except Exception:
        pass
    os.kill(os.getpid(), signal.SIGINT)
    return {"status": "closing"}
# ══════════════════════════════════════════════════════════════
# PANEL ADMINA
# ══════════════════════════════════════════════════════════════

@app.get("/admin")
async def admin_page(request: Request):
    """Panel admina – zarządzanie użytkownikami."""
    _u = get_user_from_request(request)
    if _u.get("role") != "admin":
        return RedirectResponse(url="/", status_code=302)
    users = load_users()
    configs = wm.load_configs()
    all_wf = [{"id": wid, "name": cfg.get("name", wid)} for wid, cfg in configs.items()]

    rows = ""
    for uid, u in users.items():
        allowed = u.get("allowed_workflows", [])
        wf_opts = "".join(
            f'<option value="{w["id"]}"{" selected" if w["id"] in allowed or not allowed else ""}>{w["name"]}</option>'
            for w in all_wf
        )
        # Selekty simple_workflows dla 3 akcji
        swf = u.get("simple_workflows", {})
        # Pobierz pola edytowalne z mappings dla każdego workflow
        configs = wm.load_configs()
        def get_editable_fields(wf_id):
            cfg = configs.get(wf_id, {})
            fields = []
            for m in cfg.get("mappings", []):
                role = m.get("role")
                if role == "prompt" and m.get("simple_editable"):
                    fields.append({
                        "type": "textarea",
                        "key": f'{m["node_id"]}::{m["field"]}',
                        "label": m.get("simple_label", m.get("node_title", m.get("field",""))),
                        "default": m.get("_suffix_default", ""),
                    })
                    if m.get("prefix_field"):
                        fields.append({
                            "type": "textarea",
                            "key": f'{m["node_id"]}::{m["prefix_field"]}',
                            "label": m.get("simple_label","") + " (prefix)",
                            "default": m.get("_prefix_default",""),
                        })
                elif role == "style":
                    # Pola stylu z dropdownami - opcje ładowane z workflow
                    fields.append({
                        "type": "style",
                        "key": f'{m["node_id"]}::style',
                        "label": m.get("node_title", "Styl"),
                        "node_id": m["node_id"],
                        "mode_field":   m.get("mode_field","mode"),
                        "main_field":   m.get("main_field","main_style"),
                        "sub_field":    m.get("sub_field","sub_style"),
                        "subsub_field": m.get("subsub_field","subsub_style"),
                        "wf_id": wf_id,
                    })
            return fields

        def render_simple_action(key, lbl):
            swf_val = swf.get(key, {})
            wf_id = swf_val.get("wf","") if isinstance(swf_val, dict) else swf_val
            overrides = swf_val.get("overrides", {}) if isinstance(swf_val, dict) else {}
            sel = "".join(
                f'<option value="{w["id"]}"{" selected" if wf_id==w["id"] else ""}>{w["name"]}</option>'
                for w in all_wf
            )
            # Pola edytowalne z zaznaczonego workflow
            editable = get_editable_fields(wf_id)
            fields_html = ""
            for ef in editable:
                ftype = ef.get("type","textarea")
                if ftype == "textarea":
                    val = overrides.get(ef["key"], ef.get("default",""))
                    val_esc = val.replace('"', '&quot;').replace("\n", "&#10;")
                    fields_html += (
                        f'<div style="margin-top:6px">'
                        f'<div style="font-size:10px;color:#555;margin-bottom:2px">{ef["label"]}</div>'
                        f'<textarea class="ai" data-field="simple_override__{key}__{ef["key"]}" rows="2" '
                        f'style="width:100%;background:#0a0a0a;border:1px solid #1e1e1e;color:#aaa;font-size:11px;border-radius:4px;padding:4px 6px;box-sizing:border-box;resize:vertical"'
                        f'>{val_esc}</textarea>'
                        f'</div>'
                    )
                elif ftype == "style":
                    wfid = ef.get("wf_id","")
                    style_key = ef["key"]
                    ov = overrides.get(style_key, {})
                    if isinstance(ov, str): ov = {}
                    main_val        = ov.get("main","")
                    sub_val         = ov.get("sub","")
                    subsub_val      = ov.get("subsub","")
                    mode_val        = ov.get("mode","auto")
                    mode_locked     = "checked" if ov.get("mode_locked")   else ""
                    main_locked     = "checked" if ov.get("main_locked")   else ""
                    sub_locked      = "checked" if ov.get("sub_locked")    else ""
                    subsub_locked   = "checked" if ov.get("subsub_locked") else ""
                    def lock_row(label, sel_field, sel_html, lock_field, locked):
                        return (
                            f'<div style="display:grid;grid-template-columns:1fr auto;gap:6px;align-items:center;margin-bottom:4px">'
                            f'<div>'
                            f'<div style="font-size:10px;color:#555;margin-bottom:2px">{label}</div>'
                            f'{sel_html}'
                            f'</div>'
                            f'<label style="display:flex;flex-direction:column;align-items:center;gap:2px;cursor:pointer;font-size:9px;color:#555;padding-top:14px">'
                            f'<input type="checkbox" class="ai" data-field="{lock_field}" {locked} style="width:13px;height:13px;accent-color:#e8b84b">'
                            f'zablokuj'
                            f'</label>'
                            f'</div>'
                        )
                    sel_mode = (
                        f'<select class="ai" data-field="simple_style_mode__{key}__{style_key}" '
                        f'style="width:100%;background:#0a0a0a;border:1px solid #222;color:#ccc;font-size:11px;border-radius:4px;padding:3px 6px">'
                        f'<option value="auto"{"  selected" if mode_val=="auto" else ""}>auto</option>'
                        f'<option value="manual_main"{"  selected" if mode_val=="manual_main" else ""}>manual_main</option>'
                        f'<option value="manual_sub"{"  selected" if mode_val=="manual_sub" else ""}>manual_sub</option>'
                        f'<option value="manual_all"{"  selected" if mode_val=="manual_all" else ""}>manual_all</option>'
                        f'</select>'
                    )
                    sel_main = (
                        f'<select class="ai" data-field="simple_style_main__{key}__{style_key}" '
                        f'data-autoload-style="1" data-wf-id="{wfid}" '
                        f'data-sub-field="simple_style_sub__{key}__{style_key}" '
                        f'data-subsub-field="simple_style_subsub__{key}__{style_key}" '
                        f'data-current-main="{main_val}" data-current-sub="{sub_val}" data-current-subsub="{subsub_val}" '
                        f'style="width:100%;background:#0a0a0a;border:1px solid #222;color:#ccc;font-size:11px;border-radius:4px;padding:3px 6px">'
                        f'<option value="">ładowanie...</option>'
                        f'</select>'
                    )
                    sel_sub = (
                        f'<select class="ai" data-field="simple_style_sub__{key}__{style_key}" '
                        f'style="width:100%;background:#0a0a0a;border:1px solid #222;color:#ccc;font-size:11px;border-radius:4px;padding:3px 6px">'
                        f'<option value="">— automatyczny —</option>'
                        f'</select>'
                    )
                    sel_subsub = (
                        f'<select class="ai" data-field="simple_style_subsub__{key}__{style_key}" '
                        f'style="width:100%;background:#0a0a0a;border:1px solid #222;color:#ccc;font-size:11px;border-radius:4px;padding:3px 6px">'
                        f'<option value="">— automatyczny —</option>'
                        f'</select>'
                    )
                    fields_html += (
                        f'<div style="margin-top:8px;padding:8px;background:#0a0a12;border:1px solid #1e1e2e;border-radius:6px">'
                        f'<div style="font-size:10px;color:#668;margin-bottom:6px;font-weight:600">🎨 {ef["label"]}'
                        f'<span style="font-size:9px;color:#444;font-weight:400;margin-left:6px">✓ zablokuj = user nie widzi pola</span></div>'
                        + lock_row("Tryb",            f"simple_style_mode__{key}__{style_key}",   sel_mode,   f"simple_style_mode_locked__{key}__{style_key}",   mode_locked)
                        + lock_row("Styl główny",     f"simple_style_main__{key}__{style_key}",   sel_main,   f"simple_style_main_locked__{key}__{style_key}",   main_locked)
                        + lock_row("Substyl",         f"simple_style_sub__{key}__{style_key}",    sel_sub,    f"simple_style_sub_locked__{key}__{style_key}",    sub_locked)
                        + lock_row("Substyl podrz.",  f"simple_style_subsub__{key}__{style_key}", sel_subsub, f"simple_style_subsub_locked__{key}__{style_key}", subsub_locked)
                        + f'</div>'
                    )
            if not fields_html:
                fields_html = '<div style="font-size:10px;color:#333;margin-top:4px">Brak edytowalnych pól — zaznacz "Edytowalny w simple mode" w Kreatorze</div>'
            return (
                f'<div style="margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid #111">'
                f'<div style="margin-bottom:4px;font-size:12px;color:#aaa">{lbl}</div>'
                f'<select class="ai" data-field="simple_wf__{key}" onchange="adminReloadSimpleFields(this,\'{key}\')"'
                f' style="width:100%;background:#0a0a0a;border:1px solid #222;color:#ccc;font-size:11px;border-radius:4px;padding:3px 6px;margin-bottom:2px">'
                f'<option value="">— brak —</option>{sel}</select>'
                f'<div id="simple-fields-{key}">{fields_html}</div>'
                f'</div>'
            )

        swf_rows = "".join(
            render_simple_action(key, lbl)
            for key, lbl in [("try-on","👗 Ciuch"),("style","🎨 Styl"),("bg","🌅 Tło")]
        )
        rows += f"""""
<div id="row-{uid}" style="background:#111;border:1px solid #1e1e1e;border-radius:10px;margin-bottom:8px;overflow:hidden">
  <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 14px;background:#161616;cursor:pointer" onclick="toggleCard('{uid}')">
    <div style="display:flex;align-items:center;gap:10px">
      <span style="font-weight:700;color:#e8b84b">{uid}</span>
      <span style="font-size:11px;color:#555">{u.get('name','')}</span>
      <span style="font-size:10px;background:#1a1a2a;color:#668;border-radius:4px;padding:2px 6px">{u.get('role','user')}</span>
      {"<span style='font-size:10px;background:#0d1a0d;color:#4a8;border-radius:4px;padding:2px 6px'>simple</span>" if u.get("simple_mode") else ""}
    </div>
    <div style="display:flex;gap:6px">
      <button onclick="event.stopPropagation();saveUser('{uid}')" style="background:#1a3a1a;border:1px solid #4CAF50;color:#4CAF50;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px">Zapisz</button>
      <button onclick="event.stopPropagation();deleteUser('{uid}')" style="background:#3a0000;border:1px solid #660000;color:#f44;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px">Usuń</button>
    </div>
  </div>
  <div id="card-{uid}" style="padding:14px;display:grid;grid-template-columns:1fr 1fr;gap:12px">
    <div style="display:flex;flex-direction:column;gap:8px">
      <div><div style="font-size:10px;color:#555;margin-bottom:3px">Wyświetlana nazwa</div>
        <input class="ai" data-field="name" value="{u.get('name','')}" placeholder="Wyświetlana nazwa"></div>
      <div><div style="font-size:10px;color:#555;margin-bottom:3px">Hasło (puste=bez zmian)</div>
        <input class="ai" data-field="password" value="" placeholder="Nowe hasło" type="password"></div>
      <div><div style="font-size:10px;color:#555;margin-bottom:3px">Katalog zdjęć</div>
        <input class="ai" data-field="image_dir" value="{u.get('image_dir','')}"></div>
      <div><div style="font-size:10px;color:#555;margin-bottom:3px">ComfyUI URL</div>
        <input class="ai" data-field="comfy_url" value="{u.get('comfy_url','')}"></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
        <div><div style="font-size:10px;color:#555;margin-bottom:3px">TG Token</div>
          <input class="ai" data-field="telegram_token" value="{u.get('telegram_token','')}"></div>
        <div><div style="font-size:10px;color:#555;margin-bottom:3px">TG Chat ID</div>
          <input class="ai" data-field="telegram_chat" value="{u.get('telegram_chat','')}"></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
        <div><div style="font-size:10px;color:#555;margin-bottom:3px">Rola</div>
          <select class="ai" data-field="role">
            <option value="user"{"  selected" if u.get("role")!="admin" else ""}>user</option>
            <option value="admin"{"  selected" if u.get("role")=="admin" else ""}>admin</option>
          </select></div>
        <div><div style="font-size:10px;color:#555;margin-bottom:3px">Workflow (puste=wszystkie)</div>
          <select class="ai" multiple data-field="allowed_workflows" style="height:60px" title="Puste=wszystkie">
            {wf_opts}
          </select></div>
      </div>
    </div>
    <div style="background:#0d0d0d;border:1px solid #1a1a1a;border-radius:8px;padding:12px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-bottom:10px">
        <input type="checkbox" class="ai" data-field="simple_mode" {"checked" if u.get("simple_mode") else ""}
               onchange="toggleSimpleWfPanel(this,'{uid}')" style="width:15px;height:15px">
        <span style="font-size:13px;color:#aaa;font-weight:600">Tryb uproszczony</span>
      </label>
      <div id="swf-{uid}" style="display:{"block" if u.get("simple_mode") else "none"}">
        {swf_rows}
      </div>
    </div>
  </div>
</div>"""

    html = f"""<!DOCTYPE html><html lang="pl"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Panel Admina</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0a0a0a;color:#eee;font-family:-apple-system,sans-serif;padding:20px}}
h1{{font-size:20px;margin-bottom:20px;color:#fff}}
.ai{{background:#0f0f0f;border:1px solid #2a2a2a;border-radius:6px;color:#eee;padding:5px 8px;font-size:12px;width:100%}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{background:#1a1a1a;color:#666;padding:8px;text-align:left;border-bottom:1px solid #222;white-space:nowrap}}
tr:hover td{{background:#111}}
.msg{{position:fixed;top:16px;right:16px;background:#1a3a1a;border:1px solid #4CAF50;color:#4CAF50;padding:10px 18px;border-radius:10px;font-size:13px;display:none}}
.new-form{{background:#141414;border:1px solid #222;border-radius:12px;padding:20px;margin-bottom:24px;display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px}}
.new-form input{{background:#0f0f0f;border:1px solid #2a2a2a;border-radius:6px;color:#eee;padding:8px 10px;font-size:13px;width:100%}}
.btn-green{{background:#1a3a1a;border:1px solid #4CAF50;color:#4CAF50;border-radius:8px;padding:8px 18px;cursor:pointer;font-size:13px}}
a.back{{color:#555;text-decoration:none;font-size:13px;display:inline-block;margin-bottom:16px}}
#tbody input, #tbody select, #tbody textarea {{
  background:#0a0a0a;border:1px solid #222;color:#ccc;border-radius:5px;
  padding:5px 8px;font-size:12px;width:100%;box-sizing:border-box;
}}
#tbody input:focus, #tbody select:focus, #tbody textarea:focus {{
  border-color:#e8b84b;outline:none;
}}
.msg{{display:none;position:fixed;bottom:20px;right:20px;padding:10px 18px;border-radius:8px;border:1px solid;font-size:13px;z-index:999}}
</style></head><body>
<a class="back" href="/">← Wróć do aplikacji</a>
<h1>👤 Panel Admina – Użytkownicy</h1>
<div id="tg-status-bar" style="margin-bottom:16px;display:flex;gap:10px;flex-wrap:wrap"></div>
<script>
(async function() {{
  try {{
    var r = await fetch('/api/tg_status');
    var d = await r.json();
    var bar = document.getElementById('tg-status-bar');
    (d.bots || []).forEach(function(b) {{
      var ok = b.ok;
      var pill = document.createElement('div');
      pill.style.cssText = 'padding:6px 14px;border-radius:20px;font-size:12px;border:1px solid '+(ok?'#4CAF50':'#f44')+';color:'+(ok?'#4CAF50':'#f44')+';background:'+(ok?'#1a3a1a':'#3a0000');
      pill.textContent = (ok?'🟢':'🔴') + ' ' + b.user + ' · ' + (ok ? 'OK ' + b.last_ok : 'BŁĄD ×' + b.fails);
      bar.appendChild(pill);
    }});
    if (!d.bots || !d.bots.length) {{
      bar.innerHTML = '<div style="color:#555;font-size:12px">Brak skonfigurowanych botów TG</div>';
    }}
  }} catch(e) {{}}
}})();
</script>

<div class="new-form">
  <input id="n-uid"   placeholder="Login (ID)">
  <input id="n-name"  placeholder="Wyświetlana nazwa">
  <input id="n-pass"  placeholder="Hasło" type="password">
  <input id="n-dir"   placeholder="Katalog zdjęć">
  <input id="n-comfy" placeholder="ComfyUI URL">
  <input id="n-tgt"   placeholder="Telegram token">
  <input id="n-tgc"   placeholder="Telegram chat ID">
  <div style="grid-column:span 2">
    <button class="btn-green" onclick="addUser()">+ Dodaj użytkownika</button>
  </div>
</div>

<div style="overflow-x:auto">
<div id="tbody" style="display:flex;flex-direction:column;gap:4px">{rows}</div>
</div>
<div class="msg" id="msg"></div>
<script>
function toggleCard(uid) {{
  var card = document.getElementById('card-' + uid);
  if (card) card.style.display = card.style.display === 'none' ? 'grid' : 'none';
}}

// Inicjalizuj wszystkie selekty stylu po załadowaniu DOM
document.addEventListener('DOMContentLoaded', function() {{
  document.querySelectorAll('[data-autoload-style="1"]').forEach(function(sel) {{
    var wfId      = sel.getAttribute('data-wf-id');
    var subField  = sel.getAttribute('data-sub-field');
    var curMain   = sel.getAttribute('data-current-main') || '';
    var curSub    = sel.getAttribute('data-current-sub')  || '';
    adminLoadMainStyles(sel.getAttribute('data-field'), subField, wfId, curMain, curSub);
  }});
}});

function toggleSimpleWfPanel(chk, uid) {{
  var panel = document.getElementById('swf-' + uid);
  if (panel) panel.style.display = chk.checked ? 'block' : 'none';
}}

function showMsg(txt, ok=true) {{
  var m=document.getElementById('msg');
  m.textContent=txt; m.style.display='block';
  m.style.background=ok?'#1a3a1a':'#3a0000';
  m.style.borderColor=m.style.color=ok?'#4CAF50':'#f44';
  setTimeout(()=>m.style.display='none', 2500);
}}

async function adminLoadMainStyles(mainSelId, subSelId, wfId, currentMain, currentSub) {{
  if (!wfId) return;
  try {{
    var r = await fetch('/api/workflows/' + encodeURIComponent(wfId) + '/style_options');
    var d = await r.json();
    var opts = d.options || {{}};
    var mainSel    = document.querySelector('[data-field="' + mainSelId + '"]');
    var subSel     = document.querySelector('[data-field="' + subSelId + '"]');
    // subsub: pole pochodne od sub - szukamy pola data-subsub-field na mainSel
    var subsubField = mainSel ? mainSel.getAttribute('data-subsub-field') : null;
    var subsubSel   = subsubField ? document.querySelector('[data-field="' + subsubField + '"]') : null;
    var currentSubsub = mainSel ? (mainSel.getAttribute('data-current-subsub') || '') : '';
    if (!mainSel) return;

    // main to płaska lista
    var mainList = Array.isArray(opts.main) ? opts.main : Object.keys(opts.main || {{}});
    if (!mainList.length) {{
      mainSel.innerHTML = '<option value="">⚠ Uruchom ComfyUI</option>';
      return;
    }}
    mainSel.innerHTML = '<option value="">— automatyczny —</option>' +
      mainList.map(function(k) {{
        return '<option value="' + k + '"' + (k===currentMain?' selected':'') + '>' + k + '</option>';
      }}).join('');

    // sub i subsub to płaskie listy
    var subList    = Array.isArray(opts.sub)    ? opts.sub    : [];
    var subsubList = Array.isArray(opts.subsub) ? opts.subsub : [];

    if (subSel) {{
      subSel.innerHTML = '<option value="">— automatyczny —</option>' +
        subList.map(function(s) {{ return '<option value="'+s+'"'+(s===currentSub?' selected':'')+'>'+s+'</option>'; }}).join('');
    }}
    if (subsubSel) {{
      subsubSel.innerHTML = '<option value="">— automatyczny —</option>' +
        subsubList.map(function(s) {{ return '<option value="'+s+'"'+(s===currentSubsub?' selected':'')+'>'+s+'</option>'; }}).join('');
    }}

    // Zapamiętaj listy na selectie - nie używamy hierarchii bo API daje płaskie listy
    mainSel._subList    = subList;
    mainSel._subsubList = subsubList;
    mainSel._subSel     = subSel;
    mainSel._subsubSel  = subsubSel;
  }} catch(e) {{ console.warn('adminLoadMainStyles error', e); }}
}}

async function adminLoadSubStyles(mainSel, subSelId, wfId) {{
  // Wywołane przez onchange - deleguje do adminLoadMainStyles z aktualną wartością
  var subSel = document.querySelector('[data-field="' + subSelId + '"]');
  if (!mainSel._styleOpts) return;
  var subs = mainSel._styleOpts[mainSel.value] || [];
  if (subSel) subSel.innerHTML = '<option value="">— automatyczny —</option>' +
    subs.map(function(s) {{ return '<option value="'+s+'">'+s+'</option>'; }}).join('');
}}

async function adminReloadSimpleFields(sel, actionKey) {{
  var wfId = sel.value;
  var container = document.getElementById('simple-fields-' + actionKey);
  if (!container) return;
  if (!wfId) {{ container.innerHTML = ''; return; }}
  try {{
    var r = await fetch('/admin/api/workflow_simple_fields?wf_id=' + encodeURIComponent(wfId));
    var fields = await r.json();
    if (!fields.length) {{
      container.innerHTML = '<div style="font-size:10px;color:#333;margin-top:4px">Brak edytowalnych pól — zaznacz "Edytowalny w simple mode" w Kreatorze</div>';
      return;
    }}
    container.innerHTML = fields.map(function(ef) {{
      if (ef.type === 'style') {{
        var wfId2    = ef.wf_id || wfId;
        var styleKey = ef.key;
        var F = function(lvl) {{ return 'simple_style_' + lvl + '__' + actionKey + '__' + styleKey; }};
        var L = function(lvl) {{ return 'simple_style_' + lvl + '_locked__' + actionKey + '__' + styleKey; }};
        function styleRow(label, selHtml, lockField) {{
          return '<div style="display:grid;grid-template-columns:1fr auto;gap:6px;align-items:end;margin-bottom:6px">'
            + '<div><div style="font-size:10px;color:#555;margin-bottom:2px">' + label + '</div>' + selHtml + '</div>'
            + '<label style="display:flex;flex-direction:column;align-items:center;gap:2px;cursor:pointer;padding-bottom:2px">'
            + '<input type="checkbox" class="ai" data-field="' + lockField + '" style="width:13px;height:13px;accent-color:#e8b84b">'
            + '<span style="font-size:9px;color:#555">🔒</span>'
            + '</label></div>';
        }}
        var selMode = '<select class="ai" data-field="' + F('mode') + '" style="width:100%;background:#0a0a0a;border:1px solid #222;color:#ccc;font-size:11px;border-radius:4px;padding:3px 6px">'
          + '<option value="auto">auto</option><option value="manual_main">manual_main</option>'
          + '<option value="manual_sub">manual_sub</option><option value="manual_all">manual_all</option></select>';
        var selMain = '<select class="ai" data-field="' + F('main') + '" data-autoload-style="1" data-wf-id="' + wfId2 + '" '
          + 'data-sub-field="' + F('sub') + '" data-subsub-field="' + F('subsub') + '" data-current-main="" data-current-sub="" data-current-subsub="" '
          + 'style="width:100%;background:#0a0a0a;border:1px solid #222;color:#ccc;font-size:11px;border-radius:4px;padding:3px 6px">'
          + '<option value="">ładowanie...</option></select>';
        var selSub = '<select class="ai" data-field="' + F('sub') + '" style="width:100%;background:#0a0a0a;border:1px solid #222;color:#ccc;font-size:11px;border-radius:4px;padding:3px 6px">'
          + '<option value="">— auto —</option></select>';
        var selSubsub = '<select class="ai" data-field="' + F('subsub') + '" style="width:100%;background:#0a0a0a;border:1px solid #222;color:#ccc;font-size:11px;border-radius:4px;padding:3px 6px">'
          + '<option value="">— auto —</option></select>';
        return '<div style="margin-top:8px;padding:8px;background:#0a0a12;border:1px solid #1e1e2e;border-radius:6px">'
          + '<div style="font-size:10px;color:#668;font-weight:600;margin-bottom:2px">🎨 ' + ef.label + '</div>'
          + '<div style="font-size:9px;color:#444;margin-bottom:8px">🔒 = zablokuj (user nie widzi pola, używana wartość z góry)</div>'
          + styleRow('Tryb',           selMode,   L('mode'))
          + styleRow('Styl główny',    selMain,   L('main'))
          + styleRow('Substyl',        selSub,    L('sub'))
          + styleRow('Substyl podrz.', selSubsub, L('subsub'))
          + '</div>';
      }}
      return '<div style="margin-top:6px">' +
        '<div style="font-size:10px;color:#555;margin-bottom:2px">' + ef.label + '</div>' +
        '<textarea class="ai" data-field="simple_override__' + actionKey + '__' + ef.key + '" rows="2" ' +
        'style="width:100%;background:#0a0a0a;border:1px solid #1e1e1e;color:#aaa;font-size:11px;border-radius:4px;padding:4px 6px;box-sizing:border-box;resize:vertical">' +
        ef.default.replace(/</g,'&lt;') + '</textarea></div>';
    }}).join('');
  }} catch(e) {{ container.innerHTML = '<div style="color:#f55">Błąd ładowania pól</div>'; }}
  // Po załadowaniu pól - zainicjalizuj selekty stylów
  container.querySelectorAll('[data-autoload-style="1"]').forEach(function(sel) {{
    var wfId     = sel.getAttribute('data-wf-id');
    var subField = sel.getAttribute('data-sub-field');
    var curMain  = sel.getAttribute('data-current-main') || '';
    var curSub   = sel.getAttribute('data-current-sub')  || '';
    adminLoadMainStyles(sel.getAttribute('data-field'), subField, wfId, curMain, curSub);
  }});
}}

async function saveUser(uid) {{
  var row = document.getElementById('row-'+uid);
  var data = {{id: uid}};
  var swf = {{}};
  row.querySelectorAll('.ai').forEach(function(el) {{
    var field = el.dataset.field;
    if (!field) return;
    if (field.indexOf('simple_wf__') === 0) {{
      var key = field.split('__')[1];
      if (!swf[key] || typeof swf[key] !== 'object') swf[key] = {{wf:'',overrides:{{}}}};
      swf[key].wf = el.value;
    }} else if (field.indexOf('simple_override__') === 0) {{
      var parts = field.split('__');
      var actionKey = parts[1];
      var fieldKey  = parts.slice(2).join('__');
      if (!swf[actionKey] || typeof swf[actionKey] !== 'object') swf[actionKey] = {{wf:'',overrides:{{}}}};
      if (el.value.trim()) swf[actionKey].overrides[fieldKey] = el.value;
    }} else if (field.indexOf('simple_style_mode__') === 0) {{
      var parts = field.split('__');
      var actionKey = parts[1]; var styleKey = parts.slice(2).join('__');
      if (!swf[actionKey] || typeof swf[actionKey] !== 'object') swf[actionKey] = {{wf:'',overrides:{{}}}};
      if (!swf[actionKey].overrides[styleKey] || typeof swf[actionKey].overrides[styleKey] !== 'object') swf[actionKey].overrides[styleKey] = {{}};
      swf[actionKey].overrides[styleKey].mode = el.value;
    }} else if (field.indexOf('simple_style_main__') === 0) {{
      var parts = field.split('__');
      var actionKey = parts[1]; var styleKey = parts.slice(2).join('__');
      if (!swf[actionKey] || typeof swf[actionKey] !== 'object') swf[actionKey] = {{wf:'',overrides:{{}}}};
      if (!swf[actionKey].overrides[styleKey] || typeof swf[actionKey].overrides[styleKey] !== 'object') swf[actionKey].overrides[styleKey] = {{}};
      swf[actionKey].overrides[styleKey].main = el.value;
    }} else if (field.indexOf('simple_style_sub__') === 0) {{
      var parts = field.split('__');
      var actionKey = parts[1]; var styleKey = parts.slice(2).join('__');
      if (!swf[actionKey] || typeof swf[actionKey] !== 'object') swf[actionKey] = {{wf:'',overrides:{{}}}};
      if (!swf[actionKey].overrides[styleKey] || typeof swf[actionKey].overrides[styleKey] !== 'object') swf[actionKey].overrides[styleKey] = {{}};
      swf[actionKey].overrides[styleKey].sub = el.value;
    }} else if (field.indexOf('simple_style_subsub__') === 0) {{
      var parts = field.split('__');
      var actionKey = parts[1]; var styleKey = parts.slice(2).join('__');
      if (!swf[actionKey] || typeof swf[actionKey] !== 'object') swf[actionKey] = {{wf:'',overrides:{{}}}};
      if (!swf[actionKey].overrides[styleKey] || typeof swf[actionKey].overrides[styleKey] !== 'object') swf[actionKey].overrides[styleKey] = {{}};
      swf[actionKey].overrides[styleKey].subsub = el.value;
    }} else if (field.indexOf('simple_style_mode_locked__') === 0) {{
      var parts = field.split('__');
      var actionKey = parts[1]; var styleKey = parts.slice(2).join('__');
      if (!swf[actionKey] || typeof swf[actionKey] !== 'object') swf[actionKey] = {{wf:'',overrides:{{}}}};
      if (!swf[actionKey].overrides[styleKey] || typeof swf[actionKey].overrides[styleKey] !== 'object') swf[actionKey].overrides[styleKey] = {{}};
      swf[actionKey].overrides[styleKey].mode_locked = el.checked;
    }} else if (field.indexOf('simple_style_main_locked__') === 0) {{
      var parts = field.split('__');
      var actionKey = parts[1]; var styleKey = parts.slice(2).join('__');
      if (!swf[actionKey] || typeof swf[actionKey] !== 'object') swf[actionKey] = {{wf:'',overrides:{{}}}};
      if (!swf[actionKey].overrides[styleKey] || typeof swf[actionKey].overrides[styleKey] !== 'object') swf[actionKey].overrides[styleKey] = {{}};
      swf[actionKey].overrides[styleKey].main_locked = el.checked;
    }} else if (field.indexOf('simple_style_sub_locked__') === 0) {{
      var parts = field.split('__');
      var actionKey = parts[1]; var styleKey = parts.slice(2).join('__');
      if (!swf[actionKey] || typeof swf[actionKey] !== 'object') swf[actionKey] = {{wf:'',overrides:{{}}}};
      if (!swf[actionKey].overrides[styleKey] || typeof swf[actionKey].overrides[styleKey] !== 'object') swf[actionKey].overrides[styleKey] = {{}};
      swf[actionKey].overrides[styleKey].sub_locked = el.checked;
    }} else if (field.indexOf('simple_style_subsub_locked__') === 0) {{
      var parts = field.split('__');
      var actionKey = parts[1]; var styleKey = parts.slice(2).join('__');
      if (!swf[actionKey] || typeof swf[actionKey] !== 'object') swf[actionKey] = {{wf:'',overrides:{{}}}};
      if (!swf[actionKey].overrides[styleKey] || typeof swf[actionKey].overrides[styleKey] !== 'object') swf[actionKey].overrides[styleKey] = {{}};
      swf[actionKey].overrides[styleKey].subsub_locked = el.checked;
    }} else if (field.indexOf('simple_workflows__') === 0) {{
      var key = field.split('__')[1];
      if (el.value) swf[key] = {{wf: el.value, overrides:{{}}}};
    }} else if (el.type === 'checkbox') {{
      data[field] = el.checked;
    }} else if (el.multiple) {{
      data[field] = Array.from(el.selectedOptions).map(o=>o.value);
    }} else {{
      data[field] = el.value;
    }}
  }});
  data['simple_workflows'] = swf;
  var r = await fetch('/admin/api/users', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(data)}});
  var d = await r.json();
  showMsg(d.status==='ok' ? 'Zapisano: '+uid : 'Błąd: '+d.message, d.status==='ok');
}}

async function deleteUser(uid) {{
  if (!confirm('Usunąć użytkownika '+uid+'?')) return;
  var r = await fetch('/admin/api/users/'+uid, {{method:'DELETE'}});
  var d = await r.json();
  if (d.status==='ok') {{
    document.getElementById('row-'+uid).remove();
    showMsg('Usunięto: '+uid);
  }}
}}

async function addUser() {{
  var uid = document.getElementById('n-uid').value.trim();
  if (!uid) return alert('Podaj ID użytkownika');
  var pass = document.getElementById('n-pass').value;
  if (!pass) return alert('Podaj hasło');
  var data = {{
    id: uid,
    name: document.getElementById('n-name').value || uid,
    password: pass,
    image_dir: document.getElementById('n-dir').value,
    comfy_url: document.getElementById('n-comfy').value,
    telegram_token: document.getElementById('n-tgt').value,
    telegram_chat:  document.getElementById('n-tgc').value,
    role: 'user', allowed_workflows: []
  }};
  var r = await fetch('/admin/api/users', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(data)}});
  var d = await r.json();
  if (d.status==='ok') {{ showMsg('Dodano: '+uid); setTimeout(()=>location.reload(), 800); }}
  else showMsg('Błąd: '+d.message, false);
}}
</script>
</body></html>"""
    return HTMLResponse(content=html)

@app.get("/admin/api/workflow_simple_fields")
async def admin_workflow_simple_fields(wf_id: str, request: Request):
    """Zwraca pola edytowalne w simple mode dla danego workflow"""
    if not is_admin(request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    configs = wm.load_configs()
    cfg = configs.get(wf_id, {})
    fields = []
    for m in cfg.get("mappings", []):
        role = m.get("role")
        if role == "prompt" and m.get("simple_editable"):
            fields.append({
                "type": "textarea",
                "key": f'{m["node_id"]}::{m["field"]}',
                "label": m.get("simple_label", m.get("node_title", m.get("field",""))),
                "default": m.get("_suffix_default", ""),
            })
            if m.get("prefix_field"):
                fields.append({
                    "type": "textarea",
                    "key": f'{m["node_id"]}::{m["prefix_field"]}',
                    "label": m.get("simple_label","") + " (prefix)",
                    "default": m.get("_prefix_default",""),
                })
        elif role == "style":
            fields.append({
                "type": "style",
                "key": f'{m["node_id"]}::style',
                "label": m.get("node_title", "Styl"),
                "wf_id": wf_id,
            })
    return fields

@app.post("/admin/api/users")
async def admin_save_user(request: Request):
    """Zapisuje/aktualizuje użytkownika (tylko admin)."""
    _u = get_user_from_request(request)
    if _u.get("role") != "admin":
        return {"status": "error", "message": "Brak uprawnień"}
    body = await request.json()
    uid  = body.get("id", "").strip()
    if not uid:
        return {"status": "error", "message": "Brak ID"}
    users = load_users()
    existing = users.get(uid, {})
    # Hasło - tylko jeśli podane
    password = body.get("password", "").strip()
    if password:
        existing["password_hash"] = hash_password(password)
    # Reszta pól
    for field in ["name", "role", "image_dir", "comfy_url", "telegram_token", "telegram_chat"]:
        if field in body and body[field] != "":
            existing[field] = body[field]
        elif field in body and body[field] == "" and field not in ["telegram_token","telegram_chat"]:
            pass  # nie czyść
    if "allowed_workflows" in body:
        existing["allowed_workflows"] = body["allowed_workflows"]
    existing["simple_mode"]       = bool(body.get("simple_mode", False))
    existing["simple_workflows"]  = body.get("simple_workflows", {})
    users[uid] = existing
    save_users(users)
    log.info(f"Admin: zapisano użytkownika {uid}")
    return {"status": "ok"}

@app.delete("/admin/api/users/{uid}")
async def admin_delete_user(uid: str, request: Request):
    """Usuwa użytkownika (tylko admin)."""
    _u = get_user_from_request(request)
    if _u.get("role") != "admin":
        return {"status": "error", "message": "Brak uprawnień"}
    users = load_users()
    if uid not in users:
        return {"status": "error", "message": "Nie znaleziono"}
    uid_req = get_uid_from_request(request)
    if uid == uid_req:
        return {"status": "error", "message": "Nie możesz usunąć własnego konta"}
    del users[uid]
    save_users(users)
    log.info(f"Admin: usunięto użytkownika {uid}")
    return {"status": "ok"}


