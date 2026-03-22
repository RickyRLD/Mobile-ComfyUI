import pystray
from PIL import Image, ImageDraw
import threading
import time
import subprocess
import json
import urllib.request
import os
import webbrowser
import re
import io

# KONFIGURACJA ŚCIEŻEK - importuj z config.py lub użyj domyślnych względnych
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = SCRIPT_DIR

# Domyślne wartości TELEGRAM (przed importem config)
_TELEGRAM_TOKEN_DEFAULT = ""  # set in config.py
_TELEGRAM_CHAT_DEFAULT = ""   # set in config.py

# Ścieżki bezwzględne - projekt w C:\AI\Cursor\Zdalne, ComfyUI w C:\AI\New_Comfy
COMFY_DIR = r"C:\AI\New_Comfy"
PYTHON_EXE = os.path.join(COMFY_DIR, "python_embeded", "python.exe")
CLOUDFLARED_EXE = r"C:\AI\cloudflared.exe"
TELEGRAM_BOT_TOKEN = _TELEGRAM_TOKEN_DEFAULT
TELEGRAM_CHAT_ID = _TELEGRAM_CHAT_DEFAULT

# NGROK - stały darmowy adres HTTPS (alternatywa dla Cloudflare bez domeny)
# Pobierz token z: https://dashboard.ngrok.com/get-started/your-authtoken
# ngrok.exe pobierz z: https://ngrok.com/download (domyślnie w C:\AI\)
NGROK_EXE    = os.environ.get("NGROK_EXE", r"C:\AI\ngrok.exe")
NGROK_TOKEN  = os.environ.get("NGROK_TOKEN", "3A4jRIOxOZKdugi4y6oP789EWRb_2CfB9tPMsZeueCAVdBaue")
NGROK_DOMAIN = os.environ.get("NGROK_DOMAIN", "feodal-unenigmatic-ewa.ngrok-free.dev")

# Generowanie prostej, zielonej ikonki
def create_icon_image():
    image = Image.new('RGB', (64, 64), color=(33, 33, 33))
    dc = ImageDraw.Draw(image)
    dc.rectangle((16, 16, 48, 48), fill=(76, 175, 80))
    return image

comfy_process = None
server_process = None
cloudflare_process = None
ngrok_process = None
tray_icon = None
is_running = True
cloudflare_url = "Pobieranie adresu..."

def get_gpu_info():
    try:
        output = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=temperature.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'], 
            encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW
        )
        temp, mem_used, mem_total = output.strip().split(', ')
        return f"{temp}°C", f"{mem_used}/{mem_total}MB"
    except Exception:
        return "N/A", "N/A"

def get_comfy_status():
    try:
        req = urllib.request.Request("http://127.0.0.1:8188/queue")
        resp = json.loads(urllib.request.urlopen(req, timeout=1).read())
        running = len(resp.get("queue_running", []))
        pending = len(resp.get("queue_pending", []))
        if running > 0:
            return f"Pracuje (Zadań: {running + pending})"
        elif pending > 0:
            return f"W kolejce (Zadań: {pending})"
        else:
            return "Gotowy (Czeka na zadanie)"
    except Exception:
        return "Uruchamianie (lub wyłączony)..."

def send_telegram_url(url):
    """Wysyła link i QR code na Telegram"""
    try:
        import requests
        text_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(text_url, data={
            'chat_id': TELEGRAM_CHAT_ID,
            'text': f"🟢 Serwer uruchomiony!\n\n🔗 Adres zdalny:\n{url}\n\n📱 Zeskanuj QR code w panelu lub otwórz link.",
        }, timeout=10)

        try:
            import qrcode
            qr = qrcode.make(url)
            buf = io.BytesIO()
            qr.save(buf, format='PNG')
            buf.seek(0)
            photo_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            requests.post(photo_url, data={'chat_id': TELEGRAM_CHAT_ID}, files={'photo': ('qr.png', buf, 'image/png')}, timeout=10)
        except ImportError:
            pass
    except Exception as e:
        print(f"Błąd Telegram: {e}")


def notify_server_url(url):
    """Wysyła URL do serwera FastAPI żeby panel go pokazał"""
    try:
        import requests
        requests.post("http://127.0.0.1:8001/api/set_remote_url", json={"url": url}, timeout=5)
    except Exception as e:
        print(f"Błąd notify_server_url: {e}")


def read_ngrok_url(process):
    """Czyta URL z ngrok API lokalnie - ngrok udostępnia API na porcie 4040"""
    global cloudflare_url
    import time as _t, urllib.request as _ur, json as _js
    # Poczekaj aż ngrok wystartuje (do 60s)
    for _ in range(60):
        _t.sleep(1)
        try:
            r  = _ur.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=3)
            d  = _js.loads(r.read())
            tunnels = d.get("tunnels", [])
            # Szukaj HTTPS, potem HTTP
            url = None
            for t in tunnels:
                pu = t.get("public_url", "")
                if pu.startswith("https://"):
                    url = pu
                    break
            if not url:
                for t in tunnels:
                    pu = t.get("public_url", "")
                    if pu.startswith("http://"):
                        url = pu.replace("http://", "https://")
                        break
            if url:
                cloudflare_url = url
                if tray_icon:
                    tray_icon.title = f"Adres: {url}"
                threading.Thread(target=notify_server_url, args=(url,), daemon=True).start()
                threading.Thread(target=send_telegram_url, args=(url,), daemon=True).start()
                return
        except:
            pass
    # Diagnostyka - sprawdź co zwraca ngrok API
    try:
        r2 = _ur.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=3)
        raw = r2.read().decode()
        cloudflare_url = "ngrok brak HTTPS - sprawdz log"
        threading.Thread(target=send_telegram_url, args=("ngrok API: " + raw[:200],), daemon=True).start()
    except Exception as e:
        cloudflare_url = "ngrok offline: " + str(e)[:50]

def read_cloudflare_url(process):
    """Czyta output cloudflared i wyciąga publiczny adres URL"""
    global cloudflare_url
    try:
        for line in iter(process.stderr.readline, b''):
            decoded = line.decode('utf-8', errors='ignore')
            match = re.search(r'https://[a-z0-9\-]+\.trycloudflare\.com', decoded)
            if match:
                cloudflare_url = match.group(0)
                if tray_icon:
                    tray_icon.title = f"Zdalny adres: {cloudflare_url}"
                # Wyślij URL do panelu i na Telegram
                threading.Thread(target=notify_server_url, args=(cloudflare_url,), daemon=True).start()
                threading.Thread(target=send_telegram_url, args=(cloudflare_url,), daemon=True).start()
                break
    except Exception:
        cloudflare_url = "Błąd odczytu URL"

def background_monitor():
    while is_running:
        temp, vram = get_gpu_info()
        status = get_comfy_status()
        url_short = cloudflare_url.replace("https://", "").replace(".trycloudflare.com", "")
        tooltip_text = f"Status: {status}\nGPU: {temp} | VRAM: {vram}\nURL: {url_short}"
        if tray_icon:
            tray_icon.title = tooltip_text
        time.sleep(2)

def start_engines():
    global comfy_process, server_process, cloudflare_process

    # Serwer FastAPI
    server_process = subprocess.Popen(
        [PYTHON_EXE, "-m", "uvicorn", "serwer_comfy:app", "--host", "0.0.0.0", "--port", "8001"],
        cwd=SERVER_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW
    )

    # ComfyUI - poprawiony bat z --disable-auto-launch
    comfy_process = subprocess.Popen(
        "run_nvidia_gpu — NEW.bat",
        cwd=COMFY_DIR,
        shell=True,
        creationflags=subprocess.CREATE_NO_WINDOW
    )

    # Tunel - ngrok ma pierwszenstwo jesli ma token, cloudflare jako fallback
    if os.path.exists(NGROK_EXE) and NGROK_TOKEN:
        # Uruchom ngrok z opoznieniem - serwer FastAPI musi wystartowac pierwszy
        def start_ngrok_delayed():
            global ngrok_process
            # Skonfiguruj authtoken
            subprocess.run(
                [NGROK_EXE, "config", "add-authtoken", NGROK_TOKEN],
                capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
            )
            # Czekaj az serwer bedzie gotowy (max 30s)
            import urllib.request as _ur
            for _ in range(15):
                time.sleep(2)
                try:
                    _ur.urlopen("http://127.0.0.1:8001/api/status", timeout=2)
                    break  # serwer odpowiada
                except:
                    pass
            # Stała domena jest znana od razu — ustaw URL bez czekania na ngrok API
            if NGROK_DOMAIN:
                static_url = f"https://{NGROK_DOMAIN}"
                threading.Thread(target=notify_server_url, args=(static_url,), daemon=True).start()
                threading.Thread(target=send_telegram_url, args=(static_url,), daemon=True).start()
                global cloudflare_url
                cloudflare_url = static_url
                if tray_icon:
                    tray_icon.title = f"Adres: {static_url}"
            ngrok_process = subprocess.Popen(
                [NGROK_EXE, "http", "--url", NGROK_DOMAIN, "8001"],
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            # Potwierdź przez ngrok API (w tle, URL już ustawiony)
            threading.Thread(target=read_ngrok_url, args=(ngrok_process,), daemon=True).start()
        threading.Thread(target=start_ngrok_delayed, daemon=True).start()
    elif os.path.exists(CLOUDFLARED_EXE):
        # cloudflare jako fallback (losowy adres)
        cloudflare_process = subprocess.Popen(
            [CLOUDFLARED_EXE, "tunnel", "--url", "http://localhost:8001"],
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        url_thread = threading.Thread(target=read_cloudflare_url, args=(cloudflare_process,), daemon=True)
        url_thread.start()
    else:
        global cloudflare_url
        cloudflare_url = "Brak cloudflared.exe i ngrok.exe!"



def kill_port_processes():
    for port in ["8188", "8001"]:
        try:
            result = subprocess.check_output(
                f"netstat -ano | findstr :{port}", shell=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            for line in result.splitlines():
                if "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(f"taskkill /F /PID {pid}", shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass

BASE_URL = "http://127.0.0.1:8001"

def _open_local(path):
    webbrowser.open(f"{BASE_URL}{path}")

def _open_remote(path):
    if cloudflare_url.startswith("https://"):
        webbrowser.open(cloudflare_url.rstrip("/") + path)

# ─── Akcje otwierania stron ─────────────────────────────────────────────
def action_open_panel(icon, item):         _open_local("/panel")
def action_open_mobile(icon, item):        _open_local("/")
def action_open_admin(icon, item):         _open_local("/admin")
def action_open_kreator(icon, item):       _open_local("/kreator")
def action_open_mobile_remote(icon, item): _open_remote("/")

def action_copy_url(icon, item):
    """Kopiuje zdalny URL do schowka"""
    if cloudflare_url.startswith("https://"):
        subprocess.run(f'echo {cloudflare_url}| clip', shell=True)

# backwards compat alias (używane gdzieś indziej)
def action_open_dashboard(icon, item):     action_open_panel(icon, item)
def action_open_mobile_local(icon, item):  action_open_mobile(icon, item)

def kill_process_tree(proc):
    """Zabija proces i wszystkie jego dzieci (potrzebne dla bat/shell=True)"""
    if proc is None:
        return
    try:
        subprocess.run(
            f"taskkill /T /F /PID {proc.pid}",
            shell=True, creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5
        )
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

def action_quit(icon, item):
    global is_running
    is_running = False
    icon.stop()
    # Zabij ComfyUI (cały drzewo procesów - bat uruchamia python)
    kill_process_tree(comfy_process)
    # Zabij serwer i cloudflare
    kill_process_tree(server_process)
    kill_process_tree(cloudflare_process)
    kill_process_tree(ngrok_process)
    # Na wszelki wypadek wyczyść porty
    kill_port_processes()

menu = pystray.Menu(
    # Główne pozycje — otwierają domyślny widok (Panel)
    pystray.MenuItem("⚙️  Panel zarządzania",  action_open_panel,   default=True),
    pystray.Menu.SEPARATOR,

    # Submenu: Otwórz widok
    pystray.MenuItem("🌐  Otwórz widok...", pystray.Menu(
        pystray.MenuItem("📱 Interfejs mobilny (lokalnie)", action_open_mobile),
        pystray.MenuItem("📱 Interfejs mobilny (zdalnie)",  action_open_mobile_remote),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("⚙️  Panel zarządzania",           action_open_panel),
        pystray.MenuItem("🎨  Kreator workflow",            action_open_kreator),
        pystray.MenuItem("👤  Panel administratora",        action_open_admin),
    )),

    pystray.MenuItem("📋  Kopiuj zdalny URL",  action_copy_url),
    pystray.Menu.SEPARATOR,
    pystray.MenuItem("🔴  Zamknij silnik",     action_quit),
)

if __name__ == "__main__":
    # Wyczysc stare procesy na portach przed startem
    kill_port_processes()
    start_engines()

    tray_icon = pystray.Icon("ComfyManager", create_icon_image(), "Sprawdzam status...", menu)

    monitor_thread = threading.Thread(target=background_monitor, daemon=True)
    monitor_thread.start()

    tray_icon.run()
