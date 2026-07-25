from types import SimpleNamespace

from app.dialogs import LAUNCH_QUICK_PRESETS, LaunchInstanceDialog


class FakeWidget:
    def __init__(self):
        self.state = None
        self.kwargs = {}

    def configure(self, **kwargs):
        self.kwargs.update(kwargs)
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "text" in kwargs:
            self.text = kwargs["text"]
        if "values" in kwargs:
            self.values = kwargs["values"]


class FakeVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def _dialog(subnet):
    dialog = SimpleNamespace(
        public_check=FakeWidget(),
        ipv6_check=FakeWidget(),
        assign_public=FakeVar(True),
        assign_ipv6=FakeVar(True),
        _selected_subnet=lambda: subnet,
    )
    return dialog


def test_ipv6_remains_selectable_for_ipv4_only_subnet():
    dialog = _dialog({"ipv6_enabled": False, "prohibit_public_ip_on_vnic": False})

    LaunchInstanceDialog._update_network_options(dialog)

    assert dialog.ipv6_check.state == "normal"
    assert dialog.assign_ipv6.get() is True
    assert dialog.public_check.state == "normal"


def test_private_subnet_only_disables_public_ipv4():
    dialog = _dialog({"ipv6_enabled": False, "prohibit_public_ip_on_vnic": True})

    LaunchInstanceDialog._update_network_options(dialog)

    assert dialog.public_check.state == "disabled"
    assert dialog.assign_public.get() is False
    assert dialog.ipv6_check.state == "normal"
    assert dialog.assign_ipv6.get() is True


def test_quick_presets_cover_free_tier_requests():
    assert len(LAUNCH_QUICK_PRESETS) == 3
    by_id = {p["id"]: p for p in LAUNCH_QUICK_PRESETS}

    micro = by_id["e2_micro_50"]
    assert micro["shape"] == "VM.Standard.E2.1.Micro"
    assert micro["boot_volume_size_in_gbs"] == 50
    assert micro["boot_volume_vpus_per_gb"] == 120
    assert micro["ocpus"] is None

    arm100 = by_id["a1_4c24g_100"]
    assert arm100["shape"] == "VM.Standard.A1.Flex"
    assert arm100["ocpus"] == 4
    assert arm100["memory_in_gbs"] == 24
    assert arm100["boot_volume_size_in_gbs"] == 100
    assert arm100["boot_volume_vpus_per_gb"] == 120

    arm200 = by_id["a1_4c24g_200"]
    assert arm200["shape"] == "VM.Standard.A1.Flex"
    assert arm200["ocpus"] == 4
    assert arm200["memory_in_gbs"] == 24
    assert arm200["boot_volume_size_in_gbs"] == 200
    assert arm200["boot_volume_vpus_per_gb"] == 120


def _preset_dialog():
    """Minimal stand-in for LaunchInstanceDialog._apply_quick_preset."""
    shapes = [
        {
            "shape": "VM.Standard.A1.Flex",
            "label": "VM.Standard.A1.Flex · 免费 ARM",
            "is_flexible": True,
            "processor_description": "Ampere",
            "min_ocpus": 1,
            "max_ocpus": 4,
            "min_memory_in_gbs": 1,
            "max_memory_in_gbs": 24,
        },
        {
            "shape": "VM.Standard.E2.1.Micro",
            "label": "VM.Standard.E2.1.Micro · 免费 AMD",
            "is_flexible": False,
            "processor_description": "AMD",
        },
    ]
    images = {
        "Ubuntu 22.04 aarch64": {
            "id": "img-arm",
            "label": "Ubuntu 22.04 aarch64",
            "display_name": "Canonical-Ubuntu-22.04-aarch64",
        },
        "Ubuntu 22.04": {
            "id": "img-x86",
            "label": "Ubuntu 22.04",
            "display_name": "Canonical-Ubuntu-22.04",
        },
    }
    dialog = SimpleNamespace(
        vars={
            "image_label": FakeVar("Ubuntu 22.04 aarch64"),
            "shape_label": FakeVar("VM.Standard.A1.Flex · 免费 ARM"),
            "ocpus": FakeVar("1"),
            "memory": FakeVar("6"),
            "boot_gb": FakeVar(""),
            "boot_vpu_label": FakeVar("平衡 (10 VPUs/GB)"),
        },
        _image_map={k: v["id"] for k, v in images.items()},
        _image_info_map=images,
        _all_shapes=shapes,
        _shape_map={s["label"]: s for s in shapes if "A1" in s["shape"]},
        shape_combo=FakeWidget(),
        ocpu_entry=FakeWidget(),
        memory_entry=FakeWidget(),
        shape_hint=FakeWidget(),
        quick_hint=FakeWidget(),
        _vpu_map={
            "平衡 (10 VPUs/GB)": 10,
            "超高性能 (120 VPUs/GB) — 可能额外计费": 120,
        },
    )
    dialog._compatible_shapes = lambda: LaunchInstanceDialog._compatible_shapes(dialog)
    dialog._on_shape_change = lambda: LaunchInstanceDialog._on_shape_change(dialog)
    dialog._pick_image_for_arch = lambda arch: LaunchInstanceDialog._pick_image_for_arch(dialog, arch)
    return dialog


def test_apply_quick_preset_arm_fills_shape_flex_and_boot():
    dialog = _preset_dialog()
    preset = next(p for p in LAUNCH_QUICK_PRESETS if p["id"] == "a1_4c24g_100")

    LaunchInstanceDialog._apply_quick_preset(dialog, preset)

    assert "A1.Flex" in dialog.vars["shape_label"].get()
    assert dialog.vars["ocpus"].get() == "4"
    assert dialog.vars["memory"].get() == "24"
    assert dialog.vars["boot_gb"].get() == "100"
    assert "120" in dialog.vars["boot_vpu_label"].get()
    assert "aarch64" in dialog.vars["image_label"].get()


def test_apply_quick_preset_micro_switches_to_x86_image():
    dialog = _preset_dialog()
    preset = next(p for p in LAUNCH_QUICK_PRESETS if p["id"] == "e2_micro_50")

    LaunchInstanceDialog._apply_quick_preset(dialog, preset)

    assert "E2.1.Micro" in dialog.vars["shape_label"].get()
    assert dialog.vars["boot_gb"].get() == "50"
    assert "120" in dialog.vars["boot_vpu_label"].get()
    assert "aarch64" not in dialog.vars["image_label"].get()
    # Fixed shape clears flex fields
    assert dialog.vars["ocpus"].get() == ""
    assert dialog.vars["memory"].get() == ""
