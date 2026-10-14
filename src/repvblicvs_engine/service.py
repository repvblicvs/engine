"""Reversible per-user launchd supervision with bounded private logs.

The installed agent runs only the canonical package, never task-provided commands.
macOS caffeinate -i prevents idle system sleep on battery and AC; -s also prevents
system sleep on AC. These assertions do not change display or password locking.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
from .store import Store, state_directory

LABEL = "org.repvblicvs.engine"


def plist_document(python: str, root: str | Path) -> dict:
    runtime = Path(root).resolve()
    # Preserve executable names and the selected virtualenv. Resolving a Claude
    # or Python symlink can change its command name or bypass the installed venv.
    directories = []
    for tool in ("claude", "codex", "copilot", "agy"):
        binary = shutil.which(tool)
        if binary:
            directories.append(str(Path(binary).expanduser().absolute().parent))
    local_bin = Path.home() / ".local/bin"
    if local_bin.is_dir():
        directories.append(str(local_bin))
    directories.extend(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    path = os.pathsep.join(dict.fromkeys(directories))
    interpreter = str(Path(python).expanduser().absolute())
    return {"Label": LABEL, "ProgramArguments": [interpreter, "-m", "repvblicvs_engine.service", "supervise", "--state-dir", str(runtime)], "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10, "ProcessType": "Background", "EnvironmentVariables": {"REPVBLICVS_STATE_DIR": str(runtime), "PYTHONUNBUFFERED": "1", "PATH": path}}


def agent_path() -> Path:
    return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def _loaded() -> bool:
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if result.returncode == 0:
        return True
    # Only an explicit missing job proves absence. Domain/permission failures
    # must not authorize replacing or removing a possibly running service.
    missing = f'Could not find service "{LABEL}" in domain for user gui: {os.getuid()}'.encode()
    if result.returncode == 113 and missing in result.stderr.splitlines():
        return False
    raise RuntimeError("Unable to verify launchd engine state; installed configuration preserved")


def _unload():
    """Leave the installed configuration intact if its live job cannot stop."""
    result = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError("launchd refused to unload the engine; installed configuration preserved")
    if _loaded():
        raise RuntimeError("Engine remains loaded after bootout; installed configuration preserved")


def install(root: str | Path | None = None, python: str | None = None) -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("launchd service installation is macOS only")
    store = Store(root)
    target = agent_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    document = plist_document(python or sys.executable, store.root)
    encoded = plistlib.dumps(document)
    previous = target.read_bytes() if target.exists() else None
    loaded = _loaded()
    if previous != encoded:
        if loaded:
            _unload()
            loaded = False
        if previous is not None:
            backup = store.root / "previous-launch-agent.plist"
            if not backup.exists():
                descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(previous)
        target.write_bytes(encoded)
    os.chmod(target, 0o600)
    domain = f"gui/{os.getuid()}"
    if not loaded:
        result = subprocess.run(["launchctl", "bootstrap", domain, str(target)], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError("launchd refused to load the engine; configuration retained for retry")
        if not _loaded():
            raise RuntimeError("Engine load could not be verified; configuration retained for retry")
    return {"installed": True, "loaded": True, "label": LABEL, "agent": str(target), "state_dir": str(store.root)}


def uninstall() -> dict:
    if sys.platform != "darwin":
        raise RuntimeError("launchd service removal is macOS only")
    if _loaded():
        _unload()
    target = agent_path()
    target.unlink(missing_ok=True)
    return {"installed": False, "runtime_preserved": True}


def inspect() -> dict:
    target = agent_path()
    if sys.platform != "darwin":
        return {"supported": False, "installed": False}
    # Avoid printing launchctl's complete environment.
    return {"supported": True, "installed": target.exists(), "loaded": _loaded(), "label": LABEL}


def _bounded_pipe(pipe, target: Path, maximum: int = 1_000_000):
    while True:
        chunk = pipe.read(4096)
        if not chunk:
            return
        if target.exists() and target.stat().st_size + len(chunk) > maximum:
            target.replace(target.with_suffix(target.suffix + ".1"))
        with target.open("ab") as log:
            os.chmod(target, 0o600)
            log.write(chunk)


def wake_required(store: Store) -> bool:
    """Keep managed work awake; release sleep inhibition after pause/stop drain."""
    state = store.status()
    return state["control"] == "running" or state["task_counts"].get("running", 0) > 0


class WakeController:
    """A separate caffeinate assertion tied to the supervised worker's lifetime."""
    def __init__(self, store: Store, worker_pid: int):
        self.store, self.worker_pid, self.process = store, worker_pid, None

    def update(self):
        if sys.platform != "darwin" or not Path("/usr/bin/caffeinate").exists():
            return
        if wake_required(self.store):
            if self.process is None or self.process.poll() is not None:
                # -i covers idle sleep on battery; -s covers system sleep on AC.
                # -w releases both assertions if the supervised worker dies.
                self.process = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-s", "-w", str(self.worker_pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.close()

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None


def supervise(root: str | Path | None = None) -> int:
    store = Store(root)
    log_root = store.root / "logs"
    log_root.mkdir(exist_ok=True, mode=0o700)
    command = [sys.executable, "-m", "repvblicvs_engine.daemon", "--state-dir", str(store.root)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    wake = WakeController(store, process.pid)
    def terminate(*_):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, terminate)
    readers = [threading.Thread(target=_bounded_pipe, args=(pipe, log_root / name), daemon=True) for pipe, name in ((process.stdout, "worker.stdout.log"), (process.stderr, "worker.stderr.log"))]
    for reader in readers:
        reader.start()
    try:
        while process.poll() is None:
            wake.update()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        result = process.returncode
    finally:
        wake.close()
    for reader in readers:
        reader.join(timeout=2)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "uninstall", "status", "supervise", "render"])
    parser.add_argument("--state-dir")
    parser.add_argument("--python")
    args = parser.parse_args(argv)
    if args.action == "supervise":
        return supervise(args.state_dir)
    if args.action == "render":
        sys.stdout.buffer.write(plistlib.dumps(plist_document(args.python or sys.executable, args.state_dir or state_directory())))
        return 0
    import json
    if args.action == "install":
        result = install(args.state_dir, args.python)
    elif args.action == "uninstall":
        result = uninstall()
    else:
        result = inspect()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
