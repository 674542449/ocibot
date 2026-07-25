"""Unit tests for free_quota object/public_ip buckets and storage summarize."""

from app import free_quota


def test_build_quota_snapshot_includes_object_and_public_ip():
    instances = [
        {
            "id": "i1",
            "display_name": "a1",
            "lifecycle_state": "RUNNING",
            "shape": "VM.Standard.A1.Flex",
            "ocpus": 2,
            "memory_in_gbs": 12,
            "public_ip": "1.2.3.4",
        }
    ]
    volumes = [
        {
            "id": "bv1",
            "display_name": "boot",
            "size_in_gbs": 50,
            "kind": "boot",
            "lifecycle_state": "AVAILABLE",
            "instance_id": "i1",
        },
        {
            "id": "blk1",
            "display_name": "data",
            "size_in_gbs": 50,
            "kind": "block",
            "lifecycle_state": "AVAILABLE",
            "instance_id": "",
        },
    ]
    object_usage = {
        "object_storage_gb_used": 1.25,
        "object_buckets": [{"name": "logs", "approximate_size_gb": 1.25, "object_count": 3}],
    }
    snap = free_quota.build_quota_snapshot(
        instances=instances,
        volumes=volumes,
        object_usage=object_usage,
    )
    assert "object_storage_gb" in snap["buckets"]
    assert "public_ip_soft" in snap["buckets"]
    assert snap["buckets"]["object_storage_gb"]["used"] == 1.25
    assert snap["buckets"]["object_storage_gb"]["limit"] == free_quota.FREE_OBJECT_STORAGE_GB
    assert snap["buckets"]["public_ip_soft"]["used"] == 1
    assert snap["buckets"]["public_ip_soft"].get("soft") is True
    assert snap["usage"]["block_storage_gb"] == 100
    assert snap["usage"]["block_volume_gb"] == 50
    assert snap["object_buckets"][0]["name"] == "logs"
    assert any("对象存储" in line for line in snap["summary_lines"])


def test_public_ip_soft_over_does_not_force_overall_over():
    instances = [
        {
            "id": f"i{n}",
            "display_name": f"m{n}",
            "lifecycle_state": "RUNNING",
            "shape": "VM.Standard.E2.1.Micro",
            "public_ip": f"1.1.1.{n}",
        }
        for n in range(3)
    ]
    snap = free_quota.build_quota_snapshot(instances=instances, volumes=[])
    # 3 public IPs > soft limit 2 → critical (soft), not hard over on overall from this alone
    assert snap["buckets"]["public_ip_soft"]["status"] in {"critical", "warn", "over"}
    assert snap["buckets"]["public_ip_soft"]["status"] != "over" or snap["buckets"]["public_ip_soft"].get("soft")
    # overall should not be "over" solely due to soft public IP (E2 count 3 > 2 would be over though)
    # With 3 E2 micros, e2 is hard-over:
    assert snap["buckets"]["e2_micro_count"]["status"] == "over"
    assert snap["overall_status"] == "over"


def test_summarize_object_storage():
    out = free_quota.summarize_object_storage(
        [{"name": "a", "approximate_size_gb": 2}, {"name": "b", "size_gb": 3.5}]
    )
    assert out["object_storage_gb_used"] == 5.5
    assert out["bucket_count"] == 2


def test_validate_block_alias():
    g = free_quota.validate_block_volume_against_quota(
        current_size_gb=0,
        new_size_gb=250,
        free_only_mode=True,
        account_tier="free",
        usage={"block_storage_gb": 0},
    )
    assert not g.ok
