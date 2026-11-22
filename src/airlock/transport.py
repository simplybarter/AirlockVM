"""AirlockVM Shared Transport, Identifier Validation & Asset Resolution.

Centralizes local and SSH command execution, safe identifier validation,
Python executable discovery, and packaged skill/script asset resolution.
"""

from __future__ import annotations

from pathlib import Path
import re
import shlex
import subprocess
import sys

# Strict safe identifier pattern for VM names, hypervisor aliases, and snapshot names
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "_assets"
REPO_ROOT = PACKAGE_DIR.parents[1]


def validate_identifier(value: str, label: str = "identifier") -> str:
    """Validates that a VM name, host alias, or snapshot name is safe for shell/CLI use."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid {label}: expected string.")
    cleaned = value.strip()
    if not cleaned or not _SAFE_IDENT_RE.match(cleaned):
        raise ValueError(
            f"Invalid {label} '{value}': must start with an alphanumeric character "
            "and contain only letters, digits, '.', '_', or '-'."
        )
    return cleaned


def is_local_host(host: str | None) -> bool:
    """Returns True when host targets the local machine."""
    return not host or host.strip().lower() in ("localhost", "127.0.0.1", "::1")


def get_python_bin(airlock_root: Path | None = None) -> Path:
    """Returns the preferred Python executable (.venv if present, else sys.executable)."""
    root = airlock_root or REPO_ROOT
    venv_py = root / ".venv" / "bin" / "python"
    if venv_py.exists():
        return venv_py
    return Path(sys.executable)


def resolve_asset_path(relative_path: str, airlock_root: Path | None = None) -> Path:
    """Resolves a skill script or root shell helper from repo root or packaged wheel assets."""
    root = airlock_root or REPO_ROOT
    repo_candidate = root / relative_path
    if repo_candidate.exists():
        return repo_candidate
    asset_candidate = ASSETS_DIR / relative_path
    if asset_candidate.exists():
        return asset_candidate
    return repo_candidate


def run_host_cmd(
    host: str,
    command: str | list[str],
    timeout: int = 5,
) -> tuple[int, str, str]:
    """Executes a command locally (for localhost) or on a remote hypervisor via SSH."""
    if is_local_host(host):
        if isinstance(command, list):
            full_cmd = command
        else:
            full_cmd = ["bash", "-c", command]
    else:
        safe_host = validate_identifier(host, "hypervisor host")
        remote_cmd = shlex.join(command) if isinstance(command, list) else command
        full_cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={min(timeout, 10)}",
            "-o", "StrictHostKeyChecking=accept-new",
            safe_host,
            remote_cmd,
        ]

    try:
        proc = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        cmd_desc = shlex.join(command) if isinstance(command, list) else command
        return 124, "", f"Command timed out after {timeout}s: {cmd_desc}"
    except Exception as e:
        return 1, "", str(e)


def run_guest_cmd(
    vm_name: str,
    command: str | list[str],
    timeout: int = 5,
) -> tuple[int, str, str]:
    """Executes a command inside a guest VM via SSH."""
    safe_vm = validate_identifier(vm_name, "VM name")
    remote_cmd = shlex.join(command) if isinstance(command, list) else command
    full_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={min(timeout, 10)}",
        "-o", "StrictHostKeyChecking=accept-new",
        safe_vm,
        remote_cmd,
    ]
    try:
        proc = subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Guest command timed out after {timeout}s: {remote_cmd}"
    except Exception as e:
        return 1, "", str(e)
