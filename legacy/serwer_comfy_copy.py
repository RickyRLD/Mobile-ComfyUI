import json
import urllib.request
import urllib.parse
import os
import uuid
import random
import io
import time
import requests
import subprocess
import signal
import logging
import sys
from datetime import datetime
from PIL import Image
from fastapi import FastAPI, File, Form, UploadFile, BackgroundTasks
from fastapi.responses import HTMLResponse

# === KONFIGURACJA LOGÓW ===
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serwer_comfy.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("comfy")
log.info(f"=== SERWER URUCHOMIONY === Log: {LOG_FILE}")

app = FastAPI()

# Globalne zmienne serwera
app.state.should_stop = False
app.state.is_processing = False
app.state.status_text = "Serwer gotowy. Czekam na pliki z telefonu..."
app.state.current_iter = 0
app.state.total_iter = 0
app.state.last_workflow = None 

# KONFIGURACJA 
COMFY_URL = "127.0.0.1:8188"
IMAGE_DIR = r"C:\AI\IMAGES\OlaPL"
WORKFLOW_1 = "workflows/Ricky_v4.json"
WORKFLOW_2 = "workflows/PhotoRicky_v1.0.json"

# KONFIGURACJA TELEGRAMA
TELEGRAM_BOT_TOKEN = ""  # set in config.py
TELEGRAM_CHAT_ID = ""    # set in config.py

# --- INTERFEJS MOBILNY (HTML na telefon) ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>ComfyUI Mobile</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: sans-serif; padding: 20px; background: #121212; color: #fff; }
        .container { max-width: 500px; margin: auto; background: #1e1e1e; padding: 20px; border-radius: 10px; }
        input[type="file"], input[type="text"], textarea, select { width: 100%; padding: 10px; margin: 5px 0 15px 0; box-sizing: border-box; background: #2c2c2c; color: #fff; border: 1px solid #444; border-radius: 5px; font-size: 16px; }
        label { font-size: 14px; font-weight: bold; color: #aaa; }
        .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; }
        button { width: 100%; padding: 15px; background: #4CAF50; color: white; border: none; border-radius: 5px; font-size: 16px; font-weight: bold; cursor: pointer; }
        button:disabled { background: #555; }
        #status-section { display: none; text-align: center; margin-top: 15px; }
        #status-msg { color: #aaa; padding: 10px; background: #222; border-radius: 5px; margin-bottom: 10px;}
        #live-status { color: #4CAF50; font-weight: bold; font-size: 18px; padding: 15px; background: #1a1a1a; border: 1px solid #333; border-radius: 5px; margin-bottom: 15px; }
        #stop-btn { background: #f44336; margin-bottom: 10px; }
        #new-btn { background: #2196F3; display: none; }
        #style-options { display: block; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Ustawienia Workflow</h2>
        <form id="generate-form">
            <label>Wybierz Proces (Workflow):</label>
            <select id="workflow_choice" name="workflow_choice">
                <option value="Ricky_v4">Ricky v4 (Ze stylami)</option>
                <option value="PhotoRicky_v1.0">PhotoRicky v1.0 (Prosty + FaceSwap)</option>
            </select>
            <label>Wybierz zdjęcie (Ref):</label>
            <input type="file" id="image" name="image" accept="image/*" required>
            <label>Ilość generacji (Auto-Queue):</label>
            <select id="iterations" name="iterations">
                <option value="1">1 wariant</option>
                <option value="5">5 wariantów (ciągiem)</option>
                <option value="10">10 wariantów (ciągiem)</option>
                <option value="50">50 wariantów (maksymalnie)</option>
            </select>
            <div id="style-options">
                <label>Prefix:</label>
                <textarea id="prefix" name="prefix" rows="2">Change the graphic style. Keep the face from the photo.</textarea>
                <label>Tryb (Mode):</label>
                <select id="mode" name="mode"><option>auto</option></select>
                <div class="grid-3">
                    <div><label>Main Style:</label><select id="main_style" name="main_style"><option>Claude</option></select></div>
                    <div><label>Sub Style:</label><select id="sub_style" name="sub_style"><option>Art</option></select></div>
                    <div><label>Subsub Style:</label><select id="subsub_style" name="subsub_style"><option>Boho</option></select></div>
                </div>
            </div>
            <label>Główny Opis / Zmiany:</label>
            <textarea id="suffix" name="suffix" rows="5" required>change clothes to sexy version!!\nSlim fit.\nstraight gaze, eyes looking forward, symmetrical eyes\nremove black boxes\nremove watermark</textarea>
            <button type="submit" id="submit-btn">Generuj Obrazy</button>
        </form>
        <div id="status-section">
            <div id="live-status">Łączenie...</div>
            <div id="status-msg">Możesz zostawić tę stronę otwartą lub zamknąć. Obrazy przyjdą na Telegram.</div>
            <button id="stop-btn">Zatrzymaj ComfyUI (Stop)</button>
            <button id="new-btn">Wyślij nowe zadanie</button>
        </div>
    </div>
    <script>
        let statusInterval = null;
        document.getElementById('workflow_choice').addEventListener('change', function() {
            const styleSection = document.getElementById('style-options');
            const suffixBox = document.getElementById('suffix');
            if (this.value === 'PhotoRicky_v1.0') {
                styleSection.style.display = 'none';
                suffixBox.value = "Enhance her breasts, flatter her figure, improve the photo quality, remove objects that spoil the image, and give the photo a professional photoshoot feel.";
            } else {
                styleSection.style.display = 'block';
                suffixBox.value = "change clothes to sexy version!!\\nSlim fit.\\nstraight gaze, eyes looking forward, symmetrical eyes\\nremove black boxes\\nremove watermark";
            }
        });
        document.addEventListener("DOMContentLoaded", async () => {
            try {
                const response = await fetch('/api/options');
                const options = await response.json();
                function populateSelect(id, choices, defaultVal) {
                    const select = document.getElementById(id);
                    select.innerHTML = ''; 
                    if (choices && choices.length > 0) {
                        choices.forEach(c => {
                            const opt = document.createElement('option');
                            opt.value = c; opt.textContent = c;
                            if (c === defaultVal) opt.selected = true; select.appendChild(opt);
                        });
                    } else {
                        const opt = document.createElement('option');
                        opt.textContent = "Brak opcji"; select.appendChild(opt);
                    }
                }
                populateSelect('mode', options.mode, "auto"); populateSelect('main_style', options.main_style, "Claude");
                populateSelect('sub_style', options.sub_style, "Art"); populateSelect('subsub_style', options.subsub_style, "Boho");
            } catch (error) {}
        });
        function startStatusPolling() {
            if (statusInterval) clearInterval(statusInterval);
            statusInterval = setInterval(async () => {
                try {
                    const res = await fetch('/api/status');
                    const data = await res.json();
                    const liveDiv = document.getElementById('live-status');
                    if (data.is_processing) {
                        liveDiv.innerHTML = `⏳ <b>${data.status_text}</b> <br><br> Wariant: ${data.current_iter} z ${data.total_iter}`;
                    } else {
                        liveDiv.innerHTML = `✅ <b>${data.status_text}</b>`;
                        document.getElementById('stop-btn').style.display = 'none';
                        document.getElementById('new-btn').style.display = 'block';
                        clearInterval(statusInterval);
                    }
                } catch (e) {}
            }, 2000);
        }
        document.getElementById('generate-form').onsubmit = async (e) => {
            e.preventDefault();
            const btn = document.getElementById('submit-btn');
            btn.disabled = true; btn.textContent = "Wysyłanie...";
            const formData = new FormData(e.target);
            try {
                const response = await fetch('/generate', { method: 'POST', body: formData });
                if (response.ok) {
                    document.getElementById('generate-form').style.display = 'none';
                    document.getElementById('status-section').style.display = 'block';
                    document.getElementById('stop-btn').style.display = 'block';
                    document.getElementById('new-btn').style.display = 'none';
                    startStatusPolling();
                } else { alert('Błąd serwera.'); btn.disabled = false; btn.textContent = "Generuj Obrazy"; }
            } catch (error) { alert('Błąd połączenia.'); btn.disabled = false; btn.textContent = "Generuj Obrazy"; }
        };
        document.getElementById('stop-btn').onclick = async () => {
            try { await fetch('/stop', { method: 'POST' }); document.getElementById('live-status').innerHTML = "🛑 Przerywanie w toku..."; } catch (e) {}
        };
        document.getElementById('new-btn').onclick = () => {
            document.getElementById('status-section').style.display = 'none';
            document.getElementById('generate-form').style.display = 'block';
            const btn = document.getElementById('submit-btn'); btn.disabled = false; btn.textContent = "Generuj Obrazy";
        };
    </script>
</body>
</html>
"""

# --- PANEL NA LAPTOPA (Zaktualizowany Dashboard z natywnym statusem ComfyUI) ---
DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>ComfyUI Control Panel</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0f0f11; color: #e0e0e0; padding: 30px; margin: 0; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; max-width: 1000px; margin: auto; }
        .card { background: #1e1e24; border: 1px solid #333; border-radius: 12px; padding: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h2 { color: #fff; margin-top: 0; font-size: 1.2rem; border-bottom: 1px solid #333; padding-bottom: 10px; }
        .metric { display: flex; justify-content: space-between; margin: 15px 0; font-size: 1.1rem; }
        .val { font-weight: bold; color: #4CAF50; }
        .val.warn { color: #ff9800; }
        .val.danger { color: #f44336; }
        button { width: 100%; padding: 15px; background: #f44336; color: white; border: none; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; transition: 0.2s; margin-top: 10px;}
        button:hover { background: #d32f2f; }
        .header-title { text-align: center; margin-bottom: 30px; color: #aaa; }
    </style>
</head>
<body>
    <div class="header-title">
        <h1>Silnik ComfyUI - Panel Zarządzania</h1>
        <p>Praca w tle aktywna. Nie zamykaj przeglądarki, jeśli chcesz widzieć postęp.</p>
    </div>
    
    <div class="grid">
        <div class="card">
            <h2>💻 Monitor Sprzętu (GPU)</h2>
            <div class="metric"><span>Temperatura:</span> <span id="gpu-temp" class="val">Ładowanie...</span></div>
            <div class="metric"><span>Użycie Rdzenia:</span> <span id="gpu-util" class="val">Ładowanie...</span></div>
            <div class="metric"><span>Pamięć VRAM:</span> <span id="gpu-vram" class="val">Ładowanie...</span></div>
        </div>
        
        <div class="card">
            <h2>⚙️ Status Silnika</h2>
            <div class="metric"><span>Stan ComfyUI:</span> <span id="comfy-realtime" class="val" style="color: #2196F3;">Ładowanie...</span></div>
            <div class="metric"><span>Ostatni Workflow:</span> <span id="last-wf" class="val" style="color: #aaa;">Brak</span></div>
            <div style="margin-top: 20px; color: #888; font-size: 0.9rem;">Praca serwera mobilnego:</div>
            <div id="current-action" style="color: #4CAF50; font-weight: bold; font-size: 1.1rem; margin-top: 5px;">Czekam na połączenie...</div>
        </div>

        <div class="card" style="border-color: #f44336;">
            <h2>🛑 Kontrola Awaryjna</h2>
            <p style="font-size: 0.9rem; color: #888;">Ponieważ aplikacja działa w tle bez okien konsoli, użyj tego przycisku, aby całkowicie wyłączyć silnik ComfyUI i ten serwer.</p>
            <button onclick="shutdownEngine()">WYŁĄCZ SILNIK (ZAMKNIJ WSZYSTKO)</button>
        </div>
    </div>

    <script>
        setInterval(async () => {
            try {
                const res = await fetch('/api/laptop_status');
                const data = await res.json();
                
                // Aktualizacja Hardware
                document.getElementById('gpu-temp').innerText = data.gpu.temp;
                document.getElementById('gpu-util').innerText = data.gpu.util;
                document.getElementById('gpu-vram').innerText = data.gpu.vram;
                
                const tempVal = parseInt(data.gpu.temp);
                const tempEl = document.getElementById('gpu-temp');
                if (tempVal > 80) tempEl.className = "val danger";
                else if (tempVal > 70) tempEl.className = "val warn";
                else tempEl.className = "val";

                // Aktualizacja Statusu
                document.getElementById('comfy-realtime').innerText = data.realtime_status;
                document.getElementById('last-wf').innerText = data.last_workflow || "Brak";
                document.getElementById('current-action').innerText = data.server_status;

            } catch (e) {
                document.getElementById('current-action').innerText = "Błąd połączenia z serwerem lokalnym!";
                document.getElementById('comfy-realtime').innerText = "Brak połączenia";
            }
        }, 2000);

        async function shutdownEngine() {
            if(confirm("Czy na pewno chcesz ubić wszystkie procesy ComfyUI i serwera działające w tle?")) {
                try {
                    await fetch('/shutdown_engine', {method: 'POST'});
                    document.body.innerHTML = "<h2 style='text-align:center; color:#f44336; margin-top:50px;'>Silnik został pomyślnie zamknięty. Możesz zamknąć tę kartę.</h2>";
                } catch (e) {}
            }
        }
    </script>
</body>
</html>
"""

# Funkcje pomocnicze
def get_gpu_info():
    try:
        output = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'], 
            encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW
        )
        temp, util, mem_used, mem_total = output.strip().split(', ')
        return {"temp": f"{temp}°C", "util": f"{util}%", "vram": f"{mem_used} / {mem_total} MB"}
    except Exception as e:
        log.warning(f"get_gpu_info błąd: {e}")
        return {"temp": "N/A", "util": "N/A", "vram": "N/A"}

def get_comfy_realtime_status():
    """Odpytuje ComfyUI w czasie rzeczywistym, by sprawdzić jego natywny stan"""
    try:
        req = urllib.request.Request(f"http://{COMFY_URL}/queue")
        resp = json.loads(urllib.request.urlopen(req, timeout=1).read())
        running = len(resp.get("queue_running", []))
        pending = len(resp.get("queue_pending", []))

        if running > 0:
            return f"Pracuje (Zadań: {running + pending})"
        elif pending > 0:
            return f"W kolejce (Zadań: {pending})"
        else:
            return "Gotowy (Czeka na zadanie)"
    except Exception as e:
        log.debug(f"get_comfy_realtime_status błąd: {e}")
        return "Uruchamianie (lub wyłączony)..."

# --- PROCESY W TLE DLA TELEFONU ---
def process_in_background(workflow_template: dict, iterations: int, workflow_type: str):
    log.info(f"=== BACKGROUND START: workflow={workflow_type}, iterations={iterations} ===")
    app.state.is_processing = True
    app.state.total_iter = iterations
    
    try:
        for i in range(iterations):
            if app.state.should_stop:
                log.info(f"Iter {i+1}: should_stop=True, przerywam pętlę.")
                app.state.status_text = "Przerwano ręcznie!"
                break
                
            app.state.current_iter = i + 1
            log.info(f"--- Iter {i+1}/{iterations}: Przygotowywanie węzłów ---")
            app.state.status_text = "Przygotowywanie węzłów..."

            if workflow_type == "Ricky_v4":
                seed = random.randint(1, 999999999999999)
                workflow_template["9:209:211"]["inputs"]["seed"] = seed
                log.debug(f"Iter {i+1}: Ricky_v4 seed={seed}")
            elif workflow_type == "PhotoRicky_v1.0":
                seed = random.randint(1, 999999999999999)
                workflow_template["9:225:227"]["inputs"]["noise_seed"] = seed
                log.debug(f"Iter {i+1}: PhotoRicky seed={seed}")

            log.info(f"Iter {i+1}: Wysyłanie prompt do ComfyUI...")
            data = json.dumps({"prompt": workflow_template}).encode('utf-8')
            req = urllib.request.Request(f"http://{COMFY_URL}/prompt", data=data)
            response = json.loads(urllib.request.urlopen(req).read())
            prompt_id = response['prompt_id']
            log.info(f"Iter {i+1}: prompt_id={prompt_id}")

            out_filenames = []
            
            app.state.status_text = "Generowanie w ComfyUI (praca karty GPU)..."
            poll_count = 0
            while True:
                if app.state.should_stop:
                    log.info(f"Iter {i+1}: should_stop w pętli history, przerywam.")
                    app.state.status_text = "Przerwano ręcznie!"
                    break
                
                poll_count += 1
                if poll_count % 10 == 0:
                    log.info(f"Iter {i+1}: Czekam na history... (poll #{poll_count}, ~{poll_count*2}s)")
                    
                req_hist = urllib.request.Request(f"http://{COMFY_URL}/history/{prompt_id}")
                hist_resp = json.loads(urllib.request.urlopen(req_hist).read())
                
                if prompt_id in hist_resp:
                    log.info(f"Iter {i+1}: Historia gotowa po {poll_count} pollach (~{poll_count*2}s). Odbieram wyniki...")
                    app.state.status_text = "Wysyłanie wyników na Telegram..."
                    outputs = hist_resp[prompt_id]['outputs']
                    
                    for node_id in ["60", "74", "77"]:
                        if node_id in outputs and 'images' in outputs[node_id]:
                            for img_data in outputs[node_id]['images']:
                                out_filenames.append(img_data['filename'])
                                log.debug(f"Iter {i+1}: node={node_id}, plik={img_data['filename']}")
                    break
                time.sleep(2)

            log.info(f"Iter {i+1}: out_filenames={out_filenames}")
            for out_filename in out_filenames:
                if app.state.should_stop:
                    log.info(f"Iter {i+1}: should_stop przed wysyłką pliku, przerywam.")
                    break
                    
                log.info(f"Iter {i+1}: Pobieranie pliku z ComfyUI: {out_filename}")
                req_img = urllib.request.Request(f"http://{COMFY_URL}/view?filename={urllib.parse.quote(out_filename)}&type=output")
                out_path = os.path.join(IMAGE_DIR, f"telegram_result_{uuid.uuid4().hex[:8]}.jpg")
                
                with open(out_path, 'wb') as f:
                    f.write(urllib.request.urlopen(req_img).read())
                log.info(f"Iter {i+1}: Plik zapisany: {out_path}")

                if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
                    log.info(f"Iter {i+1}: Wysyłam na Telegram...")
                    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
                    with open(out_path, 'rb') as photo:
                        payload = {'chat_id': TELEGRAM_CHAT_ID}
                        files = {'photo': photo}
                        tg_resp = requests.post(url, data=payload, files=files, timeout=30)
                        log.info(f"Iter {i+1}: Telegram response: {tg_resp.status_code} {tg_resp.text[:200]}")
                        
    except Exception as e:
        log.exception(f"BŁĄD KRYTYCZNY w process_in_background: {e}")
        app.state.status_text = "Wystąpił błąd podczas generowania."
    finally:
        log.info("=== BACKGROUND KONIEC ===")
        app.state.is_processing = False
        if not app.state.should_stop:
            app.state.status_text = "Serwer gotowy. Czekam na pliki z telefonu..."

# --- ENDPOINTY Z GŁÓWNYMI WIDOKAMI ---
@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(content=HTML_TEMPLATE)

@app.get("/panel", response_class=HTMLResponse)
async def panel():
    return HTMLResponse(content=DASHBOARD_HTML)

# --- ENDPOINTY API ---
@app.get("/api/laptop_status")
async def get_laptop_status():
    return {
        "gpu": get_gpu_info(),
        "realtime_status": get_comfy_realtime_status(),
        "server_status": app.state.status_text,
        "last_workflow": app.state.last_workflow
    }

@app.post("/shutdown_engine")
async def shutdown_engine():
    try:
        result = subprocess.check_output("netstat -ano | findstr :8188", shell=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        for line in result.splitlines():
            if "LISTENING" in line:
                pid = line.strip().split()[-1]
                subprocess.run(f"taskkill /F /PID {pid}", shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
                break
    except Exception:
        pass
    os.kill(os.getpid(), signal.SIGINT)
    return {"status": "closing"}

@app.get("/api/options")
async def get_options():
    log.debug("/api/options: odpytuję ComfyUI o opcje stylów...")
    try:
        req = urllib.request.Request(f"http://{COMFY_URL}/object_info/RandomOrManual3LevelChoicesRelaxed")
        response = urllib.request.urlopen(req).read()
        node_info = json.loads(response).get("RandomOrManual3LevelChoicesRelaxed", {})
        inputs = node_info.get("input", {}).get("required", {})
        
        def extract_choices(field_name):
            if field_name in inputs and isinstance(inputs[field_name][0], list):
                return inputs[field_name][0]
            return []

        result = {
            "mode": extract_choices("mode"),
            "main_style": extract_choices("main_style"),
            "sub_style": extract_choices("sub_style"),
            "subsub_style": extract_choices("subsub_style")
        }
        log.debug(f"/api/options OK: {len(result['main_style'])} main_style opcji")
        return result
    except Exception as e:
        log.warning(f"/api/options błąd: {e}")
        return {"mode": [], "main_style": [], "sub_style": [], "subsub_style": []}

@app.get("/api/status")
async def get_status():
    return {
        "status_text": app.state.status_text,
        "current_iter": app.state.current_iter,
        "total_iter": app.state.total_iter,
        "is_processing": app.state.is_processing
    }

@app.post("/generate")
async def generate(
    background_tasks: BackgroundTasks,
    workflow_choice: str = Form("Ricky_v4"),
    image: UploadFile = File(...),
    iterations: int = Form(1),
    prefix: str = Form(""),
    mode: str = Form("auto"),
    main_style: str = Form("Claude"),
    sub_style: str = Form("Art"),
    subsub_style: str = Form("Boho"),
    suffix: str = Form(...)
):
    log.info(f"=== /generate: workflow={workflow_choice}, iterations={iterations}, plik={image.filename} ===")
    app.state.should_stop = False
    app.state.status_text = "Przyjmowanie pliku od telefonu..."
    app.state.is_processing = True

    if app.state.last_workflow is None:
        app.state.last_workflow = workflow_choice
        
    if app.state.last_workflow != workflow_choice:
        log.info(f"Zmiana workflow z {app.state.last_workflow} na {workflow_choice} — czyszczę VRAM...")
        app.state.status_text = "Zmiana procesu: Odśmiecanie pamięci VRAM..."
        try:
            free_data = json.dumps({"unload_models": True, "free_memory": True}).encode('utf-8')
            req_free = urllib.request.Request(f"http://{COMFY_URL}/free", data=free_data, headers={'Content-Type': 'application/json'}, method="POST")
            urllib.request.urlopen(req_free)
            time.sleep(3) 
        except Exception as e:
            log.warning(f"Błąd podczas czyszczenia VRAM: {e}")
            
        app.state.last_workflow = workflow_choice
    
    os.makedirs(IMAGE_DIR, exist_ok=True)
    filename = f"mobile_{uuid.uuid4().hex[:8]}.jpg"
    file_path = os.path.join(IMAGE_DIR, filename)
    
    image_data = await image.read()
    log.info(f"Odebrano plik: {len(image_data)} bajtów -> zapisuję jako {filename}")
    img = Image.open(io.BytesIO(image_data))
    
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
        
    max_size = 1500
    if max(img.size) > max_size:
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        
    img.save(file_path, format="JPEG", quality=90)
    log.info(f"Zapisano obraz: {file_path}, rozmiar: {img.size}")

    target_file = WORKFLOW_1 if workflow_choice == "Ricky_v4" else WORKFLOW_2
    log.info(f"Wczytywanie workflow: {target_file}")
    with open(target_file, "r", encoding="utf-8") as f:
        workflow = json.load(f)

    workflow["13:3"]["inputs"]["selected_image"] = filename
    
    if workflow_choice == "Ricky_v4":
        prompter_node = workflow["9:209:211"]["inputs"]
        prompter_node["prefix"] = prefix
        prompter_node["mode"] = mode
        prompter_node["main_style"] = main_style
        prompter_node["sub_style"] = sub_style
        prompter_node["subsub_style"] = subsub_style
        prompter_node["suffix"] = suffix
        log.info(f"Ustawiono prompter Ricky_v4: mode={mode}, main={main_style}, sub={sub_style}, subsub={subsub_style}")
    elif workflow_choice == "PhotoRicky_v1.0":
        workflow["9:421"]["inputs"]["string_a"] = suffix
        log.info(f"Ustawiono prompter PhotoRicky.")

    log.info("Uruchamiam process_in_background...")
    background_tasks.add_task(process_in_background, workflow, iterations, workflow_choice)
    return {"status": "ok"}

@app.post("/stop")
async def stop_generation():
    log.info("=== /stop: Zatrzymywanie generowania ===")
    app.state.should_stop = True
    app.state.status_text = "Przerywanie pracy karty graficznej..."
    try:
        req = urllib.request.Request(f"http://{COMFY_URL}/interrupt", method="POST")
        urllib.request.urlopen(req)
        log.info("/stop: Wysłano /interrupt do ComfyUI")
    except Exception as e:
        log.warning(f"/stop: Błąd przy /interrupt: {e}")
    return {"status": "zatrzymano"}