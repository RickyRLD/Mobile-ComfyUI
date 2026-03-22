from __future__ import annotations

from pathlib import Path

from core.json_store import read_json, write_json


def load_users(path: str | Path) -> dict:
    data = read_json(path, default={})
    return data if isinstance(data, dict) else {}


def save_users(path: str | Path, users: dict):
    write_json(path, users, ensure_ascii=False, indent=2)

