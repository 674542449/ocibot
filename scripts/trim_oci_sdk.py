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


def main() -> int:
    spec = importlib.util.find_spec("oci")
    if spec is None or not spec.origin:
        print("oci SDK not installed; nothing to trim", file=sys.stderr)
        return 0
    root = pathlib.Path(spec.origin).parent

    def size_mb(path: pathlib.Path) -> float:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6

    before = size_mb(root)
    removed = 0
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("__"):
            continue
        if child.name in KEEP:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed += 1
    after = size_mb(root)
    print(f"oci SDK: {before:.0f} MB -> {after:.0f} MB ({removed} service packages removed)")

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
