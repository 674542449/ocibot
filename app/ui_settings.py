"""Persistent UI preferences (font family / size / weight)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.runtime_paths import ensure_writable_directory, resolve_data_dir

SETTINGS_FILE = "ui_settings.json"

DEFAULTS: dict[str, Any] = {
    "font_family": "",  # empty = auto-detect best CJK face
    "font_size": 11,
    "font_bold": False,
    "sidebar_width": 0,  # 0 = use theme SIDEBAR_WIDTH default
}


def settings_path(data_dir: Path | None = None) -> Path:
    base = ensure_writable_directory(data_dir or resolve_data_dir())
    return base / SETTINGS_FILE


def load_ui_settings(data_dir: Path | None = None) -> dict[str, Any]:
    """Load UI settings; missing / corrupt files fall back to defaults."""
    path = settings_path(data_dir)
    data = dict(DEFAULTS)
    if not path.exists():
        return data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return data
    if not isinstance(raw, dict):
        return data
    family = str(raw.get("font_family") or "").strip()
    data["font_family"] = family
    try:
        size = int(raw.get("font_size", DEFAULTS["font_size"]))
    except (TypeError, ValueError):
        size = DEFAULTS["font_size"]
    data["font_size"] = max(9, min(20, size))
    data["font_bold"] = bool(raw.get("font_bold", False))
    try:
        sidebar_w = int(raw.get("sidebar_width", DEFAULTS["sidebar_width"]) or 0)
    except (TypeError, ValueError):
        sidebar_w = 0
    data["sidebar_width"] = max(0, sidebar_w)
    return data


def save_ui_settings(settings: dict[str, Any], data_dir: Path | None = None) -> Path:
    """Persist UI settings atomically."""
    path = settings_path(data_dir)
    try:
        sidebar_w = int(settings.get("sidebar_width", 0) or 0)
    except (TypeError, ValueError):
        sidebar_w = 0
    payload = {
        "font_family": str(settings.get("font_family") or "").strip(),
        "font_size": max(9, min(20, int(settings.get("font_size", 11)))),
        "font_bold": bool(settings.get("font_bold", False)),
        "sidebar_width": max(0, sidebar_w),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def font_prefs_from_settings(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map stored settings to apply_classic_style(font_prefs=...) kwargs."""
    s = settings if settings is not None else load_ui_settings()
    return {
        "family": s.get("font_family") or None,
        "size": int(s.get("font_size") or 11),
        "bold": bool(s.get("font_bold")),
    }
