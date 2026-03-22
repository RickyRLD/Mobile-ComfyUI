"""
User management: loading, saving, and querying user data.
Also includes helper functions for user attributes.
"""
import os
import json
import time
from fastapi import Request
import app_config
from app_config import IMAGE_DIR, COMFY_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, log

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
    # Importujemy auth tutaj żeby uniknąć circular import
    from auth import _active_sessions
    token = request.cookies.get("session_token", "")
    session = _active_sessions.get(token, {})
    if not session or time.time() > session.get("expires", 0):
        return {}
    uid = session.get("user_id", "")
    return get_user_by_id(uid) if uid else {}

def get_uid_from_request(request: Request) -> str:
    from auth import _active_sessions
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

def user_ntfy_topic(user: dict, uid: str = "") -> str:
    """Zwraca temat ntfy użytkownika (domyślnie user_id)."""
    topic = user.get("ntfy_topic", "")
    if not topic and uid:
        return uid
    return topic or user.get("id", "")

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
    from auth import load_settings
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

