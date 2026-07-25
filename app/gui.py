"""Main GUI for multi-tenant OCI instance management — classic Win9x/VC6 style."""

from __future__ import annotations

import csv
import queue
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Optional

import tkinter as tk
from tkinter import ttk

from app import __version__
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
    Txt,
    apply_classic_style,
)
from app.config_store import ConfigStore, TenantConfig
from app.dialogs import (
    AccountDashboardDialog,
    BootVolumeDialog,
    ConsoleConnectionDialog,
    FirewallManagerDialog,
    FirewallRuleDialog,
    FontSettingsDialog,
    JobsCenterDialog,
    LaunchInstanceDialog,
    MetricsDialog,
    PasswordExpiryDialog,
    PasswordPromptDialog,
    ShapeConfigDialog,
    TenantEditorDialog,
    TextPromptDialog,
    ask_confirm,
)
from app.oci_client import (
    OCI_AVAILABLE,
    POWER_ACTIONS,
    InstanceInfo,
    OCIClientError,
    OperationResult,
    SessionManager,
    TenantSession,
    is_capacity_message,
    is_rate_limit_message,
)
from app.runtime_paths import prepare_runtime_data
from app.scheduler import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_RETRY_INTERVAL_SEC,
    MIN_RETRY_INTERVAL_SEC,
    BackgroundRunner,
    CapacityRetryJob,
    JobStore,
    clamp_max_attempts,
    clamp_retry_interval,
    rate_limit_backoff_sec,
)
from app.theme import (
    APP_SUBTITLE,
    APP_TITLE,
    DETAIL_WIDTH,
    SIDEBAR_WIDTH,
    SIDEBAR_WIDTH_MAX,
    SIDEBAR_WIDTH_MIN,
    STATE_COLORS,
    WINDOW_DEFAULT_SIZE,
    WINDOW_MIN_SIZE,
)
from app.ui_settings import font_prefs_from_settings, load_ui_settings, save_ui_settings

# Launch-wizard metadata (ADs / images / shapes / default network) cache TTL.
_LAUNCH_META_TTL_SEC = 15 * 60


class OCIBotApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_TITLE}  v{__version__}")
        self.geometry(f"{WINDOW_DEFAULT_SIZE[0]}x{WINDOW_DEFAULT_SIZE[1]}")
        self.minsize(*WINDOW_MIN_SIZE)

        try:
            self.data_dir, self.migration = prepare_runtime_data()
            self.store = ConfigStore(data_dir=self.data_dir)
            self.job_store = JobStore(data_dir=self.data_dir)
            self.ui_settings = load_ui_settings(self.data_dir)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "配置加载失败",
                f"OCIBot 无法安全读取数据目录，因此已停止启动。\n\n{exc}\n\n"
                "请检查 data/tenants.json、data/.secret 和目录写入权限。",
                parent=self,
            )
            self.destroy()
            raise

        # Apply larger / user-chosen fonts before any widgets are built.
        apply_classic_style(self, font_prefs_from_settings(self.ui_settings))
        self.sessions = SessionManager()
        self._ui_queue: queue.Queue = queue.Queue()

        self._instances: list[InstanceInfo] = []
        self._filtered: list[InstanceInfo] = []
        self._selected: Optional[InstanceInfo] = None
        self._checked: set[str] = set()
        self._row_by_id: dict[str, InstanceInfo] = {}
        self._tenant_ids: list[str] = []
        self._selected_tenant_id: Optional[str] = None
        self._all_tenants_mode = False
        self._include_subcompartments = True
        self._loading = False
        self._load_gen = 0
        self._loaded_scope: Optional[str] = None
        self._auto_refresh_sec = 0
        self._auto_job = None
        self._compartment_override: Optional[str] = None
        self._compartments_cache: list[dict] = []
        self._comp_label_map: dict = {"(默认)": None}
        # Instance IDs whose IP / boot-volume fields have been resolved this session.
        self._enriched_ids: set[str] = set()
        self._enrich_inflight: set[str] = set()
        # tenant_id -> (monotonic_ts, meta dict) for create-instance wizard.
        self._launch_meta_cache: dict[str, tuple[float, dict]] = {}

        active = self.store.get_active()
        self._selected_tenant_id = active.id if active else None

        self._build_menu()
        self._build_layout()
        self._refresh_tenant_list()
        self._set_status("就绪")
        self._log(f"{APP_TITLE} v{__version__} 已启动 · {self.store.data_location()}")
        if self.migration.migrated:
            files = ", ".join(self.migration.files)
            self._log(
                f"已从 {self.migration.source} 迁移数据到 {self.migration.destination}（{files}）",
                level="ok",
            )
        self._check_password_expiries()
        if not OCI_AVAILABLE:
            self._log("警告: 未安装 oci SDK，请 pip install -r requirements.txt", level="warn")

        self.runner = BackgroundRunner(
            store=self.job_store,
            on_log=lambda m, lv: self._ui_queue.put(("log", None, (m, lv))),
            on_schedule_fire=self._handle_schedule_fire,
            on_retry_tick=self._handle_retry_tick,
            on_jobs_changed=lambda: self._ui_queue.put(("jobs_changed", None, None)),
        )
        self.after(100, self._poll_ui_queue)
        self.after(1000, self._scheduler_tick)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if self._selected_tenant_id:
            self.after(300, self.refresh_instances)
        # Fill in unknown account tiers a bit later so it never competes with
        # the first instance load.
        self.after(1500, self._refresh_tenant_tiers)

    # ==================================================================
    # Menu + layout
    # ==================================================================
    def _build_menu(self) -> None:
        menubar = tk.Menu(self, tearoff=0, bg=C.FACE, fg=C.TEXT, font=C.FONT)
        m_file = tk.Menu(menubar, tearoff=0, bg=C.FACE, fg=C.TEXT, font=C.FONT)
        m_file.add_command(label="导入租户 JSON…", command=self._import_config)
        m_file.add_command(label="导出全部租户…", command=self._export_config)
        m_file.add_command(label="导入 OCI Config…", command=self._import_oci_config_file)
        m_file.add_separator()
        m_file.add_command(label="备份 API Key（加密 ZIP）…", command=self._backup_encrypted_zip)
        m_file.add_command(label="从加密 ZIP 恢复…", command=self._restore_encrypted_zip)
        m_file.add_separator()
        m_file.add_command(label="打开配置目录", command=self._open_data_dir)
        m_file.add_separator()
        m_file.add_command(label="退出", command=self._on_close)
        menubar.add_cascade(label="文件(F)", menu=m_file)

        m_tenant = tk.Menu(menubar, tearoff=0, bg=C.FACE, fg=C.TEXT, font=C.FONT)
        m_tenant.add_command(label="添加租户…", command=self._add_tenant)
        m_tenant.add_command(label="编辑选中租户…", command=self._edit_selected_tenant)
        m_tenant.add_command(label="删除选中租户", command=self._delete_selected_tenant)
        m_tenant.add_separator()
        m_tenant.add_command(label="账号状态与用量…", command=self._open_account_dashboard)
        m_tenant.add_command(label="密码到期提醒…", command=self._open_password_expiry)
        menubar.add_cascade(label="租户(T)", menu=m_tenant)

        m_inst = tk.Menu(menubar, tearoff=0, bg=C.FACE, fg=C.TEXT, font=C.FONT)
        m_inst.add_command(label="刷新实例", command=self.refresh_instances)
        m_inst.add_command(label="创建实例…", command=self._open_launch)
        m_inst.add_command(label="导出 CSV…", command=self._export_csv)
        menubar.add_cascade(label="实例(I)", menu=m_inst)

        m_job = tk.Menu(menubar, tearoff=0, bg=C.FACE, fg=C.TEXT, font=C.FONT)
        m_job.add_command(label="任务中心…", command=self._open_jobs_center)
        menubar.add_cascade(label="任务(J)", menu=m_job)

        m_view = tk.Menu(menubar, tearoff=0, bg=C.FACE, fg=C.TEXT, font=C.FONT)
        m_view.add_command(label="界面字体…", command=self._open_font_settings)
        menubar.add_cascade(label="视图(V)", menu=m_view)

        m_help = tk.Menu(menubar, tearoff=0, bg=C.FACE, fg=C.TEXT, font=C.FONT)
        m_help.add_command(label="关于", command=self._about)
        menubar.add_cascade(label="帮助(H)", menu=m_help)
        self.config(menu=menubar)

    def _about(self) -> None:
        prefs = C.get_font_prefs()
        messagebox.showinfo(
            "关于",
            f"{APP_TITLE} v{__version__}\n{APP_SUBTITLE}\n\nOCI SDK: "
            + ("已加载" if OCI_AVAILABLE else "未安装")
            + f"\n字体：{prefs.get('family')} {prefs.get('size')}pt"
            + (" 加粗" if prefs.get("bold") else ""),
            parent=self,
        )

    def _open_font_settings(self) -> None:
        dlg = FontSettingsDialog(self, current=self.ui_settings)
        self.wait_window(dlg)
        if not dlg.result:
            return
        # Preserve non-font prefs (e.g. sidebar_width) when updating fonts.
        self.ui_settings = {
            **dict(self.ui_settings or {}),
            "font_family": dlg.result.get("font_family") or "",
            "font_size": int(dlg.result.get("font_size") or 11),
            "font_bold": bool(dlg.result.get("font_bold")),
        }
        try:
            save_ui_settings(self.ui_settings, self.data_dir)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", f"无法写入字体设置：\n{exc}", parent=self)
            return
        self._apply_font_settings(rebuild=True)
        self._log(
            f"界面字体已更新：{self.ui_settings.get('font_family') or '自动'} "
            f"{self.ui_settings.get('font_size')}pt"
            + (" 加粗" if self.ui_settings.get("font_bold") else ""),
            level="ok",
        )

    def _apply_font_settings(self, *, rebuild: bool = False) -> None:
        """Apply stored font prefs; optionally rebuild the whole main UI."""
        apply_classic_style(self, font_prefs_from_settings(self.ui_settings))
        if not rebuild:
            return
        # Preserve runtime state across a full widget rebuild.
        geo = self.geometry()
        selected_id = self._selected.id if self._selected else None
        checked = set(self._checked)
        instances = list(self._instances)
        filtered_state = self.state_filter.get() if hasattr(self, "state_filter") else "全部"
        search = self.search_var.get() if hasattr(self, "search_var") else ""
        auto = self.auto_refresh.get() if hasattr(self, "auto_refresh") else "关闭"
        all_mode = bool(self.all_tenants_var.get()) if hasattr(self, "all_tenants_var") else self._all_tenants_mode
        sub_comp = bool(self.sub_comp_var.get()) if hasattr(self, "sub_comp_var") else self._include_subcompartments
        comp_value = self.comp_var.get() if hasattr(self, "comp_var") else "(默认)"
        status = self.status_var.get() if hasattr(self, "status_var") else "就绪"

        for child in list(self.winfo_children()):
            try:
                child.destroy()
            except tk.TclError:
                pass
        self.config(menu="")
        self._build_menu()
        self._build_layout()
        try:
            self.geometry(geo)
        except tk.TclError:
            pass

        # Restore filters / selection without forcing a network refresh.
        self._all_tenants_mode = all_mode
        self.all_tenants_var.set(all_mode)
        self.sub_comp_var.set(sub_comp)
        self._include_subcompartments = sub_comp
        try:
            self.state_filter.set(filtered_state)
        except tk.TclError:
            pass
        self.search_var.set(search)
        try:
            self.auto_refresh.set(auto)
        except tk.TclError:
            pass
        if self._compartments_cache:
            labels = ["(默认)"] + [c.get("label") or c.get("name") or c.get("id") for c in self._compartments_cache]
            self.comp_combo.configure(values=labels)
        try:
            self.comp_var.set(comp_value)
        except tk.TclError:
            pass
        self._refresh_tenant_list()
        self._instances = instances
        self._checked = checked
        # Font rebuild recreates list widgets; re-apply sidebar width and clear
        # enrichment markers so the focused row can re-fetch IP/disk if needed.
        try:
            self._apply_sidebar_width(self._sidebar_width())
        except Exception:
            pass
        self._enriched_ids.clear()
        self._enrich_inflight.clear()
        if selected_id:
            self._selected = next((i for i in instances if i.id == selected_id), None)
        self._apply_filter()
        self._render_detail(self._selected)
        if self._selected:
            self._ensure_instance_enriched(self._selected)
        self._set_status(status)
        self._update_jobs_badge()
        # Re-bind auto-refresh timer to the restored combo value.
        self._on_auto_refresh_change()

    def _build_layout(self) -> None:
        # Fluent header — white bar, blue accent strip, primary actions only.
        header = Frm(self, bg=C.WINDOW)
        header.pack(fill="x", side="top")
        brand = Frm(header, bg=C.WINDOW)
        brand.pack(side="left", padx=(12, 8), pady=8)
        tk.Frame(brand, bg=C.ACCENT, width=3, height=18).pack(side="left", padx=(0, 8))
        Lbl(brand, text=APP_TITLE, bg=C.WINDOW, fg=C.TEXT, font=C.FONT_HEADER).pack(side="left")
        self.btn_refresh = BtnPrimary(header, "刷新", command=self.refresh_instances)
        self.btn_refresh.pack(side="right", padx=(4, 12), pady=6)
        BtnPrimary(header, "创建实例", command=self._open_launch).pack(side="right", padx=4, pady=6)
        Btn(header, "任务", command=self._open_jobs_center).pack(side="right", padx=4, pady=6)
        tk.Frame(self, height=1, bg=C.BORDER).pack(fill="x", side="top")

        # Status line (bottom)
        status_bar = Frm(self)
        status_bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="就绪")
        Lbl(status_bar, textvariable=self.status_var, anchor="w", fg=C.TEXT_MUTE, font=C.FONT_SMALL).pack(
            side="left", fill="x", expand=True, padx=8, pady=2
        )
        self.lbl_jobs = Lbl(status_bar, text="任务 0", anchor="e", fg=C.TEXT_MUTE, font=C.FONT_SMALL)
        self.lbl_jobs.pack(side="right", padx=8, pady=2)
        Lbl(
            status_bar,
            text="OCI SDK: " + ("已加载" if OCI_AVAILABLE else "未安装"),
            anchor="e",
            font=C.FONT_SMALL,
            fg=C.GREEN if OCI_AVAILABLE else C.RED,
        ).pack(side="right", padx=6, pady=2)

        # Compact log strip
        log_group = Group(self)
        log_group.pack(fill="x", side="bottom", padx=6, pady=(0, 4))
        log_head = Frm(log_group, bg=C.WINDOW)
        log_head.pack(fill="x", padx=8, pady=(4, 0))
        Lbl(log_head, text="日志", bg=C.WINDOW, fg=C.TEXT_MUTE, font=C.FONT_SMALL).pack(side="left")
        Btn(log_head, "清空", command=lambda: self.log_box.delete("1.0", "end")).pack(side="right")
        log_wrap = Frm(log_group, bg=C.WINDOW)
        log_wrap.pack(fill="both", expand=True, padx=8, pady=(1, 5))
        log_scroll = ttk.Scrollbar(log_wrap)
        log_scroll.pack(side="right", fill="y")
        self.log_box = Txt(log_wrap, height=2, font=C.FIXED, highlightthickness=0, yscrollcommand=log_scroll.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        log_scroll.config(command=self.log_box.yview)

        # Main body (3 columns)
        body = Frm(self)
        body.pack(fill="both", expand=True, padx=6, pady=4)

        self._build_sidebar(body)
        self._build_detail(body)
        self._build_center(body)

    def _sidebar_width(self) -> int:
        """Resolved tenant-list width (persisted preference or theme default)."""
        try:
            stored = int((self.ui_settings or {}).get("sidebar_width") or 0)
        except (TypeError, ValueError):
            stored = 0
        width = stored if stored > 0 else SIDEBAR_WIDTH
        return max(SIDEBAR_WIDTH_MIN, min(SIDEBAR_WIDTH_MAX, width))

    def _apply_sidebar_width(self, width: int) -> None:
        width = max(SIDEBAR_WIDTH_MIN, min(SIDEBAR_WIDTH_MAX, int(width)))
        frame = getattr(self, "_sidebar_frame", None)
        if frame is not None:
            try:
                frame.configure(width=width)
            except tk.TclError:
                pass
        self.ui_settings["sidebar_width"] = width

    def _start_sidebar_resize(self, event) -> None:
        self._sidebar_resize_start_x = event.x_root
        self._sidebar_resize_start_w = self._sidebar_width()

    def _on_sidebar_resize(self, event) -> None:
        start_x = getattr(self, "_sidebar_resize_start_x", None)
        start_w = getattr(self, "_sidebar_resize_start_w", None)
        if start_x is None or start_w is None:
            return
        delta = int(event.x_root - start_x)
        self._apply_sidebar_width(start_w + delta)

    def _end_sidebar_resize(self, _event=None) -> None:
        self._sidebar_resize_start_x = None
        self._sidebar_resize_start_w = None
        try:
            save_ui_settings(self.ui_settings, self.data_dir)
        except Exception:
            pass

    def _sync_tenant_list_scroll(self) -> None:
        """Widen the listbox character width so long notes can scroll horizontally."""
        lb = getattr(self, "tenant_listbox", None)
        if lb is None:
            return
        try:
            n = lb.size()
            max_chars = 24
            for i in range(n):
                max_chars = max(max_chars, len(lb.get(i)))
            # Listbox width is in average character cells; +2 leaves a little padding.
            lb.configure(width=max(24, max_chars + 2))
        except tk.TclError:
            pass

    def _build_sidebar(self, parent: tk.Misc) -> None:
        side = Group(parent, width=self._sidebar_width())
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        self._sidebar_frame = side

        head = Frm(side, bg=C.WINDOW)
        head.pack(fill="x", padx=8, pady=(8, 3))
        Lbl(head, text="租户", bg=C.WINDOW, fg=C.TEXT, font=C.FONT_BOLD).pack(side="left")
        self.lbl_tenant_count = Lbl(head, text="0", bg=C.WINDOW, fg=C.TEXT_MUTE, anchor="e")
        self.lbl_tenant_count.pack(side="right")

        BtnPrimary(side, "+ 添加", command=self._add_tenant).pack(fill="x", padx=8, pady=(0, 4))

        self.all_tenants_var = tk.BooleanVar(value=False)
        Chk(side, "跨租户视图", self.all_tenants_var, command=self._toggle_all_tenants, bg=C.WINDOW).pack(
            fill="x", padx=8
        )

        list_wrap = Frm(side, bg=C.WINDOW)
        list_wrap.pack(fill="both", expand=True, padx=8, pady=4)
        # Pack scrollbars before the list so they keep a stable strip.
        thsb = ttk.Scrollbar(list_wrap, orient="horizontal")
        thsb.pack(side="bottom", fill="x")
        tsb = ttk.Scrollbar(list_wrap)
        tsb.pack(side="right", fill="y")
        self.tenant_listbox = tk.Listbox(
            list_wrap,
            bg=C.WINDOW,
            fg=C.TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            selectbackground=C.SELECT_BG,
            selectforeground=C.SELECT_FG,
            activestyle="none",
            exportselection=False,
            font=C.FONT,
            yscrollcommand=tsb.set,
            xscrollcommand=thsb.set,
        )
        self.tenant_listbox.pack(side="left", fill="both", expand=True)
        tsb.config(command=self.tenant_listbox.yview)
        thsb.config(command=self.tenant_listbox.xview)
        self.tenant_listbox.bind("<<ListboxSelect>>", self._on_tenant_select)
        self.tenant_listbox.bind("<Double-Button-1>", lambda _e: self._edit_selected_tenant())

        btns = Frm(side, bg=C.WINDOW)
        btns.pack(fill="x", padx=8, pady=(0, 4))
        Btn(btns, "编辑", command=self._edit_selected_tenant).pack(side="left", expand=True, fill="x", padx=(0, 2))
        Btn(btns, "删除", command=self._delete_selected_tenant, fg=C.RED).pack(side="left", expand=True, fill="x", padx=2)
        Btn(btns, "测试", command=self._test_selected_tenant).pack(side="left", expand=True, fill="x", padx=(2, 0))

        Btn(side, "账号状态", command=self._open_account_dashboard).pack(fill="x", padx=8, pady=(0, 8))

        # Drag handle on the right edge of the sidebar — pull right to read long notes.
        grip = tk.Frame(parent, width=5, bg=C.BORDER_MUTED, cursor="sb_h_double_arrow")
        grip.pack(side="left", fill="y")
        grip.pack_propagate(False)
        self._sidebar_grip = grip
        grip.bind("<Enter>", lambda _e: grip.configure(bg=C.ACCENT))
        grip.bind("<Leave>", lambda _e: grip.configure(bg=C.BORDER_MUTED))
        grip.bind("<ButtonPress-1>", self._start_sidebar_resize)
        grip.bind("<B1-Motion>", self._on_sidebar_resize)
        grip.bind("<ButtonRelease-1>", self._end_sidebar_resize)

    def _build_center(self, parent: tk.Misc) -> None:
        center = Group(parent)
        center.pack(side="left", fill="both", expand=True, padx=(6, 0))

        # Single compact toolbar: search / filter / compartment / selection
        bar = Frm(center, bg=C.WINDOW)
        bar.pack(fill="x", padx=8, pady=(8, 4))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        Ent(bar, textvariable=self.search_var, width=16).pack(side="left", ipady=1)
        Lbl(bar, text="状态", bg=C.WINDOW, fg=C.TEXT_MUTE).pack(side="left", padx=(8, 2))
        self.state_filter = Combo(
            bar,
            ["全部", "RUNNING", "STOPPED", "STARTING", "STOPPING", "PROVISIONING", "TERMINATING"],
            width=10,
            command=self._apply_filter,
        )
        self.state_filter.set("全部")
        self.state_filter.pack(side="left")
        Lbl(bar, text="刷新", bg=C.WINDOW, fg=C.TEXT_MUTE).pack(side="left", padx=(8, 2))
        self.auto_refresh = Combo(
            bar, ["关闭", "60 秒", "120 秒", "300 秒"], width=7, command=self._on_auto_refresh_change
        )
        self.auto_refresh.set("关闭")
        self.auto_refresh.pack(side="left")
        self.lbl_instance_stats = Lbl(bar, text="实例 0", bg=C.WINDOW, fg=C.TEXT_MUTE, anchor="e")
        self.lbl_instance_stats.pack(side="right")

        bar2 = Frm(center, bg=C.WINDOW)
        bar2.pack(fill="x", padx=8, pady=(0, 4))
        Lbl(bar2, text="目录", bg=C.WINDOW, fg=C.TEXT_MUTE).pack(side="left")
        self.comp_var = tk.StringVar(value="(默认)")
        self.comp_combo = Combo(
            bar2, ["(默认)"], textvariable=self.comp_var, width=18, command=self._on_compartment_change
        )
        self.comp_combo.pack(side="left", padx=4)
        Btn(bar2, "加载", command=self._load_compartments).pack(side="left", padx=1)
        self.sub_comp_var = tk.BooleanVar(value=True)
        Chk(bar2, "含子目录", self.sub_comp_var, command=self._on_subcomp_toggle, bg=C.WINDOW).pack(
            side="left", padx=6
        )
        Btn(bar2, "CSV", command=self._export_csv).pack(side="right", padx=1)
        self.lbl_checked = Lbl(bar2, text="已选 0", bg=C.WINDOW, fg=C.TEXT_MUTE, anchor="e")
        self.lbl_checked.pack(side="right", padx=6)
        Btn(bar2, "清空", command=self._clear_checks).pack(side="right", padx=1)
        Btn(bar2, "全选", command=self._select_all_visible).pack(side="right", padx=1)

        tk.Frame(center, height=1, bg=C.BORDER_MUTED).pack(fill="x", padx=8)

        # instance table
        table_wrap = Frm(center, bg=C.WINDOW)
        table_wrap.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        vsb = ttk.Scrollbar(table_wrap)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(table_wrap, orient="horizontal")
        hsb.grid(row=1, column=0, sticky="ew")
        table_wrap.grid_rowconfigure(0, weight=1)
        table_wrap.grid_columnconfigure(0, weight=1)
        cols = (
            "chk", "state", "name", "tenant", "shape",
            "ocpu", "mem", "disk", "diskperf",
            "pubip", "privip", "ipv6", "ad",
        )
        self._tree_cols = cols
        headers = {
            "chk": ("选", 28),
            "state": ("状态", 78),
            "name": ("名称", 108),
            "tenant": ("租户", 56),
            "shape": ("Shape", 118),
            "ocpu": ("核心", 42),
            "mem": ("内存", 48),
            "disk": ("硬盘", 48),
            "diskperf": ("硬盘性能", 72),
            "pubip": ("公网 IPv4", 100),
            "privip": ("私网 IPv4", 100),
            "ipv6": ("IPv6", 150),
            "ad": ("可用域", 80),
        }
        self.tree = ttk.Treeview(
            table_wrap, columns=cols, show="headings", selectmode="browse",
            yscrollcommand=vsb.set, xscrollcommand=hsb.set,
        )
        for col in cols:
            text, width = headers[col]
            if col == "chk":
                anchor = "center"
            elif col in ("ocpu", "mem", "disk", "diskperf"):
                anchor = "center"
            else:
                anchor = "w"
            stretch = col in ("name", "ipv6")
            min_w = 28 if col == "chk" else (36 if col in ("ocpu", "mem", "disk") else 48)
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, minwidth=min_w, anchor=anchor, stretch=stretch)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.config(command=self.tree.yview)
        hsb.config(command=self.tree.xview)
        for state, color in STATE_COLORS.items():
            self.tree.tag_configure(state, foreground=color)
        self.tree.tag_configure("evenrow", background=C.WINDOW)
        self.tree.tag_configure("oddrow", background="#f7f9fc")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Button-3>", self._on_tree_right_click)
        self._build_tree_menu()

    def _build_tree_menu(self) -> None:
        """Right-click menu: power actions + copy helpers for the selected row."""
        self._tree_menu = tk.Menu(self, tearoff=0, bg=C.WINDOW, fg=C.TEXT)
        self._tree_menu_target: Optional[str] = None
        for text, action in [
            ("开机", "START"),
            ("正常关机", "SOFTSTOP"),
            ("强制关机", "STOP"),
            ("正常重启", "SOFTRESET"),
            ("强制重启", "RESET"),
        ]:
            self._tree_menu.add_command(
                label=text,
                command=lambda a=action: self._power_from_menu(a),
            )
        self._tree_menu.add_separator()
        self._tree_menu.add_command(label="批量开机（已勾选）", command=lambda: self._batch_power("START"))
        self._tree_menu.add_command(label="批量正常关机（已勾选）", command=lambda: self._batch_power("SOFTSTOP"))
        self._tree_menu.add_command(label="批量强制关机（已勾选）", command=lambda: self._batch_power("STOP"))
        self._tree_menu.add_separator()
        self._tree_menu.add_command(label="复制公网 IP", command=lambda: self._copy_row_field("public_ip", "公网 IP"))
        self._tree_menu.add_command(label="复制私网 IP", command=lambda: self._copy_row_field("private_ip", "私网 IP"))
        self._tree_menu.add_command(label="复制 IPv6", command=lambda: self._copy_row_field("ipv6", "IPv6"))
        self._tree_menu.add_command(label="复制名称", command=lambda: self._copy_row_field("display_name", "名称"))
        self._tree_menu.add_command(label="复制 OCID", command=lambda: self._copy_row_field("id", "OCID"))
        self._tree_menu.add_command(label="复制 SSH 命令", command=lambda: self._copy_row_field("ssh", "SSH 命令"))
        self._tree_menu.add_command(label="复制 root 密码", command=lambda: self._copy_row_field("root_password", "root 密码"))

    def _on_tree_right_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self._tree_menu_target = row
        self.tree.selection_set(row)
        inst = self._row_by_id.get(row)
        if inst:
            self._selected = inst
            self._render_detail(inst)
            # Right-click on the already-selected row may not fire TreeviewSelect;
            # still kick off IP/disk enrich so copy actions can succeed.
            self._ensure_instance_enriched(inst)
        try:
            self._tree_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._tree_menu.grab_release()

    def _power_from_menu(self, action: str) -> None:
        """Run a single-instance power action from the tree context menu."""
        inst = self._row_by_id.get(self._tree_menu_target or "")
        if inst:
            self._selected = inst
            self._render_detail(inst)
        self._do_power(action)

    def _copy_row_field(self, field: str, label: str) -> None:
        inst = self._row_by_id.get(self._tree_menu_target or "")
        if not inst:
            return
        if field in ("public_ip", "private_ip", "ipv6", "ssh") and self._instance_needs_enrichment(inst):
            self._ensure_instance_enriched(inst)
            messagebox.showinfo("提示", f"正在解析地址，完成后请再复制{label}。", parent=self)
            return
        if field == "ssh":
            ip = inst.public_ip or inst.private_ip
            if not ip:
                messagebox.showinfo("提示", "该实例没有可用 IP", parent=self)
                return
            user = "root" if str(inst.freeform_tags.get("ocibot_ssh_user", "")).lower() == "root" else "opc"
            self._copy_text(f"ssh {user}@{ip}", label)
            return
        if field == "ipv6":
            self._copy_text(", ".join(inst.ipv6_addresses), label)
            return
        if field == "root_password":
            from app.oci_client import ROOT_PASSWORD_TAG

            pwd = str((inst.freeform_tags or {}).get(ROOT_PASSWORD_TAG) or "").strip()
            if not pwd:
                messagebox.showinfo(
                    "提示",
                    "该实例没有保存 root 密码标签。\n"
                    "仅「root + 服务器密码」方式创建、且创建时写入标签的实例才有。",
                    parent=self,
                )
                return
            self._copy_text(pwd, label)
            return
        self._copy_text(getattr(inst, field, "") or "", label)

    def _build_detail(self, parent: tk.Misc) -> None:
        detail = Group(parent, width=DETAIL_WIDTH)
        detail.pack(side="right", fill="y", padx=(6, 0))
        detail.pack_propagate(False)

        self.detail_name = Lbl(
            detail, text="未选择实例", bg=C.WINDOW, font=C.FONT_BOLD, wraplength=DETAIL_WIDTH - 20
        )
        self.detail_name.pack(fill="x", padx=8, pady=(8, 0))
        self.detail_state = Lbl(detail, text="", bg=C.WINDOW, font=C.FONT_SMALL)
        self.detail_state.pack(fill="x", padx=8)

        body_wrap = Frm(detail, bg=C.WINDOW)
        body_wrap.pack(fill="x", padx=8, pady=4)
        dsb = ttk.Scrollbar(body_wrap)
        dsb.pack(side="right", fill="y")
        self.detail_body = Txt(body_wrap, height=8, highlightthickness=0, font=C.FONT_SMALL, yscrollcommand=dsb.set)
        self.detail_body.pack(side="left", fill="both", expand=True)
        dsb.config(command=self.detail_body.yview)
        self.detail_body.configure(state="disabled")

        # Power actions only here (single instance). Batch power is in the list right-click menu.
        tk.Frame(detail, height=1, bg=C.BORDER_MUTED).pack(fill="x", padx=8)
        Lbl(detail, text="电源", bg=C.WINDOW, fg=C.TEXT_MUTE, font=C.FONT_SMALL).pack(fill="x", padx=8, pady=(4, 1))
        ops = Frm(detail, bg=C.WINDOW)
        ops.pack(fill="x", padx=8)
        self.power_buttons: dict[str, tk.Button] = {}
        power_defs = [
            ("START", "开机"),
            ("SOFTSTOP", "关机"),
            ("STOP", "强关"),
            ("SOFTRESET", "重启"),
            ("RESET", "强启"),
        ]
        for i, (action, label) in enumerate(power_defs):
            b = Btn(ops, label, command=lambda a=action: self._do_power(a))
            b.grid(row=i // 3, column=i % 3, padx=1, pady=1, sticky="ew")
            self.power_buttons[action] = b
        for col in range(3):
            ops.grid_columnconfigure(col, weight=1)

        self.btn_terminate = Btn(detail, "终止…", command=self._do_terminate, fg=C.RED)
        self.btn_terminate.pack(fill="x", padx=8, pady=(4, 3))

        tk.Frame(detail, height=1, bg=C.BORDER_MUTED).pack(fill="x", padx=8)
        util = Frm(detail, bg=C.WINDOW)
        util.pack(fill="x", padx=8, pady=(4, 8))
        # Copy helpers live in the list right-click menu — keep only management tools here.
        util_defs = [
            ("重命名", self._rename_instance),
            ("改规格", self._modify_shape),
            ("引导卷", self._adjust_boot_volume),
            ("IPv6", self._assign_ipv6),
            ("换 IP", self._replace_public_ip),
            ("防火墙", self._open_firewall),
            ("控制台", self._open_console),
            ("监控", self._open_metrics),
        ]
        for i, (text, cmd) in enumerate(util_defs):
            Btn(util, text, command=cmd).grid(row=i // 2, column=i % 2, sticky="ew", padx=1, pady=1)
        util.grid_columnconfigure(0, weight=1)
        util.grid_columnconfigure(1, weight=1)
        self._set_power_enabled(False)

    # ==================================================================
    # Tenants
    # ==================================================================
    def _refresh_tenant_list(self) -> None:
        self.tenant_listbox.delete(0, "end")
        tenants = self.store.list_tenants()
        self._tenant_ids = [t.id for t in tenants]
        self.lbl_tenant_count.configure(text=str(len(tenants)))
        active_index = None
        for idx, t in enumerate(tenants):
            # "区域 - 免费 / 已升级 - 显示名称" (+ optional · 备注)
            label = t.sidebar_label()
            if not t.enabled:
                label += "（停用）"
            self.tenant_listbox.insert("end", label)
            color = t.color if t.enabled else C.DISABLED
            if t.enabled:
                level, _text = t.password_status()
                if level == "expired":
                    color = C.RED
                elif level == "warn":
                    color = C.ORANGE
            self.tenant_listbox.itemconfig(idx, foreground=color)
            if (not self._all_tenants_mode) and t.id == self._selected_tenant_id:
                active_index = idx
        self._sync_tenant_list_scroll()
        if active_index is not None:
            self.tenant_listbox.selection_clear(0, "end")
            self.tenant_listbox.selection_set(active_index)
            self.tenant_listbox.see(active_index)

    def _selected_tenant_from_list(self) -> Optional[TenantConfig]:
        sel = self.tenant_listbox.curselection()
        if not sel:
            if self._selected_tenant_id:
                return self.store.get(self._selected_tenant_id)
            return None
        idx = sel[0]
        if 0 <= idx < len(self._tenant_ids):
            return self.store.get(self._tenant_ids[idx])
        return None

    def _on_tenant_select(self, _event=None) -> None:
        sel = self.tenant_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self._tenant_ids):
            self._select_tenant(self._tenant_ids[idx])

    def _select_tenant(self, tenant_id: str) -> None:
        tenant = self.store.get(tenant_id)
        if not tenant:
            return
        if self._all_tenants_mode:
            self._all_tenants_mode = False
            self.all_tenants_var.set(False)
        # Only skip the reload if this tenant's instances are actually the ones
        # currently displayed (not a stale/other account's list).
        if (
            self._selected_tenant_id == tenant_id
            and self._loaded_scope == tenant_id
            and not self._loading
        ):
            return
        self._selected_tenant_id = tenant_id
        self._compartment_override = None
        self.comp_var.set("(默认)")
        self._compartments_cache = []
        # Drop the previous account's compartment list so its ids can't leak into
        # the new account's queries.
        self._comp_label_map = {"(默认)": None}
        try:
            self.comp_combo.configure(values=["(默认)"])
        except Exception:
            pass
        try:
            self.store.set_active(tenant_id)
        except Exception:
            pass
        self._selected = None
        self._checked.clear()
        # Clear the previous account's rows immediately so nothing stale shows
        # while the new account loads.
        self._instances = []
        self._loaded_scope = None
        self._enriched_ids.clear()
        self._enrich_inflight.clear()
        self._apply_filter()
        self._render_detail(None)
        self._refresh_tenant_list()  # move the highlight to the newly selected account
        self._log(f"切换租户 → {tenant.name} ({tenant.region})")
        self.refresh_instances()

    def _toggle_all_tenants(self) -> None:
        self._all_tenants_mode = bool(self.all_tenants_var.get())
        self._selected = None
        self._checked.clear()
        self._instances = []
        self._loaded_scope = None
        self._enriched_ids.clear()
        self._enrich_inflight.clear()
        self._apply_filter()
        self._render_detail(None)
        self._refresh_tenant_list()
        self.refresh_instances()

    def _add_tenant(self) -> None:
        self._open_tenant_editor(None)

    def _edit_selected_tenant(self) -> None:
        tenant = self._selected_tenant_from_list()
        if not tenant:
            messagebox.showinfo("提示", "请先在左侧选择一个租户。", parent=self)
            return
        self._open_tenant_editor(tenant)

    def _delete_selected_tenant(self) -> None:
        tenant = self._selected_tenant_from_list()
        if not tenant:
            messagebox.showinfo("提示", "请先在左侧选择一个租户。", parent=self)
            return
        self._delete_tenant(tenant.id)

    def _test_selected_tenant(self) -> None:
        tenant = self._selected_tenant_from_list()
        if not tenant:
            messagebox.showinfo("提示", "请先在左侧选择一个租户。", parent=self)
            return
        self._test_tenant_connection(tenant)

    def _open_tenant_editor(self, tenant: Optional[TenantConfig]) -> None:
        def on_test(t: TenantConfig, dialog) -> None:
            self._test_tenant_connection(t, parent=dialog)

        dlg = TenantEditorDialog(self, tenant=tenant, on_test=on_test)
        self.wait_window(dlg)
        if not dlg.result:
            return
        saved: TenantConfig = dlg.result
        try:
            self.store.upsert(saved, make_active=tenant is None)
            self.sessions.drop(saved.id)
            self._selected_tenant_id = saved.id
            self._refresh_tenant_list()
            self._log(f"已保存租户配置：{saved.name}")
            self.refresh_instances()
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)

    def _delete_tenant(self, tenant_id: str) -> None:
        tenant = self.store.get(tenant_id)
        if not tenant:
            return
        if not ask_confirm(
            self,
            title="删除租户",
            message=f"确定删除租户「{tenant.name}」？\n本地 API 配置将被移除（不影响云上资源）。",
            confirm_text="删除",
            danger=True,
        ):
            return
        self.sessions.drop(tenant_id)
        self.store.delete(tenant_id)
        if self._selected_tenant_id == tenant_id:
            active = self.store.get_active()
            self._selected_tenant_id = active.id if active else None
            self._instances = []
            self._apply_filter()
            self._render_detail(None)
        self._refresh_tenant_list()
        self._log(f"已删除租户：{tenant.name}")

    def _test_tenant_connection(self, tenant: TenantConfig, parent=None) -> None:
        parent = parent or self
        self._set_status("正在测试连接…")
        self._log(f"测试连接：{tenant.name} / {tenant.region}")

        def work() -> OperationResult:
            session = TenantSession(tenant)
            try:
                return session.test_connection()
            finally:
                session.close()

        def ok(result: OperationResult) -> None:
            if result.ok:
                messagebox.showinfo("连接成功", result.message, parent=parent)
                self._log(f"连接成功：{result.message}", level="ok")
            else:
                messagebox.showerror("连接失败", result.message, parent=parent)
                self._log(f"连接失败：{result.message}", level="error")
            self._set_status("就绪")

        def err(exc: Exception) -> None:
            messagebox.showerror("连接失败", str(exc), parent=parent)
            self._log(f"连接异常：{exc}", level="error")
            self._set_status("就绪")

        self._run_async(work, ok, err)

    def _current_tenant(self) -> Optional[TenantConfig]:
        if not self._selected_tenant_id:
            return None
        return self.store.get(self._selected_tenant_id)

    def _record_tenant_tier(self, tenant_id: str, tier_code: str) -> None:
        """Persist a detected account tier and refresh the sidebar label."""
        tenant = self.store.get(tenant_id)
        if not tenant or tier_code not in ("paid", "free"):
            return
        if tenant.account_tier == tier_code:
            return
        tenant.account_tier = tier_code
        try:
            self.store.upsert(tenant)
        except Exception:  # noqa: BLE001
            return
        self._refresh_tenant_list()

    def _refresh_tenant_tiers(self) -> None:
        """Detect account tiers for tenants that don't have one cached yet.

        Subscription-only (no Service Limits / get_tenancy). Runs off the UI
        thread so the sidebar fills in 免费 / 已升级 by itself.
        """
        pending = [t for t in self.store.list_tenants() if t.enabled and not t.account_tier]
        if not pending:
            return

        def probe(tenant: TenantConfig) -> tuple[str, str]:
            try:
                result = self.sessions.get(tenant).detect_account_tier()
                code = (result.data or {}).get("tier_code", "") if result.ok else ""
            except Exception:  # noqa: BLE001
                code = ""
            return tenant.id, code

        def work() -> list[tuple[str, str]]:
            # Serialise a bit: many tenants × subscription API still risks 429.
            with ThreadPoolExecutor(max_workers=min(2, len(pending))) as pool:
                return list(pool.map(probe, pending))

        def ok(results: list[tuple[str, str]]) -> None:
            changed = False
            for tenant_id, code in results:
                tenant = self.store.get(tenant_id)
                if tenant and code in ("paid", "free") and tenant.account_tier != code:
                    tenant.account_tier = code
                    try:
                        self.store.upsert(tenant)
                    except Exception:  # noqa: BLE001
                        continue
                    changed = True
            if changed:
                self._refresh_tenant_list()

        self._run_async(work, ok, lambda _exc: None)

    # ==================================================================
    # Instances load / filter / render
    # ==================================================================
    def _is_current_load(self, gen: int, target_id: Optional[str] = None) -> bool:
        """True if a load tagged ``gen`` (for ``target_id``) is still the active one.

        A newer refresh/account-switch bumps ``_load_gen``; older in-flight loads
        return False here so their results are discarded instead of clobbering
        the freshly selected account's view.
        """
        if gen != self._load_gen:
            return False
        if target_id is not None and target_id != self._selected_tenant_id:
            return False
        return True

    def refresh_instances(self) -> None:
        # Each load gets a generation token. A newer request (e.g. the user
        # switching accounts) supersedes an older in-flight load: the stale
        # result is discarded instead of being blocked or overwriting the UI.
        # List loads intentionally skip per-instance IP / boot-volume enrichment
        # (resolve_ips=False) to stay within OCI API budgets; detail view fills those in.
        self._load_gen += 1
        gen = self._load_gen
        if self._all_tenants_mode:
            tenants = [t for t in self.store.list_tenants() if t.enabled]
            if not tenants:
                messagebox.showinfo("提示", "没有已启用的租户。", parent=self)
                return
            self._loading = True
            self.btn_refresh.configure(state="disabled", text="刷新中…")
            self._set_status(f"跨租户加载 {len(tenants)} 个账号…")
            include_sub = bool(self.sub_comp_var.get())

            def work() -> list[InstanceInfo]:
                all_items: list[InstanceInfo] = []
                errors = []

                def _load(t):
                    session = self.sessions.get(t)
                    return session.list_instances_tree(
                        resolve_ips=False, include_subcompartments=include_sub
                    )

                with ThreadPoolExecutor(max_workers=min(8, len(tenants))) as pool:
                    futures = [(t, pool.submit(_load, t)) for t in tenants]
                    for t, future in futures:
                        try:
                            all_items.extend(future.result())
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f"{t.name}: {exc}")
                if errors and not all_items:
                    raise RuntimeError("；".join(errors))
                return all_items

            def ok(items: list[InstanceInfo]) -> None:
                if not self._is_current_load(gen):
                    return  # superseded by a newer switch/refresh
                self._finish_load(items, label=f"跨租户 · {len(tenants)} 账号", scope="__all__")

            def err(exc: Exception) -> None:
                if not self._is_current_load(gen):
                    return
                self._loading = False
                self.btn_refresh.configure(state="normal", text="刷新")
                self._set_status("加载失败")
                self._log(f"跨租户加载失败：{exc}", level="error")
                messagebox.showerror("加载失败", str(exc), parent=self)

            self._run_async(work, ok, err)
            return

        tenant = self._current_tenant()
        if not tenant:
            messagebox.showinfo("提示", "请先添加并选择一个租户配置。", parent=self)
            return
        if not tenant.enabled:
            messagebox.showwarning("提示", "该租户已停用，请先编辑启用。", parent=self)
            return
        self._loading = True
        self.btn_refresh.configure(state="disabled", text="刷新中…")
        self._set_status(f"正在加载 {tenant.name} 的实例…")
        self._log(f"刷新实例：{tenant.name} · {tenant.region}")
        compartment = self._compartment_override
        include_sub = bool(self.sub_comp_var.get())
        target_id = tenant.id

        def work() -> list[InstanceInfo]:
            session = self.sessions.get(tenant)
            if include_sub:
                return session.list_instances_tree(
                    root_compartment_id=compartment,
                    resolve_ips=False,
                    include_subcompartments=True,
                )
            return session.list_instances(compartment_id=compartment, resolve_ips=False)

        def ok(items: list[InstanceInfo]) -> None:
            if not self._is_current_load(gen, target_id):
                return  # user switched away before this load finished — drop it
            self._finish_load(items, label=tenant.name, scope=target_id)

        def err(exc: Exception) -> None:
            if not self._is_current_load(gen, target_id):
                return
            self._loading = False
            self.btn_refresh.configure(state="normal", text="刷新")
            self._set_status("加载失败")
            self._log(f"加载实例失败：{exc}", level="error")
            messagebox.showerror("加载失败", str(exc), parent=self)

        self._run_async(work, ok, err)

    def _finish_load(self, items: list[InstanceInfo], label: str, scope: Optional[str] = None) -> None:
        self._loading = False
        self._loaded_scope = scope
        self.btn_refresh.configure(state="normal", text="刷新")
        prev_selected_id = self._selected.id if self._selected else None
        self._instances = items
        present = {i.id for i in items}
        # Lean list rows have empty IP/disk. Clear both completion and in-flight
        # markers so a refresh that races an enrich callback cannot leave the
        # selection stuck (inflight set + gen mismatch → never re-queues).
        self._enriched_ids.clear()
        self._enrich_inflight.clear()
        if self._selected:
            self._selected = next((i for i in items if i.id == self._selected.id), None)
        self._checked = {i for i in self._checked if i in present}
        self._apply_filter()
        self._render_detail(self._selected)
        # Re-enrich the previously selected row so IP/disk come back after a lean refresh.
        if self._selected and prev_selected_id == self._selected.id:
            self._ensure_instance_enriched(self._selected)
        running = sum(1 for i in items if i.lifecycle_state == "RUNNING")
        stopped = sum(1 for i in items if i.lifecycle_state == "STOPPED")
        self._set_status(f"{label} · 共 {len(items)} 台（运行 {running} / 停止 {stopped}）")
        self._log(f"已加载 {len(items)} 台实例（运行 {running}，停止 {stopped}）", level="ok")

    def _instance_needs_enrichment(self, inst: InstanceInfo) -> bool:
        if inst.lifecycle_state in ("TERMINATED", "TERMINATING"):
            return False
        # Require both network and disk. Do NOT treat "attempted" alone as done —
        # partial success (IP ok, disk fail) must still be retriable.
        if inst.id in self._enriched_ids:
            has_net = bool(inst.public_ip or inst.private_ip or inst.ipv6_addresses)
            has_disk = inst.boot_volume_gb is not None
            if has_net and has_disk:
                return False
            # Stale complete-marker with missing fields → allow retry.
            self._enriched_ids.discard(inst.id)
        has_net = bool(inst.public_ip or inst.private_ip or inst.ipv6_addresses)
        has_disk = inst.boot_volume_gb is not None
        return not (has_net and has_disk)

    def _ensure_instance_enriched(self, inst: InstanceInfo) -> None:
        """Background-fill IP / boot volume for one instance when the user focuses it."""
        if not inst or not self._instance_needs_enrichment(inst):
            return
        if inst.id in self._enrich_inflight:
            return
        tenant = self._tenant_for_instance(inst)
        if not tenant:
            return
        self._enrich_inflight.add(inst.id)
        instance_id = inst.id
        gen = self._load_gen
        # Snapshot for the worker — avoid touching self._instances off the UI thread.
        # enrich_instance mutates the object; work on a shallow copy so a concurrent
        # refresh that replaces list rows cannot race mid-fill.
        from copy import copy

        target = copy(inst)

        def work() -> InstanceInfo:
            session = self.sessions.get(tenant)
            return session.enrich_instance(target)

        def ok(info: InstanceInfo) -> None:
            self._enrich_inflight.discard(instance_id)
            if gen != self._load_gen:
                # A newer list load won; re-queue for the current selection if it
                # is still this instance (finish_load may already have done so).
                if self._selected and self._selected.id == instance_id:
                    self._ensure_instance_enriched(self._selected)
                return
            # Merge into the list copy of the same id (may have been replaced by filter rebuild).
            for i, row in enumerate(self._instances):
                if row.id == instance_id:
                    self._instances[i] = info
                    break
            else:
                # Instance no longer in the loaded set (switched tenant / terminated).
                return
            has_net = bool(info.public_ip or info.private_ip or info.ipv6_addresses)
            has_disk = info.boot_volume_gb is not None
            if has_net and has_disk:
                self._enriched_ids.add(instance_id)
            else:
                # Partial fill — keep retriable (e.g. disk attachment lag).
                self._enriched_ids.discard(instance_id)
            if instance_id in self._row_by_id:
                self._row_by_id[instance_id] = info
            if self._selected and self._selected.id == instance_id:
                self._selected = info
                self._render_detail(info)
            # Patch the tree row without a full re-filter when still visible.
            if self.tree.exists(instance_id):
                try:
                    self.tree.set(instance_id, "disk", info.disk_text())
                    self.tree.set(instance_id, "diskperf", info.disk_perf_text())
                    self.tree.set(instance_id, "pubip", info.public_ip or "—")
                    self.tree.set(instance_id, "privip", info.private_ip or "—")
                    self.tree.set(instance_id, "ipv6", info.ipv6_text())
                except tk.TclError:
                    pass

        def err(_exc: Exception) -> None:
            self._enrich_inflight.discard(instance_id)
            # Leave id out of _enriched_ids so the user can re-select to retry.

        self._run_async(work, ok, err)

    def _on_tree_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        inst = self._row_by_id.get(sel[0])
        if inst:
            self._selected = inst
            self._render_detail(inst)
            self._ensure_instance_enriched(inst)

    def _apply_filter(self) -> None:
        q = (self.search_var.get() or "").strip().lower()
        state = self.state_filter.get()
        items = list(self._instances)
        if state and state != "全部":
            items = [i for i in items if i.lifecycle_state == state]
        if q:
            def match(i: InstanceInfo) -> bool:
                blob = " ".join([
                    i.display_name, i.id, i.shape, i.public_ip, i.private_ip,
                    i.ipv6_text(), i.availability_domain, i.lifecycle_state,
                    i.tenant_name, i.ocpu_text(), i.memory_text(),
                    i.disk_text(), i.disk_perf_text(),
                ]).lower()
                return q in blob

            items = [i for i in items if match(i)]
        self._filtered = items
        self._render_instance_rows()
        self.lbl_instance_stats.configure(text=f"显示 {len(items)} / 共 {len(self._instances)}")
        self.lbl_checked.configure(text=f"已选 {len(self._checked)}")

    def _render_instance_rows(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._row_by_id = {}
        for idx, inst in enumerate(self._filtered):
            self._row_by_id[inst.id] = inst
            mark = "☑" if inst.id in self._checked else "☐"
            stripe = "oddrow" if idx % 2 else "evenrow"
            # Shape column keeps just the shape name; OCPU/mem/disk have own columns.
            self.tree.insert(
                "",
                "end",
                iid=inst.id,
                values=(
                    mark,
                    inst.lifecycle_state,
                    inst.display_name,
                    inst.tenant_name,
                    inst.shape or "—",
                    inst.ocpu_text(),
                    inst.memory_text(),
                    inst.disk_text(),
                    inst.disk_perf_text(),
                    inst.public_ip or "—",
                    inst.private_ip or "—",
                    inst.ipv6_text(),
                    inst.availability_domain or "—",
                ),
                tags=(stripe, inst.lifecycle_state),
            )
        if self._selected and self._selected.id in self._row_by_id:
            try:
                self.tree.selection_set(self._selected.id)
            except tk.TclError:
                pass

    def _on_tree_click(self, event) -> Optional[str]:
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return None
        col = self.tree.identify_column(event.x)
        row = self.tree.identify_row(event.y)
        if not row:
            return None
        if col == "#1":  # checkbox column
            if row in self._checked:
                self._checked.discard(row)
                self.tree.set(row, "chk", "☐")
            else:
                self._checked.add(row)
                self.tree.set(row, "chk", "☑")
            self.lbl_checked.configure(text=f"已选 {len(self._checked)}")
            return "break"  # do not change selection
        # Clicking an address cell copies that address.
        col_name = self._tree_column_name(col)
        if col_name in ("pubip", "privip", "ipv6"):
            inst = self._row_by_id.get(row)
            if not inst:
                return None
            # Lazy-load IP if the user clicks the address cell before selection enrich finishes.
            if self._instance_needs_enrichment(inst):
                self._ensure_instance_enriched(inst)
                self._set_status("正在解析地址…稍后再点复制")
                return None
            if col_name == "pubip":
                value, label = inst.public_ip, "公网 IPv4"
            elif col_name == "privip":
                value, label = inst.private_ip, "私网 IPv4"
            else:
                # Prefer first IPv6; multi-address rows still copy all via right-click.
                value, label = inst.primary_ipv6(), "IPv6"
            if value:
                self._copy_text(value, label)
            else:
                self._set_status(f"该实例没有可复制的{label}")
        return None

    def _tree_column_name(self, column_id: str) -> str:
        """Map a Treeview '#N' column id to its logical name."""
        try:
            index = int(str(column_id).lstrip("#")) - 1
        except (TypeError, ValueError):
            return ""
        cols = getattr(self, "_tree_cols", ())
        return cols[index] if 0 <= index < len(cols) else ""

    def _render_detail(self, inst: Optional[InstanceInfo]) -> None:
        self.detail_body.configure(state="normal")
        self.detail_body.delete("1.0", "end")
        if not inst:
            self.detail_name.configure(text="未选择实例")
            self.detail_state.configure(text="", fg=C.TEXT_MUTE)
            self.detail_body.insert("1.0", "点选中间列表中的实例。\n右键可电源/批量/复制。")
            self.detail_body.configure(state="disabled")
            self._set_power_enabled(False)
            return
        color = STATE_COLORS.get(inst.lifecycle_state, C.TEXT_MUTE)
        self.detail_name.configure(text=inst.display_name)
        self.detail_state.configure(text=f"● {inst.lifecycle_state}", fg=color)
        pending = self._instance_needs_enrichment(inst) or inst.id in self._enrich_inflight
        net_placeholder = "解析中…" if pending else "—"
        disk_placeholder = "解析中…" if pending else "—"
        disk_text = inst.disk_text() if inst.boot_volume_gb is not None else disk_placeholder
        disk_perf = inst.disk_perf_text() if inst.boot_vpus_per_gb is not None else disk_placeholder
        lines = [
            f"租户：{inst.tenant_name}",
            f"Region：{inst.region}",
            f"Shape：{inst.shape or '—'}",
            f"核心：{inst.ocpu_text()} OCPU",
            f"内存：{inst.memory_text()}",
            f"硬盘：{disk_text} · {disk_perf}",
            f"可用域：{inst.availability_domain or '—'}",
            f"公网：{inst.public_ip or net_placeholder}",
            f"私网：{inst.private_ip or net_placeholder}",
            f"IPv6：{inst.ipv6_text() if inst.ipv6_addresses else net_placeholder}",
            f"创建：{_short_time(inst.time_created)}",
        ]
        # Surface root password (if tagged at launch) so the user can always find it.
        root_pw = ""
        if inst.freeform_tags:
            from app.oci_client import ROOT_PASSWORD_TAG

            root_pw = str(inst.freeform_tags.get(ROOT_PASSWORD_TAG) or "").strip()
        if root_pw:
            lines.append(f"root 密码：{root_pw}")
        if inst.freeform_tags:
            # Hide the raw password tag from the dump (already shown above); keep other tags.
            from app.oci_client import ROOT_PASSWORD_TAG

            other = {
                k: v
                for k, v in inst.freeform_tags.items()
                if k != ROOT_PASSWORD_TAG
            }
            if other:
                lines.append("标签：" + ", ".join(f"{k}={v}" for k, v in other.items()))
        self.detail_body.insert("1.0", "\n".join(lines))
        self.detail_body.configure(state="disabled")
        allowed = inst.allowed_actions()
        for action, btn in self.power_buttons.items():
            btn.configure(state="normal" if action in allowed else "disabled")
        self.btn_terminate.configure(state="normal" if inst.lifecycle_state not in ("TERMINATED", "TERMINATING") else "disabled")

    def _set_power_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in self.power_buttons.values():
            btn.configure(state=state)
        self.btn_terminate.configure(state=state)

    def _select_all_visible(self) -> None:
        for i in self._filtered:
            self._checked.add(i.id)
        self._apply_filter()

    def _clear_checks(self) -> None:
        self._checked.clear()
        self._apply_filter()

    # ==================================================================
    # Power / batch / terminate / rename
    # ==================================================================
    def _tenant_for_instance(self, inst: InstanceInfo) -> Optional[TenantConfig]:
        return self.store.get(inst.tenant_id)

    def _do_power(self, action: str) -> None:
        inst = self._selected
        if not inst:
            return
        tenant = self._tenant_for_instance(inst)
        if not tenant:
            messagebox.showerror("错误", "找不到该实例所属租户配置", parent=self)
            return
        label = POWER_ACTIONS.get(action, action)
        if not ask_confirm(
            self,
            title=f"确认{label}",
            message=f"确定对「{inst.display_name}」执行【{label}】？\n状态：{inst.lifecycle_state}\n租户：{inst.tenant_name}",
            confirm_text=label,
            danger=action in {"STOP", "RESET"},
        ):
            return
        self._set_status(f"提交 {label}…")
        self._log(f"→ {label}：{inst.display_name} @ {inst.tenant_name}")

        def work() -> OperationResult:
            return self.sessions.get(tenant).instance_action(inst.id, action)

        def ok(result: OperationResult) -> None:
            if result.ok:
                self._log(f"✓ {result.message}", level="ok")
                self._set_status(result.message)
                self.after(1500, self.refresh_instances)
            else:
                self._log(f"✗ {result.message}", level="error")
                messagebox.showerror("操作失败", result.message, parent=self)

        def err(exc: Exception) -> None:
            self._log(f"✗ 异常：{exc}", level="error")
            messagebox.showerror("操作异常", str(exc), parent=self)

        self._run_async(work, ok, err)

    def _batch_power(self, action: str) -> None:
        targets = [i for i in self._instances if i.id in self._checked]
        if not targets:
            messagebox.showinfo("提示", "请先勾选要操作的实例。", parent=self)
            return
        label = POWER_ACTIONS.get(action, action)
        eligible = [i for i in targets if i.can(action)]
        skipped = len(targets) - len(eligible)
        if not eligible:
            messagebox.showwarning("无法执行", f"选中的实例当前状态都不支持「{label}」。", parent=self)
            return
        if not ask_confirm(
            self,
            title=f"批量{label}",
            message=f"将对 {len(eligible)} 台实例执行【{label}】。\n" + (f"另有 {skipped} 台因状态跳过。\n" if skipped else "") + "是否继续？",
            confirm_text=f"批量{label}",
            danger=action in {"STOP", "RESET"},
        ):
            return
        self._log(f"→ 批量{label}：{len(eligible)} 台")
        self._set_status(f"批量{label} 进行中…")

        def work() -> list[tuple[str, OperationResult]]:
            results = []
            for inst in eligible:
                tenant = self._tenant_for_instance(inst)
                if not tenant:
                    results.append((inst.display_name, OperationResult(False, "租户配置丢失")))
                    continue
                try:
                    r = self.sessions.get(tenant).instance_action(inst.id, action)
                except Exception as exc:  # noqa: BLE001
                    r = OperationResult(False, str(exc))
                results.append((inst.display_name, r))
            return results

        def ok(results: list) -> None:
            ok_n = sum(1 for _, r in results if r.ok)
            for name, r in results:
                self._log(("✓ " if r.ok else "✗ ") + f"{name}: {r.message}", level="ok" if r.ok else "error")
            self._set_status(f"批量{label} 完成：成功 {ok_n}/{len(results)}")
            self.after(1500, self.refresh_instances)

        def err(exc: Exception) -> None:
            self._log(f"批量异常：{exc}", level="error")
            messagebox.showerror("批量失败", str(exc), parent=self)

        self._run_async(work, ok, err)

    def _do_terminate(self) -> None:
        inst = self._selected
        tenant = self._tenant_for_instance(inst) if inst else None
        if not inst or not tenant:
            return
        if not ask_confirm(
            self,
            title="终止实例（危险）",
            message=f"终止后计算资源不可恢复！\n\n实例：{inst.display_name}\n租户：{inst.tenant_name}\nOCID：{inst.id}\n\n确定要终止吗？",
            confirm_text="终止",
            danger=True,
        ):
            return
        delete_boot = messagebox.askyesno(
            "Boot Volume",
            "是否同时删除引导卷（Boot Volume）？\n\n是 = 一并删除（默认）\n否 = 保留引导卷",
            parent=self,
            default=messagebox.YES,
        )
        preserve = not delete_boot
        self._log(f"→ 终止：{inst.display_name}（删除引导卷={delete_boot}）")

        def work() -> OperationResult:
            return self.sessions.get(tenant).terminate_instance(inst.id, preserve_boot_volume=bool(preserve))

        def ok(result: OperationResult) -> None:
            if result.ok:
                self._log(f"✓ {result.message}", level="ok")
                self.after(1500, self.refresh_instances)
            else:
                self._log(f"✗ {result.message}", level="error")
                messagebox.showerror("终止失败", result.message, parent=self)

        def err(exc: Exception) -> None:
            self._log(f"✗ 异常：{exc}", level="error")
            messagebox.showerror("终止异常", str(exc), parent=self)

        self._run_async(work, ok, err)

    def _rename_instance(self) -> None:
        inst = self._selected
        tenant = self._tenant_for_instance(inst) if inst else None
        if not inst or not tenant:
            return
        dlg = TextPromptDialog(self, title="重命名实例", label="新的显示名称：", initial=inst.display_name)
        self.wait_window(dlg)
        if not dlg.result or dlg.result == inst.display_name:
            return
        new_name = dlg.result

        def work() -> OperationResult:
            return self.sessions.get(tenant).rename_instance(inst.id, new_name)

        def ok(result: OperationResult) -> None:
            if result.ok:
                self._log(f"✓ {result.message}", level="ok")
                self.refresh_instances()
            else:
                messagebox.showerror("重命名失败", result.message, parent=self)

        def err(exc: Exception) -> None:
            messagebox.showerror("重命名异常", str(exc), parent=self)

        self._run_async(work, ok, err)

    # ==================================================================
    # Launch / capacity retry
    # ==================================================================
    def _open_launch(self) -> None:
        tenant = self._current_tenant()
        if not tenant or self._all_tenants_mode:
            messagebox.showinfo("提示", "请先选择单个租户（关闭跨租户视图）再创建实例。", parent=self)
            return
        cache_key = f"{tenant.id}|{tenant.region}|{tenant.compartment_ocid or tenant.tenancy_ocid}"
        cached = self._launch_meta_cache.get(cache_key)
        if cached:
            ts, meta = cached
            if time.monotonic() - ts < _LAUNCH_META_TTL_SEC and meta.get("ads"):
                age = int(time.monotonic() - ts)
                self._log(f"使用缓存的创建元数据（{age}s 前，{tenant.name}）", level="info")
                self._present_launch_dialog(tenant, meta, cache_key=cache_key)
                return

        self._set_status("加载创建向导元数据…")
        self._log(f"准备创建实例：{tenant.name}")

        def work() -> dict:
            return self._fetch_launch_meta(tenant)

        def ok(meta: dict) -> None:
            self._set_status("就绪")
            self._launch_meta_cache[cache_key] = (time.monotonic(), meta)
            self._present_launch_dialog(tenant, meta, cache_key=cache_key)

        def err(exc: Exception) -> None:
            self._set_status("就绪")
            self._log(f"加载创建元数据失败：{exc}", level="error")
            messagebox.showerror("加载失败", str(exc), parent=self)

        self._run_async(work, ok, err)

    def _fetch_launch_meta(self, tenant: TenantConfig) -> dict:
        """Load AD / image / shape / default-network data for the create wizard.

        Intentionally does NOT scan every compartment for VCNs — the form uses
        the account default network only, so a full VCN walk is pure API waste.
        """
        session = self.sessions.get(tenant)
        comps = session.list_compartments()
        ads = session.list_availability_domains()
        images = session.list_images(compartment_id=tenant.tenancy_ocid, ubuntu_only=True)
        if not images:
            images = session.list_images(ubuntu_only=True)
        shapes = session.list_shapes(compartment_id=tenant.tenancy_ocid)
        if not shapes:
            shapes = session.list_shapes()

        default_comp = tenant.compartment_ocid or tenant.tenancy_ocid
        network = session.ensure_default_network(compartment_id=default_comp, create_if_missing=True)
        if not network.ok:
            raise OCIClientError(network.message or "无法准备默认网络（VCN/Subnet）")
        net_data = network.data or {}
        vcns = list(net_data.get("vcns") or [])
        subnets_by_vcn = dict(net_data.get("subnets_by_vcn") or {})
        return {
            "compartments": comps,
            "ads": ads,
            "images": images,
            "shapes": shapes,
            "vcns": vcns,
            "subnets_by_vcn": subnets_by_vcn,
            "default_compartment": default_comp,
            "network_note": network.message,
            "network_created": bool(net_data.get("created")),
            "preferred_vcn_id": (net_data.get("vcn") or {}).get("id", ""),
            "preferred_subnet_id": (net_data.get("subnet") or {}).get("id", ""),
        }

    def _present_launch_dialog(
        self,
        tenant: TenantConfig,
        meta: dict,
        *,
        cache_key: str = "",
    ) -> None:
        if not meta.get("ads"):
            messagebox.showerror("错误", "无法获取 Availability Domain，请检查权限。", parent=self)
            return
        if not meta.get("images"):
            messagebox.showwarning("警告", "未获取到镜像列表，仍可打开表单但可能无法创建。", parent=self)
        if not meta.get("vcns") or not any(meta.get("subnets_by_vcn") or {}.values()):
            messagebox.showerror(
                "网络不可用",
                "当前租户没有可用的 VCN/Subnet，且自动创建失败。\n"
                + (meta.get("network_note") or "请检查 virtual-network-family 权限后重试。"),
                parent=self,
            )
            # Drop a bad cache entry so the next open retries the network setup.
            if cache_key:
                self._launch_meta_cache.pop(cache_key, None)
            return
        if meta.get("network_created"):
            self._log(f"✓ {meta.get('network_note') or '已自动创建默认网络'}", level="ok")
            messagebox.showinfo(
                "已自动创建网络",
                (meta.get("network_note") or "已自动创建默认 VCN / 公网 Subnet。")
                + "\n\n创建向导将自动使用该默认网络（不再手动选择 Compartment / VCN / Subnet）。",
                parent=self,
            )
            # Don't re-toast "network created" on every open within the cache TTL.
            meta = dict(meta)
            meta["network_created"] = False
            if cache_key:
                prev = self._launch_meta_cache.get(cache_key)
                ts = prev[0] if prev else time.monotonic()
                self._launch_meta_cache[cache_key] = (ts, meta)
        elif meta.get("network_note"):
            self._log(meta["network_note"], level="info")
        dlg = LaunchInstanceDialog(self, meta=meta, default_name=f"ocibot-{datetime.now().strftime('%m%d%H%M')}")
        self.wait_window(dlg)
        if not dlg.result:
            return
        self._submit_launch(tenant, dlg.result)

    def _submit_launch(self, tenant: TenantConfig, result: dict) -> None:
        payload = result["payload"]
        root_password = (result.get("secrets") or {}).get("root_password", "")
        (result.get("secrets") or {}).clear()
        as_retry = bool(result.get("as_retry", False)) and payload.get("auth_mode") == "key"

        def work() -> OperationResult:
            session = self.sessions.get(tenant)
            if payload.get("assign_ipv6_ip"):
                ipv6 = session.ensure_subnet_ipv6(
                    payload["subnet_id"],
                    payload.get("network_compartment_id") or payload["compartment_id"],
                )
                if not ipv6.ok:
                    ipv6.data = {**(ipv6.data or {}), "stage": "ipv6"}
                    ipv6.message = "IPv6 网络准备失败：" + ipv6.message
                    return ipv6
            if not payload.get("managed_nsg_id"):
                token = uuid.uuid4().hex
                nsg = session.create_managed_nsg(
                    vcn_id=payload["vcn_id"],
                    compartment_id=payload["network_compartment_id"],
                    display_name=payload.get("display_name", "instance"),
                    include_ipv6=bool(payload.get("assign_ipv6_ip")),
                    launch_token=token,
                )
                if not nsg.ok:
                    nsg.data = {"stage": "nsg"}
                    return nsg
                payload["managed_nsg_id"] = nsg.data["nsg_id"]
                payload["nsg_ids"] = [nsg.data["nsg_id"]]
                payload["launch_token"] = token
            try:
                op = session.launch_from_payload(payload, root_password=root_password)
            except Exception:
                session.delete_managed_nsg(payload.get("managed_nsg_id", ""))
                raise
            capacity_failure = bool((op.data or {}).get("capacity")) or is_capacity_message(op.message)
            if not op.ok and (not capacity_failure or payload.get("auth_mode") == "password"):
                session.delete_managed_nsg(payload.get("managed_nsg_id", ""))
            return op

        def ok(op: OperationResult) -> None:
            self._set_status("就绪")
            if op.ok:
                instance_id = (op.data or {}).get("instance_id", "")
                self._log(f"✓ {op.message} id={instance_id}", level="ok")
                # VPU is ignored at launch on Always-Free; apply it by editing the
                # boot volume once it exists.
                vpu = int(payload.get("boot_volume_vpus_per_gb") or 10)
                if vpu != 10 and instance_id:
                    comp = payload["compartment_id"]
                    self._log(f"→ 等引导卷数据同步（hydration）完成后自动调整性能为 {vpu} VPUs/GB（可能需要几分钟到二十分钟）…", level="info")
                    self._run_async(
                        lambda: self.sessions.get(tenant).resize_boot_volume(instance_id, comp, vpus_per_gb=vpu),
                        lambda r: self._log(("✓ " if r.ok else "✗ ") + r.message, level="ok" if r.ok else "warn"),
                        lambda exc: self._log(f"调整引导卷性能失败：{exc}", level="warn"),
                    )
                assigned_ipv6 = bool(payload.get("assign_ipv6_ip")) and bool(payload.get("subnet_id"))
                password_note = ""
                if payload.get("auth_mode") == "password" and root_password:
                    password_note = (
                        f"\n\nroot 密码：{root_password}\n"
                        "已写入实例标签 ocibot_root_password，"
                        "之后可在右侧详情或列表右键「复制 root 密码」查看。"
                        "\n注意：能读取该实例的人都能看到此标签。"
                    )
                messagebox.showinfo(
                    "已提交",
                    op.message
                    + password_note
                    + (
                        f"\n\n引导卷性能将在数据同步（hydration）完成后自动调整为 {vpu} VPUs/GB，"
                        "通常需要几分钟到二十分钟，进度见操作日志。"
                        if vpu != 10
                        else ""
                    )
                    + (
                        "\n\n已选择 IPv6：VCN / Subnet IPv6、Internet Gateway 与 ::/0 默认路由"
                        "已在提交前准备完成（NSG 已同时放行 IPv4 与 IPv6）。"
                        if assigned_ipv6
                        else ""
                    ),
                    parent=self,
                )
                self.after(2000, self.refresh_instances)
                return
            self._log(f"✗ 创建失败：{op.message}", level="error")
            capacity_failure = bool((op.data or {}).get("capacity")) or is_capacity_message(op.message)

            def _enqueue_retry() -> None:
                interval = clamp_retry_interval(result.get("retry_interval") or DEFAULT_RETRY_INTERVAL_SEC)
                max_attempts = clamp_max_attempts(result.get("retry_max") or DEFAULT_MAX_ATTEMPTS)
                job = CapacityRetryJob(
                    id=str(uuid.uuid4()),
                    name=f"容量重试 {payload.get('display_name', '')}",
                    tenant_id=tenant.id,
                    enabled=True,
                    launch_payload=payload,
                    availability_domains=list(result.get("retry_ads") or []),
                    interval_sec=interval,
                    max_attempts=max_attempts,
                    attempts=1,
                    status="running",
                    last_error=op.message,
                    last_attempt_at=datetime.now().isoformat(timespec="seconds"),
                    cooldown_until="",
                    consecutive_rate_limits=0,
                )
                self.job_store.upsert_retry(job)
                self._update_jobs_badge()
                ad_note = f"，轮询 {len(job.availability_domains)} 个可用域" if job.availability_domains else ""
                self._log(
                    f"已加入容量重试：{job.name}（间隔 {job.interval_sec}s，最多 {job.max_attempts} 次{ad_note}）",
                    level="warn",
                )

            if payload.get("auth_mode") == "key" and capacity_failure and (op.data or {}).get("stage") not in {"nsg", "ipv6"}:
                if as_retry:
                    _enqueue_retry()
                    messagebox.showinfo(
                        "容量不足",
                        f"创建失败：\n{op.message}\n\n已按合规限速加入容量重试"
                        f"（间隔 ≥{MIN_RETRY_INTERVAL_SEC} 秒，有限次数）。",
                        parent=self,
                    )
                    return
                if messagebox.askyesno(
                    "容量不足",
                    f"创建失败：\n{op.message}\n\n"
                    f"是否加入容量重试？将按合规限速后台重试"
                    f"（间隔 ≥{MIN_RETRY_INTERVAL_SEC} 秒，有限次数，遇 429 自动拉长冷却）。",
                    parent=self,
                ):
                    _enqueue_retry()
                    return
                managed_nsg_id = payload.get("managed_nsg_id", "")
                if managed_nsg_id:
                    self._run_async(
                        lambda: self.sessions.get(tenant).delete_managed_nsg(managed_nsg_id),
                        lambda cleanup: self._log(cleanup.message, level="ok" if cleanup.ok else "warn"),
                        lambda exc: self._log(f"清理 NSG 失败 …{managed_nsg_id[-8:]}：{exc}", level="warn"),
                    )
            messagebox.showerror("创建失败", op.message, parent=self)

        def err(exc: Exception) -> None:
            self._set_status("就绪")
            self._log(f"创建异常：{exc}", level="error")
            messagebox.showerror("创建异常", str(exc), parent=self)

        self._set_status("提交创建实例…")
        self._log(f"→ 创建实例：{payload.get('display_name')} shape={payload.get('shape')}")
        self._run_async(work, ok, err)

    # ==================================================================
    # Compartment helpers
    # ==================================================================
    def _load_compartments(self) -> None:
        tenant = self._current_tenant()
        if not tenant or self._all_tenants_mode:
            messagebox.showinfo("提示", "请先选择单个租户。", parent=self)
            return
        self._set_status("加载 Compartment…")

        def work():
            return self.sessions.get(tenant).list_compartments()

        def ok(items: list) -> None:
            self._compartments_cache = items
            self._comp_label_map = {"(默认)": None}
            used: set[str] = set()
            labels = ["(默认)"]
            for c in items:
                lab = c["name"] or c["id"][-12:]
                if lab in used:
                    lab = f"{c['name']} ({c['id'][-6:]})"
                used.add(lab)
                labels.append(lab)
                self._comp_label_map[lab] = c["id"]
            self.comp_combo.configure(values=labels)
            self.comp_var.set("(默认)")
            self._compartment_override = None
            self._set_status("就绪")
            self._log(f"已加载 {len(items)} 个 Compartment", level="ok")

        def err(exc: Exception) -> None:
            self._set_status("就绪")
            messagebox.showerror("加载失败", str(exc), parent=self)

        self._run_async(work, ok, err)

    def _on_compartment_change(self) -> None:
        choice = self.comp_var.get()
        mapping = getattr(self, "_comp_label_map", {"(默认)": None})
        if choice not in mapping and choice != "(默认)":
            return
        self._compartment_override = mapping.get(choice)
        self.refresh_instances()

    def _on_subcomp_toggle(self) -> None:
        self._include_subcompartments = bool(self.sub_comp_var.get())
        self.refresh_instances()

    # ==================================================================
    # Jobs center + background handlers
    # ==================================================================
    def _open_jobs_center(self) -> None:
        dlg = JobsCenterDialog(
            self,
            self.job_store,
            self.store.list_tenants(),
            on_changed=self._update_jobs_badge,
            on_delete_retry=self._delete_retry_job,
        )
        self.wait_window(dlg)
        self._update_jobs_badge()

    def _delete_retry_job(self, job: CapacityRetryJob) -> None:
        self.job_store.delete_retry(job.id)
        nsg_id = (job.launch_payload or {}).get("managed_nsg_id", "")
        tenant = self.store.get(job.tenant_id)
        if not nsg_id or not tenant or job.success_instance_id:
            return
        self._run_async(
            lambda: self.sessions.get(tenant).delete_managed_nsg(nsg_id),
            lambda result: self._log(result.message, level="ok" if result.ok else "warn"),
            lambda exc: self._log(f"清理未使用 NSG …{nsg_id[-8:]} 失败：{exc}", level="warn"),
        )

    def _update_jobs_badge(self) -> None:
        n_s = sum(1 for j in self.job_store.list_schedules() if j.enabled)
        n_r = sum(1 for j in self.job_store.list_retries() if j.enabled and j.status in ("idle", "running"))
        self.lbl_jobs.configure(text=f"定时 {n_s} · 重试 {n_r}")

    def _scheduler_tick(self) -> None:
        try:
            self.runner.tick()
        except Exception as exc:  # noqa: BLE001
            self._report_background_error("任务调度异常", exc)
        self.after(1000, self._scheduler_tick)

    def _handle_schedule_fire(self, job) -> None:
        """Runs in background thread."""
        tenant = self.store.get(job.tenant_id)
        if not tenant:
            self._ui_queue.put(("log", None, (f"定时任务 {job.name}：租户不存在", "error")))
            return
        if not tenant.enabled:
            self._ui_queue.put(("log", None, (f"定时任务 {job.name}：租户已停用，跳过", "warn")))
            return
        try:
            session = self.sessions.get(tenant)
            if job.instance_ids:
                targets = list(job.instance_ids)
            else:
                items = session.list_instances_tree(resolve_ips=False, include_subcompartments=True)
                targets = [i.id for i in items if i.lifecycle_state not in ("TERMINATED", "TERMINATING")]
            if not targets:
                self._ui_queue.put(("log", None, (f"定时任务 {job.name}：没有可操作的实例", "warn")))
                return
            ok_n = 0
            for iid in targets:
                r = session.instance_action(iid, job.action)
                if r.ok:
                    ok_n += 1
                self._ui_queue.put(
                    (
                        "log",
                        None,
                        (
                            (("✓ " if r.ok else "✗ ") + f"定时 {job.name}: …{iid[-12:]} {r.message}"),
                            "ok" if r.ok else "error",
                        ),
                    )
                )
            self._ui_queue.put(("log", None, (f"定时任务完成：{job.name} 成功 {ok_n}/{len(targets)}", "ok")))
            self._ui_queue.put(("refresh", None, None))
        except Exception as exc:  # noqa: BLE001
            self._ui_queue.put(("log", None, (f"定时任务失败 {job.name}: {exc}", "error")))

    def _handle_retry_tick(self, job: CapacityRetryJob) -> None:
        """Runs in background thread; job already marked running.

        Compliance behaviour:
        - only capacity (OutOfHostCapacity) continues the loop
        - 429 / TooManyRequests sets cooldown_until with exponential backoff
        - finite max_attempts always enforced
        """
        tenant = self.store.get(job.tenant_id)
        if not tenant:
            job.status = "failed"
            job.last_error = "租户不存在"
            job.enabled = False
            self.job_store.upsert_retry(job)
            self._ui_queue.put(("log", None, (f"容量重试失败：{job.name} 租户不存在", "error")))
            return
        job.attempts = int(job.attempts or 0) + 1
        job.last_attempt_at = datetime.now().isoformat(timespec="seconds")
        job.interval_sec = clamp_retry_interval(job.interval_sec)
        job.max_attempts = clamp_max_attempts(job.max_attempts)
        # Rotate availability domains across attempts (still one LaunchInstance per tick).
        payload = dict(job.launch_payload or {})
        ad_note = ""
        ads = list(getattr(job, "availability_domains", None) or [])
        if ads:
            ad = ads[(job.attempts - 1) % len(ads)]
            payload["availability_domain"] = ad
            ad_note = f"（AD：{ad}）"
        try:
            session = self.sessions.get(tenant)
            result = session.launch_from_payload(payload)
            if result.ok:
                job.status = "success"
                job.enabled = False
                job.last_error = ""
                job.cooldown_until = ""
                job.consecutive_rate_limits = 0
                job.success_instance_id = (result.data or {}).get("instance_id", "")
                self.job_store.upsert_retry(job)
                self._ui_queue.put(("log", None, (f"容量重试成功：{job.name} → {job.success_instance_id} {ad_note}", "ok")))
                self._ui_queue.put(("refresh", None, None))
                return
            job.last_error = result.message
            rate_limited = bool((result.data or {}).get("rate_limited")) or is_rate_limit_message(result.message)
            capacity_failure = bool((result.data or {}).get("capacity")) or is_capacity_message(result.message)

            if rate_limited:
                job.consecutive_rate_limits = int(job.consecutive_rate_limits or 0) + 1
                delay = rate_limit_backoff_sec(job.consecutive_rate_limits)
                until = datetime.now().astimezone().timestamp() + delay
                job.cooldown_until = datetime.fromtimestamp(until).astimezone().isoformat(timespec="seconds")
                job.status = "running"
                # Cap attempts still apply after cooldown.
                if job.attempts >= job.max_attempts:
                    job.status = "failed"
                    job.enabled = False
                    job.cooldown_until = ""
                    self.job_store.upsert_retry(job)
                    nsg_id = (job.launch_payload or {}).get("managed_nsg_id", "")
                    if nsg_id:
                        cleanup = session.delete_managed_nsg(nsg_id)
                        self._ui_queue.put(("log", None, (cleanup.message, "ok" if cleanup.ok else "warn")))
                    self._ui_queue.put(
                        ("log", None, (f"容量重试达最大次数（含限流）：{job.name} — {result.message}", "error"))
                    )
                    return
                self.job_store.upsert_retry(job)
                self._ui_queue.put(
                    (
                        "log",
                        None,
                        (
                            f"API 限流 429，冷却 {delay}s 后再试 "
                            f"#{job.attempts}/{job.max_attempts} {ad_note}：{job.name}",
                            "warn",
                        ),
                    )
                )
                return

            # Non-capacity permanent failure → stop (do not hammer the API).
            if not capacity_failure:
                job.status = "failed"
                job.enabled = False
                job.cooldown_until = ""
                job.consecutive_rate_limits = 0
                self.job_store.upsert_retry(job)
                nsg_id = (job.launch_payload or {}).get("managed_nsg_id", "")
                if nsg_id:
                    cleanup = session.delete_managed_nsg(nsg_id)
                    self._ui_queue.put(("log", None, (cleanup.message, "ok" if cleanup.ok else "warn")))
                self._ui_queue.put(
                    ("log", None, (f"容量重试遇到非容量错误，已停止：{job.name} — {result.message}", "error"))
                )
                return

            # Capacity path: clear rate-limit streak, keep fixed interval.
            job.consecutive_rate_limits = 0
            job.cooldown_until = ""
            if job.attempts >= job.max_attempts:
                job.status = "failed"
                job.enabled = False
                self.job_store.upsert_retry(job)
                nsg_id = (job.launch_payload or {}).get("managed_nsg_id", "")
                if nsg_id:
                    cleanup = session.delete_managed_nsg(nsg_id)
                    self._ui_queue.put(("log", None, (cleanup.message, "ok" if cleanup.ok else "warn")))
                self._ui_queue.put(
                    ("log", None, (f"容量重试达最大次数：{job.name} — {result.message}", "error"))
                )
            else:
                job.status = "running"
                self.job_store.upsert_retry(job)
                self._ui_queue.put(
                    (
                        "log",
                        None,
                        (
                            f"容量重试 #{job.attempts}/{job.max_attempts} {ad_note}："
                            f"{job.name} — {result.message}",
                            "warn",
                        ),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            job.last_error = str(exc)
            # Network blips: keep running but apply a short cooldown to avoid tight loops.
            job.consecutive_rate_limits = int(job.consecutive_rate_limits or 0) + 1
            delay = rate_limit_backoff_sec(job.consecutive_rate_limits)
            until = datetime.now().astimezone().timestamp() + delay
            job.cooldown_until = datetime.fromtimestamp(until).astimezone().isoformat(timespec="seconds")
            job.status = "running"
            if job.attempts >= clamp_max_attempts(job.max_attempts):
                job.status = "failed"
                job.enabled = False
                job.cooldown_until = ""
            self.job_store.upsert_retry(job)
            self._ui_queue.put(("log", None, (f"容量重试异常 #{job.attempts}（冷却 {delay}s）：{exc}", "error")))

    # ==================================================================
    # Clipboard / export
    # ==================================================================
    def _copy_text(self, text: str, label: str) -> None:
        if not text:
            messagebox.showinfo("提示", f"没有可复制的{label}", parent=self)
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self._log(f"已复制{label}：{text}")
        self._set_status(f"已复制{label}")

    def _replace_public_ip(self) -> None:
        inst = self._selected
        tenant = self.store.get(inst.tenant_id) if inst else None
        if not inst or not tenant:
            return
        if not messagebox.askyesno(
            "更换公网 IPv4",
            f"当前地址：{inst.public_ip or '无'}\n\n此操作会中断 SSH，DNS 与白名单需要更新；删除旧临时地址后分配新地址不是原子操作。是否继续？",
            parent=self,
        ):
            return
        self._set_status("更换公网 IPv4…")
        self._run_async(
            lambda: self.sessions.get(tenant).replace_ephemeral_public_ip(inst.id, inst.compartment_id),
            lambda op: self._finish_public_ip_replace(op),
            lambda exc: (self._set_status("就绪"), messagebox.showerror("更换失败", str(exc), parent=self)),
        )

    def _finish_public_ip_replace(self, op: OperationResult) -> None:
        self._set_status("就绪")
        if op.ok:
            self._log(op.message, level="ok")
            messagebox.showinfo("更换成功", op.message, parent=self)
            self.refresh_instances()
        else:
            self._log(op.message, level="error")
            messagebox.showerror("更换失败", op.message, parent=self)

    def _adjust_boot_volume(self) -> None:
        inst = self._selected
        tenant = self.store.get(inst.tenant_id) if inst else None
        if not inst or not tenant:
            return
        self._set_status("读取引导卷信息…")

        def loaded(info: OperationResult) -> None:
            self._set_status("就绪")
            if not info.ok:
                messagebox.showerror("读取失败", info.message, parent=self)
                return
            data = info.data or {}
            dlg = BootVolumeDialog(self, current_size=data.get("size_in_gbs", 0), current_vpu=data.get("vpus_per_gb", 10))
            self.wait_window(dlg)
            if not dlg.result:
                return
            size = dlg.result.get("size_in_gbs")
            vpu = dlg.result.get("vpus_per_gb")
            self._set_status("调整引导卷…")
            self._log(f"→ 调整引导卷：size={size} vpu={vpu}")
            self._run_async(
                lambda: self.sessions.get(tenant).resize_boot_volume(
                    inst.id, inst.compartment_id, size_in_gbs=size, vpus_per_gb=vpu, wait_for_volume=False
                ),
                self._finish_boot_volume_adjust,
                lambda exc: (self._set_status("就绪"), messagebox.showerror("调整失败", str(exc), parent=self)),
            )

        self._run_async(
            lambda: self.sessions.get(tenant).get_boot_volume_info(inst.id, inst.compartment_id),
            loaded,
            lambda exc: (self._set_status("就绪"), messagebox.showerror("读取失败", str(exc), parent=self)),
        )

    def _finish_boot_volume_adjust(self, op: OperationResult) -> None:
        self._set_status("就绪")
        if op.ok:
            self._log(f"✓ {op.message}", level="ok")
            messagebox.showinfo("引导卷调整", op.message, parent=self)
            # Refresh so disk size / VPU columns pick up the new values.
            self.after(1500, self.refresh_instances)
        else:
            self._log(f"✗ {op.message}", level="error")
            messagebox.showerror("调整失败", op.message, parent=self)

    # ==================================================================
    # Instance shape / IPv6 / console / metrics
    # ==================================================================
    def _finish_simple_op(self, op: OperationResult, title: str, *, refresh: bool = False, info: bool = True) -> None:
        self._set_status("就绪")
        if op.ok:
            self._log(f"✓ {op.message}", level="ok")
            if info:
                messagebox.showinfo(title, op.message, parent=self)
            if refresh:
                self.after(1500, self.refresh_instances)
        else:
            self._log(f"✗ {op.message}", level="error")
            messagebox.showerror(f"{title}失败", op.message, parent=self)

    def _modify_shape(self) -> None:
        inst = self._selected
        tenant = self.store.get(inst.tenant_id) if inst else None
        if not inst or not tenant:
            return
        if not str(inst.shape).lower().endswith(".flex"):
            messagebox.showinfo("提示", "仅弹性（Flex）规格支持修改 OCPU / 内存。\n当前实例为固定规格。", parent=self)
            return
        dlg = ShapeConfigDialog(
            self,
            shape=inst.shape,
            current_ocpus=inst.ocpus or 1,
            current_memory=inst.memory_gb or 1,
        )
        self.wait_window(dlg)
        if not dlg.result:
            return
        ocpus = dlg.result["ocpus"]
        memory = dlg.result["memory_in_gbs"]
        self._set_status("提交规格变更…")
        self._log(f"→ 修改规格：{inst.display_name} → {ocpus:g} OCPU / {memory:g} GB")
        self._run_async(
            lambda: self.sessions.get(tenant).update_instance_shape(inst.id, ocpus, memory),
            lambda op: self._finish_simple_op(op, "规格修改", refresh=True),
            lambda exc: (self._set_status("就绪"), messagebox.showerror("规格修改失败", str(exc), parent=self)),
        )

    def _assign_ipv6(self) -> None:
        inst = self._selected
        tenant = self.store.get(inst.tenant_id) if inst else None
        if not inst or not tenant:
            return
        if inst.ipv6_addresses and not messagebox.askyesno(
            "分配 IPv6",
            f"该实例已有 IPv6：{', '.join(inst.ipv6_addresses)}\n\n仍要继续吗？",
            parent=self,
        ):
            return
        if not messagebox.askyesno(
            "分配公网 IPv6",
            "将为实例主 VNIC 分配一个公网 IPv6 地址。\n\n"
            "若 VCN/Subnet 尚未启用 IPv6，会自动：\n"
            "1. 为 VCN 申请 Oracle 公网 /56 前缀\n"
            "2. 为 Subnet 划出 /64\n"
            "3. 配置 Internet Gateway 与 ::/0 默认路由\n\n"
            "是否继续？",
            parent=self,
        ):
            return
        self._set_status("分配 IPv6…")
        self._log(f"→ 分配 IPv6（必要时自动启用 VCN/Subnet IPv6 前缀）：{inst.display_name}")
        self._run_async(
            lambda: self.sessions.get(tenant).assign_public_ipv6(inst.id, inst.compartment_id),
            lambda op: self._finish_simple_op(op, "分配 IPv6", refresh=True),
            lambda exc: (self._set_status("就绪"), messagebox.showerror("分配 IPv6 失败", str(exc), parent=self)),
        )

    def _open_console(self) -> None:
        inst = self._selected
        tenant = self.store.get(inst.tenant_id) if inst else None
        if not inst or not tenant:
            return
        dlg = ConsoleConnectionDialog(
            self,
            instance_name=inst.display_name,
            list_fn=lambda: self.sessions.get(tenant).list_console_connections(inst.id, inst.compartment_id),
            create_fn=lambda key: self.sessions.get(tenant).create_console_connection(inst.id, inst.compartment_id, key),
            delete_fn=lambda cid: self.sessions.get(tenant).delete_console_connection(cid),
        )
        self._log(f"打开控制台连接管理：{inst.display_name}")
        self.wait_window(dlg)

    def _open_metrics(self) -> None:
        inst = self._selected
        tenant = self.store.get(inst.tenant_id) if inst else None
        if not inst or not tenant:
            return
        dlg = MetricsDialog(
            self,
            instance_name=inst.display_name,
            fetch_fn=lambda hours: self.sessions.get(tenant).get_instance_metrics(
                inst.id, inst.compartment_id, hours=hours
            ),
        )
        self._log(f"打开实例监控：{inst.display_name}")
        self.wait_window(dlg)

    def _open_account_dashboard(self) -> None:
        tenant = self._current_tenant()
        if self._all_tenants_mode or not tenant:
            messagebox.showinfo("提示", "请先在左侧选择单个租户（关闭跨租户视图）。", parent=self)
            return

        def account_fn():
            result = self.sessions.get(tenant).get_account_status()
            if getattr(result, "ok", False):
                # Cache the tier so the sidebar can show it without a network call.
                self._ui_queue.put(
                    ("tier", None, (tenant.id, (result.data or {}).get("tier_code", "")))
                )
            return result

        dlg = AccountDashboardDialog(self, tenant_name=tenant.name, account_fn=account_fn)
        self._log(f"打开账号状态：{tenant.name}")
        self.wait_window(dlg)

    def _check_password_expiries(self) -> None:
        """Log a reminder for any tenant whose Oracle password is due / overdue."""
        for t in self.store.list_tenants():
            level, _ = t.password_status()
            if level == "expired":
                self._log(
                    f"❗ 甲骨文密码已过期：{t.name}（已过期 {abs(t.password_days_left() or 0)} 天），"
                    "请尽快登录 OCI 修改，然后到「密码到期提醒」点「设为今天」",
                    level="error",
                )
            elif level == "warn":
                self._log(f"⚠ 甲骨文密码即将到期：{t.name}（剩 {t.password_days_left()} 天）", level="warn")

    def _open_password_expiry(self) -> None:
        tenant = self._selected_tenant_from_list() or self._current_tenant()
        if not tenant:
            messagebox.showinfo("提示", "请先在左侧选择一个租户。", parent=self)
            return
        dlg = PasswordExpiryDialog(
            self,
            tenant_name=tenant.name,
            changed_at=tenant.password_changed_at,
            expiry_days=tenant.password_expiry_days,
            created_at=tenant.created_at,
        )
        self.wait_window(dlg)
        if not dlg.result:
            return
        tenant.password_changed_at = dlg.result["password_changed_at"]
        tenant.password_expiry_days = int(dlg.result["password_expiry_days"])
        try:
            self.store.upsert(tenant)
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self)
            return
        self._refresh_tenant_list()
        level, text = tenant.password_status()
        self._log(f"已更新密码到期提醒：{tenant.name} · {text}", level="warn" if level in ("warn", "expired") else "ok")
        messagebox.showinfo("已保存", f"「{tenant.name}」\n{text}", parent=self)

    # ==================================================================
    # Encrypted ZIP backup / restore
    # ==================================================================
    def _backup_encrypted_zip(self) -> None:
        if self.store.count() == 0:
            messagebox.showinfo("提示", "没有可备份的租户。", parent=self)
            return
        if not messagebox.askyesno(
            "加密备份",
            "将把全部租户（含 API 私钥）导出为密码加密的 ZIP（WinZip AES-256）。\n\n"
            "请务必牢记密码：密码丢失将无法恢复备份内容。是否继续？",
            parent=self,
        ):
            return
        pw_dlg = PasswordPromptDialog(
            self, title="设置备份密码", label="为加密 ZIP 设置打开密码（至少 6 位）：", confirm=True, minimum=6
        )
        self.wait_window(pw_dlg)
        if not pw_dlg.result:
            return
        password = pw_dlg.result
        path = filedialog.asksaveasfilename(
            parent=self,
            title="保存加密备份",
            defaultextension=".zip",
            initialfile=f"ocibot-backup-{datetime.now().strftime('%Y%m%d')}.zip",
            filetypes=[("加密 ZIP", "*.zip")],
        )
        if not path:
            return
        self._set_status("正在生成加密备份…")

        def work() -> int:
            return self.store.backup_to_encrypted_zip(Path(path), password)

        def ok(count: int) -> None:
            self._set_status("就绪")
            self._log(f"已加密备份 {count} 个租户 → {path}", level="ok")
            messagebox.showinfo(
                "备份成功",
                f"已备份 {count} 个租户到：\n{path}\n\n恢复时需要输入该密码，请妥善保管。",
                parent=self,
            )

        def err(exc: Exception) -> None:
            self._set_status("就绪")
            self._log(f"加密备份失败：{exc}", level="error")
            messagebox.showerror("备份失败", str(exc), parent=self)

        self._run_async(work, ok, err)

    def _restore_encrypted_zip(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="选择加密备份 ZIP", filetypes=[("加密 ZIP", "*.zip"), ("All", "*.*")]
        )
        if not path:
            return
        pw_dlg = PasswordPromptDialog(
            self, title="输入备份密码", label="输入该加密 ZIP 的打开密码：", confirm=False, minimum=1
        )
        self.wait_window(pw_dlg)
        if not pw_dlg.result:
            return
        password = pw_dlg.result
        self._set_status("正在恢复备份…")

        def work() -> list[TenantConfig]:
            return self.store.restore_from_encrypted_zip(Path(path), password, make_active=True)

        def ok(imported: list) -> None:
            self._set_status("就绪")
            for t in imported:
                self.sessions.drop(t.id)
            if imported:
                self._selected_tenant_id = imported[-1].id
            self._refresh_tenant_list()
            self._log(f"已从加密备份恢复 {len(imported)} 个租户", level="ok")
            messagebox.showinfo("恢复成功", f"成功恢复 {len(imported)} 个租户。", parent=self)
            if imported:
                self.refresh_instances()

        def err(exc: Exception) -> None:
            self._set_status("就绪")
            self._log(f"恢复失败：{exc}", level="error")
            messagebox.showerror("恢复失败", str(exc), parent=self)

        self._run_async(work, ok, err)

    def _open_firewall(self) -> None:
        inst = self._selected
        tenant = self.store.get(inst.tenant_id) if inst else None
        if not inst or not tenant:
            messagebox.showinfo(
                "防火墙",
                "请先在中间列表中选择一台实例，再打开防火墙。",
                parent=self,
            )
            return

        def loaded(op: OperationResult) -> None:
            self._set_status("就绪")
            if not op.ok:
                messagebox.showerror("防火墙加载失败", op.message, parent=self)
                return
            dlg = FirewallManagerDialog(self, op.data or {})
            self.wait_window(dlg)
            if not dlg.result:
                return
            action = dlg.result.get("action")
            if action == "create_nsg":
                self._run_firewall_change(
                    tenant,
                    lambda session: session.ensure_instance_nsg(inst.id, inst.compartment_id),
                )
            elif action == "refresh":
                self._open_firewall()
            elif action == "add":
                rule_dlg = FirewallRuleDialog(self, (op.data or {}).get("groups", []))
                self.wait_window(rule_dlg)
                if rule_dlg.result:
                    self._run_firewall_change(
                        tenant,
                        lambda session: session.add_instance_firewall_rule(
                            rule_dlg.result["nsg_id"], rule_dlg.result["spec"]
                        ),
                    )
            elif action == "delete":
                if messagebox.askyesno(
                    "删除规则",
                    "确定删除选中的规则？\n\n若该网络安全组被多台实例共享，删除可能影响其他实例。",
                    parent=self,
                ):
                    def delete_selected(session):
                        messages = []
                        all_ok = True
                        for nsg_id, ids in dlg.result["rules"].items():
                            result = session.delete_nsg_rules(nsg_id, ids)
                            all_ok = all_ok and result.ok
                            messages.append(f"…{nsg_id[-8:]}: {result.message}")
                        return OperationResult(all_ok, "\n".join(messages))
                    self._run_firewall_change(tenant, delete_selected)
            elif action == "open_all":
                has_ipv6 = bool((op.data or {}).get("has_ipv6"))
                families = "IPv4 + IPv6" if has_ipv6 else "仅 IPv4"
                nsg_note = (
                    "将先清空所有关联网络安全组（NSG）的现有规则，再写入全开放规则。"
                    if (op.data or {}).get("groups")
                    else "实例当前没有网络安全组，将自动创建实例专属 NSG 并写入全开放规则。"
                )
                if messagebox.askyesno(
                    "一键开启所有端口",
                    f"{nsg_note}\n\n"
                    f"开放范围：{families} 入站 + 出站，所有协议、所有端口。\n"
                    "共享 NSG 的改动可能影响其他实例；子网安全列表不会被修改。\n\n"
                    "这会把实例上的服务完全暴露到公网，确认继续？",
                    parent=self,
                ):
                    self._run_firewall_change(
                        tenant,
                        lambda session: session.replace_instance_firewall_with_open_all(
                            inst.id, inst.compartment_id
                        ),
                    )

        self._set_status("正在加载防火墙规则…")
        self._run_async(
            lambda: self.sessions.get(tenant).get_instance_firewall(inst.id, inst.compartment_id),
            loaded,
            lambda exc: (
                self._set_status("就绪"),
                messagebox.showerror("防火墙异常", str(exc), parent=self),
            ),
        )

    def _run_firewall_change(self, tenant: TenantConfig, operation) -> None:
        self._set_status("正在更新防火墙规则…")

        def done(op: OperationResult) -> None:
            self._set_status("就绪")
            if op.ok:
                self._log(op.message, level="ok")
                messagebox.showinfo("防火墙", op.message, parent=self)
            else:
                self._log(op.message, level="error")
                messagebox.showerror("防火墙更新失败", op.message, parent=self)
            self._open_firewall()

        self._run_async(
            lambda: operation(self.sessions.get(tenant)),
            done,
            lambda exc: (
                self._set_status("就绪"),
                messagebox.showerror("防火墙异常", str(exc), parent=self),
            ),
        )

    def _export_csv(self) -> None:
        rows = self._filtered or self._instances
        if not rows:
            messagebox.showinfo("提示", "没有可导出的实例。", parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出实例 CSV", defaultextension=".csv", initialfile="ocibot-instances.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow([
                    "tenant", "name", "state", "shape", "ocpus", "memory_gb",
                    "boot_gb", "boot_vpus", "public_ip", "private_ip", "ipv6",
                    "ad", "region", "ocid", "compartment",
                ])
                for i in rows:
                    w.writerow([
                        i.tenant_name, i.display_name, i.lifecycle_state, i.shape,
                        i.ocpus, i.memory_gb, i.boot_volume_gb, i.boot_vpus_per_gb,
                        i.public_ip, i.private_ip, ";".join(i.ipv6_addresses),
                        i.availability_domain, i.region, i.id, i.compartment_id,
                    ])
            self._log(f"已导出 CSV → {path}", level="ok")
            messagebox.showinfo("导出成功", path, parent=self)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", str(exc), parent=self)

    # ==================================================================
    # Import / export config
    # ==================================================================
    def _import_config(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="导入租户 JSON", filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if not path:
            return
        try:
            imported = self.store.import_from_file(Path(path), make_active=True)
            for t in imported:
                self.sessions.drop(t.id)
            if imported:
                self._selected_tenant_id = imported[-1].id
            self._refresh_tenant_list()
            self._log(f"已导入 {len(imported)} 个租户配置", level="ok")
            messagebox.showinfo("导入成功", f"成功导入 {len(imported)} 个租户。", parent=self)
            if imported:
                self.refresh_instances()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导入失败", str(exc), parent=self)

    def _export_config(self) -> None:
        if self.store.count() == 0:
            messagebox.showinfo("提示", "没有可导出的租户。", parent=self)
            return
        include_key = messagebox.askyesno("导出私钥？", "是否在导出文件中包含私钥？\n\n是 = 完整备份\n否 = 仅元数据", parent=self)
        path = filedialog.asksaveasfilename(parent=self, title="导出全部租户", defaultextension=".json", initialfile="ocibot-tenants.json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            self.store.export_all(Path(path), include_private_key=bool(include_key))
            self._log(f"已导出配置 → {path}", level="ok")
            messagebox.showinfo("导出成功", path, parent=self)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", str(exc), parent=self)

    def _import_oci_config_file(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="选择 OCI config 文件", initialfile="config", filetypes=[("Config", "config *.config *"), ("All", "*.*")])
        if not path:
            return
        dlg = TextPromptDialog(self, title="Profile", label="OCI Config Profile 名称：", initial="DEFAULT")
        self.wait_window(dlg)
        profile = dlg.result or "DEFAULT"
        name_dlg = TextPromptDialog(self, title="显示名称", label="在面板中显示的租户名称：", initial=profile)
        self.wait_window(name_dlg)
        name = name_dlg.result or profile
        try:
            tenant = self.store.import_from_oci_config(Path(path), profile=profile, name=name, make_active=True)
            self.sessions.drop(tenant.id)
            self._selected_tenant_id = tenant.id
            self._refresh_tenant_list()
            self._log(f"已从 OCI Config 导入：{tenant.name}", level="ok")
            messagebox.showinfo("导入成功", f"租户「{tenant.name}」已添加。", parent=self)
            self.refresh_instances()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导入失败", str(exc), parent=self)

    def _open_data_dir(self) -> None:
        path = self.store.data_dir
        try:
            import os
            import subprocess
            import sys

            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showinfo("配置目录", str(path) + f"\n\n({exc})", parent=self)

    # ==================================================================
    # Async / log / lifecycle
    # ==================================================================
    def _on_auto_refresh_change(self) -> None:
        choice = self.auto_refresh.get()
        # Floor at 60s — shorter intervals burn OCI list quotas for little gain.
        mapping = {"关闭": 0, "60 秒": 60, "120 秒": 120, "300 秒": 300}
        self._auto_refresh_sec = mapping.get(choice, 0)
        if self._auto_refresh_sec and self._auto_refresh_sec < 60:
            self._auto_refresh_sec = 60
            try:
                self.auto_refresh.set("60 秒")
            except tk.TclError:
                pass
        if self._auto_job:
            try:
                self.after_cancel(self._auto_job)
            except Exception:
                pass
            self._auto_job = None
        if self._auto_refresh_sec > 0:
            self._schedule_auto_refresh()
            self._log(f"自动刷新：每 {self._auto_refresh_sec} 秒")
        else:
            self._log("自动刷新已关闭")

    def _schedule_auto_refresh(self) -> None:
        if self._auto_refresh_sec <= 0:
            return

        def tick() -> None:
            if self._auto_refresh_sec > 0 and not self._loading and (self._current_tenant() or self._all_tenants_mode):
                self.refresh_instances()
            if self._auto_refresh_sec > 0:
                self._auto_job = self.after(self._auto_refresh_sec * 1000, tick)

        self._auto_job = self.after(self._auto_refresh_sec * 1000, tick)

    def _run_async(self, work, on_ok, on_err) -> None:
        def runner() -> None:
            try:
                result = work()
                self._ui_queue.put(("ok", on_ok, result))
            except Exception as exc:  # noqa: BLE001
                self._ui_queue.put(("err", on_err, exc))

        threading.Thread(target=runner, daemon=True).start()

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                kind, cb, payload = self._ui_queue.get_nowait()
                try:
                    if kind == "ok":
                        cb(payload)
                    elif kind == "err":
                        cb(payload)
                    elif kind == "log":
                        msg, level = payload
                        self._log(msg, level=level)
                    elif kind == "refresh":
                        if not self._loading:
                            self.refresh_instances()
                    elif kind == "jobs_changed":
                        self._update_jobs_badge()
                    elif kind == "tier":
                        tenant_id, tier_code = payload
                        self._record_tenant_tier(tenant_id, tier_code)
                except Exception as exc:  # noqa: BLE001
                    self._report_background_error("界面消息处理异常", exc)
        except queue.Empty:
            pass
        self.after(80, self._poll_ui_queue)

    def _report_background_error(self, context: str, exc: Exception) -> None:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        try:
            log_path = self.data_dir / "ocibot-error.log"
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] {context}\n{detail}")
        except OSError:
            pass
        self._log(f"{context}: {exc}", level="error")

    def _log(self, message: str, level: str = "info") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        prefix = {"ok": "OK ", "error": "ERR", "warn": "WRN"}.get(level, "INF")
        self.log_box.insert("end", f"[{ts}] {prefix}  {message}\n")
        self.log_box.see("end")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _on_close(self) -> None:
        try:
            self.sessions.close_all()
        except Exception:
            pass
        self.destroy()


def _short_time(value: str) -> str:
    if not value:
        return "—"
    text = str(value).replace("T", " ")
    if "+" in text:
        text = text.split("+", 1)[0]
    if "." in text:
        text = text.split(".", 1)[0]
    return text[:19]


def run_app() -> None:
    app = OCIBotApp()
    app.mainloop()
