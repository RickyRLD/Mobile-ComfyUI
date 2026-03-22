from __future__ import annotations

from pathlib import Path

from core.json_store import read_json, write_json


def load_history(path: str | Path) -> list:
    data = read_json(path, default=[])
    return data if isinstance(data, list) else []


def append_history(path: str | Path, entry: dict, *, max_items: int = 200):
    p = Path(path)
    history = load_history(p)
    history.append(entry)
    if len(history) > max_items:
        history = history[-max_items:]
    write_json(p, history, ensure_ascii=False, indent=0)

