from __future__ import annotations

from pathlib import Path

from core.json_store import read_json, write_json


def load_subscriptions(path: str | Path) -> list:
    data = read_json(path, default=[])
    return data if isinstance(data, list) else []


def save_subscriptions(path: str | Path, subs: list):
    write_json(path, subs, ensure_ascii=False, indent=2)

