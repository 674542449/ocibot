"""`region` must never be able to redirect the OCI SDK to an arbitrary host.

The OCI SDK builds its endpoint as ``<service>.<region>.oraclecloud.com`` — but
``oci/regions.py::_endpoint_for`` carries a backwards-compatibility branch: if
``region`` already contains a ``.`` it is treated as the *complete* domain and
nothing is appended. ``region`` was accepted as free text (only "not empty"),
so any authenticated user could point every OCI call at a host of their choice:

    region='attacker.example.com:6379' -> https://iaas.attacker.example.com:6379/...
    region='@127.0.0.1:6379'           -> https://iaas.@127.0.0.1:6379/...
                                          -> socket connects to 127.0.0.1:6379
    region='@169.254.169.254'          -> socket connects to the metadata endpoint

``iaas.`` is swallowed as URL userinfo and the real authority is whatever follows
the ``@``. None of this passes through ``web/backend/url_safety.py``, so the
address/port/IDNA controls never applied, and ``POST /tenants/{id}/test`` echoes
the connection outcome straight back — an internal port scanner.

``oci.config.validate_config`` does not help: its ``PATTERNS`` cover only
fingerprint / tenancy / user.

The fix bans ``.``, ``@``, ``:`` and ``/`` in a region, which makes every one of
those constructions impossible while still accepting any real OCI region name.
"""

from __future__ import annotations

import pytest

from app.config_store import TenantConfig

VALID_PEM = "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"


def _cfg(region: str) -> TenantConfig:
    return TenantConfig(
        id="t1",
        name="probe",
        user_ocid="ocid1.user.oc1.." + "a" * 40,
        tenancy_ocid="ocid1.tenancy.oc1.." + "a" * 44,
        fingerprint="aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
        region=region,
        private_key_pem=VALID_PEM,
    )


def _region_errors(region: str) -> list[str]:
    return [e for e in _cfg(region).validate() if "Region" in e]


# Every one of these redirects the SDK endpoint somewhere it must never go.
@pytest.mark.parametrize(
    "region",
    [
        "@127.0.0.1:6379",
        "@169.254.169.254",
        "@[::1]:6379",
        "attacker.example.com",
        "attacker.example.com:6379",
        "ap-tokyo-1.attacker.example.com",
        "foo/bar",
        "foo:6379",
        "ap-tokyo-1\n@127.0.0.1",
        " @127.0.0.1 ",
        "-leading-hyphen",
        "a" * 64,  # also wider than tenants.region String(64)
    ],
)
def test_hostile_region_is_rejected(region: str) -> None:
    assert _region_errors(region), f"region {region!r} was accepted"


@pytest.mark.parametrize(
    "region",
    [
        "ap-tokyo-1",
        "us-ashburn-1",
        "eu-frankfurt-1",
        "sa-saopaulo-1",
        "ap-chuncheon-1",
        "me-jeddah-1",
        "uk-gov-london-1",
        "us-langley-1",
    ],
)
def test_real_region_names_still_accepted(region: str) -> None:
    assert not _region_errors(region), f"legitimate region {region!r} was rejected"


def test_empty_region_still_reported() -> None:
    assert _region_errors("")


def test_sdk_endpoint_stays_on_oraclecloud_for_every_accepted_region() -> None:
    """The property that actually matters, asserted against the real SDK.

    Rather than trusting the regex in isolation, build what the SDK would dial
    and confirm the authority is an oraclecloud.com host with no port and no
    userinfo — for a region that passes validation.
    """
    oci = pytest.importorskip("oci")
    from urllib.parse import urlparse

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        .decode()
    )

    def endpoint_for(region: str) -> str:
        client = oci.core.ComputeClient(
            {
                "user": "ocid1.user.oc1.." + "a" * 40,
                "tenancy": "ocid1.tenancy.oc1.." + "a" * 44,
                "fingerprint": "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99",
                "region": region,
                "key_content": pem,
            }
        )
        return client.base_client.endpoint

    # Sanity: the attack really does work on an unvalidated value, so this test
    # is guarding something real rather than asserting a tautology.
    hostile = urlparse(endpoint_for("@127.0.0.1:6379"))
    assert hostile.hostname == "127.0.0.1" and hostile.port == 6379

    # And a region that passes validate() cannot leave oraclecloud.com.
    #
    # Asserted on the raw endpoint string rather than via urlparse: this SDK
    # version returns a *templated* endpoint for real regions —
    # "https://iaas.ap-tokyo-1.{dualStack?ds.oci.:}oraclecloud.com/20160918" —
    # which urlparse cannot decompose (the "{...}" swallows the authority).
    # The properties that matter survive the template intact.
    for region in ("ap-tokyo-1", "us-ashburn-1", "uk-gov-london-1"):
        assert not _region_errors(region)
        ep = endpoint_for(region)
        assert ep.startswith("https://")
        # No userinfo, so nothing can displace the authority.
        assert "@" not in ep, ep
        # The region is only ever a *label* inside the hostname, never the
        # authority itself. Deliberately not asserting a specific domain:
        # Oracle has several realms (ap-tokyo-1 -> oraclecloud.com,
        # uk-gov-london-1 -> oraclegovcloud.uk), and pinning one would make this
        # test fail on a perfectly legitimate region.
        assert f".{region}." in ep, ep
        # No attacker-chosen port.
        assert ":" not in ep.split("//", 1)[1].split("/", 1)[0].replace(
            "{dualStack?ds.oci.:}", ""
        ), ep
