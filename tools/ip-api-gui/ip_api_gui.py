"""多出口 IP 更换工具（本地图形界面）。

调用面板的「换 IP API」（接口说明见仓库 docs/API.md）：
  * 列出一台实例的多出口 IP（名字、私网 IP、公网 IP）；
  * 换选中的那个私网 IP 的公网 IP；
  * 一键换全部；
  * 看更换进度和结果（旧地址 → 新地址）。

只用 Python 标准库（tkinter + urllib），不用装任何第三方包：
    python ip_api_gui.py            Windows 也可以直接双击同目录的「启动.bat」

设置保存在本机用户目录，不在这个文件夹里：
    Windows：%APPDATA%\\ip-api-gui\\config.json
             勾了「记住密钥」时，密钥用 Windows DPAPI 加密保存，只有当前 Windows 用户能解开。
    其他系统：~/.config/ip-api-gui/config.json（权限 600）
             勾了「记住密钥」时密钥是明文保存的。

更换时只发起、立即返回，之后每 2 秒查一次进度接口（它只读面板数据库，不请求 Oracle），
所以面板前面有 Cloudflare 之类的反向代理也不会因为等太久被断开。
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Optional

APP_TITLE = "多出口 IP 更换工具"
USER_AGENT = "ip-api-gui/1"
POLL_MS = 2000
REQUEST_TIMEOUT = 60

# 面板给的 BASE：https://面板/api/v1/tenants/<租户ID>/instances/<实例OCID>/multi-ips
BASE_RE = re.compile(r"^(https?://[^/\s]+)/api/v1/tenants/([^/\s]+)/instances/([^/\s]+)/multi-ips$")

STATE_LABEL = {"running": "进行中", "done": "已完成", "failed": "有失败", "interrupted": "已中断"}
PHASE_LABEL = {
    "plan": "准备",
    "release": "删除旧 IP",
    "wait_release": "等 Oracle 释放名额",
    "create": "申请新 IP",
    "wait_assign": "等新 IP 绑定",
    "done": "结束",
}
STATUS_LABEL = {
    "pending": "待处理",
    "releasing": "删除中",
    "released": "待申请",
    "assigning": "绑定中",
    "create_failed": "申请失败",
    "done": "完成",
    "failed": "失败",
    "skipped": "跳过",
}


# ---------------------------------------------------------------------------
# 设置文件 + 密钥保护
# ---------------------------------------------------------------------------


def config_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "ip-api-gui" / "config.json"


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows DPAPI 加密 / 解密（CryptProtectData / CryptUnprotectData），只有当前用户能解开。"""
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = Blob()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # 0x1 = CRYPTPROTECT_UI_FORBIDDEN：不弹任何系统对话框
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0x1, ctypes.byref(blob_out)):
        raise OSError("DPAPI 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    key = ""
    try:
        if data.get("key_dpapi") and os.name == "nt":
            key = _dpapi(base64.b64decode(data["key_dpapi"]), protect=False).decode("utf-8")
        elif data.get("key_plain"):
            key = str(data["key_plain"])
    except Exception:  # noqa: BLE001 - 解不开（换了 Windows 用户等）就当没存
        key = ""
    return {"base": str(data.get("base") or ""), "key": key, "remember": bool(key)}


def save_config(base: str, key: str, remember: bool) -> None:
    data: dict[str, Any] = {"base": base}
    if remember and key:
        if os.name == "nt":
            data["key_dpapi"] = base64.b64encode(_dpapi(key.encode("utf-8"), protect=True)).decode("ascii")
        else:
            data["key_plain"] = key
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def clean_base(text: str) -> str:
    """接受直接粘贴的 BASE="…" 整行，去掉 BASE=、引号、空白和末尾的斜杠。"""
    s = (text or "").strip()
    if s.upper().startswith("BASE="):
        s = s[5:].strip()
    return s.strip("'\"").strip().rstrip("/")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


class ApiError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class Api:
    def __init__(self, base: str, key: str):
        self.base = base
        self.key = key

    def _call(self, method: str, path: str = "") -> dict:
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=b"" if method == "POST" else None,
            headers={
                "Authorization": f"Bearer {self.key}",
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(raw).get("detail")
            except ValueError:
                detail = None
            if isinstance(detail, list):  # 参数校验错误（422）
                detail = "；".join(str(d.get("msg", d)) for d in detail)
            raise ApiError(exc.code, str(detail or raw[:200] or exc.reason)) from None
        except urllib.error.URLError as exc:
            raise ApiError(0, f"连不上面板：{exc.reason}") from None
        except (TimeoutError, OSError) as exc:
            raise ApiError(0, f"请求失败：{exc}") from None
        except ValueError:
            raise ApiError(0, "面板返回的不是 JSON，BASE 地址可能填错了") from None

    def list(self) -> dict:
        return self._call("GET")

    def rotate_one(self, private_ip: str) -> dict:
        return self._call("POST", f"/{private_ip}/rotate")

    def rotate_all(self) -> dict:
        return self._call("POST", "/rotate-all")

    def status(self) -> dict:
        return self._call("GET", "/rotate")


def explain(err: ApiError) -> str:
    hints = {
        401: "检查 API 密钥：是否复制完整、是否已删除，或改过密码 / 退出过所有会话（旧密钥会失效）。",
        403: "多出口 IP 只对升级账号开放；也可能是本机 IP 被面板封禁了。",
        404: "检查 BASE 地址里的租户 ID、实例 OCID，以及私网 IP 是否属于这台实例。",
        409: "这台实例正在更换，等它结束再操作。",
        502: "面板请求 Oracle 失败，稍后再试。",
    }
    head = f"HTTP {err.status}：" if err.status else ""
    hint = hints.get(err.status, "")
    return f"{head}{err.detail}" + (f"\n\n{hint}" if hint else "")


def fmt_elapsed(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 60} 分 {sec % 60} 秒" if sec >= 60 else f"{sec} 秒"


def parse_time(text: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(text).timestamp() if text else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue[tuple[Callable, tuple]]" = queue.Queue()
        self.inflight = 0
        self.polling = False
        self.poll_errors = 0
        self.job: Optional[dict] = None
        self.local_start: Optional[float] = None
        self.primary_ip = ""
        self.rows: dict[str, dict] = {}  # 私网 IP → 列表里那一项（原始数据）

        root.title(APP_TITLE)
        root.geometry("980x720")
        root.minsize(820, 600)
        self._build()
        cfg = load_config()
        self.base_var.set(cfg.get("base", ""))
        self.key_var.set(cfg.get("key", ""))
        self.remember_var.set(cfg.get("remember", False))
        self._update_hint()
        root.after(100, self._drain)

    # ---- 布局 ----
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        top = ttk.LabelFrame(self.root, text="连接设置")
        top.pack(fill="x", **pad)
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="BASE 地址").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.base_var = tk.StringVar()
        base_entry = ttk.Entry(top, textvariable=self.base_var)
        base_entry.grid(row=0, column=1, columnspan=4, sticky="ew", padx=6, pady=4)
        self.base_var.trace_add("write", lambda *_: self._update_hint())

        ttk.Label(top, text="API 密钥").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(top, textvariable=self.key_var, show="•")
        self.key_entry.grid(row=1, column=1, sticky="ew", padx=6, pady=4)
        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="显示", variable=self.show_key_var, command=self._toggle_key).grid(
            row=1, column=2, padx=4
        )
        self.remember_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="记住密钥", variable=self.remember_var).grid(row=1, column=3, padx=4)
        ttk.Button(top, text="保存设置", command=self.save).grid(row=1, column=4, padx=6)

        self.hint_var = tk.StringVar()
        self.hint_label = ttk.Label(top, textvariable=self.hint_var, foreground="#666")
        self.hint_label.grid(row=2, column=0, columnspan=5, sticky="w", padx=6, pady=(0, 6))

        # 日志固定在最底下：先 pack 它，窗口变矮时被压缩的是上面两张表，不是日志。
        logf = ttk.LabelFrame(self.root, text="日志")
        logf.pack(side="bottom", fill="x", **pad)
        self.log_text = tk.Text(logf, height=5, state="disabled", wrap="word", font="TkDefaultFont")
        self.log_text.pack(fill="x", padx=6, pady=(0, 6))

        # 多出口 IP 列表
        mid = ttk.LabelFrame(self.root, text="多出口 IP")
        mid.pack(fill="both", expand=True, **pad)
        bar = ttk.Frame(mid)
        bar.pack(fill="x", padx=6, pady=4)
        self.btn_list = ttk.Button(bar, text="刷新列表", command=self.refresh_list)
        self.btn_list.pack(side="left")
        self.btn_one = ttk.Button(bar, text="更换选中的", command=self.rotate_selected)
        self.btn_one.pack(side="left", padx=6)
        self.btn_all = ttk.Button(bar, text="一键换全部", command=self.rotate_all)
        self.btn_all.pack(side="left")
        ttk.Button(bar, text="复制选中的公网 IP", command=self.copy_selected).pack(side="left", padx=6)
        self.list_info = tk.StringVar(value="还没读取。填好上面两项后点「刷新列表」。")
        ttk.Label(bar, textvariable=self.list_info, foreground="#666").pack(side="left", padx=8)

        cols = ("name", "private", "public", "source")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse", height=10)
        for col, text, width in (("name", "名称", 160), ("private", "私网 IP", 150),
                                 ("public", "公网 IP", 170), ("source", "来源", 80)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.tag_configure("noip", foreground="#b26a00")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        sb.pack(side="left", fill="y", pady=(0, 6), padx=(0, 6))

        # 更换进度
        prog = ttk.LabelFrame(self.root, text="更换进度 / 结果")
        prog.pack(fill="both", expand=True, **pad)
        pbar = ttk.Frame(prog)
        pbar.pack(fill="x", padx=6, pady=4)
        self.btn_status = ttk.Button(pbar, text="查看最近一次", command=self.load_status)
        self.btn_status.pack(side="left")
        ttk.Button(pbar, text="复制新地址", command=self.copy_new).pack(side="left", padx=6)
        self.job_info = tk.StringVar(value="")
        ttk.Label(pbar, textvariable=self.job_info).pack(side="left", padx=8)

        pcols = ("private", "old", "new", "status", "message")
        self.ptree = ttk.Treeview(prog, columns=pcols, show="headings", height=7)
        for col, text, width in (("private", "私网 IP", 120), ("old", "旧公网 IP", 140), ("new", "新公网 IP", 140),
                                 ("status", "状态", 80), ("message", "说明", 420)):
            self.ptree.heading(col, text=text)
            self.ptree.column(col, width=width, anchor="w")
        self.ptree.tag_configure("done", foreground="#1a7f37")
        self.ptree.tag_configure("failed", foreground="#cf222e")
        psb = ttk.Scrollbar(prog, orient="vertical", command=self.ptree.yview)
        self.ptree.configure(yscrollcommand=psb.set)
        self.ptree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=(0, 6))
        psb.pack(side="left", fill="y", pady=(0, 6), padx=(0, 6))

    # ---- 小工具 ----
    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.show_key_var.get() else "•")

    def _update_hint(self) -> None:
        base = clean_base(self.base_var.get())
        m = BASE_RE.match(base)
        if not base:
            self.hint_var.set("BASE 地址在面板实例详情 →「保留 IP」→「多出口 IP」→「用 API 换 IP」里，"
                              "可以把 BASE=\"…\" 整行粘贴进来。")
            self.hint_label.configure(foreground="#666")
        elif not m:
            self.hint_var.set("BASE 地址格式不对：应是 https://面板地址/api/v1/tenants/<租户ID>/instances/<实例OCID>/multi-ips")
            self.hint_label.configure(foreground="#cf222e")
        elif m.group(1).startswith("http://"):
            self.hint_var.set(f"面板 {m.group(1)} 用的是 http：密钥会明文经过网络，建议改用域名 + HTTPS。")
            self.hint_label.configure(foreground="#b26a00")
        else:
            inst = m.group(3)
            self.hint_var.set(f"面板 {m.group(1)} · 实例 …{inst[-12:]} · 设置保存在 {config_path()}")
            self.hint_label.configure(foreground="#666")

    def log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{datetime.now():%H:%M:%S}] {text}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _api(self) -> Optional[Api]:
        base = clean_base(self.base_var.get())
        key = self.key_var.get().strip()
        if not BASE_RE.match(base):
            messagebox.showwarning(APP_TITLE, "先填好 BASE 地址（面板「用 API 换 IP」里的那一行）。")
            return None
        if not key:
            messagebox.showwarning(APP_TITLE, "先填 API 密钥（面板「设置 → API 密钥」里新建）。")
            return None
        if self.base_var.get() != base:
            self.base_var.set(base)
        return Api(base, key)

    def _set_busy(self, delta: int) -> None:
        self.inflight += delta
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        running = bool(self.job and self.job.get("state") == "running")
        busy = self.inflight > 0
        for btn in (self.btn_one, self.btn_all):
            btn.configure(state="disabled" if (busy or running) else "normal")
        self.btn_list.configure(state="disabled" if busy else "normal")

    def bg(
        self,
        fn: Callable[[], Any],
        ok: Callable[[Any], None],
        what: str,
        fail: Optional[Callable[[ApiError], None]] = None,
        busy: bool = True,
    ) -> None:
        """在后台线程里请求，结果回到界面线程处理（tkinter 只能在主线程里改）。

        busy=False 的（定时查进度）不让按钮变灰，否则每 2 秒闪一下。
        """
        if busy:
            self._set_busy(1)
        on_fail = fail or (lambda exc: self._failed(exc, what))

        def run() -> None:
            try:
                result = fn()
            except ApiError as exc:
                self.q.put((on_fail, (exc,)))
            except Exception as exc:  # noqa: BLE001
                self.q.put((on_fail, (ApiError(0, str(exc)),)))
            else:
                self.q.put((ok, (result,)))
            finally:
                if busy:
                    self.q.put((self._set_busy, (-1,)))

        threading.Thread(target=run, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                fn, args = self.q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def _failed(self, err: ApiError, what: str) -> None:
        self.log(f"{what}失败：{err.detail}")
        if err.status == 409:
            # 已经有一个在换：把它的进度显示出来
            self.log("这台实例正在更换，开始跟踪它的进度。")
            self._start_polling()
            return
        messagebox.showerror(APP_TITLE, f"{what}失败\n\n{explain(err)}")

    # ---- 操作 ----
    def save(self) -> None:
        base = clean_base(self.base_var.get())
        self.base_var.set(base)
        try:
            save_config(base, self.key_var.get().strip(), self.remember_var.get())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"保存失败：{exc}")
            return
        note = "（密钥已加密保存）" if self.remember_var.get() and os.name == "nt" else (
            "（密钥已保存）" if self.remember_var.get() else "（没有保存密钥）")
        self.log(f"设置已保存{note}：{config_path()}")

    def refresh_list(self) -> None:
        api = self._api()
        if api is None:
            return
        self.list_info.set("读取中…")
        self.bg(api.list, self._show_list, "读取列表")

    def _show_list(self, data: dict) -> None:
        self.tree.delete(*self.tree.get_children())
        items = data.get("items") or []
        self.primary_ip = data.get("primary_private_ip") or ""
        self.rows = {str(it.get("private_ip") or ""): it for it in items if it.get("private_ip")}
        for it in items:
            if not it.get("private_ip"):
                continue
            pub = it.get("public_ip") or ""
            self.tree.insert(
                "", "end", iid=it.get("private_ip"),
                values=(it.get("name") or "—", it.get("private_ip") or "", pub or "（没有公网 IP）",
                        "面板" if it.get("managed") else "手工"),
                tags=() if pub else ("noip",),
            )
        self.list_info.set(f"共 {len(items)} 个辅助 IP · 主私网 IP {self.primary_ip or '—'}（不能换）")
        self.log(f"已读取列表：{len(items)} 个辅助 IP")

    def _selected(self) -> Optional[dict]:
        sel = self.tree.selection()
        return self.rows.get(sel[0]) if sel else None

    def rotate_selected(self) -> None:
        picked = self._selected()
        if not picked:
            messagebox.showinfo(APP_TITLE, "先在列表里选中一个私网 IP。")
            return
        private_ip = picked["private_ip"]
        name = picked.get("name") or "未命名"
        public_ip = picked.get("public_ip") or ""
        text = (
            f"{'更换' if public_ip else '分配'}私网 IP {private_ip}（{name}）的公网 IP？\n\n"
            + (f"现在是 {public_ip}。旧地址会立即释放、无法找回；\n这个出口会断开，直到新 IP 绑好。\n\n"
               if public_ip else "它现在没有公网 IP，会直接分配一个。\n\n")
            + "私网 IP 不变，服务器里不用改配置。"
        )
        if not messagebox.askokcancel(APP_TITLE, text):
            return
        api = self._api()
        if api is None:
            return
        self.log(f"开始更换 {private_ip}（{name}）")
        self.bg(lambda: api.rotate_one(private_ip), self._started, f"更换 {private_ip}")

    def rotate_all(self) -> None:
        count = len(self.rows)
        what = f"全部 {count} 个" if count else "全部"
        text = (
            f"一键更换这台实例{what}辅助 IP 的公网 IP？\n\n"
            "会先删除这些旧公网 IP（地址立即释放、无法找回），再申请新的绑回原来的私网 IP。\n"
            "期间这些出口全部断开，一般几十秒到几分钟。\n\n"
            "主公网 IP 和 SSH 不受影响；私网 IP 不变，服务器里不用改配置。"
        )
        if not messagebox.askokcancel(APP_TITLE, text):
            return
        api = self._api()
        if api is None:
            return
        self.log("开始一键更换全部")
        self.bg(api.rotate_all, self._started, "一键更换全部")

    def _started(self, data: dict) -> None:
        self.local_start = time.monotonic()
        self.log(data.get("message") or "已开始")
        self._show_job(data.get("job"))
        self._start_polling()

    def load_status(self) -> None:
        api = self._api()
        if api is None:
            return
        self.bg(api.status, lambda d: self._status_loaded(d, user=True), "查看进度")

    def _start_polling(self) -> None:
        """开始每 2 秒查一次进度；已经在查就不再开第二条。"""
        if self.polling:
            return
        self.polling = True
        self.poll_errors = 0
        self.root.after(POLL_MS, self._poll)

    def _poll(self) -> None:
        api = self._api()
        if api is None:
            self.polling = False
            return
        self.bg(api.status, lambda d: self._status_loaded(d, user=False), "查看进度",
                fail=self._poll_failed, busy=False)

    def _poll_failed(self, err: ApiError) -> None:
        # 网络抖一下、面板重启中（连不上 / 5xx）：任务还在面板后台跑，过一会儿接着查。
        # 4xx（密钥失效、租户没了）不会自己好，停下并说明。
        self.poll_errors += 1
        if err.status and err.status < 500:
            self.polling = False
            self.log(f"查看进度失败，已停止跟踪：{err.detail}")
            messagebox.showerror(APP_TITLE, f"查看进度失败\n\n{explain(err)}")
            return
        if self.poll_errors in (1, 5) or self.poll_errors % 15 == 0:
            self.log(f"查看进度暂时失败（第 {self.poll_errors} 次），继续重试：{err.detail}")
        self.root.after(POLL_MS * min(self.poll_errors, 5), self._poll)

    def _status_loaded(self, data: dict, user: bool) -> None:
        job = data.get("job")
        if not user:
            self.poll_errors = 0
        if job is None:
            if not user:
                self.polling = False
            else:
                self.job_info.set("这台实例还没有更换记录。")
            return
        was_running = bool(self.job and self.job.get("state") == "running")
        self._show_job(job)
        if job.get("state") == "running":
            if user:
                self._start_polling()  # 手动点「查看」发现还在换：接着跟踪
            else:
                self.root.after(POLL_MS, self._poll)
            return
        if not user:
            self.polling = False
        if was_running or not user:
            self.log(f"更换结束：{job.get('message') or STATE_LABEL.get(job.get('state'), '')}")
            self.local_start = None
            self.refresh_list()

    def _show_job(self, job: Optional[dict]) -> None:
        self.job = job
        self._sync_buttons()
        self.ptree.delete(*self.ptree.get_children())
        if not job:
            self.job_info.set("")
            return
        items = job.get("items") or []
        for it in items:
            status = it.get("status") or ""
            msg = it.get("message") or ""
            if it.get("same_address") and not msg:
                msg = "Oracle 分回了原来的地址"
            self.ptree.insert(
                "", "end",
                values=(it.get("private_ip") or "", it.get("old_ip") or "—", it.get("new_ip") or "—",
                        STATUS_LABEL.get(status, status), msg),
                tags=(status,) if status in ("done", "failed") else (),
            )
        state = job.get("state") or ""
        total = sum(1 for it in items if it.get("status") != "skipped")
        finished = sum(1 for it in items if it.get("status") in ("done", "failed"))
        parts = [STATE_LABEL.get(state, state)]
        if state == "running":
            parts.append(PHASE_LABEL.get(job.get("phase") or "", job.get("phase") or ""))
        parts.append(f"{finished}/{total}")
        start, end = parse_time(job.get("started_at") or ""), parse_time(job.get("finished_at") or "")
        if start and end:
            parts.append(f"用时 {fmt_elapsed(end - start)}")
        elif state == "running" and self.local_start is not None:
            parts.append(f"已用 {fmt_elapsed(time.monotonic() - self.local_start)}")
        if state != "running" and job.get("message"):
            parts.append(job["message"])
        self.job_info.set(" · ".join(p for p in parts if p))

    def _copy(self, text: str, what: str) -> None:
        if not text:
            messagebox.showinfo(APP_TITLE, f"没有可复制的{what}。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log(f"已复制{what}")

    def copy_selected(self) -> None:
        picked = self._selected()
        self._copy((picked or {}).get("public_ip") or "", "公网 IP")

    def copy_new(self) -> None:
        lines = [
            f"{it.get('private_ip')}\t{it.get('new_ip')}"
            for it in (self.job or {}).get("items") or []
            if it.get("status") == "done" and it.get("new_ip")
        ]
        self._copy("\n".join(lines), "新地址（私网 IP ⇥ 新公网 IP）")


def main() -> None:
    if os.name == "nt":
        try:  # 高分屏下字不发虚
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    root = tk.Tk()
    if os.name == "nt":
        try:
            import tkinter.font as tkfont

            for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
                tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=10)
        except Exception:  # noqa: BLE001
            pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
