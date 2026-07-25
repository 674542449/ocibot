import json

from app.scheduler import CapacityRetryJob, JobStore


BASE = {
    "compartment_id": "ocid1.compartment.test",
    "availability_domain": "AD-1",
    "shape": "VM.Standard.A1.Flex",
    "image_id": "ocid1.image.test",
    "subnet_id": "ocid1.subnet.test",
    "auth_mode": "key",
    "ssh_public_key": "ssh-ed25519 AAAATEST user",
}


def test_retry_store_never_persists_extra_fields(tmp_path):
    store = JobStore(tmp_path)
    store.upsert_retry(CapacityRetryJob(id="1", name="test", tenant_id="tenant", launch_payload={**BASE, "unknown": "drop"}))
    raw = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert "unknown" not in raw["retries"][0]["launch_payload"]


def test_old_user_data_is_removed_and_job_disabled(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"version": 1, "schedules": [], "retries": [{"id": "old", "name": "old", "tenant_id": "t", "launch_payload": {**BASE, "user_data_b64": "secret"}}]}), encoding="utf-8")
    store = JobStore(tmp_path)
    job = store.get_retry("old")
    assert job is not None and not job.enabled and job.status == "failed"
    rewritten = path.read_text(encoding="utf-8")
    assert "secret" not in rewritten
    assert "user_data_b64" not in rewritten
