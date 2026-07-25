"""Tests for guest FS grow script builder."""

from app.fs_grow import build_grow_script, truncate_output


def test_build_grow_script_contains_tools():
    script = build_grow_script()
    assert "growpart" in script
    assert "resize2fs" in script
    assert "xfs_growfs" in script
    assert "findmnt" in script
    assert "ocibot-grow" in script


def test_truncate_output():
    s = "a" * 100
    assert truncate_output(s, max_len=50).count("truncated") == 1
    assert truncate_output("short") == "short"
    assert truncate_output(None) == ""
