from app.oci_client import _is_lts_version, _latest_lts_ubuntu_images


def _img(version, arch, tag):
    dn = f"Canonical-Ubuntu-{version}-{'aarch64' if arch == 'ARM64' else 'amd64'}-{tag}"
    return {
        "id": "ocid." + tag,
        "display_name": dn,
        "operating_system": "Canonical Ubuntu",
        "operating_system_version": version,
        "base_image_id": "",
        "architecture": arch,
        "label": f"Ubuntu {version} {arch} [{tag}]",
    }


def test_lts_detection():
    assert _is_lts_version("24.04")
    assert _is_lts_version("22.04")
    assert not _is_lts_version("24.10")  # interim
    assert not _is_lts_version("23.04")  # odd year
    assert not _is_lts_version("")


def test_keeps_only_four_latest_lts_newest_build_per_arch():
    # newest-first, with duplicate builds, an interim release, and an older LTS
    items = [
        _img("24.04", "ARM64", "new"),
        _img("24.04", "ARM64", "old"),   # duplicate (version, arch) -> dropped
        _img("24.04", "AMD64", "new"),
        _img("24.10", "ARM64", "interim"),  # interim -> dropped
        _img("22.04", "ARM64", "x"),
        _img("22.04", "AMD64", "y"),
        _img("20.04", "ARM64", "z"),     # 3rd LTS -> dropped
        _img("20.04", "AMD64", "w"),
    ]
    out = _latest_lts_ubuntu_images(items)
    keys = {(i["operating_system_version"], i["architecture"], i["id"]) for i in out}
    assert len(out) == 4
    assert ("24.04", "ARM64", "ocid.new") in keys  # newest build wins
    assert all(v in ("24.04", "22.04") for v, _, _ in keys)


def test_never_hides_everything_when_versions_unparseable():
    weird = [{"operating_system_version": "", "architecture": "ARM64", "display_name": "x", "id": "1"}]
    assert _latest_lts_ubuntu_images(weird) == weird
