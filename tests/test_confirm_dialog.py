import tkinter as tk

import pytest

from app.dialogs import ConfirmDialog, LaunchConfirmDialog


@pytest.fixture
def root():
    try:
        window = tk.Tk()
        window.geometry("800x600")
        window.update_idletasks()
    except tk.TclError as exc:  # pragma: no cover - headless CI
        pytest.skip(f"Tk unavailable: {exc}")
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass


def _assert_footer_visible(dialog):
    try:
        dialog.deiconify()
    except tk.TclError:
        pass
    dialog.update()
    dialog.update_idletasks()
    height = dialog.winfo_height()
    for widget in (dialog.footer, dialog.cancel_button, dialog.confirm_button):
        assert widget.winfo_ismapped()
        assert widget.winfo_rooty() + widget.winfo_height() <= dialog.winfo_rooty() + height


def test_terminate_confirmation_buttons_stay_visible(root):
    dialog = ConfirmDialog(
        root,
        title="终止实例（危险）",
        message=(
            "终止后计算资源不可恢复！\n\n实例：long-instance-name\n租户：tenant\n"
            "OCID：ocid1.instance.oc1.ap-tokyo-1." + "x" * 120 + "\n\n确定要终止吗？"
        ),
        confirm_text="终止",
        danger=True,
    )
    dialog.geometry("460x200")
    _assert_footer_visible(dialog)
    dialog.destroy()


def test_required_text_confirmation_buttons_stay_visible(root):
    dialog = ConfirmDialog(
        root,
        title="删除资源",
        message="此操作不可恢复。" * 10,
        confirm_text="删除",
        danger=True,
        require_text="DELETE",
    )
    dialog.geometry("460x260")
    _assert_footer_visible(dialog)
    dialog.destroy()


def test_launch_confirm_dialog_shows_spec_rows(root):
    rows = [
        ("显示名称", "ocibot-demo"),
        ("机器型号", "VM.Standard.A1.Flex（免费 ARM）"),
        ("核心", "4 OCPU"),
        ("内存", "24 GB"),
        ("硬盘", "100 GB"),
        ("硬盘性能", "超高性能 (120 VPUs/GB)"),
    ]
    dialog = LaunchConfirmDialog(
        root,
        rows,
        note="创建后将开放公网全部协议。",
    )
    dialog.geometry("520x480")
    _assert_footer_visible(dialog)
    assert dialog.confirm_button.cget("text") == "确认创建"
    assert dialog.cancel_button.cget("text") == "返回修改"
    dialog.destroy()
