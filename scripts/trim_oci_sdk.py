#!/usr/bin/env python3
"""Delete the OCI SDK service packages this panel never imports.

Why: the SDK ships 175 service packages totalling ~403 MB, and OCIBot reaches
17 of them (~57 MB). The other ~346 MB is Autonomous Database, Data Safe,
GoldenGate, Log Analytics and so on — code that can never execute here. It is
by far the largest thing in the image, and the image is rebuilt on the
operator's own server on every `install.sh update`, on a machine that is often
an Always-Free instance with a small boot volume.

Safe because the SDK does NOT import service packages eagerly: oci/__init__.py
pulls in only the infrastructure modules, and a service package loads when a
client class is referenced (`from oci.core import ComputeClient`). Deleting one
that is never referenced is therefore unobservable.

"Therefore" is not good enough on its own, so this script VERIFIES: after
deleting, it imports every client the panel actually constructs. If a delete
was wrong, the Docker build fails here — not a user's request six weeks later.

Two things guard the two ways this can rot:
  * this verification catches the SDK growing a new transitive import;
  * tests/test_oci_sdk_trim.py catches the panel growing a new service import
    that is missing from KEEP below.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util  # `import importlib` alone does not bind the util submodule
import pathlib
import shutil
import sys

# Service packages the panel imports, plus the infrastructure oci/__init__.py
# and its dependencies need. `dns` looks droppable and is not: oci.pagination
# does `from .. import dns, object_storage` at module scope, and __init__.py
# imports pagination — so removing dns breaks `import oci` itself.
KEEP = {
    # --- referenced by app/oci_client.py ---
    "core",
    "identity",
    "identity_domains",
    "limits",
    "monitoring",
    "usage_api",
    "object_storage",
    "compute_instance_agent",
    "osp_gateway",
    "tenant_manager_control_plane",
    "work_requests",
    # --- pulled in by the SDK's own machinery ---
    "dns",
    "auth",
    "config",
    "constants",
    "decorators",
    "exceptions",
    "regions",
    "pagination",
    "retry",
    "fips",
    "circuit_breaker",
    "developer_tool_configuration",
    "_vendor",
}

# Imported after the delete to prove the delete was safe. Mirrors the imports in
# app/oci_client.py, including the lazy ones inside functions — those are the
# easiest to miss precisely because they only run when that feature is used.
VERIFY = [
    ("oci.core", ("BlockstorageClient", "ComputeClient", "VirtualNetworkClient")),
    ("oci.identity", ("IdentityClient",)),
    ("oci.limits", ("LimitsClient",)),
    ("oci.monitoring", ("MonitoringClient",)),
    ("oci.usage_api", ("UsageapiClient",)),
    ("oci.object_storage", ("ObjectStorageClient",)),
    ("oci.compute_instance_agent", ("ComputeInstanceAgentClient",)),
    ("oci.osp_gateway", ("InvoiceServiceClient",)),
    ("oci.identity_domains", ("IdentityDomainsClient",)),
    ("oci.identity_domains.models", ("Operations", "PatchOp")),
    ("oci.tenant_manager_control_plane", ("SubscriptionClient",)),
    ("oci.exceptions", ("ServiceError",)),
]


def _prune_init(init_path: pathlib.Path, deleted: set[str]) -> int:
    """Remove deleted packages from oci/__init__.py's `__all__` and eager import.

    Necessary because __init__.py has TWO code paths:

        if ... os.getenv("OCI_PYTHON_SDK_LAZY_IMPORTS_DISABLED") != "true":
            def __getattr__(x): ...          # lazy — fine with a trimmed tree
        else:
            from . import <all 175 packages>  # eager — ImportError on a trimmed tree

    Deleting directories alone only satisfies the lazy path. An operator who sets
    that SDK variable (it is a documented knob, and web/.env is passed straight
    into the container) would get a panel that will not start, with a traceback
    pointing at the SDK and nothing connecting it to this trim.

    Pruning both lists makes the two paths agree with what is actually on disk,
    so the variable stops mattering. It also keeps `dir(oci)` honest.
    """
    text = init_path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines(keepends=True)
    edits: list[tuple[int, int, str]] = []  # (start_line, end_line, replacement)

    for node in ast.walk(tree):
        # __all__ = [...]
        if (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "__all__" for t in node.targets)
            and isinstance(node.value, ast.List)
        ):
            kept = [
                ast.literal_eval(e)
                for e in node.value.elts
                if ast.literal_eval(e) not in deleted
            ]
            indent = " " * (node.col_offset)
            body = ", ".join(f'"{n}"' for n in kept)
            edits.append((node.lineno, node.end_lineno or node.lineno, f"{indent}__all__ = [{body}]\n"))
        # from . import a, b, c, ...   (the eager fallback)
        elif isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
            kept_names = [a.name for a in node.names if a.name not in deleted]
            if len(kept_names) == len(node.names):
                continue
            indent = " " * (node.col_offset)
            edits.append(
                (node.lineno, node.end_lineno or node.lineno,
                 f"{indent}from . import {', '.join(kept_names)}\n")
            )

    if not edits:
        return 0
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start - 1 : end] = [replacement]
    init_path.write_text("".join(lines), encoding="utf-8")
    return len(edits)


def main() -> int:
    spec = importlib.util.find_spec("oci")
    if spec is None or not spec.origin:
        print("oci SDK not installed; nothing to trim", file=sys.stderr)
        return 0
    root = pathlib.Path(spec.origin).parent

    def size_mb(path: pathlib.Path) -> float:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6

    before = size_mb(root)
    deleted: set[str] = set()
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("__"):
            continue
        if child.name in KEEP:
            continue
        shutil.rmtree(child, ignore_errors=True)
        deleted.add(child.name)
    after = size_mb(root)
    print(f"oci SDK: {before:.0f} MB -> {after:.0f} MB ({len(deleted)} service packages removed)")

    edits = _prune_init(root / "__init__.py", deleted)
    print(f"pruned {edits} reference list(s) in oci/__init__.py")

    # The point of the whole script: prove the remaining tree still satisfies
    # every import the panel makes.
    failures: list[str] = []
    for module, names in VERIFY:
        try:
            mod = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures.append(f"{module}: {exc.__class__.__name__}: {exc}")
            continue
        for name in names:
            if not hasattr(mod, name):
                failures.append(f"{module}.{name} missing")
    if failures:
        print("\nTRIM VERIFICATION FAILED — add the package to KEEP:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"verified {len(VERIFY)} import sites")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
