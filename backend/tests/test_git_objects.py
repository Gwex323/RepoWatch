import subprocess

import pytest

from repowatch.adapters.git_objects import GitObjects
from repowatch.runtime.git_handoff import create_fixture


def test_bare_bundle_import_preserves_objects_without_checkout_or_hooks(tmp_path, monkeypatch):
    fixture = create_fixture(tmp_path)
    trap = tmp_path / "trap"
    configuration = tmp_path / "host.gitconfig"
    configuration.write_text(
        "[core]\n hooksPath = " + trap.as_posix() + '\n[filter "evil"]\n smudge = touch bad\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(configuration))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    objects = GitObjects(tmp_path / "objects")
    sha = objects.import_bundle("repo", fixture["bundle"])
    assert sha == fixture["commit"]
    assert not (objects.repository("repo") / "tracked.txt").exists()
    entries = objects.entries("repo", sha)
    assert {entry["path"] for entry in entries} == {entry["path"] for entry in fixture["entries"]}
    assert (
        objects.blob("repo", next(entry["oid"] for entry in entries if entry["path"] == "crlf.txt"))
        == b"line1\r\nline2\r\n"
    )
    exported = objects.export("repo", sha, tmp_path / "export.bundle")
    assert exported.is_file()
    config = (objects.repository("repo") / "config").read_text()
    assert "remote" not in config and "Authorization" not in config
    assert not trap.exists()


def test_fetch_uses_credential_only_in_transient_host_process(tmp_path, monkeypatch):
    from repowatch.application.credentials import SecretValue

    objects = GitObjects(tmp_path / "objects")
    objects.initialize("repo")
    calls = []

    def execute(argv, **kwargs):
        calls.append((argv, dict(kwargs["env"])))
        assert "test-token" not in repr(argv)
        output = b"true\n" if "--is-bare-repository" in argv else b"a" * 40 + b"\n"
        return subprocess.CompletedProcess(argv, 0, output, b"")

    monkeypatch.setattr(subprocess, "run", execute)
    objects.fetch("repo", "up", "repo", "main", secret=SecretValue("test-token"))
    authenticated = [call for call in calls if "fetch" in call[0]]
    assert len(authenticated) == 1
    assert "REPOWATCH_GIT_AUTH" in authenticated[0][1]
    assert "GIT_CONFIG_COUNT" not in authenticated[0][1]
    assert not (objects.repository("repo") / "config").read_text().find("token") >= 0


@pytest.mark.parametrize("identifier", ["../user", "a/b", "a\\b", "a:b"])
def test_object_paths_cannot_escape(tmp_path, identifier):
    with pytest.raises(ValueError):
        GitObjects(tmp_path / "objects").initialize(identifier)
