from __future__ import annotations

from pathlib import Path

from core.json_store import read_json, write_json


def load_settings(path: str | Path) -> dict:
    data = read_json(path, default={})
    return data if isinstance(data, dict) else {}


def save_settings(path: str | Path, data: dict):
    write_json(path, data, ensure_ascii=False, indent=2)

