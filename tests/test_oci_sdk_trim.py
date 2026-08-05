"""The image drops OCI SDK service packages the panel never imports.

scripts/trim_oci_sdk.py verifies its own half at build time: after deleting, it
imports every client and fails the Docker build if one is gone.

It cannot verify the other half. If someone adds `from oci.vault import ...` to
app/oci_client.py and does not add "vault" to KEEP, the build still succeeds —
the trim script does not know about the new import, so it happily deletes the
package, and the failure appears at runtime as a 502 on whichever feature needed
it. That is the half this test covers: every oci.<service> named anywhere in the
source must survive the trim.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TRIM = ROOT / "scripts" / "trim_oci_sdk.py"
SOURCES = list((ROOT / "app").rglob("*.py")) + list((ROOT / "web" / "backend").rglob("*.py"))


def _keep_set() -> set[str]:
    """Read KEEP out of the script without importing it (it has side effects)."""
    tree = ast.parse(TRIM.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "KEEP" for t in node.targets
        ):
            return {ast.literal_eval(e) for e in node.value.elts}  # type: ignore[attr-defined]
    raise AssertionError("KEEP not found in scripts/trim_oci_sdk.py")


def _referenced_services() -> dict[str, str]:
    """Every oci.<service> mentioned in the backend, mapped to where."""
    found: dict[str, str] = {}
    pattern = re.compile(r"\boci\.([a-z_][a-z0-9_]*)")
    for path in SOURCES:
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"^\s*from\s+oci\.([a-z_][a-z0-9_]*)", text, re.M):
            found.setdefault(m.group(1), f"{path.relative_to(ROOT)}")
        for m in pattern.finditer(text):
            found.setdefault(m.group(1), f"{path.relative_to(ROOT)}")
    return found


def _would_be_deleted(service: str, keep: set[str]) -> bool:
    """Mirror the script's rule: it removes DIRECTORIES not in KEEP.

    Single-module members (oci/util.py, oci/exceptions.py, ...) are never
    touched, so requiring them in KEEP would be noise. If a future SDK version
    turns one of them into a package, this starts requiring it — which is the
    correct behaviour, since it would then become deletable.
    """
    oci = pytest.importorskip("oci")
    root = Path(oci.__file__).parent
    return (root / service).is_dir() and service not in keep


def test_every_referenced_service_survives_the_trim():
    keep = _keep_set()
    missing = {
        service: where
        for service, where in _referenced_services().items()
        if _would_be_deleted(service, keep)
    }
    assert not missing, (
        "these oci service packages are used but would be deleted from the image; "
        "add them to KEEP in scripts/trim_oci_sdk.py:\n"
        + "\n".join(f"  oci.{s}  ({w})" for s, w in sorted(missing.items()))
    )


def test_trim_keeps_the_non_obvious_transitive_dependency():
    """oci.pagination does `from .. import dns, object_storage` at module scope
    and oci/__init__.py imports pagination, so dropping dns breaks `import oci`
    itself — with a traceback that points at the SDK, not at this trim."""
    assert "dns" in _keep_set()


@pytest.mark.skipif(not TRIM.exists(), reason="trim script absent")
def test_trim_script_verifies_rather_than_trusting_itself():
    src = TRIM.read_text(encoding="utf-8")
    assert "VERIFY" in src
    assert "return 1" in src, "a failed verification must fail the build"


def test_trim_also_prunes_the_sdk_init_lists():
    """Deleting the directories is only half the job.

    oci/__init__.py carries both a `__all__` naming every service package and an
    EAGER `from . import <all of them>` used when
    OCI_PYTHON_SDK_LAZY_IMPORTS_DISABLED=true. Delete the directories without
    pruning those lists and that variable turns `import oci` into an ImportError
    — the panel does not start, and the traceback points at the SDK with nothing
    tying it to the trim. Verified by simulation: the eager branch on an
    unpruned-but-trimmed tree raises ImportError on the first name.
    """
    src = TRIM.read_text(encoding="utf-8")
    assert "_prune_init" in src
    assert "__all__" in src
    # Both statement kinds must be handled, not just the easy one.
    assert "ast.ImportFrom" in src and "ast.Assign" in src


def test_trim_runs_in_the_same_layer_as_pip_install():
    """Otherwise the trim frees nothing.

    Docker layers are additive. With

        RUN pip install ...
        RUN python trim_oci_sdk.py

    the install layer still contains all 404 MB and the trim layer merely records
    deletions, so image size is unchanged — while every local check still passes,
    because the trimmed tree really is what the container sees at runtime. The
    only symptom is `docker images` reporting a size that never went down, which
    is exactly how it was missed: shipped in 0.4.64, and the operator's image came
    back at 1.15 GB.
    """
    dockerfile = (ROOT / "web" / "Dockerfile").read_text(encoding="utf-8")
    # Join line continuations so a single logical RUN reads as one line.
    logical = re.sub(r"\\\s*\n\s*", " ", dockerfile)
    run_lines = [ln for ln in logical.splitlines() if ln.strip().startswith("RUN")]
    installs = [ln for ln in run_lines if "pip install" in ln]
    assert installs, "no `RUN pip install` found in web/Dockerfile"
    assert any("trim_oci_sdk.py" in ln for ln in installs), (
        "trim_oci_sdk.py must run in the SAME `RUN` as pip install; a separate "
        "instruction leaves the untrimmed SDK in the lower layer and saves nothing"
    )


def test_sdk_still_has_both_import_branches():
    """If Oracle drops the eager fallback, _prune_init's second edit becomes dead
    code and this test says so rather than leaving it to be puzzled over."""
    oci = pytest.importorskip("oci")
    init = Path(oci.__file__).read_text(encoding="utf-8")
    assert "OCI_PYTHON_SDK_LAZY_IMPORTS_DISABLED" in init, (
        "the SDK no longer branches on this variable — re-check whether "
        "scripts/trim_oci_sdk.py still needs to prune the eager import list"
    )
