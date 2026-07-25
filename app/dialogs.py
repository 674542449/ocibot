"""Modal dialogs — classic Win9x/VC6 style (plain tkinter + ttk)."""

from __future__ import annotations

import os
import queue as _queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from app import classic as C
from app.classic import (
    Btn,
    BtnPrimary,
    Chk,
    Combo,
    Ent,
    Frm,
    Group,
    Lbl,
    Rad,
    ScrollFrame,
    Txt,
    list_ui_font_families,
)
from app.config_store import TenantConfig, parse_oci_api_text
from app.formatting import (
    axis_max,
    format_launch_confirm_rows,
    human_bytes,
    scale_points,
    validate_zip_password,
)
from app.oci_client import (
    BOOT_VPU_PRESETS,
    FREE_TIER_SHAPES,
    FirewallRuleSpec,
    free_tier_tag,
    generate_root_password,
)
from app.theme import TENANT_COLORS

# One-click free-tier launch presets (shape + boot). Network uses the account default.
LAUNCH_QUICK_PRESETS: list[dict] = [
    {
        "id": "e2_micro_50",
        "label": "免费 AMD · 50G",
        "hint": "VM.Standard.E2.1.Micro · 硬盘 50GB · 性能 120",
        "shape": "VM.Standard.E2.1.Micro",
        "arch": "x86",
        "ocpus": None,
        "memory_in_gbs": None,
        "boot_volume_size_in_gbs": 50,
        "boot_volume_vpus_per_gb": 120,
    },
    {
        "id": "a1_4c24g_100",
        "label": "免费 ARM 4C24G · 100G",
        "hint": "VM.Standard.A1.Flex · 4 OCPU / 24GB · 硬盘 100GB · 性能 120",
        "shape": "VM.Standard.A1.Flex",
        "arch": "arm",
        "ocpus": 4,
        "memory_in_gbs": 24,
        "boot_volume_size_in_gbs": 100,
        "boot_volume_vpus_per_gb": 120,
    },
    {
        "id": "a1_4c24g_200",
        "label": "免费 ARM 4C24G · 200G",
        "hint": "VM.Standard.A1.Flex · 4 OCPU / 24GB · 硬盘 200GB · 性能 120",
        "shape": "VM.Standard.A1.Flex",
        "arch": "arm",
        "ocpus": 4,
        "memory_in_gbs": 24,
        "boot_volume_size_in_gbs": 200,
        "boot_volume_vpus_per_gb": 120,
    },
]

try:
    import windnd  # type: ignore

    _WINDND_AVAILABLE = True
except ImportError:  # pragma: no cover
    windnd = None  # type: ignore
    _WINDND_AVAILABLE = False

OCI_API_HINT = (
    "格式示例（请勿原样保存）：\n"
    "[DEFAULT]\n"
    "user=ocid1.user.oc1..xxxxxxxx\n"
    "fingerprint=aa:bb:cc:...\n"
    "tenancy=ocid1.tenancy.oc1..xxxxxxxx\n"
    "region=ap-tokyo-1\n"
    "# key_file=C:\\Users\\you\\.oci\\oci_api_key.pem"
)


def _is_placeholder_api_text(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    low = raw.lower()
    if "user=ocid1.user.oc1..aaaaaaaa..." in low:
        return True
    if "tenancy=ocid1.tenancy.oc1..aaaaaaaa..." in low:
        return True
    if "user=ocid1.user.oc1..xxxxxxxx" in low and "tenancy=ocid1.tenancy.oc1..xxxxxxxx" in low:
        return True
    if "格式示例（请勿原样保存）" in raw:
        return True
    if "ocid1.user.oc1..aaaaaaaa..." in low or "ocid1.tenancy.oc1..aaaaaaaa..." in low:
        return True
    return False


def _looks_like_placeholder_ocid(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return True
    low = v.lower()
    if low.endswith("...") or "xxxxxxxx" in low:
        return True
    if low in ("ocid1.user.oc1..aaaaaaaa...", "ocid1.tenancy.oc1..aaaaaaaa..."):
        return True
    if low.startswith("ocid1.user.oc1..aaaaaaaa") and len(v) < 40:
        return True
    if low.startswith("ocid1.tenancy.oc1..aaaaaaaa") and len(v) < 44:
        return True
    return False


class BaseDialog(tk.Toplevel):
    def __init__(self, master, title: str, width: int = 640, height: int = 560):
        super().__init__(master)
        self.title(title)
        # Cap initial size to the parent / screen so large-font laptops still fit.
        try:
            max_w = max(480, int(master.winfo_width() * 0.92))
            max_h = max(360, int(master.winfo_height() * 0.92))
        except Exception:
            max_w, max_h = width, height
        try:
            sw = max(640, int(self.winfo_screenwidth() * 0.9))
            sh = max(480, int(self.winfo_screenheight() * 0.85))
            max_w = min(max_w, sw) if max_w else sw
            max_h = min(max_h, sh) if max_h else sh
        except Exception:
            pass
        width = max(400, min(int(width), max_w))
        height = max(280, min(int(height), max_h))
        self.geometry(f"{width}x{height}")
        # Allow shrinking well below the preferred size; content should scroll.
        self.minsize(min(480, width), min(360, height))
        self.resizable(True, True)
        self.configure(bg=C.FACE)
        self.transient(master)
        self.result = None
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.after(10, self._grab)
        self.after(20, self._center)

    def _grab(self) -> None:
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _center(self) -> None:
        try:
            self.update_idletasks()
            parent = self.master
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            x = px + max(0, (pw - w) // 2)
            y = py + max(0, (ph - h) // 2)
            self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _on_cancel(self) -> None:
        self.result = None
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    def _finish(self, value) -> None:
        self.result = value
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()

    # -- background work --------------------------------------------------
    def _run_bg(self, work: Callable, on_done: Callable) -> None:
        """Run ``work()`` off the UI thread; call ``on_done(result, error)`` on it.

        ``on_done`` always runs on the Tk thread via an ``after`` poll loop, so
        it may safely touch dialog widgets. Results arriving after the dialog is
        destroyed are dropped.
        """
        if not hasattr(self, "_bg_queue"):
            self._bg_queue: _queue.Queue = _queue.Queue()
            self._bg_after_id = self.after(120, self._poll_bg)

        def runner() -> None:
            try:
                self._bg_queue.put((on_done, work(), None))
            except Exception as exc:  # noqa: BLE001
                self._bg_queue.put((on_done, None, exc))

        threading.Thread(target=runner, daemon=True).start()

    def _poll_bg(self) -> None:
        self._bg_after_id = None
        try:
            while True:
                cb, result, error = self._bg_queue.get_nowait()
                try:
                    cb(result, error)
                except Exception:
                    pass
        except _queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self._bg_after_id = self.after(120, self._poll_bg)
        except tk.TclError:
            pass

    def destroy(self) -> None:  # noqa: D401
        # Cancel a pending poll so it can't fire after teardown.
        after_id = getattr(self, "_bg_after_id", None)
        if after_id is not None:
            try:
                self.after_cancel(after_id)
            except tk.TclError:
                pass
            self._bg_after_id = None
        super().destroy()


class TenantEditorDialog(BaseDialog):
    """Add/edit tenant: paste OCI API config text + select/drag key file."""

    def __init__(self, master, tenant: Optional[TenantConfig] = None, on_test: Optional[Callable] = None):
        self._editing = tenant
        self._on_test = on_test
        self._key_path: Optional[str] = None
        self._private_key_pem = (tenant.private_key_pem if tenant else "") or ""
        title = "编辑租户配置" if tenant else "添加租户配置"
        super().__init__(master, title=title, width=680, height=720)
        self._build(tenant or TenantConfig.new_empty())
        self.after(80, self._setup_drag_drop)

    def _build(self, tenant: TenantConfig) -> None:
        Lbl(self, text="添加 / 编辑甲骨文账号", font=C.FONT_TITLE).pack(fill="x", padx=10, pady=(8, 0))
        Lbl(self, text="① 粘贴 OCI 原格式 API 配置文本   ② 选择或拖入密钥文件（.pem）", fg=C.TEXT_DIM).pack(
            fill="x", padx=10
        )

        scroll = ScrollFrame(self)
        scroll.pack(fill="both", expand=True, padx=8, pady=6)
        body = scroll.inner
        self.vars: dict = {}

        Lbl(body, text="显示名称（面板中显示，可自定义）", fg=C.TEXT_DIM).pack(fill="x", padx=8, pady=(8, 1))
        self.vars["name"] = tk.StringVar(value=tenant.name if tenant.name != "新租户" else "")
        Ent(body, textvariable=self.vars["name"]).pack(fill="x", padx=8)

        api_head = Frm(body)
        api_head.pack(fill="x", padx=8, pady=(10, 1))
        Lbl(api_head, text="API 配置文本（原格式）*", fg=C.TEXT_DIM).pack(side="left")
        Btn(api_head, "从 config 文件导入…", command=self._load_config_file).pack(side="right")
        Btn(api_head, "解析文本", command=self._parse_api_text).pack(side="right", padx=(0, 4))

        self.api_box = Txt(body, height=9, font=C.FIXED, wrap="none")
        self.api_box.pack(fill="x", padx=8, pady=(2, 2))
        if (tenant.user_ocid or tenant.tenancy_ocid) and not _looks_like_placeholder_ocid(tenant.user_ocid):
            self._set_api_text(self._tenant_to_api_text(tenant))
        else:
            self._set_api_text("")
            self.after(50, self.api_box.focus_set)

        Lbl(body, text=OCI_API_HINT, fg=C.TEXT_MUTE, font=C.FIXED, justify="left").pack(fill="x", padx=8)

        api_tools = Frm(body)
        api_tools.pack(fill="x", padx=8, pady=(4, 0))
        Btn(api_tools, "清空", command=lambda: self._set_api_text("")).pack(side="left")
        Btn(api_tools, "从剪贴板粘贴", command=self._paste_api_from_clipboard).pack(side="left", padx=6)

        self.parse_status = Lbl(
            body,
            text="请粘贴真实 API 配置，或点「从 config 文件导入 / 从剪贴板粘贴」",
            fg=C.TEXT_MUTE,
            justify="left",
            wraplength=630,
        )
        self.parse_status.pack(fill="x", padx=8, pady=(4, 2))
        self._bind_api_box_events()

        Lbl(body, text="API 私钥文件 *", fg=C.TEXT_DIM).pack(fill="x", padx=8, pady=(10, 1))
        self.drop_zone = Frm(body, bg=C.WINDOW, highlightbackground=C.BORDER, highlightcolor=C.BORDER, highlightthickness=1, height=80)
        self.drop_zone.pack(fill="x", padx=8, pady=(2, 2))
        self.drop_zone.pack_propagate(False)
        self.drop_title = Lbl(self.drop_zone, text="点击选择密钥文件（或拖入 .pem）", bg=C.WINDOW, font=C.FONT_BOLD, anchor="center")
        self.drop_title.pack(pady=(16, 2))
        self.drop_hint = Lbl(self.drop_zone, text="支持 .pem / .key · 私钥仅本机加密保存", bg=C.WINDOW, fg=C.TEXT_MUTE, anchor="center")
        self.drop_hint.pack()
        for w in (self.drop_zone, self.drop_title, self.drop_hint):
            w.bind("<Button-1>", lambda _e: self._browse_key_file())

        key_bar = Frm(body)
        key_bar.pack(fill="x", padx=8, pady=(4, 2))
        self.key_path_var = tk.StringVar(value="")
        Ent(key_bar, textvariable=self.key_path_var).pack(side="left", fill="x", expand=True)
        Btn(key_bar, "选择文件…", command=self._browse_key_file).pack(side="left", padx=(6, 0))
        Btn(key_bar, "加载路径", command=self._load_key_from_path_var).pack(side="left", padx=(4, 0))

        self.key_status = Lbl(
            body,
            text=self._key_status_text(),
            fg="#1e8e3e" if self._private_key_pem else "#b25000",
        )
        self.key_status.pack(fill="x", padx=8, pady=(2, 2))

        Lbl(body, text="可选设置", fg=C.TEXT_DIM).pack(fill="x", padx=8, pady=(10, 1))
        self.vars["compartment_ocid"] = tk.StringVar(value=tenant.compartment_ocid or "")
        Ent(body, textvariable=self.vars["compartment_ocid"]).pack(fill="x", padx=8, pady=(0, 2))
        Lbl(body, text="↑ 默认 Compartment OCID（可留空 = Tenancy 根）", fg=C.TEXT_MUTE).pack(fill="x", padx=8)

        self.vars["description"] = tk.StringVar(value=tenant.description or "")
        Ent(body, textvariable=self.vars["description"]).pack(fill="x", padx=8, pady=(6, 2))
        Lbl(body, text="↑ 备注（可选）", fg=C.TEXT_MUTE).pack(fill="x", padx=8)

        Lbl(body, text="标识颜色", fg=C.TEXT_DIM).pack(fill="x", padx=8, pady=(8, 1))
        self.vars["color"] = tk.StringVar(value=tenant.color or TENANT_COLORS[0])
        color_row = Frm(body)
        color_row.pack(fill="x", padx=8, pady=(0, 2))
        for c in TENANT_COLORS:
            tk.Button(
                color_row, text="", width=2, bg=c, activebackground=c, relief="flat", bd=0,
                highlightthickness=1, highlightbackground=C.BORDER,
                command=lambda col=c: self.vars["color"].set(col),
            ).pack(side="left", padx=2)

        self.enabled_var = tk.BooleanVar(value=tenant.enabled)
        Chk(body, "启用此租户", self.enabled_var).pack(fill="x", padx=8, pady=(8, 12))

        self.vars["user_ocid"] = tk.StringVar(value=tenant.user_ocid or "")
        self.vars["tenancy_ocid"] = tk.StringVar(value=tenant.tenancy_ocid or "")
        self.vars["fingerprint"] = tk.StringVar(value=tenant.fingerprint or "")
        self.vars["region"] = tk.StringVar(value=tenant.region or "ap-tokyo-1")

        if tenant.user_ocid:
            self._update_parse_status(
                {
                    "user_ocid": tenant.user_ocid,
                    "tenancy_ocid": tenant.tenancy_ocid,
                    "fingerprint": tenant.fingerprint,
                    "region": tenant.region,
                }
            )
        if self._private_key_pem:
            self.after(30, self._refresh_key_ui)

        footer = Frm(self)
        footer.pack(fill="x", padx=10, pady=(2, 10))
        Btn(footer, "取消", command=self._on_cancel, width=8).pack(side="right", padx=(6, 0))
        BtnPrimary(footer, "保存", command=self._on_save, width=8).pack(side="right")
        if self._on_test:
            Btn(footer, "测试连接", command=self._on_test_click, width=8).pack(side="left")

    # ---- API text helpers ----
    def _set_api_text(self, text: str) -> None:
        try:
            self.api_box.delete("1.0", "end")
        except Exception:
            pass
        content = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        if content:
            self.api_box.insert("1.0", content if content.endswith("\n") else content + "\n")

    def _get_api_text(self) -> str:
        try:
            return self.api_box.get("1.0", "end-1c")
        except Exception:
            return self.api_box.get("1.0", "end")

    def _bind_api_box_events(self) -> None:
        def on_paste(_event=None):
            self._paste_api_from_clipboard()
            return "break"

        for seq in ("<<Paste>>", "<Control-v>", "<Control-V>", "<Shift-Insert>"):
            try:
                self.api_box.bind(seq, on_paste)
            except Exception:
                pass

    def _paste_api_from_clipboard(self) -> None:
        try:
            clip = self.clipboard_get()
        except Exception:
            messagebox.showinfo("提示", "剪贴板为空或无法读取", parent=self)
            return
        clip = (clip or "").strip()
        if not clip:
            messagebox.showinfo("提示", "剪贴板为空", parent=self)
            return
        self._set_api_text(clip)
        self._parse_api_text(auto_load_key=True)

    @staticmethod
    def _tenant_to_api_text(tenant: TenantConfig) -> str:
        lines = [
            "[DEFAULT]",
            f"user={tenant.user_ocid}",
            f"fingerprint={tenant.fingerprint}",
            f"tenancy={tenant.tenancy_ocid}",
            f"region={tenant.region}",
        ]
        if tenant.compartment_ocid:
            lines.append(f"compartment_id={tenant.compartment_ocid}")
        return "\n".join(lines) + "\n"

    def _key_status_text(self) -> str:
        if self._private_key_pem and "BEGIN" in self._private_key_pem:
            name = Path(self._key_path).name if self._key_path else "已加载（内存）"
            return f"✓ 密钥已就绪：{name}  （{len(self._private_key_pem)} 字符）"
        return "⚠ 尚未加载密钥文件 — 请选择或拖入 .pem"

    def _refresh_key_ui(self) -> None:
        ok = bool(self._private_key_pem and "BEGIN" in self._private_key_pem)
        self.key_status.configure(text=self._key_status_text(), fg="#1e8e3e" if ok else "#b25000")
        if ok:
            self.drop_title.configure(text="密钥已加载（可重新选择替换）")
            self.drop_zone.configure(highlightbackground="#1e8e3e", highlightcolor="#1e8e3e")
            if self._key_path:
                self.drop_hint.configure(text=self._key_path)
                self.key_path_var.set(self._key_path)
        else:
            self.drop_title.configure(text="点击选择密钥文件（或拖入 .pem）")
            self.drop_zone.configure(highlightbackground=C.BORDER, highlightcolor=C.BORDER)
            self.drop_hint.configure(text="支持 .pem / .key · 私钥仅本机加密保存")

    def _setup_drag_drop(self) -> None:
        if not _WINDND_AVAILABLE:
            self.drop_hint.configure(
                text="支持 .pem / .key · 点击选择文件"
                + ("（可选: pip install windnd 启用拖放）" if os.name == "nt" else "")
            )
            return
        try:
            windnd.hook_dropfiles(self.drop_zone, func=self._on_drop_files)
        except Exception:
            pass
        try:
            windnd.hook_dropfiles(self.api_box, func=self._on_drop_files)
        except Exception:
            pass

    def _on_drop_files(self, files) -> None:
        paths: list[str] = []
        for f in files or []:
            if isinstance(f, bytes):
                try:
                    paths.append(f.decode("utf-8"))
                except UnicodeDecodeError:
                    paths.append(f.decode("gbk", errors="replace"))
            else:
                paths.append(str(f))
        if not paths:
            return
        preferred = next((p for p in paths if p.lower().endswith((".pem", ".key", ".txt"))), paths[0])
        self.after(0, lambda: self._apply_key_file(preferred))

    def _browse_key_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择 API 私钥文件",
            filetypes=[("PEM / Key", "*.pem *.key *.txt"), ("All", "*.*")],
        )
        if path:
            self._apply_key_file(path)

    def _load_key_from_path_var(self) -> None:
        path = self.key_path_var.get().strip().strip('"')
        if not path:
            messagebox.showinfo("提示", "请先填写或粘贴密钥文件路径", parent=self)
            return
        self._apply_key_file(path)

    def _apply_key_file(self, path: str) -> None:
        path = os.path.expanduser(path.strip().strip('"'))
        p = Path(path)
        if not p.exists() or not p.is_file():
            messagebox.showerror("读取失败", f"文件不存在：\n{path}", parent=self)
            return
        try:
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = p.read_text(encoding="latin-1")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取失败", str(exc), parent=self)
            return
        if "PRIVATE KEY" not in text and "BEGIN" not in text:
            if not messagebox.askyesno("确认", "文件内容看起来不像 PEM 私钥，仍要加载吗？", parent=self):
                return
        self._private_key_pem = text.strip() + "\n"
        self._key_path = str(p.resolve())
        self._refresh_key_ui()

    def _load_config_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择 OCI config 文件",
            filetypes=[("Config", "config *.config *.txt *"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取失败", str(exc), parent=self)
            return
        self._set_api_text(text)
        parsed = self._parse_api_text(auto_load_key=False)
        if parsed.get("key_file"):
            key_path = Path(os.path.expanduser(parsed["key_file"]))
            if not key_path.is_absolute():
                key_path = (Path(path).parent / key_path).resolve()
            if key_path.exists():
                self._apply_key_file(str(key_path))
            else:
                self.key_path_var.set(str(key_path))
                self.parse_status.configure(
                    text=self._format_parse_preview(parsed) + f"\n⚠ key_file 不存在：{key_path}，请手动选择密钥",
                    fg="#b25000",
                )

    def _parse_api_text(self, auto_load_key: bool = True) -> dict:
        text = self._get_api_text()
        if _is_placeholder_api_text(text):
            self.parse_status.configure(
                text="⚠ 文本框为空或仍是示例 — 请粘贴真实 API 配置（可用「从剪贴板粘贴」）", fg="#b25000"
            )
            return {}
        parsed = parse_oci_api_text(text)
        if not parsed:
            self.parse_status.configure(text="⚠ 未能解析出有效字段，请检查格式", fg="#b25000")
            return {}
        if _looks_like_placeholder_ocid(parsed.get("user_ocid", "")) or _looks_like_placeholder_ocid(
            parsed.get("tenancy_ocid", "")
        ):
            self.parse_status.configure(text="⚠ 解析到的 OCID 仍是占位内容，请确认粘贴的是真实配置", fg="#b25000")
            return {}
        for key in ("user_ocid", "tenancy_ocid", "fingerprint", "region", "compartment_ocid"):
            if parsed.get(key) and key in self.vars:
                self.vars[key].set(parsed[key])
        if not self.vars["name"].get().strip():
            if parsed.get("name"):
                self.vars["name"].set(parsed["name"])
            elif parsed.get("profile") and parsed["profile"].upper() != "DEFAULT":
                self.vars["name"].set(parsed["profile"])
            elif parsed.get("region"):
                self.vars["name"].set(f"OCI-{parsed['region']}")
        if auto_load_key and parsed.get("key_file"):
            key_path = os.path.expanduser(parsed["key_file"])
            if Path(key_path).exists():
                self._apply_key_file(key_path)
            else:
                self.key_path_var.set(parsed["key_file"])
                self.parse_status.configure(
                    text=self._format_parse_preview(parsed) + f"\n⚠ config 中的 key_file 不存在：{parsed['key_file']}，请手动选择密钥",
                    fg="#b25000",
                )
                return parsed
        self._update_parse_status(parsed)
        return parsed

    def _format_parse_preview(self, parsed: dict) -> str:
        def short(v: str, n: int = 28) -> str:
            v = v or "—"
            return v if len(v) <= n else f"{v[:12]}…{v[-10:]}"

        return (
            f"✓ 已解析  user={short(parsed.get('user_ocid', ''))}  "
            f"tenancy={short(parsed.get('tenancy_ocid', ''))}  "
            f"region={parsed.get('region') or '—'}  "
            f"fp={short(parsed.get('fingerprint', ''), 24)}"
        )

    def _update_parse_status(self, parsed: dict) -> None:
        self.parse_status.configure(text=self._format_parse_preview(parsed), fg="#1e8e3e")

    def _collect(self) -> TenantConfig:
        text = self._get_api_text()
        parsed = {} if _is_placeholder_api_text(text) else parse_oci_api_text(text)
        user = (parsed.get("user_ocid") or self.vars["user_ocid"].get() or "").strip()
        tenancy = (parsed.get("tenancy_ocid") or self.vars["tenancy_ocid"].get() or "").strip()
        fingerprint = (parsed.get("fingerprint") or self.vars["fingerprint"].get() or "").strip()
        region = (parsed.get("region") or self.vars["region"].get() or "").strip()
        compartment = self.vars["compartment_ocid"].get().strip() or parsed.get("compartment_ocid") or ""

        self.vars["user_ocid"].set(user)
        self.vars["tenancy_ocid"].set(tenancy)
        self.vars["fingerprint"].set(fingerprint)
        self.vars["region"].set(region)

        name = self.vars["name"].get().strip()
        if not name:
            name = parsed.get("name") or parsed.get("profile") or (f"OCI-{region}" if region else "未命名租户")

        pem = self._private_key_pem.strip()
        if not pem and parsed.get("key_file"):
            kp = Path(os.path.expanduser(parsed["key_file"]))
            if kp.exists():
                try:
                    pem = kp.read_text(encoding="utf-8").strip()
                    self._private_key_pem = pem
                    self._key_path = str(kp)
                    self._refresh_key_ui()
                except Exception:
                    pass

        base = self._editing or TenantConfig.new_empty()
        return TenantConfig(
            id=base.id,
            name=name,
            description=self.vars["description"].get().strip(),
            user_ocid=user,
            tenancy_ocid=tenancy,
            fingerprint=fingerprint,
            region=region or "ap-tokyo-1",
            private_key_pem=pem if pem.endswith("\n") else (pem + "\n" if pem else ""),
            compartment_ocid=compartment,
            enabled=bool(self.enabled_var.get()),
            color=self.vars["color"].get() or TENANT_COLORS[0],
            created_at=base.created_at,
        )

    def _on_save(self) -> None:
        text = self._get_api_text()
        if _is_placeholder_api_text(text):
            messagebox.showwarning(
                "请先导入 API",
                "文本框为空或仍是示例内容。\n\n请使用：\n• 「从剪贴板粘贴」\n• 「从 config 文件导入」\n• 或直接在框内粘贴真实配置（会覆盖旧内容）",
                parent=self,
            )
            return
        self._parse_api_text(auto_load_key=True)
        tenant = self._collect()
        errors = tenant.validate()
        if errors:
            messagebox.showwarning("配置不完整", "\n".join(errors), parent=self)
            return
        self._finish(tenant)

    def _on_test_click(self) -> None:
        text = self._get_api_text()
        if _is_placeholder_api_text(text):
            messagebox.showwarning("请先导入 API", "请先粘贴或导入真实 API 配置文本。", parent=self)
            return
        self._parse_api_text(auto_load_key=True)
        tenant = self._collect()
        errors = tenant.validate()
        if errors:
            messagebox.showwarning("配置不完整", "\n".join(errors), parent=self)
            return
        if self._on_test:
            self._on_test(tenant, self)


class ConfirmDialog(BaseDialog):
    def __init__(self, master, title: str, message: str, confirm_text: str = "确认", danger: bool = False, require_text: str = ""):
        self._require_text = require_text
        super().__init__(master, title=title, width=460, height=260 if require_text else 200)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        content = Frm(self)
        content.grid(row=0, column=0, sticky="nsew", padx=14, pady=(12, 6))
        content.grid_rowconfigure(1, weight=1)
        content.grid_columnconfigure(0, weight=1)
        Lbl(content, text=title, font=C.FONT_TITLE, fg=C.RED if danger else C.TEXT).grid(
            row=0, column=0, sticky="ew"
        )
        self.message_label = Lbl(content, text=message, wraplength=420, justify="left", anchor="nw")
        self.message_label.grid(row=1, column=0, sticky="nsew", pady=(8, 4))

        self._entry_var = tk.StringVar()
        if require_text:
            entry_wrap = Frm(content)
            entry_wrap.grid(row=2, column=0, sticky="ew", pady=(2, 0))
            Lbl(entry_wrap, text=f'请输入 "{require_text}" 以确认：', fg=C.TEXT_MUTE).pack(anchor="w")
            self.confirm_entry = Ent(entry_wrap, textvariable=self._entry_var)
            self.confirm_entry.pack(fill="x", pady=(2, 0))

        self.footer = Frm(self)
        self.footer.grid(row=1, column=0, sticky="ew", padx=14, pady=(4, 12))
        self.cancel_button = Btn(self.footer, "取消", command=self._on_cancel, width=8)
        self.cancel_button.pack(side="right", padx=(6, 0))
        self.confirm_button = Btn(
            self.footer, confirm_text, command=self._on_ok, width=10,
            fg="#d70015" if danger else C.TEXT,
        )
        self.confirm_button.pack(side="right")

    def _on_ok(self) -> None:
        if self._require_text and self._entry_var.get().strip() != self._require_text:
            messagebox.showwarning("确认失败", "输入内容不匹配", parent=self)
            return
        self._finish(True)


class TextPromptDialog(BaseDialog):
    def __init__(self, master, title: str, label: str, initial: str = ""):
        super().__init__(master, title=title, width=460, height=180)
        wrap = Frm(self)
        wrap.pack(fill="both", expand=True, padx=14, pady=12)
        Lbl(wrap, text=label).pack(anchor="w")
        self.var = tk.StringVar(value=initial)
        entry = Ent(wrap, textvariable=self.var)
        entry.pack(fill="x", pady=(6, 0))
        entry.focus_set()
        entry.bind("<Return>", lambda _e: self._finish(self.var.get().strip()))
        footer = Frm(wrap)
        footer.pack(fill="x", side="bottom", pady=(14, 0))
        Btn(footer, "取消", command=self._on_cancel, width=8).pack(side="right", padx=(6, 0))
        BtnPrimary(footer, "确定", command=lambda: self._finish(self.var.get().strip()), width=8).pack(side="right")


def ask_confirm(master, title: str, message: str, confirm_text: str = "确认", danger: bool = False, require_text: str = "") -> bool:
    dlg = ConfirmDialog(master, title=title, message=message, confirm_text=confirm_text, danger=danger, require_text=require_text)
    master.wait_window(dlg)
    return bool(dlg.result)


class LaunchConfirmDialog(BaseDialog):
    """Pre-launch review: show important server specs before submitting create."""

    def __init__(self, master, rows: list[tuple[str, str]], *, note: str = ""):
        super().__init__(master, title="确认创建配置", width=520, height=480)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        content = Frm(self)
        content.grid(row=0, column=0, sticky="nsew", padx=14, pady=(12, 6))
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(2, weight=1)

        Lbl(content, text="请确认以下服务器配置", font=C.FONT_TITLE).grid(row=0, column=0, sticky="ew")
        Lbl(
            content,
            text="核对型号、核心、内存、硬盘与性能后再创建。点「返回修改」可回到上一页。",
            fg=C.TEXT_MUTE,
            wraplength=480,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))

        card = Group(content)
        card.grid(row=2, column=0, sticky="nsew", pady=(12, 6))
        card.grid_columnconfigure(1, weight=1)

        for i, (label, value) in enumerate(rows):
            top = 10 if i == 0 else 4
            bottom = 10 if i == len(rows) - 1 else 4
            Lbl(card, text=str(label), fg=C.TEXT_MUTE, bg=C.WINDOW).grid(
                row=i, column=0, sticky="nw", padx=(14, 12), pady=(top, bottom)
            )
            Lbl(
                card,
                text=str(value),
                font=C.FONT_BOLD,
                bg=C.WINDOW,
                wraplength=340,
                justify="left",
                anchor="w",
            ).grid(row=i, column=1, sticky="ew", padx=(0, 14), pady=(top, bottom))

        if note:
            Lbl(
                content,
                text=note,
                fg="#b25000",
                wraplength=480,
                justify="left",
            ).grid(row=3, column=0, sticky="ew", pady=(2, 0))

        self.footer = Frm(self)
        self.footer.grid(row=1, column=0, sticky="ew", padx=14, pady=(4, 12))
        self.cancel_button = Btn(self.footer, "返回修改", command=self._on_cancel, width=10)
        self.cancel_button.pack(side="right", padx=(6, 0))
        self.confirm_button = BtnPrimary(self.footer, "确认创建", command=self._on_ok, width=10)
        self.confirm_button.pack(side="right")

    def _on_ok(self) -> None:
        self._finish(True)


def ask_launch_confirm(master, rows: list[tuple[str, str]], *, note: str = "") -> bool:
    dlg = LaunchConfirmDialog(master, rows, note=note)
    master.wait_window(dlg)
    return bool(dlg.result)


class BootVolumeDialog(BaseDialog):
    """Edit an existing instance's boot volume size + performance (VPUs/GB)."""

    def __init__(self, master, current_size: int = 0, current_vpu: int = 10):
        self._current_size = int(current_size or 0)
        self._current_vpu = int(current_vpu or 10)
        super().__init__(master, title="调整引导卷", width=460, height=320)
        body = Frm(self)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        Lbl(body, text=f"当前：{self._current_size or '?'} GB · {self._current_vpu} VPUs/GB", font=C.FONT_BOLD).pack(anchor="w")
        Lbl(body, text="引导卷只能增大不能缩小；120 VPUs/GB 在本区域 200GB 免费额度内通常不额外计费（以账号为准）。", fg=C.TEXT_MUTE, wraplength=420, justify="left").pack(anchor="w", pady=(4, 8))

        Lbl(body, text="大小 GB").pack(anchor="w")
        self.size_var = tk.StringVar(value=str(self._current_size or 50))
        size_row = Frm(body)
        size_row.pack(fill="x", pady=(0, 8))
        Ent(size_row, textvariable=self.size_var, width=10).pack(side="left")
        for preset in (50, 100, 150, 200):
            Btn(size_row, str(preset), command=lambda p=preset: self.size_var.set(str(p)), width=4).pack(side="left", padx=2)

        Lbl(body, text="性能（VPUs/GB）").pack(anchor="w")
        self._vpu_map = {label: value for value, label in BOOT_VPU_PRESETS}
        current_label = next((lb for lb, v in self._vpu_map.items() if v == self._current_vpu), list(self._vpu_map)[0])
        self.vpu_var = tk.StringVar(value=current_label)
        Combo(body, list(self._vpu_map), textvariable=self.vpu_var).pack(fill="x", pady=(0, 8))

        footer = Frm(body)
        footer.pack(fill="x", side="bottom", pady=(10, 0))
        Btn(footer, "取消", command=self._on_cancel, width=8).pack(side="right", padx=(6, 0))
        BtnPrimary(footer, "应用", command=self._on_ok, width=8).pack(side="right")

    def _on_ok(self) -> None:
        try:
            size = int(float(self.size_var.get().strip()))
        except ValueError:
            messagebox.showwarning("参数错误", "大小必须是数字", parent=self)
            return
        if not 50 <= size <= 32768:
            messagebox.showwarning("参数错误", "引导卷大小必须在 50–32768 GB 之间", parent=self)
            return
        if self._current_size and size < self._current_size:
            messagebox.showwarning("参数错误", f"引导卷只能增大：当前 {self._current_size} GB，不能改小。", parent=self)
            return
        vpu = self._vpu_map.get(self.vpu_var.get(), 10)
        result = {
            "size_in_gbs": size if size != self._current_size else None,
            "vpus_per_gb": vpu if vpu != self._current_vpu else None,
        }
        if result["size_in_gbs"] is None and result["vpus_per_gb"] is None:
            messagebox.showinfo("提示", "大小和性能都没有变化。", parent=self)
            return
        self._finish(result)


# ---------------------------------------------------------------------------
# Launch instance
# ---------------------------------------------------------------------------


class LaunchInstanceDialog(BaseDialog):
    """Wizard-like form to launch a VM; can also enqueue capacity-retry."""

    def __init__(self, master, meta: dict, default_name: str = "ocibot-instance"):
        self.meta = meta
        # Prefer a compact window; ScrollFrame holds overflow when fonts are large.
        super().__init__(master, title="创建实例", width=640, height=560)
        self.minsize(480, 360)
        self._build(default_name)
        self.after(80, self._setup_ssh_drop)

    def _build(self, default_name: str) -> None:
        wrap = 600
        Lbl(self, text="创建实例", font=C.FONT_BOLD).pack(fill="x", padx=10, pady=(6, 0))
        Lbl(
            self,
            text="填写启动参数。默认网络自动选用；容量不足可勾选「加入容量重试」。",
            fg=C.TEXT_MUTE,
            font=C.FONT_SMALL,
            wraplength=wrap,
            justify="left",
        ).pack(fill="x", padx=10, pady=(0, 2))

        scroll = ScrollFrame(self)
        scroll.pack(fill="both", expand=True, padx=6, pady=4)
        body = scroll.inner
        self.vars: dict = {}

        def lab(text: str) -> None:
            Lbl(body, text=text, fg=C.TEXT_DIM, font=C.FONT_SMALL).pack(fill="x", padx=8, pady=(5, 0))

        def entry(key: str, value: str = "") -> tk.Entry:
            var = tk.StringVar(value=value)
            self.vars[key] = var
            e = Ent(body, textvariable=var)
            e.pack(fill="x", padx=8, pady=(1, 0))
            return e

        def combo(key: str, values: list[str], value: str = "") -> ttk.Combobox:
            var = tk.StringVar(value=value or (values[0] if values else ""))
            self.vars[key] = var
            cb = Combo(body, values or [""], textvariable=var)
            cb.pack(fill="x", padx=8, pady=(1, 0))
            return cb

        lab("显示名称 *")
        entry("display_name", default_name)

        # Quick free-tier configs — fill shape / flex size / boot in one click.
        lab("快捷配置（免费套餐）")
        quick_row = Frm(body)
        quick_row.pack(fill="x", padx=8, pady=(0, 1))
        for preset in LAUNCH_QUICK_PRESETS:
            Btn(
                quick_row,
                preset["label"],
                command=lambda p=preset: self._apply_quick_preset(p),
                width=16,
            ).pack(side="left", padx=(0, 4))
        self.quick_hint = Lbl(
            body,
            text="点选后自动填入 Shape / 规格 / 硬盘；仍可再改。",
            fg=C.TEXT_MUTE,
            font=C.FONT_SMALL,
        )
        self.quick_hint.pack(fill="x", padx=8, pady=(0, 1))

        # Compartment / VCN / Subnet: always use account defaults (hidden in UI).
        comps = self.meta.get("compartments") or []
        self._comp_labels = []
        self._comp_label_to_id = {}
        for c in comps:
            label = f"{c['name']}"
            if label in self._comp_label_to_id:
                label = f"{c['name']} ({c['id'][-8:]})"
            self._comp_labels.append(label)
            self._comp_label_to_id[label] = c["id"]
        default_comp = self.meta.get("default_compartment") or (comps[0]["id"] if comps else "")
        default_label = next(
            (k for k, v in self._comp_label_to_id.items() if v == default_comp),
            self._comp_labels[0] if self._comp_labels else "",
        )
        self.vars["compartment_label"] = tk.StringVar(value=default_label)

        ads = self.meta.get("ads") or []
        lab("Availability Domain *")
        self.ad_combo = combo("ad", ads, ads[0] if ads else "")
        self.ad_combo.bind("<<ComboboxSelected>>", lambda _e: self._reload_subnets())

        images = self.meta.get("images") or []
        self._image_map = {i["label"]: i["id"] for i in images}
        self._image_info_map = {i["label"]: i for i in images}
        lab("Ubuntu 版本 / 镜像 *（仅官方 Canonical Ubuntu）")
        self.image_combo = combo("image_label", list(self._image_map.keys()), list(self._image_map.keys())[0] if self._image_map else "")
        self.image_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_image_change())

        self._all_shapes = self.meta.get("shapes") or []
        self._shape_map: dict[str, dict] = {}
        for shape_info in self._compatible_shapes():
            label = shape_info.get("label") or shape_info["shape"]
            self._shape_map[label] = shape_info
        shape_labels = list(self._shape_map)
        prefer = next((label for label in shape_labels if "A1.Flex" in label), None)
        lab("Shape *（仅免费套餐候选：A1.Flex / E2.1.Micro）")
        self.shape_combo = combo("shape_label", shape_labels, prefer or (shape_labels[0] if shape_labels else ""))
        self.shape_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_shape_change())

        row = Frm(body)
        row.pack(fill="x", padx=8, pady=(6, 0))
        left = Frm(row)
        left.pack(side="left", fill="x", expand=True, padx=(0, 4))
        right = Frm(row)
        right.pack(side="left", fill="x", expand=True, padx=(4, 0))
        Lbl(left, text="OCPU（Flex）", fg=C.TEXT_DIM, font=C.FONT_SMALL).pack(fill="x")
        self.vars["ocpus"] = tk.StringVar(value="1")
        self.ocpu_entry = Ent(left, textvariable=self.vars["ocpus"])
        self.ocpu_entry.pack(fill="x")
        Lbl(right, text="内存 GB（Flex）", fg=C.TEXT_DIM, font=C.FONT_SMALL).pack(fill="x")
        self.vars["memory"] = tk.StringVar(value="6")
        self.memory_entry = Ent(right, textvariable=self.vars["memory"])
        self.memory_entry.pack(fill="x")
        self.shape_hint = Lbl(body, text="", fg=C.TEXT_MUTE, font=C.FONT_SMALL)
        self.shape_hint.pack(fill="x", padx=8, pady=(1, 0))

        vcns = self.meta.get("vcns") or []
        self._vcn_map = {}
        self._vcn_info_map: dict[str, dict] = {}
        preferred_vcn_id = self.meta.get("preferred_vcn_id") or ""
        preferred_vcn_label = ""
        for v in vcns:
            label = v.get("label") or v.get("display_name") or v["id"]
            if label in self._vcn_map:
                label = f"{label} [{v['id'][-6:]}]"
            self._vcn_map[label] = v["id"]
            self._vcn_info_map[label] = v
            if preferred_vcn_id and v.get("id") == preferred_vcn_id:
                preferred_vcn_label = label
        default_vcn = preferred_vcn_label or (list(self._vcn_map.keys())[0] if self._vcn_map else "")
        self.vars["vcn_label"] = tk.StringVar(value=default_vcn)
        self.vcn_combo = None  # hidden: always default VCN

        self._subnets_by_vcn = self.meta.get("subnets_by_vcn") or {}
        self._preferred_subnet_id = self.meta.get("preferred_subnet_id") or ""
        self._subnet_map: dict[str, str] = {}
        self._subnet_info_map: dict[str, dict] = {}
        self.vars["subnet_label"] = tk.StringVar(value="")
        self.subnet_combo = None  # hidden: always preferred/public default subnet
        self._reload_subnets()

        lab("Root 登录方式 *（选其一）")
        self.auth_mode = tk.StringVar(value="key")
        auth_row = Frm(body)
        auth_row.pack(fill="x", padx=8, pady=(2, 2))
        # Natural width only — avoid a full-row hit target next to the other option.
        Rad(auth_row, "root + SSH 公钥", self.auth_mode, "key", command=self._on_auth_change).pack(
            side="left", padx=(0, 8)
        )
        Rad(auth_row, "root + 服务器密码", self.auth_mode, "password", command=self._on_auth_change).pack(
            side="left", padx=(8, 0)
        )

        # key section
        self.key_section = Frm(body)
        Lbl(
            self.key_section,
            text="SSH 公钥 *（粘贴 / 选择文件 / 拖入 .pub）",
            fg=C.TEXT_DIM,
            font=C.FONT_SMALL,
        ).pack(fill="x", padx=8, pady=(5, 0))
        key_tools = Frm(self.key_section)
        key_tools.pack(fill="x", padx=8, pady=(0, 1))
        Btn(key_tools, "选择公钥文件…", command=self._pick_ssh_key_file).pack(side="left")
        Lbl(key_tools, text="id_ed25519.pub / id_rsa.pub", fg=C.TEXT_MUTE, font=C.FONT_SMALL).pack(
            side="left", padx=6
        )
        self.ssh_box = Txt(self.key_section, height=3, font=C.FIXED, wrap="none")
        self.ssh_box.pack(fill="x", padx=8, pady=(0, 1))

        # password section
        self.password_section = Frm(body)
        Lbl(
            self.password_section,
            text="root 密码 *（至少 12 位）",
            fg=C.TEXT_DIM,
            font=C.FONT_SMALL,
        ).pack(fill="x", padx=8, pady=(5, 0))
        password_row = Frm(self.password_section)
        password_row.pack(fill="x", padx=8, pady=(0, 1))
        self.vars["root_password"] = tk.StringVar()
        self.vars["root_password_confirm"] = tk.StringVar()
        self.password_entry = Ent(password_row, textvariable=self.vars["root_password"], show="")
        self.password_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.password_confirm_entry = Ent(
            password_row, textvariable=self.vars["root_password_confirm"], show=""
        )
        self.password_confirm_entry.pack(side="left", fill="x", expand=True, padx=(4, 0))
        Btn(password_row, "随机生成", command=self._generate_root_password, width=10).pack(
            side="left", padx=(6, 0)
        )
        Lbl(
            self.password_section,
            text="创建成功后写入标签 ocibot_root_password；能读实例的人都能看到该标签。",
            fg=C.TEXT_MUTE,
            font=C.FONT_SMALL,
            wraplength=wrap,
            justify="left",
        ).pack(fill="x", padx=8, pady=(0, 1))

        self._auth_anchor = Frm(body, height=1)
        self._auth_anchor.pack(fill="x")

        lab("Boot Volume 大小 GB（可选；留空≈47GB，填写≥50）")
        entry("boot_gb", "")
        self._vpu_map = {label: value for value, label in BOOT_VPU_PRESETS}
        lab("Boot Volume 性能（>10 需付费账号）")
        combo("boot_vpu_label", list(self._vpu_map), list(self._vpu_map)[0])

        # Pack checkboxes at natural width (no fill="x") so the clickable hit
        # area is only the indicator + label — not the whole dialog row.
        self.assign_public = tk.BooleanVar(value=True)
        self.public_check = Chk(body, "分配公网 IPv4（默认）", self.assign_public)
        self.public_check.pack(anchor="w", padx=8, pady=(8, 2))
        self.assign_ipv6 = tk.BooleanVar(value=False)
        self.ipv6_check = Chk(body, "同时分配 IPv6（可自动启用 VCN / Subnet IPv6）", self.assign_ipv6)
        self.ipv6_check.pack(anchor="w", padx=8, pady=(0, 2))

        self.as_retry = tk.BooleanVar(value=False)
        self.retry_check = Chk(body, "容量不足时加入自动重试（仅密钥模式，限速合规）", self.as_retry)
        self.retry_check.pack(anchor="w", padx=8, pady=(2, 2))
        self.retry_all_ads = tk.BooleanVar(value=False)
        self._ad_count = len(self.meta.get("ads") or [])
        self.retry_ads_check = Chk(
            body,
            f"重试时轮询该区域全部可用域（共 {self._ad_count} 个）",
            self.retry_all_ads,
        )
        self.retry_ads_check.pack(anchor="w", padx=8, pady=(0, 4))

        from app.scheduler import (
            DEFAULT_MAX_ATTEMPTS,
            DEFAULT_RETRY_INTERVAL_SEC,
            MAX_MAX_ATTEMPTS,
            MAX_RETRY_INTERVAL_SEC,
            MIN_RETRY_INTERVAL_SEC,
        )

        retry_row = Frm(body)
        retry_row.pack(fill="x", padx=8, pady=(0, 2))
        Lbl(retry_row, text=f"重试间隔秒(≥{MIN_RETRY_INTERVAL_SEC})", fg=C.TEXT_MUTE, font=C.FONT_SMALL).pack(
            side="left"
        )
        self.vars["retry_interval"] = tk.StringVar(value=str(DEFAULT_RETRY_INTERVAL_SEC))
        Ent(retry_row, textvariable=self.vars["retry_interval"], width=8).pack(side="left", padx=6)
        Lbl(retry_row, text=f"最大次数(1–{MAX_MAX_ATTEMPTS})", fg=C.TEXT_MUTE, font=C.FONT_SMALL).pack(
            side="left", padx=(12, 0)
        )
        self.vars["retry_max"] = tk.StringVar(value=str(DEFAULT_MAX_ATTEMPTS))
        Ent(retry_row, textvariable=self.vars["retry_max"], width=8).pack(side="left", padx=6)
        Lbl(
            body,
            text=(
                f"合规限速：间隔 {MIN_RETRY_INTERVAL_SEC}–{MAX_RETRY_INTERVAL_SEC} 秒（默认 {DEFAULT_RETRY_INTERVAL_SEC}），"
                f"次数上限默认 {DEFAULT_MAX_ATTEMPTS}；429 自动加长冷却。"
            ),
            wraplength=wrap,
            justify="left",
            fg=C.TEXT_MUTE,
            font=C.FONT_SMALL,
        ).pack(fill="x", padx=8, pady=(0, 2))
        Lbl(
            body,
            text="⚠ 将开放 Guest 防火墙与 OCI NSG 的全部入站/出站协议，请确认公网暴露风险。",
            wraplength=wrap,
            justify="left",
            fg="#b25000",
            font=C.FONT_SMALL,
        ).pack(fill="x", padx=8, pady=(2, 6))

        self._on_shape_change()
        self._on_auth_change()
        self._update_network_options()

        footer = Frm(self)
        footer.pack(fill="x", padx=10, pady=(2, 8))
        Btn(footer, "取消", command=self._on_cancel, width=8).pack(side="right", padx=(6, 0))
        BtnPrimary(footer, "创建", command=self._on_create, width=10).pack(side="right")

    def _compatible_shapes(self) -> list[dict]:
        image_label = self.vars.get("image_label").get() if self.vars.get("image_label") else ""
        image = self._image_info_map.get(image_label, {}) if hasattr(self, "_image_info_map") else {}
        image_blob = f"{image.get('label', '')} {image.get('display_name', '')}".lower()
        arm_image = "aarch64" in image_blob or "arm64" in image_blob
        free_shapes = [s for s in getattr(self, "_all_shapes", []) if str(s.get("shape", "")) in FREE_TIER_SHAPES]
        compatible = []
        for shape in free_shapes:
            shape_blob = f"{shape.get('shape', '')} {shape.get('processor_description', '')}".lower()
            arm_shape = "a1" in str(shape.get("shape", "")).lower() or "ampere" in shape_blob or " arm" in shape_blob
            if arm_shape == arm_image:
                compatible.append(shape)
        # Never fall back to wrong-arch free shapes — that only yields OCI launch errors.
        return compatible

    def _pick_image_for_arch(self, arch: str) -> None:
        """Select a Ubuntu image matching arm (aarch64) or x86 architecture."""
        want_arm = (arch or "").lower() == "arm"
        labels = list(self._image_map.keys()) if hasattr(self, "_image_map") else []
        if not labels:
            return
        current = self.vars.get("image_label").get() if self.vars.get("image_label") else ""
        current_info = self._image_info_map.get(current, {}) if hasattr(self, "_image_info_map") else {}
        current_blob = f"{current_info.get('label', '')} {current_info.get('display_name', '')}".lower()
        current_is_arm = "aarch64" in current_blob or "arm64" in current_blob
        if current and current_is_arm == want_arm:
            return
        for label in labels:
            info = self._image_info_map.get(label, {})
            blob = f"{info.get('label', '')} {info.get('display_name', '')}".lower()
            is_arm = "aarch64" in blob or "arm64" in blob
            if is_arm == want_arm:
                self.vars["image_label"].set(label)
                return

    def _apply_quick_preset(self, preset: dict) -> None:
        """Fill shape / flex size / boot from a free-tier quick config."""
        arch = str(preset.get("arch") or "arm")
        self._pick_image_for_arch(arch)
        # Rebuild shape list for the (possibly new) image arch.
        self._shape_map = {}
        for shape_info in self._compatible_shapes():
            label = shape_info.get("label") or shape_info["shape"]
            self._shape_map[label] = shape_info
        labels = list(self._shape_map) or [""]
        try:
            self.shape_combo.configure(values=labels)
        except Exception:
            pass
        target_shape = str(preset.get("shape") or "")
        shape_label = next(
            (lb for lb, info in self._shape_map.items() if str(info.get("shape") or "") == target_shape),
            None,
        )
        if not shape_label:
            # Fall back: match by substring (A1.Flex / E2.1.Micro in display label).
            shape_label = next((lb for lb in labels if target_shape.split(".")[-2:] and target_shape in lb), None)
            if not shape_label:
                shape_label = next((lb for lb in labels if "A1.Flex" in lb or "E2.1.Micro" in lb), labels[0] if labels else "")
        if shape_label:
            self.vars["shape_label"].set(shape_label)
        self._on_shape_change()
        ocpus = preset.get("ocpus")
        memory = preset.get("memory_in_gbs")
        if ocpus is not None:
            self.vars["ocpus"].set(str(int(ocpus) if float(ocpus) == int(ocpus) else ocpus))
        if memory is not None:
            self.vars["memory"].set(str(int(memory) if float(memory) == int(memory) else memory))
        boot = preset.get("boot_volume_size_in_gbs")
        if boot is not None and "boot_gb" in self.vars:
            self.vars["boot_gb"].set(str(int(boot)))
        vpu = preset.get("boot_volume_vpus_per_gb")
        if vpu is not None and "boot_vpu_label" in self.vars:
            vpu_label = next((lb for lb, val in self._vpu_map.items() if val == int(vpu)), None)
            if vpu_label:
                self.vars["boot_vpu_label"].set(vpu_label)
        hint = preset.get("hint") or preset.get("label") or ""
        if hasattr(self, "quick_hint") and hint:
            self.quick_hint.configure(text=f"已应用：{hint}")

    def _on_image_change(self) -> None:
        self._shape_map = {}
        for shape_info in self._compatible_shapes():
            label = shape_info.get("label") or shape_info["shape"]
            self._shape_map[label] = shape_info
        labels = list(self._shape_map) or [""]
        self.shape_combo.configure(values=labels)
        prefer = next((label for label in labels if "A1.Flex" in label), None)
        self.vars["shape_label"].set(prefer or labels[0])
        self._on_shape_change()

    def _on_shape_change(self) -> None:
        info = self._shape_map.get(self.vars.get("shape_label").get(), {}) if self.vars.get("shape_label") else {}
        flexible = bool(info.get("is_flexible", str(info.get("shape", "")).lower().endswith(".flex")))
        state = "normal" if flexible else "disabled"
        self.ocpu_entry.configure(state=state)
        self.memory_entry.configure(state=state)
        if not flexible:
            self.vars["ocpus"].set("")
            self.vars["memory"].set("")
            self.shape_hint.configure(text="固定 Shape：OCPU / 内存由型号决定")
        else:
            if not self.vars["ocpus"].get():
                self.vars["ocpus"].set("1")
            if not self.vars["memory"].get():
                self.vars["memory"].set("6")
            self.shape_hint.configure(
                text=f"Flex 范围：OCPU {info.get('min_ocpus') or '?'}–{info.get('max_ocpus') or '?'}；内存 {info.get('min_memory_in_gbs') or '?'}–{info.get('max_memory_in_gbs') or '?'} GB"
            )

    def _on_auth_change(self) -> None:
        password_mode = self.auth_mode.get() == "password"
        self.key_section.pack_forget()
        self.password_section.pack_forget()
        if password_mode:
            self.password_section.pack(fill="x", before=self._auth_anchor)
        else:
            self.key_section.pack(fill="x", before=self._auth_anchor)
        if hasattr(self, "retry_check"):
            self.retry_check.configure(state="disabled" if password_mode else "normal")
        if hasattr(self, "retry_ads_check"):
            self.retry_ads_check.configure(state="disabled" if password_mode else "normal")
        if password_mode:
            self.as_retry.set(False)
            self.retry_all_ads.set(False)
            # First switch into password mode: auto-fill a random password if empty.
            if not (self.vars["root_password"].get() or "").strip():
                self._generate_root_password()
        else:
            self.vars["root_password"].set("")
            self.vars["root_password_confirm"].set("")

    def _generate_root_password(self) -> None:
        """Fill both password fields with a strong random password (shown in clear text)."""
        pwd = generate_root_password(16)
        self.vars["root_password"].set(pwd)
        self.vars["root_password_confirm"].set(pwd)
        try:
            # Ensure the fields show the value in clear text for copy/paste.
            self.password_entry.configure(show="")
            self.password_confirm_entry.configure(show="")
            self.password_entry.selection_range(0, "end")
            self.password_entry.focus_set()
        except Exception:
            pass

    def _pick_ssh_key_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择 SSH 公钥文件",
            filetypes=[("SSH 公钥", "*.pub *.txt *"), ("All", "*.*")],
        )
        if path:
            self._load_ssh_key_from_path(path)

    def _load_ssh_key_from_path(self, path: str) -> None:
        p = Path(os.path.expanduser(str(path).strip().strip('"')))
        if not p.exists() or not p.is_file():
            messagebox.showerror("读取失败", f"文件不存在：\n{p}", parent=self)
            return
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取失败", str(exc), parent=self)
            return
        if "PRIVATE KEY" in text:
            messagebox.showwarning("这是私钥", "请选择 SSH 公钥（.pub），不要选私钥文件。", parent=self)
            return
        key_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if not key_line.startswith(("ssh-", "ecdsa-")):
            messagebox.showwarning("格式不正确", "文件内容看起来不是 SSH 公钥（应以 ssh-ed25519 / ssh-rsa 开头）。", parent=self)
            return
        try:
            self.ssh_box.delete("1.0", "end")
            self.ssh_box.insert("1.0", key_line)
        except Exception:
            pass

    def _setup_ssh_drop(self) -> None:
        if not _WINDND_AVAILABLE:
            return

        def _on_drop(files) -> None:
            paths: list[str] = []
            for f in files or []:
                if isinstance(f, bytes):
                    try:
                        paths.append(f.decode("utf-8"))
                    except UnicodeDecodeError:
                        paths.append(f.decode("gbk", errors="replace"))
                else:
                    paths.append(str(f))
            if paths:
                self.after(0, lambda: self._load_ssh_key_from_path(paths[0]))

        try:
            windnd.hook_dropfiles(self.ssh_box, func=_on_drop)
        except Exception:
            pass

    def _selected_subnet(self) -> dict:
        label = self.vars.get("subnet_label").get() if self.vars.get("subnet_label") else ""
        return self._subnet_info_map.get(label, {})

    def _update_network_options(self) -> None:
        subnet = self._selected_subnet()
        private = bool(subnet.get("prohibit_public_ip_on_vnic", False))
        self.public_check.configure(state="disabled" if private else "normal")
        if private:
            self.assign_public.set(False)
        self.ipv6_check.configure(state="normal")

    def _reload_subnets(self) -> None:
        vcn_label = self.vars.get("vcn_label")
        vcn_id = self._vcn_map.get(vcn_label.get() if vcn_label else "", "")
        subs = list(self._subnets_by_vcn.get(vcn_id, []) or [])
        # Prefer public subnets so new free-tier accounts get a usable default.
        subs.sort(key=lambda s: (1 if s.get("prohibit_public_ip_on_vnic") else 0, s.get("display_name") or ""))
        selected_ad = self.vars.get("ad").get() if self.vars.get("ad") else ""
        preferred_subnet_id = getattr(self, "_preferred_subnet_id", "") or ""

        def _matches_ad(s: dict, ad: str) -> bool:
            subnet_ad = (s.get("availability_domain") or "").strip()
            # Empty AD = regional subnet, usable from any AD.
            return (not subnet_ad) or (not ad) or subnet_ad == ad

        matched = [s for s in subs if _matches_ad(s, selected_ad)]
        # Hidden network UI: if the chosen AD has no subnet, auto-pick an AD that
        # has the preferred/public subnet instead of leaving subnet_label empty.
        if not matched and subs and selected_ad:
            prefer = next((s for s in subs if preferred_subnet_id and s.get("id") == preferred_subnet_id), None)
            fallback = prefer or next((s for s in subs if not s.get("prohibit_public_ip_on_vnic")), subs[0])
            fallback_ad = (fallback.get("availability_domain") or "").strip()
            if fallback_ad and self.vars.get("ad"):
                try:
                    self.vars["ad"].set(fallback_ad)
                    selected_ad = fallback_ad
                except Exception:
                    pass
            matched = [s for s in subs if _matches_ad(s, selected_ad)] or list(subs)

        self._subnet_map = {}
        self._subnet_info_map: dict[str, dict] = {}
        preferred_label = ""
        for s in matched:
            label = s.get("label") or s.get("display_name") or s["id"]
            if label in self._subnet_map:
                label = f"{label} [{s['id'][-6:]}]"
            self._subnet_map[label] = s["id"]
            self._subnet_info_map[label] = s
            if preferred_subnet_id and s.get("id") == preferred_subnet_id:
                preferred_label = label
        labels = list(self._subnet_map.keys()) or [""]
        subnet_combo = getattr(self, "subnet_combo", None)
        if subnet_combo is not None:
            try:
                subnet_combo.configure(values=labels)
            except Exception:
                pass
        if "subnet_label" in self.vars:
            self.vars["subnet_label"].set(preferred_label or labels[0])
        if hasattr(self, "public_check"):
            self._update_network_options()

    def _on_cancel(self) -> None:
        if hasattr(self, "vars"):
            for key in ("root_password", "root_password_confirm"):
                if key in self.vars:
                    self.vars[key].set("")
        super()._on_cancel()

    def _on_create(self) -> None:
        try:
            name = self.vars["display_name"].get().strip()
            if not name:
                raise ValueError("显示名称不能为空")
            comp = self._comp_label_to_id.get(self.vars["compartment_label"].get(), "")
            if not comp:
                # Hidden default path: use account default when UI no longer exposes compartment.
                comp = str(self.meta.get("default_compartment") or "").strip()
            if not comp:
                raise ValueError("未找到默认 Compartment，请检查租户配置")
            ad = self.vars["ad"].get().strip()
            if not ad:
                raise ValueError("请选择 Availability Domain")
            image_id = self._image_map.get(self.vars["image_label"].get(), "")
            if not image_id:
                raise ValueError("请选择镜像")
            shape_info = self._shape_map.get(self.vars["shape_label"].get(), {})
            shape = str(shape_info.get("shape") or "").strip()
            if not shape:
                raise ValueError("请选择与当前镜像架构匹配的 Shape（无可用免费型号）")
            # Ensure subnet map is current for the selected AD (hidden network UI).
            self._reload_subnets()
            subnet = self._selected_subnet()
            subnet_id = self._subnet_map.get(self.vars["subnet_label"].get(), "")
            if not subnet_id:
                raise ValueError(
                    "当前可用域下没有可用 Subnet。\n"
                    "请切换 Availability Domain，或在控制台为默认 VCN 创建公网 Subnet。"
                )
            flexible = bool(shape_info.get("is_flexible", shape.lower().endswith(".flex")))
            ocpus = float(self.vars["ocpus"].get()) if flexible else None
            memory = float(self.vars["memory"].get()) if flexible else None
            if flexible:
                min_ocpus = shape_info.get("min_ocpus")
                max_ocpus = shape_info.get("max_ocpus")
                min_memory = shape_info.get("min_memory_in_gbs")
                max_memory = shape_info.get("max_memory_in_gbs")
                min_per_ocpu = shape_info.get("min_gbs_per_ocpu")
                max_per_ocpu = shape_info.get("max_gbs_per_ocpu")
                if min_ocpus is not None and ocpus < float(min_ocpus):
                    raise ValueError(f"OCPU 不能低于 {min_ocpus}")
                if max_ocpus is not None and ocpus > float(max_ocpus):
                    raise ValueError(f"OCPU 不能高于 {max_ocpus}")
                if min_memory is not None and memory < float(min_memory):
                    raise ValueError(f"内存不能低于 {min_memory} GB")
                if max_memory is not None and memory > float(max_memory):
                    raise ValueError(f"内存不能高于 {max_memory} GB")
                memory_per_ocpu = memory / ocpus
                if min_per_ocpu is not None and memory_per_ocpu < float(min_per_ocpu):
                    raise ValueError(f"每 OCPU 内存不能低于 {min_per_ocpu} GB")
                if max_per_ocpu is not None and memory_per_ocpu > float(max_per_ocpu):
                    raise ValueError(f"每 OCPU 内存不能高于 {max_per_ocpu} GB")
            boot = None
            if self.vars["boot_gb"].get().strip():
                boot = int(float(self.vars["boot_gb"].get().strip()))
                if not 50 <= boot <= 32768:
                    raise ValueError("Boot Volume 大小必须在 50–32768 GB 之间")
            auth_mode = self.auth_mode.get()
            ssh_key = self.ssh_box.get("1.0", "end").strip() if auth_mode == "key" else ""
            if auth_mode == "key" and "\n" in ssh_key:
                raise ValueError("每次只能填写一条 SSH 公钥")
            password = self.vars["root_password"].get() if auth_mode == "password" else ""
            if auth_mode == "key" and not ssh_key:
                raise ValueError("密钥模式必须填写 SSH 公钥")
            if auth_mode == "password":
                if len(password) < 12 or not password.strip():
                    raise ValueError("root 密码至少需要 12 个非空字符")
                if password != self.vars["root_password_confirm"].get():
                    raise ValueError("两次输入的 root 密码不一致")
            if self.assign_public.get() and subnet.get("prohibit_public_ip_on_vnic"):
                raise ValueError("当前 Subnet 禁止分配公网 IPv4")
            vcn_info = self._vcn_info_map.get(self.vars["vcn_label"].get(), {})
            payload = {
                "display_name": name,
                "compartment_id": comp,
                "availability_domain": ad,
                "shape": shape,
                "image_id": image_id,
                "subnet_id": subnet_id,
                "vcn_id": vcn_info.get("id") or self._vcn_map.get(self.vars["vcn_label"].get(), ""),
                "network_compartment_id": vcn_info.get("compartment_id") or subnet.get("compartment_id") or comp,
                "ssh_public_key": ssh_key,
                "auth_mode": auth_mode,
                "ocpus": ocpus,
                "memory_in_gbs": memory,
                "assign_public_ip": bool(self.assign_public.get()),
                "assign_ipv6_ip": bool(self.assign_ipv6.get()),
                "boot_volume_size_in_gbs": boot,
                "boot_volume_vpus_per_gb": self._vpu_map[self.vars["boot_vpu_label"].get()],
                "open_guest_firewall": True,
            }
            from app.scheduler import (
                DEFAULT_MAX_ATTEMPTS,
                DEFAULT_RETRY_INTERVAL_SEC,
                clamp_max_attempts,
                clamp_retry_interval,
            )

            retry_interval = clamp_retry_interval(self.vars["retry_interval"].get() or DEFAULT_RETRY_INTERVAL_SEC)
            retry_max = clamp_max_attempts(self.vars["retry_max"].get() or DEFAULT_MAX_ATTEMPTS)
            # Reflect clamped values so the user sees what will actually run.
            self.vars["retry_interval"].set(str(retry_interval))
            self.vars["retry_max"].set(str(retry_max))
            retry_ads: list[str] = []
            if auth_mode == "key" and bool(self.retry_all_ads.get()):
                ads = self.meta.get("ads") or []
                retry_ads = [
                    a.get("name") if isinstance(a, dict) else str(a)
                    for a in ads
                    if (a.get("name") if isinstance(a, dict) else a)
                ]
            as_retry = bool(self.as_retry.get()) if auth_mode == "key" else False
            vpu_label = self.vars["boot_vpu_label"].get()
            image_label = self.vars["image_label"].get()
            confirm_rows = format_launch_confirm_rows(
                display_name=name,
                shape=shape,
                ocpus=ocpus,
                memory_in_gbs=memory,
                boot_volume_size_in_gbs=boot,
                boot_volume_vpus_per_gb=payload["boot_volume_vpus_per_gb"],
                boot_vpu_label=vpu_label,
                image_label=image_label,
                availability_domain=ad,
                auth_mode=auth_mode,
                assign_public_ip=bool(self.assign_public.get()),
                assign_ipv6_ip=bool(self.assign_ipv6.get()),
                as_retry=as_retry,
                retry_interval=retry_interval,
                retry_max=retry_max,
                free_tier_tag=free_tier_tag(shape),
            )
            if not ask_launch_confirm(
                self,
                confirm_rows,
                note=(
                    "创建后将开放 Guest 防火墙与 NSG 的全部入站/出站协议；"
                    "选择 IPv6 时会自动启用 IPv6 与 ::/0 公网路由。"
                    "这会将实例上的所有服务暴露到公网。"
                ),
            ):
                return
            result = {
                "payload": payload,
                "secrets": {"root_password": password},
                "as_retry": as_retry,
                "retry_interval": retry_interval,
                "retry_max": retry_max,
                "retry_ads": retry_ads,
            }
            self.vars["root_password"].set("")
            self.vars["root_password_confirm"].set("")
            self._finish(result)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("参数错误", str(exc), parent=self)


# ---------------------------------------------------------------------------
# NSG firewall management
# ---------------------------------------------------------------------------


_FW_DIRECTION_CHOICES = [
    ("入站（允许外部访问本机）", "INGRESS"),
    ("出站（允许本机访问外部）", "EGRESS"),
]
_FW_PROTOCOL_CHOICES = [
    ("全部协议", "all"),
    ("TCP", "6"),
    ("UDP", "17"),
    ("ICMPv4（ping 等）", "1"),
    ("ICMPv6", "58"),
]


def _format_firewall_rule_line(rule: dict) -> str:
    direction = rule.get("direction_label") or {
        "INGRESS": "入站",
        "EGRESS": "出站",
    }.get(str(rule.get("direction", "")).upper(), rule.get("direction") or "未知")
    protocol = rule.get("protocol_label") or {
        "all": "全部协议",
        "6": "TCP",
        "17": "UDP",
        "1": "ICMPv4",
        "58": "ICMPv6",
    }.get(str(rule.get("protocol", "")).lower(), f"协议 {rule.get('protocol') or '?'}")
    cidr = rule.get("cidr") or "—"
    port = rule.get("port") or "全部"
    desc = (rule.get("description") or "").strip()
    state = "无状态" if rule.get("stateless") else "有状态"
    base = f"【{direction}】{protocol} · 地址 {cidr} · 端口 {port} · {state}"
    return f"{base} · {desc}" if desc else base


class FirewallRuleDialog(BaseDialog):
    def __init__(self, master, groups: list[dict]):
        self.groups = groups
        super().__init__(master, title="新增防火墙规则", width=540, height=500)
        body = Frm(self)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        self.nsg_map = {f"{g['display_name']}（…{g['id'][-8:]}）": g["id"] for g in groups}
        nsg_labels = list(self.nsg_map) or [""]

        Lbl(body, text="网络安全组（NSG）").pack(anchor="w")
        self.nsg = Combo(body, nsg_labels)
        self.nsg.set(nsg_labels[0])
        self.nsg.pack(fill="x", pady=(0, 6))

        Lbl(body, text="方向").pack(anchor="w")
        self._direction_map = {label: value for label, value in _FW_DIRECTION_CHOICES}
        direction_labels = [label for label, _ in _FW_DIRECTION_CHOICES]
        self.direction = Combo(body, direction_labels)
        self.direction.set(direction_labels[0])
        self.direction.pack(fill="x", pady=(0, 6))

        Lbl(body, text="协议").pack(anchor="w")
        self._protocol_map = {label: value for label, value in _FW_PROTOCOL_CHOICES}
        protocol_labels = [label for label, _ in _FW_PROTOCOL_CHOICES]
        self.protocol = Combo(body, protocol_labels)
        self.protocol.set("TCP")
        self.protocol.pack(fill="x", pady=(0, 6))

        Lbl(body, text="地址范围（CIDR），例如 0.0.0.0/0 或 ::/0").pack(anchor="w")
        self.cidr = Ent(body)
        self.cidr.insert(0, "0.0.0.0/0")
        self.cidr.pack(fill="x", pady=(0, 6))

        Lbl(body, text="端口或范围，例如 22 或 8000-8080；留空表示全部端口").pack(anchor="w")
        self.ports = Ent(body)
        self.ports.pack(fill="x", pady=(0, 6))

        Lbl(body, text="描述（可选）").pack(anchor="w")
        self.description = Ent(body)
        self.description.pack(fill="x", pady=(0, 6))

        self.stateless = tk.BooleanVar(value=False)
        Chk(body, "无状态（Stateless，一般保持不勾选）", self.stateless).pack(anchor="w", pady=6)
        Lbl(
            body,
            text="提示：入站规则控制「谁可以访问本机」；出站规则控制「本机可以访问哪里」。",
            fg=C.TEXT_MUTE,
            wraplength=500,
        ).pack(anchor="w", pady=(0, 6))
        BtnPrimary(body, "新增规则", command=self._submit).pack(fill="x", pady=8)

    def _submit(self) -> None:
        try:
            if not self.nsg.get() or self.nsg.get() not in self.nsg_map:
                raise ValueError("请选择网络安全组")
            direction = self._direction_map.get(self.direction.get(), self.direction.get())
            protocol = self._protocol_map.get(self.protocol.get(), "all")
            pmin = pmax = None
            raw = self.ports.get().strip()
            if raw:
                parts = raw.split("-", 1)
                pmin = int(parts[0].strip())
                pmax = int(parts[-1].strip())
            cidr = self.cidr.get().strip()
            if not cidr:
                raise ValueError("请填写地址范围（CIDR）")
            if protocol in {"6", "17"} and not raw:
                # Allow empty ports = all ports for TCP/UDP
                pass
            spec = FirewallRuleSpec(
                direction,
                protocol,
                cidr,
                pmin,
                pmax,
                bool(self.stateless.get()),
                self.description.get().strip(),
            )
            spec.validate()
            self._finish({"nsg_id": self.nsg_map[self.nsg.get()], "spec": spec})
        except Exception as exc:
            messagebox.showwarning("规则错误", str(exc), parent=self)


class FirewallManagerDialog(BaseDialog):
    def __init__(self, master, state: dict):
        self.state = state
        super().__init__(master, title="实例防火墙（网络安全组 NSG）", width=920, height=640)
        Lbl(
            self,
            text=(
                "这里管理实例网卡挂载的「网络安全组（NSG）」规则。"
                "共享 NSG 的改动可能影响其他实例；子网安全列表（Security List）不会在此修改。"
            ),
            fg="#b25000",
            wraplength=880,
        ).pack(fill="x", padx=12, pady=10)

        # Summary line
        groups = list(state.get("groups") or [])
        has_ipv6 = bool(state.get("has_ipv6"))
        public_ip = (state.get("public_ipv4") or "").strip() or "无"
        private_ip = (state.get("private_ipv4") or "").strip() or "—"
        ipv6_text = "、".join(state.get("ipv6_addresses") or []) or ("无" if not has_ipv6 else "—")
        summary = (
            f"已关联 {len(groups)} 个网络安全组  ·  公网 IPv4：{public_ip}  ·  "
            f"私网：{private_ip}  ·  IPv6：{ipv6_text}"
        )
        Lbl(self, text=summary, fg=C.TEXT_DIM, wraplength=880).pack(fill="x", padx=12, pady=(0, 6))

        scroll = ScrollFrame(self)
        scroll.pack(fill="both", expand=True, padx=12, pady=4)
        box = scroll.inner
        self.selected: dict[tuple[str, str], tk.BooleanVar] = {}

        if not groups:
            Lbl(
                box,
                text=(
                    "当前实例没有挂载任何网络安全组（NSG）。\n\n"
                    "可选操作：\n"
                    "· 点「创建实例专属网络安全组」新建并挂载一个空组后自行加规则\n"
                    "· 点「一键开启所有端口」自动创建并写入全开放规则\n\n"
                    "说明：最终能否连通还取决于子网安全列表与路由；本界面只改 NSG。"
                ),
                fg=C.TEXT_DIM,
                wraplength=860,
                justify="left",
            ).pack(anchor="w", pady=12, padx=4)
        else:
            for group in groups:
                managed = "本工具管理" if group.get("is_managed") else "外部/共享"
                header = f"{group.get('display_name') or '未命名'}  ·  {managed}  ·  …{str(group.get('id', ''))[-12:]}"
                Lbl(box, text=header, font=C.FONT_BOLD).pack(anchor="w", pady=(10, 2))
                rules = list(group.get("rules") or [])
                if not rules:
                    Lbl(box, text="（暂无规则 — 默认拒绝未允许的流量）", fg=C.TEXT_MUTE).pack(
                        anchor="w", padx=12, pady=2
                    )
                for rule in rules:
                    var = tk.BooleanVar(value=False)
                    text = _format_firewall_rule_line(rule)
                    Chk(box, text, var).pack(anchor="w", padx=12, pady=1)
                    rid = rule.get("id") or ""
                    if rid:
                        self.selected[(group["id"], rid)] = var

        bar = Frm(self)
        bar.pack(fill="x", padx=12, pady=10)
        if groups:
            Btn(bar, "新增规则", command=lambda: self._finish({"action": "add"})).pack(side="left", padx=4)
            Btn(bar, "删除选中", command=self._delete, fg="#d70015").pack(side="left", padx=4)
        else:
            Btn(bar, "创建实例专属网络安全组", command=lambda: self._finish({"action": "create_nsg"})).pack(
                side="left", padx=4
            )
        # Always available: clear existing rules (if any) then open all ports.
        # IPv6 is included only when the instance already has an IPv6 address.
        open_label = "一键开启所有端口（IPv4+IPv6）" if has_ipv6 else "一键开启所有端口（仅 IPv4）"
        Btn(bar, open_label, command=lambda: self._finish({"action": "open_all"}), fg="#d70015").pack(
            side="left", padx=4
        )
        Btn(bar, "关闭", command=self._on_cancel).pack(side="right", padx=4)
        Btn(bar, "刷新", command=lambda: self._finish({"action": "refresh"})).pack(side="right", padx=4)

    def _delete(self) -> None:
        grouped: dict[str, list[str]] = {}
        for (nsg_id, rule_id), var in self.selected.items():
            if var.get():
                grouped.setdefault(nsg_id, []).append(rule_id)
        if not grouped:
            messagebox.showinfo("提示", "请先勾选要删除的规则", parent=self)
            return
        self._finish({"action": "delete", "rules": grouped})


# ---------------------------------------------------------------------------
# Schedule editor
# ---------------------------------------------------------------------------


class ScheduleEditorDialog(BaseDialog):
    WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    def __init__(self, master, tenants: list[TenantConfig], job=None, instances: Optional[list] = None):
        from app.scheduler import ScheduleJob
        import uuid as _uuid

        self._tenants = tenants
        self._instances = instances or []
        self._editing = job
        super().__init__(master, title="定时开关机" if job else "新建定时任务", width=540, height=560)
        self._build(job or ScheduleJob(id=str(_uuid.uuid4()), name="下班关机", tenant_id=tenants[0].id if tenants else ""))

    def _build(self, job) -> None:
        scroll = ScrollFrame(self)
        scroll.pack(fill="both", expand=True, padx=12, pady=12)
        body = scroll.inner

        def lab(t):
            Lbl(body, text=t, fg=C.TEXT_DIM).pack(fill="x", padx=6, pady=(8, 1))

        lab("任务名称")
        self.name_var = tk.StringVar(value=job.name)
        Ent(body, textvariable=self.name_var).pack(fill="x", padx=6)

        lab("租户")
        tmap = {t.name: t.id for t in self._tenants}
        self._tmap = tmap
        rev = {t.id: t.name for t in self._tenants}
        self.tenant_var = tk.StringVar(value=rev.get(job.tenant_id, next(iter(tmap), "")))
        Combo(body, list(tmap.keys()) or [""], textvariable=self.tenant_var).pack(fill="x", padx=6)

        lab("操作")
        from app.oci_client import POWER_ACTIONS

        labels = [f"{k} — {v}" for k, v in POWER_ACTIONS.items() if k in ("START", "SOFTSTOP", "STOP", "SOFTRESET", "RESET")]
        self._action_map = {f"{k} — {v}": k for k, v in POWER_ACTIONS.items()}
        current_label = next((lb for lb, a in self._action_map.items() if a == job.action), labels[0] if labels else "")
        self.action_var = tk.StringVar(value=current_label)
        Combo(body, labels, textvariable=self.action_var).pack(fill="x", padx=6)

        lab("每天时间（本地 HH:MM）")
        self.time_var = tk.StringVar(value=job.time_of_day or "22:00")
        Ent(body, textvariable=self.time_var).pack(fill="x", padx=6)

        lab("星期")
        self.day_vars = []
        days = Frm(body)
        days.pack(fill="x", padx=6, pady=4)
        selected = set(job.weekdays or [])
        for i, name in enumerate(self.WEEKDAY_LABELS):
            var = tk.BooleanVar(value=i in selected)
            self.day_vars.append(var)
            Chk(days, name, var, width=6).grid(row=i // 4, column=i % 4, sticky="w", padx=2, pady=1)

        lab("目标实例（留空=该租户全部非终止实例）")
        self.inst_box = Txt(body, height=4)
        self.inst_box.pack(fill="x", padx=6)
        if job.instance_ids:
            self.inst_box.insert("1.0", "\n".join(job.instance_ids))
        Lbl(body, text="每行一个 Instance OCID；也可只填当前选中实例的 OCID。", fg=C.TEXT_MUTE).pack(anchor="w", padx=6, pady=4)

        self.enabled_var = tk.BooleanVar(value=job.enabled)
        Chk(body, "启用", self.enabled_var).pack(anchor="w", padx=6, pady=8)

        footer = Frm(self)
        footer.pack(fill="x", padx=12, pady=(0, 12))
        Btn(footer, "取消", command=self._on_cancel, width=8).pack(side="right", padx=(6, 0))
        BtnPrimary(footer, "保存", command=self._on_save, width=8).pack(side="right")

    def _on_save(self) -> None:
        from app.scheduler import ScheduleJob
        import re

        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("校验", "名称不能为空", parent=self)
            return
        tid = self._tmap.get(self.tenant_var.get(), "")
        if not tid:
            messagebox.showwarning("校验", "请选择租户", parent=self)
            return
        action = self._action_map.get(self.action_var.get(), "SOFTSTOP")
        time_of_day = self.time_var.get().strip()
        if not re.fullmatch(r"\d{1,2}:\d{2}", time_of_day):
            messagebox.showwarning("校验", "时间格式应为 HH:MM", parent=self)
            return
        hh, mm = time_of_day.split(":")
        time_of_day = f"{int(hh):02d}:{int(mm):02d}"
        weekdays = [i for i, v in enumerate(self.day_vars) if v.get()]
        if not weekdays:
            messagebox.showwarning("校验", "请至少选择一个星期", parent=self)
            return
        ids = [line.strip() for line in self.inst_box.get("1.0", "end").splitlines() if line.strip()]
        import uuid
        from app.scheduler import _utc_now_iso

        base = self._editing
        job = ScheduleJob(
            id=base.id if base else str(uuid.uuid4()),
            name=name,
            tenant_id=tid,
            enabled=bool(self.enabled_var.get()),
            time_of_day=time_of_day,
            weekdays=weekdays,
            action=action,
            instance_ids=ids,
            last_run_date=base.last_run_date if base else "",
            created_at=base.created_at if base else _utc_now_iso(),
        )
        self._finish(job)


class JobsCenterDialog(BaseDialog):
    """Manage schedule jobs and capacity-retry jobs."""

    def __init__(self, master, job_store, tenants: list[TenantConfig], on_changed: Optional[Callable] = None, on_delete_retry: Optional[Callable] = None):
        self.job_store = job_store
        self.tenants = tenants
        self.on_changed = on_changed
        self.on_delete_retry = on_delete_retry
        self._tenant_name = {t.id: t.name for t in tenants}
        super().__init__(master, title="任务中心 — 定时任务 / 容量重试", width=820, height=600)
        self._build()

    def _build(self) -> None:
        bar = Frm(self)
        bar.pack(fill="x", padx=12, pady=(12, 4))
        Lbl(bar, text="任务中心", font=C.FONT_TITLE).pack(side="left")
        Btn(bar, "新建定时", command=self._add_schedule, width=9).pack(side="right", padx=4)
        Btn(bar, "刷新", command=self._reload, width=6).pack(side="right", padx=4)

        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=6)
        sched_tab = Frm(self.tabs)
        retry_tab = Frm(self.tabs)
        self.tabs.add(sched_tab, text="定时开关机")
        self.tabs.add(retry_tab, text="容量重试")

        sched_scroll = ScrollFrame(sched_tab)
        sched_scroll.pack(fill="both", expand=True, padx=4, pady=4)
        self.sched_list = sched_scroll.inner
        retry_scroll = ScrollFrame(retry_tab)
        retry_scroll.pack(fill="both", expand=True, padx=4, pady=4)
        self.retry_list = retry_scroll.inner
        self._reload()

        Btn(self, "关闭", command=self._on_cancel, width=8).pack(pady=(0, 12))

    def _reload(self) -> None:
        for w in self.sched_list.winfo_children():
            w.destroy()
        for w in self.retry_list.winfo_children():
            w.destroy()

        for job in self.job_store.list_schedules():
            self._sched_row(job)
        if not self.job_store.list_schedules():
            Lbl(self.sched_list, text="暂无定时任务", fg=C.TEXT_MUTE).pack(pady=20)

        for job in self.job_store.list_retries():
            self._retry_row(job)
        if not self.job_store.list_retries():
            Lbl(self.retry_list, text="暂无容量重试任务\n可在「创建实例」时勾选自动重试", fg=C.TEXT_MUTE, justify="center").pack(pady=20)

    def _sched_row(self, job) -> None:
        card = Group(self.sched_list)
        card.pack(fill="x", pady=4, padx=4)
        top = Frm(card, bg=C.WINDOW)
        top.pack(fill="x", padx=8, pady=(6, 1))
        Lbl(top, text=job.name, bg=C.WINDOW, font=C.FONT_BOLD).pack(side="left")
        Lbl(top, text="启用" if job.enabled else "停用", bg=C.WINDOW, fg="#1e8e3e" if job.enabled else C.TEXT_MUTE).pack(side="right")
        days = ",".join(["一二三四五六日"[d] for d in sorted(job.weekdays or [])])
        Lbl(card, text=f"{self._tenant_name.get(job.tenant_id, job.tenant_id[:8])}  ·  {job.action}  ·  {job.time_of_day}  ·  周{days}", bg=C.WINDOW, fg=C.TEXT_MUTE).pack(fill="x", padx=10)
        btns = Frm(card, bg=C.WINDOW)
        btns.pack(fill="x", padx=6, pady=6)
        Btn(btns, "编辑", command=lambda j=job: self._edit_schedule(j), width=6).pack(side="left", padx=2)
        Btn(btns, "启用" if not job.enabled else "停用", command=lambda j=job: self._toggle_schedule(j), width=6).pack(side="left", padx=2)
        Btn(btns, "删除", command=lambda j=job: self._del_schedule(j), width=6, fg="#d70015").pack(side="right", padx=2)

    def _retry_row(self, job) -> None:
        card = Group(self.retry_list)
        card.pack(fill="x", pady=4, padx=4)
        top = Frm(card, bg=C.WINDOW)
        top.pack(fill="x", padx=8, pady=(6, 1))
        Lbl(top, text=job.name, bg=C.WINDOW, font=C.FONT_BOLD).pack(side="left")
        color = {"running": "#b25000", "success": "#1e8e3e", "failed": "#d70015", "stopped": C.TEXT_MUTE, "idle": C.ACCENT}.get(job.status, C.TEXT_MUTE)
        Lbl(top, text=job.status, bg=C.WINDOW, fg=color).pack(side="right")
        payload = job.launch_payload or {}
        Lbl(
            card,
            text=f"{self._tenant_name.get(job.tenant_id, '?')}  ·  {payload.get('shape', '?')}  ·  {payload.get('auth_mode', 'key')}"
            + f"  ·  IPv4 {'是' if payload.get('assign_public_ip', True) else '否'} / IPv6 {'是' if payload.get('assign_ipv6_ip') else '否'}"
            + f"  ·  VPU {payload.get('boot_volume_vpus_per_gb', 10)}  ·  尝试 {job.attempts}"
            + f"/{job.max_attempts or '—'}"
            + f"  ·  间隔 {job.interval_sec}s",
            bg=C.WINDOW,
            fg=C.TEXT_MUTE,
            wraplength=760,
            justify="left",
        ).pack(fill="x", padx=10)
        cooldown = getattr(job, "cooldown_until", "") or ""
        if cooldown and job.status in ("idle", "running"):
            Lbl(card, text=f"冷却至：{cooldown}", bg=C.WINDOW, fg="#b25000").pack(fill="x", padx=10)
        if job.last_error:
            Lbl(card, text=job.last_error[:120], bg=C.WINDOW, fg="#d70015", wraplength=760, justify="left").pack(fill="x", padx=10)
        if job.success_instance_id:
            Lbl(card, text=f"实例: {job.success_instance_id}", bg=C.WINDOW, fg="#1e8e3e").pack(fill="x", padx=10)
        btns = Frm(card, bg=C.WINDOW)
        btns.pack(fill="x", padx=6, pady=6)
        if job.status not in ("success", "failed"):
            label = "继续" if job.status in ("stopped", "idle") else "暂停"
            Btn(btns, label, command=lambda j=job: self._toggle_retry(j), width=6).pack(side="left", padx=2)
        Btn(btns, "删除", command=lambda j=job: self._del_retry(j), width=6, fg="#d70015").pack(side="right", padx=2)

    def _add_schedule(self) -> None:
        if not self.tenants:
            messagebox.showinfo("提示", "请先添加租户", parent=self)
            return
        dlg = ScheduleEditorDialog(self, self.tenants)
        self.wait_window(dlg)
        if dlg.result:
            self.job_store.upsert_schedule(dlg.result)
            self._reload()
            if self.on_changed:
                self.on_changed()

    def _edit_schedule(self, job) -> None:
        dlg = ScheduleEditorDialog(self, self.tenants, job=job)
        self.wait_window(dlg)
        if dlg.result:
            self.job_store.upsert_schedule(dlg.result)
            self._reload()
            if self.on_changed:
                self.on_changed()

    def _toggle_schedule(self, job) -> None:
        job.enabled = not job.enabled
        self.job_store.upsert_schedule(job)
        self._reload()
        if self.on_changed:
            self.on_changed()

    def _del_schedule(self, job) -> None:
        if messagebox.askyesno("删除", f"删除定时任务「{job.name}」？", parent=self):
            self.job_store.delete_schedule(job.id)
            self._reload()
            if self.on_changed:
                self.on_changed()

    def _toggle_retry(self, job) -> None:
        if job.status in ("running", "idle") and job.enabled:
            job.enabled = False
            job.status = "stopped"
        else:
            job.enabled = True
            if job.status in ("stopped", "failed", "success"):
                if job.status == "success":
                    return
                job.status = "idle"
                job.last_error = ""
        self.job_store.upsert_retry(job)
        self._reload()
        if self.on_changed:
            self.on_changed()

    def _del_retry(self, job) -> None:
        if messagebox.askyesno("删除", f"删除重试任务「{job.name}」？\n\n未使用的 ocibot NSG 也将尝试清理。", parent=self):
            if self.on_delete_retry:
                self.on_delete_retry(job)
            else:
                self.job_store.delete_retry(job.id)
            self._reload()
            if self.on_changed:
                self.on_changed()


# ---------------------------------------------------------------------------
# Password prompt (encrypted backup / restore)
# ---------------------------------------------------------------------------


class PasswordPromptDialog(BaseDialog):
    """Prompt for a password, optionally with a confirmation field."""

    def __init__(self, master, title: str, label: str, confirm: bool = False, minimum: int = 6):
        self._confirm = confirm
        self._minimum = minimum
        super().__init__(master, title=title, width=460, height=240 if confirm else 190)
        wrap = Frm(self)
        wrap.pack(fill="both", expand=True, padx=14, pady=12)
        Lbl(wrap, text=label, wraplength=420, justify="left").pack(anchor="w")
        self.pw_var = tk.StringVar()
        entry = Ent(wrap, textvariable=self.pw_var, show="*")
        entry.pack(fill="x", pady=(6, 4))
        entry.focus_set()
        self.confirm_var = tk.StringVar()
        if confirm:
            Lbl(wrap, text="再次输入以确认：", fg=C.TEXT_MUTE).pack(anchor="w", pady=(4, 1))
            confirm_entry = Ent(wrap, textvariable=self.confirm_var, show="*")
            confirm_entry.pack(fill="x", pady=(0, 4))
            confirm_entry.bind("<Return>", lambda _e: self._on_ok())
        else:
            entry.bind("<Return>", lambda _e: self._on_ok())
        self.show_var = tk.BooleanVar(value=False)
        Chk(wrap, "显示密码", self.show_var, command=lambda: self._toggle_show(entry)).pack(anchor="w")
        footer = Frm(wrap)
        footer.pack(fill="x", side="bottom", pady=(14, 0))
        Btn(footer, "取消", command=self._on_cancel, width=8).pack(side="right", padx=(6, 0))
        BtnPrimary(footer, "确定", command=self._on_ok, width=8).pack(side="right")
        self._show_entries = [entry]
        if confirm:
            self._show_entries.append(confirm_entry)

    def _toggle_show(self, *_widgets) -> None:
        show = "" if self.show_var.get() else "*"
        for e in self._show_entries:
            try:
                e.configure(show=show)
            except tk.TclError:
                pass

    def _on_ok(self) -> None:
        pw = self.pw_var.get()
        error = validate_zip_password(
            pw,
            confirm=self.confirm_var.get() if self._confirm else None,
            minimum=self._minimum,
        )
        if error:
            messagebox.showwarning("密码不符合要求", error, parent=self)
            return
        self._finish(pw)


# ---------------------------------------------------------------------------
# Oracle password expiry reminder
# ---------------------------------------------------------------------------


class PasswordExpiryDialog(BaseDialog):
    """Configure a local reminder for the Oracle console password expiry.

    Oracle requires periodic password changes; a lapsed password can lock the
    account or trigger reclamation of free resources. This tracks the last
    change date and a customizable validity period (default 120 days) locally
    and returns ``{"password_changed_at", "password_expiry_days"}``.
    """

    def __init__(self, master, *, tenant_name: str, changed_at: str, expiry_days: int, created_at: str):
        super().__init__(master, title=f"密码到期提醒 — {tenant_name}", width=480, height=400)
        body = Frm(self)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        Lbl(
            body,
            text="甲骨文要求定期修改登录密码，否则账号或免费资源可能被回收。此处仅在本机提醒，"
            "不会真正修改甲骨文密码——到期后请自行登录 OCI 控制台修改，再回来点「设为今天」。",
            fg=C.TEXT_MUTE, wraplength=440, justify="left",
        ).pack(anchor="w", pady=(0, 8))

        Lbl(body, text="上次修改密码日期（YYYY-MM-DD）").pack(anchor="w")
        date_row = Frm(body)
        date_row.pack(fill="x", pady=(2, 8))
        baseline = (changed_at or created_at or "")[:10]
        if not baseline:
            baseline = self._today_str()
        self.date_var = tk.StringVar(value=baseline)
        Ent(date_row, textvariable=self.date_var, width=16).pack(side="left")
        Btn(date_row, "设为今天（我刚改过密码）", command=self._set_today).pack(side="left", padx=6)

        Lbl(body, text="有效期天数（默认 120；0 = 关闭提醒）").pack(anchor="w")
        self.days_var = tk.StringVar(value=str(int(expiry_days or 0)))
        days_row = Frm(body)
        days_row.pack(fill="x", pady=(2, 8))
        Ent(days_row, textvariable=self.days_var, width=10).pack(side="left")
        for preset in (30, 60, 90, 120, 180):
            Btn(days_row, str(preset), command=lambda p=preset: self.days_var.set(str(p)), width=4).pack(side="left", padx=2)

        self.preview = Lbl(body, text="", wraplength=440, justify="left")
        self.preview.pack(anchor="w", pady=(6, 4))

        footer = Frm(body)
        footer.pack(fill="x", side="bottom", pady=(10, 0))
        Btn(footer, "取消", command=self._on_cancel, width=8).pack(side="right", padx=(6, 0))
        BtnPrimary(footer, "保存", command=self._on_ok, width=8).pack(side="right")

        self.date_var.trace_add("write", lambda *_: self._recompute())
        self.days_var.trace_add("write", lambda *_: self._recompute())
        self._recompute()

    @staticmethod
    def _today_str() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).date().isoformat()

    def _set_today(self) -> None:
        self.date_var.set(self._today_str())

    def _parse(self):
        from datetime import datetime, timezone, timedelta
        try:
            base = datetime.strptime(self.date_var.get().strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None, None, None
        try:
            days = int(float(self.days_var.get().strip()))
        except ValueError:
            return base, None, None
        if days <= 0:
            return base, days, None
        expiry = base + timedelta(days=days)
        now = datetime.now(timezone.utc)
        left = (expiry.date() - now.date()).days
        return base, days, (expiry.date().isoformat(), left)

    def _recompute(self) -> None:
        base, days, result = self._parse()
        if base is None:
            self.preview.configure(text="⚠ 日期格式应为 YYYY-MM-DD", fg=C.RED)
            return
        if days is None:
            self.preview.configure(text="⚠ 有效期天数必须是整数", fg=C.RED)
            return
        if days == 0 or result is None:
            self.preview.configure(text="提醒已关闭（有效期为 0）。", fg=C.TEXT_MUTE)
            return
        expiry_date, left = result
        if left < 0:
            self.preview.configure(text=f"到期日：{expiry_date} —— 已过期 {abs(left)} 天，请尽快修改。", fg=C.RED)
        elif left <= 14:
            self.preview.configure(text=f"到期日：{expiry_date} —— 还剩 {left} 天，临近到期。", fg=C.ORANGE)
        else:
            self.preview.configure(text=f"到期日：{expiry_date} —— 还剩 {left} 天。", fg=C.GREEN)

    def _on_ok(self) -> None:
        base, days, _ = self._parse()
        if base is None:
            messagebox.showwarning("参数错误", "上次修改日期格式应为 YYYY-MM-DD", parent=self)
            return
        if days is None or days < 0:
            messagebox.showwarning("参数错误", "有效期天数必须是 0 或正整数", parent=self)
            return
        self._finish({
            "password_changed_at": self.date_var.get().strip(),
            "password_expiry_days": days,
        })


# ---------------------------------------------------------------------------
# Modify Flex shape (OCPU / memory)
# ---------------------------------------------------------------------------


class ShapeConfigDialog(BaseDialog):
    """Change an existing Flex instance's OCPU / memory."""

    def __init__(self, master, shape: str, current_ocpus: float, current_memory: float, limits: Optional[dict] = None):
        self._shape = shape or ""
        self._current_ocpus = float(current_ocpus or 1)
        self._current_memory = float(current_memory or 1)
        self._limits = limits or {}
        super().__init__(master, title="修改实例规格", width=460, height=340)
        body = Frm(self)
        body.pack(fill="both", expand=True, padx=14, pady=12)

        Lbl(body, text=self._shape, font=C.FONT_BOLD).pack(anchor="w")
        Lbl(
            body,
            text=f"当前：{self._current_ocpus:g} OCPU · {self._current_memory:g} GB",
            fg=C.TEXT_MUTE,
        ).pack(anchor="w", pady=(2, 6))

        hint = "Always Free ARM (A1.Flex) 上限通常为 4 OCPU / 24 GB。变更后可能需要重启才能完全生效。"
        min_o = self._limits.get("min_ocpus")
        max_o = self._limits.get("max_ocpus")
        min_m = self._limits.get("min_memory_in_gbs")
        max_m = self._limits.get("max_memory_in_gbs")
        if max_o or max_m:
            hint = (
                f"该 Shape 范围：OCPU {min_o or '?'}–{max_o or '?'}；"
                f"内存 {min_m or '?'}–{max_m or '?'} GB。变更后可能需要重启才能完全生效。"
            )
        Lbl(body, text=hint, fg=C.TEXT_MUTE, wraplength=420, justify="left").pack(anchor="w", pady=(0, 8))

        row = Frm(body)
        row.pack(fill="x", pady=(0, 8))
        left = Frm(row)
        left.pack(side="left", fill="x", expand=True, padx=(0, 4))
        right = Frm(row)
        right.pack(side="left", fill="x", expand=True, padx=(4, 0))
        Lbl(left, text="OCPU").pack(anchor="w")
        self.ocpu_var = tk.StringVar(value=f"{self._current_ocpus:g}")
        Ent(left, textvariable=self.ocpu_var).pack(fill="x")
        Lbl(right, text="内存 GB").pack(anchor="w")
        self.memory_var = tk.StringVar(value=f"{self._current_memory:g}")
        Ent(right, textvariable=self.memory_var).pack(fill="x")

        presets = Frm(body)
        presets.pack(fill="x", pady=(2, 4))
        Lbl(presets, text="常用：", fg=C.TEXT_MUTE).pack(side="left")
        for label, o, m in (("1C/6G", 1, 6), ("2C/12G", 2, 12), ("4C/24G", 4, 24)):
            Btn(
                presets,
                label,
                command=lambda o=o, m=m: (self.ocpu_var.set(str(o)), self.memory_var.set(str(m))),
                width=7,
            ).pack(side="left", padx=2)

        footer = Frm(body)
        footer.pack(fill="x", side="bottom", pady=(10, 0))
        Btn(footer, "取消", command=self._on_cancel, width=8).pack(side="right", padx=(6, 0))
        BtnPrimary(footer, "应用", command=self._on_ok, width=8).pack(side="right")

    def _on_ok(self) -> None:
        try:
            ocpus = float(self.ocpu_var.get().strip())
            memory = float(self.memory_var.get().strip())
        except ValueError:
            messagebox.showwarning("参数错误", "OCPU 和内存必须是数字", parent=self)
            return
        if ocpus <= 0 or memory <= 0:
            messagebox.showwarning("参数错误", "OCPU 和内存必须大于 0", parent=self)
            return
        if ocpus == self._current_ocpus and memory == self._current_memory:
            messagebox.showinfo("提示", "规格没有变化。", parent=self)
            return
        self._finish({"ocpus": ocpus, "memory_in_gbs": memory})


# ---------------------------------------------------------------------------
# Serial / VNC console connection manager
# ---------------------------------------------------------------------------


class ConsoleConnectionDialog(BaseDialog):
    """Create / view / delete a serial + VNC console connection for an instance.

    ``list_fn``   -> list[obj] existing console connections (with connection_string,
                     vnc_connection_string, id, lifecycle_state).
    ``create_fn`` (public_key) -> OperationResult(data={id, serial, vnc}).
    ``delete_fn`` (connection_id) -> None.
    """

    def __init__(self, master, *, instance_name: str, list_fn: Callable, create_fn: Callable,
                 delete_fn: Callable, default_public_key: str = ""):
        self._list_fn = list_fn
        self._create_fn = create_fn
        self._delete_fn = delete_fn
        self._default_key = default_public_key or ""
        super().__init__(master, title=f"控制台连接 — {instance_name}", width=760, height=560)

        Lbl(self, text="串口 / VNC 控制台", font=C.FONT_TITLE).pack(fill="x", padx=12, pady=(10, 0))
        Lbl(
            self,
            text="系统起不来（如改坏 SSH / 防火墙）时，可通过串口控制台进入救援。创建需要一条 SSH 公钥；"
            "在本机用该公钥对应的私钥执行下方命令即可连接。",
            fg=C.TEXT_MUTE,
            wraplength=720,
            justify="left",
        ).pack(fill="x", padx=12, pady=(2, 6))

        keybar = Frm(self)
        keybar.pack(fill="x", padx=12, pady=(0, 4))
        Lbl(keybar, text="SSH 公钥", fg=C.TEXT_DIM).pack(anchor="w")
        self.key_box = Txt(keybar, height=3, font=C.FIXED, wrap="none")
        self.key_box.pack(fill="x", pady=(2, 2))
        if self._default_key:
            self.key_box.insert("1.0", self._default_key.strip())
        btns = Frm(keybar)
        btns.pack(fill="x")
        Btn(btns, "选择公钥文件…", command=self._pick_key).pack(side="left")
        self.create_btn = BtnPrimary(btns, "创建 / 重建控制台连接", command=self._create)
        self.create_btn.pack(side="left", padx=6)
        Btn(btns, "刷新", command=self._reload).pack(side="right")

        self.status = Lbl(self, text="正在加载现有连接…", fg=C.TEXT_MUTE)
        self.status.pack(fill="x", padx=12, pady=(4, 2))

        scroll = ScrollFrame(self)
        scroll.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        self.conn_list = scroll.inner

        Btn(self, "关闭", command=self._on_cancel, width=8).pack(pady=(0, 12))
        self.after(60, self._reload)

    def _pick_key(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择 SSH 公钥文件",
            filetypes=[("SSH 公钥", "*.pub *.txt *"), ("All", "*.*")],
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取失败", str(exc), parent=self)
            return
        if "PRIVATE KEY" in text:
            messagebox.showwarning("这是私钥", "请选择 SSH 公钥（.pub），不要选私钥。", parent=self)
            return
        line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        self.key_box.delete("1.0", "end")
        self.key_box.insert("1.0", line)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self.create_btn.configure(state="disabled" if busy else "normal")
        if message:
            self.status.configure(text=message, fg=C.TEXT_MUTE)

    def _reload(self) -> None:
        self._set_busy(True, "正在加载现有连接…")
        self._run_bg(self._list_fn, self._render)

    def _render(self, connections, error) -> None:
        self._set_busy(False)
        for w in self.conn_list.winfo_children():
            w.destroy()
        if error is not None:
            self.status.configure(text=f"加载失败：{error}", fg=C.RED)
            return
        connections = connections or []
        active = [c for c in connections if str(getattr(c, "lifecycle_state", "")) == "ACTIVE"]
        self.status.configure(
            text=f"现有连接：{len(connections)} 个（可用 {len(active)}）" if connections else "暂无控制台连接，粘贴公钥后点「创建」。",
            fg=C.GREEN if active else C.TEXT_MUTE,
        )
        for conn in connections:
            self._conn_card(conn)

    def _conn_card(self, conn) -> None:
        card = Group(self.conn_list)
        card.pack(fill="x", pady=4, padx=4)
        state = str(getattr(conn, "lifecycle_state", "") or "")
        top = Frm(card, bg=C.WINDOW)
        top.pack(fill="x", padx=8, pady=(6, 1))
        Lbl(top, text=f"连接 …{str(getattr(conn, 'id', ''))[-12:]}", bg=C.WINDOW, font=C.FONT_BOLD).pack(side="left")
        Lbl(top, text=state, bg=C.WINDOW, fg=C.GREEN if state == "ACTIVE" else C.TEXT_MUTE).pack(side="right")
        serial = getattr(conn, "connection_string", "") or ""
        vnc = getattr(conn, "vnc_connection_string", "") or ""
        for label, value in (("串口 SSH", serial), ("VNC", vnc)):
            if not value:
                continue
            row = Frm(card, bg=C.WINDOW)
            row.pack(fill="x", padx=8, pady=(0, 2))
            Lbl(row, text=label, bg=C.WINDOW, fg=C.TEXT_MUTE, width=8, anchor="w").pack(side="left")
            box = Txt(row, height=2, font=C.FIXED, wrap="char")
            box.pack(side="left", fill="x", expand=True, padx=(0, 4))
            box.insert("1.0", value)
            box.configure(state="disabled")
            Btn(row, "复制", command=lambda v=value: self._copy(v), width=5).pack(side="left")
        actions = Frm(card, bg=C.WINDOW)
        actions.pack(fill="x", padx=8, pady=(0, 6))
        Btn(actions, "删除此连接", command=lambda c=conn: self._delete(getattr(c, "id", "")), fg=C.RED, width=10).pack(side="right")

    def _copy(self, text: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.status.configure(text="已复制到剪贴板", fg=C.GREEN)
        except tk.TclError:
            pass

    def _create(self) -> None:
        key = self.key_box.get("1.0", "end").strip()
        if not key:
            messagebox.showwarning("需要公钥", "请粘贴或选择一条 SSH 公钥。", parent=self)
            return
        self._set_busy(True, "正在创建控制台连接（可能需要 30–90 秒）…")
        self._run_bg(lambda: self._create_fn(key), self._on_created)

    def _on_created(self, result, error) -> None:
        self._set_busy(False)
        if error is not None:
            self.status.configure(text=f"创建失败：{error}", fg=C.RED)
            messagebox.showerror("创建失败", str(error), parent=self)
            return
        if result is not None and not getattr(result, "ok", False):
            self.status.configure(text=getattr(result, "message", "创建失败"), fg=C.RED)
            messagebox.showerror("创建失败", getattr(result, "message", "创建失败"), parent=self)
            return
        self.status.configure(text="控制台连接已就绪", fg=C.GREEN)
        self._reload()

    def _delete(self, connection_id: str) -> None:
        if not connection_id:
            return
        if not messagebox.askyesno("删除连接", "删除此控制台连接？可随时重建。", parent=self):
            return
        self._set_busy(True, "正在删除…")
        self._run_bg(lambda: self._delete_fn(connection_id), self._on_deleted)

    def _on_deleted(self, _result, error) -> None:
        if error is not None:
            self._set_busy(False)
            self.status.configure(text=f"删除失败：{error}", fg=C.RED)
            messagebox.showerror("删除失败", str(error), parent=self)
            return
        self._reload()


# ---------------------------------------------------------------------------
# Instance monitoring (CPU / memory / network charts)
# ---------------------------------------------------------------------------


class MetricsDialog(BaseDialog):
    """Show CPU / memory / network time-series for an instance on plain canvases.

    ``fetch_fn`` (hours) -> OperationResult(data={"series": {...}, "has_data": bool}).
    """

    RANGES = [("最近 1 小时", 1), ("最近 3 小时", 3), ("最近 6 小时", 6), ("最近 12 小时", 12), ("最近 24 小时", 24)]

    def __init__(self, master, *, instance_name: str, fetch_fn: Callable):
        self._fetch_fn = fetch_fn
        super().__init__(master, title=f"实例监控 — {instance_name}", width=760, height=640)

        bar = Frm(self)
        bar.pack(fill="x", padx=12, pady=(10, 4))
        Lbl(bar, text="监控曲线", font=C.FONT_TITLE).pack(side="left")
        Lbl(bar, text="时间范围", fg=C.TEXT_MUTE).pack(side="left", padx=(16, 4))
        self._range_map = {label: hours for label, hours in self.RANGES}
        self.range_var = tk.StringVar(value=self.RANGES[1][0])
        Combo(bar, list(self._range_map), textvariable=self.range_var, width=14, command=self._reload).pack(side="left")
        Btn(bar, "刷新", command=self._reload).pack(side="right")

        self.status = Lbl(self, text="正在加载监控数据…", fg=C.TEXT_MUTE)
        self.status.pack(fill="x", padx=12, pady=(0, 4))

        charts = Frm(self, bg=C.WINDOW)
        charts.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        self._charts: dict[str, tk.Canvas] = {}
        self._chart_defs = [
            ("cpu", "CPU 使用率", "%", C.ACCENT, 100.0),
            ("memory", "内存使用率", "%", "#1e8e3e", 100.0),
            ("net", "网络流量（入/出）", "bytes", "#8944ab", None),
        ]
        for idx, (key, title, _unit, _color, _hint) in enumerate(self._chart_defs):
            cell = Group(charts)
            cell.grid(row=idx, column=0, sticky="nsew", padx=2, pady=3)
            Lbl(cell, text=title, bg=C.WINDOW, fg=C.TEXT_DIM, font=C.FONT_BOLD).pack(anchor="w", padx=8, pady=(6, 0))
            canvas = tk.Canvas(cell, height=140, bg=C.WINDOW, highlightthickness=0, bd=0)
            canvas.pack(fill="both", expand=True, padx=8, pady=(2, 8))
            canvas.bind("<Configure>", lambda _e: self._redraw())
            self._charts[key] = canvas
        charts.grid_columnconfigure(0, weight=1)
        for i in range(len(self._chart_defs)):
            charts.grid_rowconfigure(i, weight=1)

        Btn(self, "关闭", command=self._on_cancel, width=8).pack(pady=(0, 12))
        self._series: dict[str, list] = {}
        self.after(60, self._reload)

    def _reload(self) -> None:
        hours = self._range_map.get(self.range_var.get(), 3)
        self.status.configure(text="正在加载监控数据…", fg=C.TEXT_MUTE)
        self._run_bg(lambda: self._fetch_fn(hours), self._on_data)

    def _on_data(self, result, error) -> None:
        if error is not None:
            self.status.configure(text=f"加载失败：{error}", fg=C.RED)
            self._series = {}
            self._redraw()
            return
        data = getattr(result, "data", None) or {}
        self._series = data.get("series", {}) or {}
        has_data = bool(data.get("has_data"))
        self.status.configure(
            text=getattr(result, "message", "") or ("已获取监控数据" if has_data else "暂无监控数据"),
            fg=C.GREEN if has_data else C.ORANGE,
        )
        self._redraw()

    def _redraw(self) -> None:
        for key, title, unit, color, hint in self._chart_defs:
            canvas = self._charts.get(key)
            if not canvas:
                continue
            if key == "net":
                self._draw_network(canvas, color)
            else:
                points = self._series.get(key, []) or []
                values = [v for _ts, v in points]
                self._draw_chart(canvas, values, color, unit=unit, y_max_hint=hint)

    def _draw_chart(self, canvas: tk.Canvas, values: list, color: str, *, unit: str, y_max_hint=None,
                    second_values: Optional[list] = None, second_color: str = "", legend: str = "") -> None:
        canvas.delete("all")
        try:
            w = int(canvas.winfo_width())
            h = int(canvas.winfo_height())
        except tk.TclError:
            return
        if w < 40 or h < 40:
            return
        pad_left, pad_top, pad_bottom = 46, 10, 18
        # frame + gridlines
        canvas.create_rectangle(pad_left, pad_top, w - 6, h - pad_bottom, outline=C.BORDER_MUTED)
        if not values and not (second_values or []):
            canvas.create_text(w // 2, h // 2, text="无数据", fill=C.TEXT_MUTE, font=C.FONT_SMALL)
            return
        all_values = list(values) + list(second_values or [])
        y_max = float(y_max_hint) if y_max_hint else axis_max(all_values, minimum=1.0)
        # horizontal gridlines + y labels
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = pad_top + (h - pad_top - pad_bottom) * (1 - frac)
            canvas.create_line(pad_left, y, w - 6, y, fill=C.BORDER_MUTED)
            label = self._fmt_axis(y_max * frac, unit)
            canvas.create_text(pad_left - 4, y, text=label, fill=C.TEXT_MUTE, font=C.FONT_MICRO, anchor="e")

        def plot(vals, col):
            if not vals:
                return
            coords = scale_points(vals, w - 6, h, y_max, pad_left=pad_left, pad_top=pad_top, pad_bottom=pad_bottom)
            if len(coords) == 1:
                x, y = coords[0]
                canvas.create_oval(x - 2, y - 2, x + 2, y + 2, fill=col, outline=col)
            elif len(coords) >= 2:
                flat = [c for xy in coords for c in xy]
                canvas.create_line(*flat, fill=col, width=2, smooth=True)

        plot(values, color)
        plot(second_values or [], second_color or color)
        # last value
        if values:
            canvas.create_text(
                w - 8, pad_top + 2, text=self._fmt_axis(values[-1], unit),
                fill=color, font=C.FONT_SMALL, anchor="ne",
            )
        if legend:
            canvas.create_text(pad_left + 4, pad_top + 2, text=legend, fill=C.TEXT_MUTE, font=C.FONT_MICRO, anchor="nw")

    def _draw_network(self, canvas: tk.Canvas, color: str) -> None:
        net_in = [v for _ts, v in (self._series.get("net_in", []) or [])]
        net_out = [v for _ts, v in (self._series.get("net_out", []) or [])]
        self._draw_chart(
            canvas, net_in, color, unit="bytes", y_max_hint=None,
            second_values=net_out, second_color="#b25000",
            legend="紫=入站  橙=出站",
        )

    @staticmethod
    def _fmt_axis(value: float, unit: str) -> str:
        if unit == "%":
            return f"{value:.0f}%"
        if unit == "bytes":
            return human_bytes(value)
        return f"{value:.0f}"


# ---------------------------------------------------------------------------
# Account status (tier + quotas)
# ---------------------------------------------------------------------------


class AccountDashboardDialog(BaseDialog):
    """Show the tenancy tier (Always Free / PAYG) and compute quotas.

    ``account_fn`` () -> OperationResult(data=account status dict).
    """

    def __init__(self, master, *, tenant_name: str, account_fn: Callable):
        self._account_fn = account_fn
        super().__init__(master, title=f"账号状态 — {tenant_name}", width=720, height=620)
        self.minsize(480, 400)

        Lbl(self, text="账号状态", font=C.FONT_BOLD).pack(fill="x", padx=12, pady=(10, 2))
        head = Frm(self)
        head.pack(fill="x", padx=12, pady=(0, 2))
        Btn(head, "刷新", command=self._load_account).pack(side="right")

        acc_scroll = ScrollFrame(self)
        acc_scroll.pack(fill="both", expand=True, padx=12, pady=6)
        self.acc_body = acc_scroll.inner
        self.acc_status = Lbl(self.acc_body, text="正在读取账号信息…", fg=C.TEXT_MUTE)
        self.acc_status.pack(fill="x", padx=8, pady=10)

        Btn(self, "关闭", command=self._on_cancel, width=8).pack(pady=(0, 12))
        self.after(60, self._load_account)

    def _load_account(self) -> None:
        self._run_bg(self._account_fn, self._render_account)

    def _render_account(self, result, error) -> None:
        for w in self.acc_body.winfo_children():
            w.destroy()
        if error is not None:
            Lbl(self.acc_body, text=f"读取失败：{error}", fg=C.RED, wraplength=650, justify="left").pack(fill="x", padx=8, pady=10)
            return
        if result is not None and not getattr(result, "ok", False):
            Lbl(self.acc_body, text=getattr(result, "message", "读取失败"), fg=C.RED, wraplength=650, justify="left").pack(fill="x", padx=8, pady=10)
            return
        data = getattr(result, "data", None) or {}
        tier = data.get("tier", "未知")
        tier_code = str(data.get("tier_code") or "")
        is_paid = tier_code == "paid" or "PAYG" in tier or "付费" in tier or "已升级" in tier
        Lbl(self.acc_body, text=data.get("tenancy_name", "") or "租户", font=C.FONT_BOLD).pack(anchor="w", padx=8, pady=(10, 2))
        badge = Group(self.acc_body)
        badge.pack(fill="x", padx=8, pady=(0, 6))
        Lbl(badge, text=f"账号类型：{tier}", bg=C.WINDOW, fg=C.ORANGE if is_paid else C.GREEN, font=C.FONT_BOLD).pack(anchor="w", padx=10, pady=(8, 2))
        if data.get("tier_reason"):
            Lbl(badge, text=data["tier_reason"], bg=C.WINDOW, fg=C.TEXT_MUTE, wraplength=640, justify="left").pack(anchor="w", padx=10, pady=(0, 4))
        if data.get("tier_note"):
            Lbl(badge, text="注：" + data["tier_note"], bg=C.WINDOW, fg=C.TEXT_MUTE, font=C.FONT_MICRO, wraplength=640, justify="left").pack(anchor="w", padx=10, pady=(0, 8))
        info_rows = [
            ("Home Region", data.get("home_region", "") or "—"),
            ("说明", data.get("description", "") or "—"),
        ]
        for label, value in info_rows:
            row = Frm(self.acc_body, bg=C.FACE)
            row.pack(fill="x", padx=8, pady=1)
            Lbl(row, text=label, fg=C.TEXT_MUTE, width=14, anchor="w").pack(side="left")
            Lbl(row, text=str(value), wraplength=560, justify="left").pack(side="left")
        limits = data.get("limits", []) or []
        Lbl(self.acc_body, text="计算配额（免费额度追踪）", fg=C.TEXT_DIM, font=C.FONT_BOLD).pack(anchor="w", padx=8, pady=(10, 2))
        if not limits:
            Lbl(self.acc_body, text="未读取到配额信息（可能无 limits 读取权限）。", fg=C.TEXT_MUTE).pack(anchor="w", padx=8)
        for item in limits:
            row = Frm(self.acc_body, bg=C.FACE)
            row.pack(fill="x", padx=8, pady=1)
            Lbl(row, text=item.get("name", ""), fg=C.TEXT_DIM, width=32, anchor="w").pack(side="left")
            Lbl(row, text=f"{item.get('value', 0):g}", fg=C.TEXT).pack(side="left")
        Lbl(
            self.acc_body,
            text="提示：Always Free 账号无法开启付费性能（引导卷会被降回 10 VPUs/GB），"
            "且创建付费规格实例会失败或产生账单，请以此判断能否开机 / 创建。",
            fg=C.TEXT_MUTE, wraplength=650, justify="left",
        ).pack(fill="x", padx=8, pady=(10, 8))


class FontSettingsDialog(BaseDialog):
    """Pick UI font family, size, and optional bold body text."""

    AUTO_LABEL = "（自动 · 优先微软雅黑）"
    SIZE_CHOICES = [str(n) for n in range(9, 21)]

    def __init__(self, master, current: Optional[dict] = None):
        cur = current or {}
        self._initial_family = str(cur.get("font_family") or "").strip()
        self._initial_size = int(cur.get("font_size") or 11)
        self._initial_bold = bool(cur.get("font_bold"))
        super().__init__(master, title="界面字体", width=460, height=340)

        wrap = Frm(self)
        wrap.pack(fill="both", expand=True, padx=16, pady=14)

        Lbl(wrap, text="选择更清晰的字体和字号，设置会保存到本机配置。", fg=C.TEXT_MUTE, wraplength=420).pack(
            anchor="w"
        )

        Lbl(wrap, text="字体", fg=C.TEXT_MUTE).pack(anchor="w", pady=(12, 2))
        families = [self.AUTO_LABEL] + list_ui_font_families(self)
        self.family_var = tk.StringVar()
        if self._initial_family and self._initial_family in families:
            self.family_var.set(self._initial_family)
        elif self._initial_family:
            families.insert(1, self._initial_family)
            self.family_var.set(self._initial_family)
        else:
            self.family_var.set(self.AUTO_LABEL)
        self.family_combo = Combo(wrap, families, textvariable=self.family_var, width=36, command=self._update_preview)
        self.family_combo.pack(fill="x")

        row = Frm(wrap)
        row.pack(fill="x", pady=(12, 0))
        Lbl(row, text="字号", fg=C.TEXT_MUTE).pack(side="left")
        self.size_var = tk.StringVar(value=str(max(9, min(20, self._initial_size))))
        self.size_combo = Combo(row, self.SIZE_CHOICES, textvariable=self.size_var, width=6, command=self._update_preview)
        self.size_combo.pack(side="left", padx=(8, 16))
        self.bold_var = tk.BooleanVar(value=self._initial_bold)
        Chk(row, "正文加粗（更清晰）", self.bold_var, command=self._update_preview).pack(side="left")

        Lbl(wrap, text="预览", fg=C.TEXT_MUTE).pack(anchor="w", pady=(14, 2))
        preview_box = Group(wrap)
        preview_box.pack(fill="both", expand=True)
        self.preview_lbl = Lbl(
            preview_box,
            text="OCI Bot 实例列表 · 东京区域 · RUNNING\n0123456789  AaBbCc  核心 内存 硬盘",
            bg=C.WINDOW,
            justify="left",
            wraplength=400,
        )
        self.preview_lbl.pack(anchor="w", padx=12, pady=12)
        self._update_preview()

        footer = Frm(wrap)
        footer.pack(fill="x", side="bottom", pady=(12, 0))
        Btn(footer, "取消", command=self._on_cancel, width=8).pack(side="right", padx=(6, 0))
        BtnPrimary(footer, "应用", command=self._on_ok, width=8).pack(side="right")
        Btn(footer, "恢复默认", command=self._reset_defaults, width=10).pack(side="left")

    def _selected_family(self) -> str:
        value = (self.family_var.get() or "").strip()
        if not value or value == self.AUTO_LABEL:
            return ""
        return value

    def _selected_size(self) -> int:
        try:
            return max(9, min(20, int(self.size_var.get())))
        except (TypeError, ValueError):
            return 11

    def _update_preview(self) -> None:
        family = self._selected_family() or (C.get_font_prefs().get("family") if hasattr(C, "get_font_prefs") else "Microsoft YaHei UI")
        if not family:
            family = "Microsoft YaHei UI"
        size = self._selected_size()
        weight = "bold" if self.bold_var.get() else "normal"
        try:
            self.preview_lbl.configure(font=(family, size, weight))
        except tk.TclError:
            self.preview_lbl.configure(font=(family, size))

    def _reset_defaults(self) -> None:
        self.family_var.set(self.AUTO_LABEL)
        self.size_var.set("11")
        self.bold_var.set(False)
        self._update_preview()

    def _on_ok(self) -> None:
        self._finish(
            {
                "font_family": self._selected_family(),
                "font_size": self._selected_size(),
                "font_bold": bool(self.bold_var.get()),
            }
        )
