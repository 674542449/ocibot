#!/usr/bin/env python3
"""OCI Bot entry point — multi-tenant Oracle Cloud instance control panel."""

from __future__ import annotations

import sys
import traceback
from datetime import datetime


def _show_startup_error(message: str) -> None:
    """Show startup failures even when running as a windowed executable."""
    try:
        from tkinter import messagebox

        messagebox.showerror("OCIBot 启动失败", message)
        return
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "OCIBot 启动失败", 0x10)
            return
        except Exception:
            pass
    print(message, file=sys.stderr)


def _write_startup_log(detail: str) -> None:
    try:
        from app.runtime_paths import ensure_writable_directory, resolve_data_dir

        data_dir = ensure_writable_directory(resolve_data_dir())
        log_path = data_dir / "ocibot-startup-error.log"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n[{datetime.now().isoformat(timespec='seconds')}]\n{detail}\n")
    except Exception:
        pass


def main() -> int:
    try:
        import tkinter as _tk  # noqa: F401
    except ImportError:
        message = (
            "缺少 tkinter（Python 标准库 GUI 组件）。\n"
            "Windows/macOS 官方 Python 默认自带；Linux 请安装 python3-tk。"
        )
        _show_startup_error(message)
        return 1

    try:
        from app.gui import run_app

        run_app()
    except Exception as exc:  # noqa: BLE001
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _write_startup_log(detail)
        _show_startup_error(f"程序无法启动：\n\n{exc}\n\n详细信息已写入启动错误日志。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
