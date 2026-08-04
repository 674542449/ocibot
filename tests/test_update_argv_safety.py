"""Repo / branch reaching the updater's argv are validated in one place.

The update path is the highest privilege the panel has: it drives the mounted
docker socket and, on the preferred route, runs commands in the host namespaces.
Both values come from the environment rather than from a request, so this is
defence in depth — but the failure mode is argument injection, not shell
injection, and a list argv does nothing to stop it.

`git fetch --depth 50 origin <branch>` with a branch of "--upload-pack=<cmd>"
runs <cmd>. git parses any argument starting with "-" as an option. Only the
check-for-updates path used to validate, and its pattern allowed a leading dash
anyway.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("OCIBOT_MASTER_KEY", "argv-master-key-0123456789abcd")
os.environ.setdefault("OCIBOT_JWT_SECRET", "argv-jwt-secret-0123456789abcd")

from web.backend.self_update import _checked_branch, _checked_repo  # noqa: E402


@pytest.mark.parametrize("branch", ["main", "release/1.2", "feature-x", "v0.4.58", "a/b/c"])
def test_ordinary_branches_pass(branch):
    assert _checked_branch(branch) == branch


@pytest.mark.parametrize(
    "branch",
    [
        "--upload-pack=touch /tmp/pwn",  # the one that executes a command
        "--exec=id",
        "-o",
        "../evil",
        "a/../b",
        "main;rm -rf /",
        "main branch",
        "main\nmore",
        "",
    ],
)
def test_dangerous_branches_are_refused(branch):
    with pytest.raises(RuntimeError):
        _checked_branch(branch)


@pytest.mark.parametrize("repo", ["674542449/ocibot", "owner/name", "a_b/c.d-e"])
def test_ordinary_repos_pass(repo):
    assert _checked_repo(repo) == repo


@pytest.mark.parametrize(
    "repo", ["-x/y", "a/b/c", "; rm -rf /", "owner", "", "owner/name extra"]
)
def test_dangerous_repos_are_refused(repo):
    with pytest.raises(RuntimeError):
        _checked_repo(repo)


def test_the_env_readers_validate_not_just_the_check_path(monkeypatch):
    """The apply path calls _branch() directly, so the guard has to live there
    and not only inside fetch_remote_head."""
    from web.backend import self_update

    monkeypatch.setenv("OCIBOT_UPDATE_BRANCH", "--upload-pack=id")
    with pytest.raises(RuntimeError):
        self_update._branch()

    monkeypatch.setenv("OCIBOT_UPDATE_BRANCH", "main")
    assert self_update._branch() == "main"

    monkeypatch.setenv("OCIBOT_UPDATE_REPO", "-evil/repo")
    with pytest.raises(RuntimeError):
        self_update._repo()
