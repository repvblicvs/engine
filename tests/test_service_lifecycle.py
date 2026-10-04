import plistlib
import os
import subprocess
from unittest.mock import patch

import pytest

from repvblicvs_engine import service


class LaunchctlFixture:
    def __init__(self, *, loaded=False, bootout_fails=False, remains_loaded=False,
                 bootstrap_fails=False, bootstrap_unverified=False, print_fails=False):
        self.loaded = loaded
        self.bootout_fails = bootout_fails
        self.remains_loaded = remains_loaded
        self.bootstrap_fails = bootstrap_fails
        self.bootstrap_unverified = bootstrap_unverified
        self.print_fails = print_fails
        self.operations = []

    def run(self, command, **kwargs):
        assert command[0] == "launchctl"
        operation = command[1]
        self.operations.append(operation)
        code = 0
        diagnostic = b""
        if operation == "print":
            if self.print_fails:
                code, diagnostic = 5, b"Unable to inspect domain"
            elif not self.loaded:
                code = 113
                diagnostic = f'Bad request.\nCould not find service "{service.LABEL}" in domain for user gui: {os.getuid()}\n'.encode()
        elif operation == "bootout":
            code = 1 if self.bootout_fails else 0
            if code == 0 and not self.remains_loaded:
                self.loaded = False
        elif operation == "bootstrap":
            code = 1 if self.bootstrap_fails else 0
            if code == 0 and not self.bootstrap_unverified:
                self.loaded = True
        else:
            raise AssertionError(operation)
        return subprocess.CompletedProcess(command, code, stdout=b"", stderr=diagnostic)


@pytest.fixture
def isolated_agent(tmp_path):
    target = tmp_path / "LaunchAgents" / "engine.plist"
    target.parent.mkdir()
    with patch.object(service.sys, "platform", "darwin"), \
            patch.object(service, "agent_path", return_value=target), \
            patch.object(service.shutil, "which", return_value=None):
        yield target, tmp_path / "state"


@pytest.mark.parametrize("failure", ["bootout_fails", "remains_loaded"])
def test_failed_replacement_preserves_installed_configuration(isolated_agent, failure):
    target, state = isolated_agent
    original = plistlib.dumps({"Label": service.LABEL, "ProgramArguments": ["/fixture/old/python"]})
    target.write_bytes(original)
    fake = LaunchctlFixture(loaded=True, **{failure: True})
    with patch.object(service.subprocess, "run", side_effect=fake.run), pytest.raises(RuntimeError):
        service.install(state, python="/fixture/new/python")
    assert target.read_bytes() == original
    assert "bootstrap" not in fake.operations
    assert not (state / "previous-launch-agent.plist").exists()


def test_replacement_unloads_before_loading_and_keeps_private_backup(isolated_agent):
    target, state = isolated_agent
    original = plistlib.dumps({"Label": service.LABEL, "ProgramArguments": ["/fixture/old/python"]})
    target.write_bytes(original)
    fake = LaunchctlFixture(loaded=True)
    with patch.object(service.subprocess, "run", side_effect=fake.run):
        result = service.install(state, python="/fixture/new/python")
    assert result["installed"] and result["loaded"]
    assert fake.operations == ["print", "bootout", "print", "bootstrap", "print"]
    assert plistlib.loads(target.read_bytes())["ProgramArguments"][0] == "/fixture/new/python"
    backup = state / "previous-launch-agent.plist"
    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == 0o600


def test_loaded_job_without_installed_file_is_replaced(isolated_agent):
    target, state = isolated_agent
    fake = LaunchctlFixture(loaded=True)
    with patch.object(service.subprocess, "run", side_effect=fake.run):
        assert service.install(state, python="/fixture/python")["loaded"]
    assert target.exists()
    assert fake.operations == ["print", "bootout", "print", "bootstrap", "print"]


def test_matching_loaded_configuration_does_not_restart(isolated_agent):
    target, state = isolated_agent
    target.write_bytes(plistlib.dumps(service.plist_document("/fixture/python", state)))
    fake = LaunchctlFixture(loaded=True)
    with patch.object(service.subprocess, "run", side_effect=fake.run):
        assert service.install(state, python="/fixture/python")["loaded"]
    assert fake.operations == ["print"]


@pytest.mark.parametrize("failure", ["bootstrap_fails", "bootstrap_unverified"])
def test_failed_load_does_not_report_success_and_retains_retry_configuration(isolated_agent, failure):
    target, state = isolated_agent
    fake = LaunchctlFixture(**{failure: True})
    with patch.object(service.subprocess, "run", side_effect=fake.run), pytest.raises(RuntimeError):
        service.install(state, python="/fixture/python")
    assert target.exists()
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["bootout_fails", "remains_loaded"])
def test_failed_uninstall_preserves_configuration(isolated_agent, failure):
    target, _ = isolated_agent
    target.write_bytes(b"installed fixture")
    fake = LaunchctlFixture(loaded=True, **{failure: True})
    with patch.object(service.subprocess, "run", side_effect=fake.run), pytest.raises(RuntimeError):
        service.uninstall()
    assert target.read_bytes() == b"installed fixture"


@pytest.mark.parametrize("operation", ["install", "uninstall", "inspect"])
def test_inspection_failure_cannot_authorize_configuration_changes(isolated_agent, operation):
    target, state = isolated_agent
    target.write_bytes(b"installed fixture")
    fake = LaunchctlFixture(print_fails=True)
    with patch.object(service.subprocess, "run", side_effect=fake.run), pytest.raises(RuntimeError):
        if operation == "install":
            service.install(state, python="/fixture/python")
        elif operation == "uninstall":
            service.uninstall()
        else:
            service.inspect()
    assert target.read_bytes() == b"installed fixture"
    assert fake.operations == ["print"]


@pytest.mark.parametrize("loaded", [True, False])
def test_uninstall_is_verified_and_idempotent(isolated_agent, loaded):
    target, state = isolated_agent
    target.write_bytes(b"installed fixture")
    state.mkdir()
    (state / "runtime.txt").write_text("synthetic runtime")
    fake = LaunchctlFixture(loaded=loaded)
    with patch.object(service.subprocess, "run", side_effect=fake.run):
        assert service.uninstall() == {"installed": False, "runtime_preserved": True}
        assert service.uninstall()["runtime_preserved"]
    assert not target.exists()
    assert (state / "runtime.txt").read_text() == "synthetic runtime"
