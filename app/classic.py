"""Fluent-inspired light theme helpers on plain tkinter + ttk.

Windows 11 / Fluent-ish: soft gray canvas, white cards with hairline borders,
Microsoft blue accent, calm selection wash, and Microsoft YaHei UI for Chinese.
ttk uses the fully-themeable ``clam`` base. Fonts are resolved against what's
installed so the app degrades gracefully off Windows.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# Fluent light palette (Windows 11 admin / cloud-console feel)
FACE = "#f3f3f3"          # app canvas
FACE_HOVER = "#ebebeb"
WINDOW = "#ffffff"        # cards / entries / lists
BORDER = "#e0e0e0"
BORDER_MUTED = "#ededed"
BTN_FACE = "#fbfbfb"      # secondary: near-white with border
BTN_HOVER = "#f0f0f0"
BTN_ACTIVE = "#e6e6e6"
BTN_BORDER = "#d1d1d1"
ACCENT = "#0f6cbd"        # Fluent / Windows blue
ACCENT_HOVER = "#115ea3"
ACCENT_ACTIVE = "#0c3b5e"
ACCENT_SOFT = "#e8f3fc"   # soft wash for selection / chips
TEXT = "#1a1a1a"
TEXT_DIM = "#424242"
TEXT_MUTE = "#707070"
LIGHT = "#ffffff"
SHADOW = "#d6d6d6"
DARK = "#4a4a4a"
SELECT_BG = "#cfe4fa"     # list selection: soft blue, dark text (Fluent)
SELECT_FG = "#1a1a1a"
DISABLED = "#a0a0a0"
GREEN = "#107c10"
RED = "#c50f1f"
ORANGE = "#c43e1c"

# Font families / sizes are placeholders; re-resolved by apply_ui_fonts().
# Default is intentionally larger and bolder for Chinese readability.
_UI = "Microsoft YaHei UI"
_MONO = "Consolas"
_BASE_SIZE = 11
_UI_WEIGHT = "normal"  # "normal" or "bold" for body text

FONT = (_UI, 11)
FONT_SMALL = (_UI, 10)
FONT_BOLD = (_UI, 11, "bold")
FONT_TITLE = (_UI, 14, "bold")
FONT_HEADER = (_UI, 15, "bold")
FONT_MICRO = (_UI, 9)
FIXED = (_MONO, 11)

# Preferred UI fonts: CJK-capable first so Chinese stays sharp.
_UI_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "Segoe UI",
    "Segoe UI Variable",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "sans-serif",
)
_MONO_CANDIDATES = (
    "Cascadia Mono",
    "Consolas",
    "Cascadia Code",
    "DejaVu Sans Mono",
    "Menlo",
    "Courier New",
)


def list_ui_font_families(root: tk.Misc | None = None) -> list[str]:
    """Return preferred UI fonts that are actually installed, then other families."""
    try:
        available = set(tkfont.families(root)) if root is not None else set(tkfont.families())
    except tk.TclError:
        available = set()
    preferred = [name for name in _UI_CANDIDATES if name in available and name != "sans-serif"]
    # Keep a short, useful list — not every random installed face.
    extras = sorted(
        n for n in available
        if n not in preferred
        and not n.startswith("@")
        and any(ch.isalpha() for ch in n)
        and len(n) < 40
    )
    # Prefer common readable faces near the top of extras.
    boost = [
        n for n in (
            "Microsoft YaHei", "微软雅黑", "SimHei", "SimSun", "NSimSun",
            "Arial", "Tahoma", "Calibri", "Verdana",
        )
        if n in available and n not in preferred
    ]
    rest = [n for n in extras if n not in boost]
    return preferred + boost + rest


def get_font_prefs() -> dict:
    """Current effective font preferences (family / size / bold body)."""
    return {
        "family": _UI,
        "size": int(_BASE_SIZE),
        "bold": _UI_WEIGHT == "bold",
        "mono": _MONO,
    }


def apply_ui_fonts(
    root: tk.Misc | None = None,
    *,
    family: str | None = None,
    size: int | None = None,
    bold: bool | None = None,
) -> None:
    """Resolve and apply UI font family/size/weight to module-level font tuples.

    Call before building widgets, or after changing settings (then rebuild UI).
    ``family=None`` keeps auto-detect; empty string also means auto.
    """
    global _UI, _MONO, _BASE_SIZE, _UI_WEIGHT
    global FONT, FONT_SMALL, FONT_BOLD, FONT_TITLE, FONT_HEADER, FONT_MICRO, FIXED

    try:
        available = set(tkfont.families(root)) if root is not None else set()
    except tk.TclError:
        available = set()

    chosen = (family or "").strip()
    if chosen and (not available or chosen in available):
        _UI = chosen
    else:
        for cand in _UI_CANDIDATES:
            if not available or cand in available:
                _UI = cand
                break

    for cand in _MONO_CANDIDATES:
        if not available or cand in available:
            _MONO = cand
            break

    if size is not None:
        try:
            _BASE_SIZE = max(9, min(20, int(size)))
        except (TypeError, ValueError):
            _BASE_SIZE = 11
    if bold is not None:
        _UI_WEIGHT = "bold" if bold else "normal"

    base = int(_BASE_SIZE)
    small = max(8, base - 1)
    micro = max(8, base - 2)
    title = base + 3
    header = base + 4
    body = (_UI, base, "bold") if _UI_WEIGHT == "bold" else (_UI, base)

    FONT = body
    FONT_SMALL = (_UI, small, "bold") if _UI_WEIGHT == "bold" else (_UI, small)
    FONT_BOLD = (_UI, base, "bold")
    FONT_TITLE = (_UI, title, "bold")
    FONT_HEADER = (_UI, header, "bold")
    FONT_MICRO = (_UI, micro)
    FIXED = (_MONO, base)


def _resolve_fonts(root: tk.Misc) -> None:
    """Back-compat: auto-detect fonts at default size."""
    apply_ui_fonts(root)


def apply_classic_style(root: tk.Misc, font_prefs: dict | None = None) -> ttk.Style:
    """Configure the Fluent-light look on the root and ttk widgets."""
    prefs = font_prefs or {}
    apply_ui_fonts(
        root,
        family=prefs.get("family"),
        size=prefs.get("size"),
        bold=prefs.get("bold"),
    )
    root.option_add("*Font", FONT)
    try:
        root.configure(bg=FACE)
    except tk.TclError:
        pass
    root.option_add("*TCombobox*Listbox.background", WINDOW)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", SELECT_BG)
    root.option_add("*TCombobox*Listbox.selectForeground", SELECT_FG)
    root.option_add("*TCombobox*Listbox.font", FONT)

    style = ttk.Style()
    for theme in ("clam", "vista", "default"):
        if theme in style.theme_names():
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
    style.configure(".", background=FACE, foreground=TEXT, font=FONT)

    # Row height scales with font size so glyphs aren't clipped.
    row_h = max(28, int(_BASE_SIZE) + 18)
    style.configure(
        "Treeview",
        background=WINDOW,
        fieldbackground=WINDOW,
        foreground=TEXT,
        rowheight=row_h,
        borderwidth=0,
        relief="flat",
        font=FONT,
    )
    style.configure(
        "Treeview.Heading",
        background="#fafafa",
        foreground=TEXT_MUTE,
        relief="flat",
        font=FONT_SMALL if isinstance(FONT_SMALL, tuple) else FONT,
        borderwidth=0,
        padding=(8, 6),
    )
    style.map(
        "Treeview",
        background=[("selected", SELECT_BG)],
        foreground=[("selected", SELECT_FG)],
    )
    style.map("Treeview.Heading", background=[("active", FACE_HOVER)])

    style.configure(
        "TCombobox",
        fieldbackground=WINDOW,
        background=WINDOW,
        foreground=TEXT,
        arrowcolor=TEXT_MUTE,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        relief="flat",
        padding=(6, 4),
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", WINDOW), ("disabled", FACE)],
        selectbackground=[("readonly", WINDOW)],
        selectforeground=[("readonly", TEXT)],
        bordercolor=[("focus", ACCENT), ("active", ACCENT)],
        arrowcolor=[("active", ACCENT)],
    )

    for orient in ("Vertical", "Horizontal"):
        style.configure(
            f"{orient}.TScrollbar",
            background="#c8c8c8",
            troughcolor=FACE,
            bordercolor=FACE,
            arrowcolor=TEXT_MUTE,
            relief="flat",
            arrowsize=12,
        )
        style.map(f"{orient}.TScrollbar", background=[("active", "#a8a8a8")])

    style.configure("TNotebook", background=FACE, borderwidth=0)
    style.configure(
        "TNotebook.Tab",
        background=FACE,
        foreground=TEXT_MUTE,
        padding=(14, 6),
        borderwidth=0,
        font=FONT,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", WINDOW)],
        foreground=[("selected", ACCENT)],
        expand=[("selected", (0, 0, 0, 0))],
    )
    return style


def Frm(master: tk.Misc, **kw) -> tk.Frame:
    kw.setdefault("bg", FACE)
    return tk.Frame(master, **kw)


def Group(master: tk.Misc, text: str = "", **kw) -> tk.Frame:
    """White card with a soft hairline border and optional muted title."""
    kw.setdefault("bg", WINDOW)
    outer = tk.Frame(
        master,
        highlightbackground=BORDER,
        highlightcolor=BORDER,
        highlightthickness=1,
        bd=0,
        **kw,
    )
    if text:
        tk.Label(
            outer,
            text=text,
            bg=kw.get("bg", WINDOW),
            fg=TEXT_MUTE,
            font=FONT_SMALL,
            anchor="w",
        ).pack(fill="x", padx=12, pady=(10, 2))
    return outer


def Lbl(master: tk.Misc, text: str = "", **kw) -> tk.Label:
    kw.setdefault("bg", FACE)
    kw.setdefault("fg", TEXT)
    kw.setdefault("anchor", "w")
    return tk.Label(master, text=text, **kw)


def Btn(master: tk.Misc, text: str, command=None, width=None, **kw) -> tk.Button:
    """Secondary Fluent button: soft fill + hairline border."""
    fg = kw.pop("fg", TEXT)
    b = tk.Button(
        master,
        text=text,
        command=command,
        fg=fg,
        bg=BTN_FACE,
        activebackground=BTN_ACTIVE,
        activeforeground=fg,
        disabledforeground=DISABLED,
        relief="flat",
        bd=0,
        highlightthickness=1,
        highlightbackground=BTN_BORDER,
        highlightcolor=BTN_BORDER,
        padx=12,
        pady=4,
        font=FONT,
        cursor="hand2",
        **kw,
    )
    if width is not None:
        b.configure(width=width)

    def on_enter(_e):
        if str(b["state"]) != "disabled":
            b.configure(bg=BTN_HOVER, highlightbackground=ACCENT, highlightcolor=ACCENT)

    def on_leave(_e):
        b.configure(bg=BTN_FACE, highlightbackground=BTN_BORDER, highlightcolor=BTN_BORDER)

    b.bind("<Enter>", on_enter)
    b.bind("<Leave>", on_leave)
    return b


def BtnPrimary(master: tk.Misc, text: str, command=None, width=None, **kw) -> tk.Button:
    """Fluent blue filled call-to-action button."""
    b = tk.Button(
        master,
        text=text,
        command=command,
        fg="#ffffff",
        bg=ACCENT,
        activebackground=ACCENT_ACTIVE,
        activeforeground="#ffffff",
        disabledforeground="#b7d4ee",
        relief="flat",
        bd=0,
        highlightthickness=1,
        highlightbackground=ACCENT,
        highlightcolor=ACCENT,
        padx=14,
        pady=5,
        font=FONT_BOLD,
        cursor="hand2",
        **kw,
    )
    if width is not None:
        b.configure(width=width)

    def on_enter(_e):
        if str(b["state"]) != "disabled":
            b.configure(bg=ACCENT_HOVER, highlightbackground=ACCENT_HOVER)

    def on_leave(_e):
        b.configure(bg=ACCENT, highlightbackground=ACCENT)

    b.bind("<Enter>", on_enter)
    b.bind("<Leave>", on_leave)
    return b


def Ent(master: tk.Misc, textvariable=None, **kw) -> tk.Entry:
    kw.setdefault("bg", WINDOW)
    kw.setdefault("fg", TEXT)
    kw.setdefault("relief", "flat")
    kw.setdefault("bd", 0)
    kw.setdefault("highlightthickness", 1)
    kw.setdefault("highlightbackground", BORDER)
    kw.setdefault("highlightcolor", ACCENT)
    kw.setdefault("disabledbackground", FACE)
    kw.setdefault("insertwidth", 1)
    kw.setdefault("insertbackground", TEXT)
    kw.setdefault("font", FONT)
    return tk.Entry(master, textvariable=textvariable, **kw)


def Txt(master: tk.Misc, **kw) -> tk.Text:
    kw.setdefault("bg", WINDOW)
    kw.setdefault("fg", TEXT)
    kw.setdefault("relief", "flat")
    kw.setdefault("bd", 0)
    kw.setdefault("highlightthickness", 1)
    kw.setdefault("highlightbackground", BORDER_MUTED)
    kw.setdefault("highlightcolor", ACCENT)
    kw.setdefault("insertbackground", TEXT)
    kw.setdefault("wrap", "word")
    kw.setdefault("font", FONT)
    return tk.Text(master, **kw)


def Chk(master: tk.Misc, text: str, variable, command=None, **kw) -> tk.Checkbutton:
    kw.setdefault("bg", FACE)
    kw.setdefault("fg", TEXT)
    kw.setdefault("activebackground", kw.get("bg", FACE))
    kw.setdefault("activeforeground", TEXT)
    kw.setdefault("selectcolor", WINDOW)
    kw.setdefault("anchor", "w")
    kw.setdefault("highlightthickness", 0)
    kw.setdefault("font", FONT)
    kw.setdefault("cursor", "hand2")
    return tk.Checkbutton(master, text=text, variable=variable, command=command, **kw)


def Rad(master: tk.Misc, text: str, variable, value, command=None, **kw) -> tk.Radiobutton:
    kw.setdefault("bg", FACE)
    kw.setdefault("fg", TEXT)
    kw.setdefault("activebackground", kw.get("bg", FACE))
    kw.setdefault("activeforeground", TEXT)
    kw.setdefault("selectcolor", WINDOW)
    kw.setdefault("anchor", "w")
    kw.setdefault("highlightthickness", 0)
    kw.setdefault("font", FONT)
    kw.setdefault("cursor", "hand2")
    return tk.Radiobutton(master, text=text, variable=variable, value=value, command=command, **kw)


def Combo(master: tk.Misc, values, textvariable=None, width=None, command=None, **kw) -> ttk.Combobox:
    cb = ttk.Combobox(
        master,
        values=list(values) if values else [],
        textvariable=textvariable,
        state="readonly",
        font=FONT,
        **kw,
    )
    if width is not None:
        cb.configure(width=width)
    if command is not None:
        cb.bind("<<ComboboxSelected>>", lambda _e: command())
    # Readonly comboboxes cycle values on mouse wheel by default — easy to
    # mis-change while scrolling a form. Block that and forward the wheel to
    # an enclosing ScrollFrame so the page still scrolls under the pointer.
    def _on_combo_wheel(event, widget=cb):
        parent = getattr(widget, "master", None)
        while parent is not None:
            if isinstance(parent, ScrollFrame):
                parent._on_wheel(event)
                break
            parent = getattr(parent, "master", None)
        return "break"

    cb.bind("<MouseWheel>", _on_combo_wheel)
    cb.bind("<Button-4>", _on_combo_wheel)  # Linux scroll up
    cb.bind("<Button-5>", _on_combo_wheel)  # Linux scroll down
    return cb


def Sep(master: tk.Misc, **kw) -> tk.Frame:
    """A hairline separator."""
    kw.setdefault("height", 1)
    kw.setdefault("bg", BORDER_MUTED)
    return tk.Frame(master, **kw)


class ScrollFrame(tk.Frame):
    """A vertically scrollable container. Pack children into ``.inner``."""

    # Shared registry of armed ScrollFrames so one leave does not kill others'
    # bind_all wheel handlers (unbind_all would wipe every global MouseWheel).
    _armed_frames: set = set()
    _global_wheel_bound: bool = False

    def __init__(self, master: tk.Misc, bg: str = FACE, **kw):
        kw.setdefault("bg", bg)
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._wheel_armed = False
        self.inner.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._win, width=e.width),
        )
        for w in (self, self.canvas, self.inner):
            w.bind("<Enter>", self._arm_wheel, add="+")
            w.bind("<Leave>", self._schedule_disarm_wheel, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")

    def _contains_pointer(self) -> bool:
        try:
            px, py = self.winfo_pointerxy()
            x, y = self.winfo_rootx(), self.winfo_rooty()
            return x <= px < x + self.winfo_width() and y <= py < y + self.winfo_height()
        except tk.TclError:
            return False

    @classmethod
    def _ensure_global_wheel(cls, root: tk.Misc) -> None:
        if cls._global_wheel_bound:
            return
        try:
            root.bind_all("<MouseWheel>", cls._dispatch_wheel, add="+")
            root.bind_all("<Button-4>", cls._dispatch_wheel_linux, add="+")
            root.bind_all("<Button-5>", cls._dispatch_wheel_linux, add="+")
            cls._global_wheel_bound = True
        except tk.TclError:
            pass

    @classmethod
    def _dispatch_wheel(cls, event):
        # First armed frame under the pointer wins.
        for frame in list(cls._armed_frames):
            try:
                if frame._contains_pointer():
                    return frame._on_wheel(event)
            except tk.TclError:
                cls._armed_frames.discard(frame)
        return None

    @classmethod
    def _dispatch_wheel_linux(cls, event):
        num = getattr(event, "num", 0)
        delta = 120 if num == 4 else -120
        try:
            event.delta = delta  # type: ignore[attr-defined]
        except Exception:
            pass
        return cls._dispatch_wheel(event)

    def _arm_wheel(self, _event=None) -> None:
        if self._wheel_armed:
            return
        try:
            self._ensure_global_wheel(self.winfo_toplevel())
        except tk.TclError:
            return
        ScrollFrame._armed_frames.add(self)
        self._wheel_armed = True

    def _schedule_disarm_wheel(self, _event=None) -> None:
        try:
            self.after(1, self._disarm_if_outside)
        except tk.TclError:
            self._disarm_wheel()

    def _disarm_if_outside(self) -> None:
        if self._wheel_armed and not self._contains_pointer():
            self._disarm_wheel()

    def _disarm_wheel(self, _event=None) -> None:
        if not self._wheel_armed:
            return
        ScrollFrame._armed_frames.discard(self)
        self._wheel_armed = False

    def _on_destroy(self, _event=None) -> None:
        self._disarm_wheel()

    def _on_wheel(self, event) -> str | None:
        if not self._contains_pointer():
            return None
        try:
            delta = int(getattr(event, "delta", 0) or 0)
            if delta == 0:
                return None
            self.canvas.yview_scroll(int(-delta / 120), "units")
        except tk.TclError:
            pass
        return "break"
