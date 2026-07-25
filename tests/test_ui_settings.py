from pathlib import Path

from app import classic as C
from app.ui_settings import (
    DEFAULTS,
    font_prefs_from_settings,
    load_ui_settings,
    save_ui_settings,
    settings_path,
)


def test_defaults_when_missing(tmp_path: Path):
    s = load_ui_settings(tmp_path)
    assert s == DEFAULTS
    assert not settings_path(tmp_path).exists()


def test_roundtrip(tmp_path: Path):
    save_ui_settings(
        {
            "font_family": "Microsoft YaHei UI",
            "font_size": 14,
            "font_bold": True,
            "sidebar_width": 320,
        },
        tmp_path,
    )
    s = load_ui_settings(tmp_path)
    assert s["font_family"] == "Microsoft YaHei UI"
    assert s["font_size"] == 14
    assert s["font_bold"] is True
    assert s["sidebar_width"] == 320
    prefs = font_prefs_from_settings(s)
    assert prefs == {"family": "Microsoft YaHei UI", "size": 14, "bold": True}


def test_size_clamped(tmp_path: Path):
    save_ui_settings({"font_family": "", "font_size": 99, "font_bold": False}, tmp_path)
    assert load_ui_settings(tmp_path)["font_size"] == 20
    save_ui_settings({"font_family": "", "font_size": 3, "font_bold": False}, tmp_path)
    assert load_ui_settings(tmp_path)["font_size"] == 9


def test_corrupt_file_falls_back(tmp_path: Path):
    path = settings_path(tmp_path)
    path.write_text("{not-json", encoding="utf-8")
    assert load_ui_settings(tmp_path) == DEFAULTS


def test_apply_ui_fonts_sizes():
    C.apply_ui_fonts(None, family="Microsoft YaHei UI", size=11, bold=False)
    assert C.FONT[1] == 11
    assert C.FONT_SMALL[1] == 10
    assert C.FONT_HEADER[1] == 15
    C.apply_ui_fonts(None, family="Microsoft YaHei UI", size=13, bold=True)
    assert C.FONT == ("Microsoft YaHei UI", 13, "bold")
    prefs = C.get_font_prefs()
    assert prefs["size"] == 13 and prefs["bold"] is True
